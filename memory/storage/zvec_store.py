"""Zvec向量数据库存储实现

使用阿里开源的 Zvec 进程内向量数据库。
Zvec 是纯进程内运行，无需服务器、Docker 或任何外部基础设施。
只需 pip install zvec 即可使用。

Zvec 官方文档: https://zvec.org/llms.txt

=== Zvec vs Qdrant 对比 ===

  Qdrant: 客户端-服务器架构，需要 Docker 或云服务，功能丰富但运维成本高。
  Zvec:   进程内库，pip install 即可，零配置，适合开发、边缘设备、轻量部署。

=== Zvec 核心概念映射 ===

  Qdrant 术语        →  Zvec 术语
  ──────────────────────────────────
  Collection          →  Collection（同名）
  Point               →  Doc（文档）
  Point.id            →  Doc.id
  Point.vector        →  Doc.vectors（命名向量字典）
  Point.payload       →  Doc.fields（标量字段字典）
  FieldCondition      →  SQL 风格过滤字符串（如 "memory_type = 'episodic'"）
  HnswConfigDiff      →  HnswIndexParam
  upsert              →  upsert（同名）

=== Zvec 的数据存储策略 ===

  Zvec 要求所有标量字段必须在 Schema 中预先定义。
  但业务代码的 metadata 字典字段灵活多变（不同记忆类型有不同的字段）。
  为解决这个矛盾，我们采用 "固定字段 + JSON 载荷" 策略：

  1. 固定字段（在 Schema 中定义，用于过滤和索引）:
     - memory_id   (STRING, 建索引)  → 用于 delete_memories 过滤
     - user_id     (STRING, 建索引)  → 用于用户隔离查询
     - memory_type (STRING, 建索引)  → 用于分类过滤（episodic/semantic/...）
     - content     (STRING)         → 原始文本内容
     - importance  (FLOAT)          → 重要性分数
     - timestamp   (INT64)          → Unix 时间戳

  2. payload_json (STRING):
     所有不在上述固定字段中的额外元数据，序列化为 JSON 字符串存入此字段。
     查询时再反序列化还原，保证数据的完整性。

  3. 向量字段:
     - embedding (VECTOR_FP32, HNSW索引, 余弦距离)
"""

import logging
import json
import os
from typing import Dict, List, Optional, Any

from .vector_store_base import VectorStoreBase

# ========== 依赖检查 ==========
# Zvec 是可选依赖，未安装时给出明确的错误提示

try:
    import zvec
    ZVEC_AVAILABLE = True
except ImportError:
    ZVEC_AVAILABLE = False
    zvec = None

logger = logging.getLogger(__name__)


# ========================================================================
# 模块级工具函数
# ========================================================================

def _dict_to_filter_string(where: Dict[str, Any]) -> str:
    """将字典格式的过滤条件转换为 Zvec SQL 风格过滤字符串。

    这是连接 "统一接口" 和 "Zvec 原生 API" 的关键适配函数。
    上层业务代码统一使用字典格式（与 Qdrant 接口一致），
    此函数负责将字典转换为 Zvec 能理解的 SQL 风格字符串。

    转换规则:
      - 单值匹配:   {"memory_type": "episodic"}  →  "memory_type = 'episodic'"
      - 多条件 AND: {"memory_type": "episodic", "user_id": "abc"}
                    →  "memory_type = 'episodic' AND user_id = 'abc'"
      - 列表 IN:    {"memory_id": ["id1", "id2", "id3"]}
                    →  "memory_id IN ('id1', 'id2', 'id3')"

    Args:
        where: 过滤条件字典，与 VectorStoreBase 接口一致。

    Returns:
        Zvec 兼容的 SQL 风格过滤字符串。
    """
    parts = []
    for key, value in where.items():
        if isinstance(value, list):
            # 列表值 → IN 子句
            # 例如: memory_id IN ('id1', 'id2', 'id3')
            escaped = [f"'{v}'" if isinstance(v, str) else str(v) for v in value]
            parts.append(f"{key} IN ({', '.join(escaped)})")
        elif isinstance(value, str):
            # 字符串值 → 单引号包裹
            parts.append(f"{key} = '{value}'")
        elif isinstance(value, bool):
            # 布尔值 → 小写 true/false
            parts.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, (int, float)):
            # 数值 → 直接拼接
            parts.append(f"{key} = {value}")
    # 多个条件用 AND 连接
    return " AND ".join(parts)


def _build_zvec_schema(
    collection_name: str,
    vector_size: int,
    distance: str = "cosine",
) -> "zvec.CollectionSchema":
    """构建 Zvec Collection Schema。

    定义集合的结构：包含哪些标量字段、哪些向量字段、各用什么索引。
    这个 Schema 在首次创建集合时使用，之后打开已有集合时不需要。

    === 标量字段设计 ===

    字段           类型      是否索引    用途
    ──────────────────────────────────────────────────
    memory_id     STRING     是       用于 delete_memories 按 memory_id 过滤删除
    user_id       STRING     是       用于多用户隔离查询
    memory_type   STRING     是       用于分类过滤（episodic/semantic/perceptual/working）
    content       STRING     否       原始文本，仅存储不检索
    importance    FLOAT      否       重要性分数，仅存储
    timestamp     INT64      否       Unix 时间戳，仅存储
    payload_json  STRING     否       额外元数据的 JSON 序列化，仅存储

    === 向量字段设计 ===

    字段名       类型           维度        索引    距离度量
    ──────────────────────────────────────────────────
    embedding   VECTOR_FP32    vector_size   HNSW    用户指定（默认余弦）

    === 距离度量映射 ===

    统一接口   →  Zvec MetricType
    ─────────────────────────────
    cosine     →  MetricType.COSINE  （余弦相似度，推荐用于文本嵌入）
    dot        →  MetricType.IP      （内积，适用于已归一化的向量）
    euclidean  →  MetricType.L2      （欧几里得距离）

    Args:
        collection_name: 集合名称。
        vector_size: 向量维度（必须与嵌入模型输出维度一致）。
        distance: 距离度量类型字符串。

    Returns:
        配置好的 zvec.CollectionSchema 对象。
    """
    # 距离度量映射
    metric_map = {
        "cosine": zvec.MetricType.COSINE,
        "dot": zvec.MetricType.IP,
        "euclidean": zvec.MetricType.L2,
    }
    metric = metric_map.get(distance.lower(), zvec.MetricType.COSINE)

    return zvec.CollectionSchema(
        name=collection_name,
        # ---- 标量字段 ----
        fields=[
            # 以下三个字段建了倒排索引，用于高效过滤查询
            zvec.FieldSchema(
                name="memory_id",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),  # 倒排索引，支持 = 和 IN 过滤
            ),
            zvec.FieldSchema(
                name="user_id",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(
                name="memory_type",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            # 以下字段仅存储，不建索引（不需要用于过滤）
            zvec.FieldSchema(
                name="content",
                data_type=zvec.DataType.STRING,
            ),
            zvec.FieldSchema(
                name="importance",
                data_type=zvec.DataType.FLOAT,
            ),
            zvec.FieldSchema(
                name="timestamp",
                data_type=zvec.DataType.INT64,
            ),
            zvec.FieldSchema(
                name="payload_json",
                data_type=zvec.DataType.STRING,
            ),
            # RAG 管线专用字段（索引字段，用于过滤查询）
            zvec.FieldSchema(
                name="is_rag_data",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(
                name="data_source",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(
                name="rag_namespace",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
            zvec.FieldSchema(
                name="modality",
                data_type=zvec.DataType.STRING,
                index_param=zvec.InvertIndexParam(),
            ),
        ],
        # ---- 向量字段 ----
        vectors=[
            zvec.VectorSchema(
                name="embedding",                     # 向量字段名
                data_type=zvec.DataType.VECTOR_FP32,   # 32位浮点密集向量
                dimension=vector_size,                 # 向量维度
                index_param=zvec.HnswIndexParam(       # HNSW 索引（分层可导航小世界图）
                    metric_type=metric                 # 距离度量
                ),
            ),
        ],
    )


# ========== 固定字段名集合 ==========
# 这些字段在 Schema 中有独立定义，不进入 payload_json。
# 用于 _meta_to_fields / _fields_to_meta 的分拣逻辑。

_SCALAR_FIELD_NAMES = {
    "memory_id", "user_id", "memory_type",
    "content", "importance", "timestamp", "payload_json",
    "is_rag_data", "data_source", "rag_namespace", "modality",
}


# ========================================================================
# ZvecVectorStore — Zvec 后端的 VectorStoreBase 实现
# ========================================================================

class ZvecVectorStore(VectorStoreBase):
    """Zvec 进程内向量数据库存储实现。

    特点:
      - 纯进程内运行，无需 Docker、云服务或任何外部进程。
      - 数据持久化到磁盘目录，支持重启后恢复。
      - 支持密集向量 + HNSW 索引，可扩展到百万级数据。
      - SQL 风格过滤表达式（比 Qdrant 的 Python 对象过滤器更简洁）。
      - 多进程可同时读取，写入需独占。

    使用示例:
        store = ZvecVectorStore(
            path="./my_data",
            collection_name="memories",
            vector_size=384,
            distance="cosine",
        )
        store.add_vectors(vectors=..., metadata=..., ids=...)
        results = store.search_similar(query_vector=..., limit=10)
    """

    # ========== VectorStoreBase 要求的三个只读属性 ==========

    @property
    def collection_name(self) -> str:
        """集合名称，只读。在 __init__ 中设定后不可更改。"""
        return self._collection_name

    @property
    def vector_size(self) -> int:
        """向量维度，只读。所有存入/查询的向量必须匹配此维度。"""
        return self._vector_size

    @property
    def store_type(self) -> str:
        """存储类型标识，固定返回 "zvec"。用于日志和统计。"""
        return "zvec"

    # ========== 构造与初始化 ==========

    def __init__(
        self,
        path: Optional[str] = None,
        collection_name: str = "hello_agents_vectors",
        vector_size: int = 384,
        distance: str = "cosine",
        **kwargs
    ):
        """初始化 Zvec 向量存储。

        === 初始化流程 ===

        1. 检查 zvec 包是否已安装。
        2. 确定数据目录: {path}/{collection_name}/
        3. 尝试打开已有 collection → 如果不存在则新建。

        === 数据目录结构 ===

        {path}/
          └── {collection_name}/     ← 每个 collection 一个独立目录
              ├── data/              ← Zvec 内部数据文件
              └── wal/               ← Write-Ahead Log（写入日志，保证持久性）

        Args:
            path: 数据根目录。默认为 ./zvec_data。
                  不同 collection 在此目录下各自拥有子目录。
            collection_name: 集合名称。不同记忆类型使用不同的 collection。
            vector_size: 向量维度。必须与嵌入模型的输出维度一致。
            distance: 距离度量。cosine（余弦）, dot（内积）, euclidean（欧几里得）。
            **kwargs: 预留扩展参数。
        """
        if not ZVEC_AVAILABLE:
            raise ImportError(
                "zvec 未安装。请运行: pip install zvec\n"
                "Zvec 是阿里开源的进程内向量数据库，无需服务器或 Docker。\n"
                "文档: https://zvec.org/llms.txt"
            )

        # ---- 保存基本配置 ----
        self._collection_name = collection_name
        self._vector_size = vector_size
        self._distance = distance

        # ---- 确定数据存储目录 ----
        # 数据目录结构: {path}/{collection_name}/
        # 这样不同 collection 的数据互相隔离
        # 注意: 只创建父目录，不创建 collection 目录本身。
        #       Zvec 的 create_and_open 要求路径不存在（它会自动创建），
        #       而 open 要求路径已存在且是有效的 collection。
        #       如果提前创建了空目录，open 会因找不到有效数据而报错，
        #       create_and_open 又会因为目录已存在而报错，导致两头不靠。
        if path is None:
            path = os.path.join(os.getcwd(), "zvec_data")
        self._path = os.path.join(path, collection_name)
        # 只确保父目录存在
        os.makedirs(path, exist_ok=True)

        # ---- 打开或创建 collection ----
        self._collection = None
        self._init_collection()

    def _init_collection(self):
        """初始化 Zvec collection：优先打开已有，不存在则新建。

        这是 __init__ 的核心步骤。策略：

        1. 先尝试 zvec.open() —— 适用于进程重启后恢复已有数据。
        2. 如果 open 失败（路径不存在 或 不是有效的 collection）:
           a. 清理路径（zvec.create_and_open 要求路径不存在）
           b. 调用 create_and_open 新建。

        === 为什么不能预先创建目录 ===

        Zvec 有两个入口:
          - zvec.open(path):      要求 path 已存在且是有效的 collection。
          - zvec.create_and_open(path, schema): 要求 path 不存在（自动创建）。

        如果预先 os.makedirs(path)，会导致:
          - open 失败（空目录不是有效 collection）
          - create_and_open 也失败（目录已存在）

        所以只能创建父目录，让 Zvec 自己管理 collection 子目录。
        """
        try:
            # 步骤1: 尝试打开已有 collection（进程重启恢复数据场景）
            self._collection = zvec.open(self._path)
            logger.info(
                f"打开已有 Zvec 集合: {self._collection_name} "
                f"(路径: {self._path})"
            )
        except Exception:
            # 步骤2: open 失败 → 需要新建。
            #         先清理可能残留的路径（空目录、损坏数据等），
            #         因为 create_and_open 要求目标路径不存在。
            import shutil
            if os.path.exists(self._path):
                shutil.rmtree(self._path)
                logger.debug(f"清理残留路径: {self._path}")

            # _build_zvec_schema 定义了所有字段和索引
            schema = _build_zvec_schema(
                self._collection_name,
                self._vector_size,
                self._distance,
            )
            self._collection = zvec.create_and_open(self._path, schema)
            logger.info(
                f"创建新 Zvec 集合: {self._collection_name} "
                f"(路径: {self._path}, 维度: {self._vector_size}, 距离: {self._distance})"
            )

    # ========================================================================
    # 元数据 ↔ Zvec 标量字段 转换
    # ========================================================================
    # 这是 Zvec 实现最关键的部分。
    #
    # 问题: 上游业务代码的 metadata 字典字段灵活多变。
    #       episodic 有 session_id、context、outcome 等，
    #       semantic 有 entities、entity_count、relations 等。
    #       但 Zvec Schema 要求所有标量字段预先定义。
    #
    # 方案: 固定字段 + JSON 载荷（payload_json）
    #       固定字段 → Schema 中有独立定义，用于过滤查询。
    #       payload_json → 所有其他字段序列化为 JSON 存入，查询时还原。
    #
    # 数据流:
    #   写入: metadata dict → _meta_to_fields() → Zvec fields dict → Doc.fields
    #   读取: Doc.fields → _fields_to_meta() → metadata dict

    def _meta_to_fields(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        """将上游元数据字典转换为 Zvec 标量字段字典。

        分拣逻辑:
        1. 固定字段直接提取 → Zvec 独立字段。
        2. 固定字段之外的 key → 序列化到 payload_json。

        Args:
            meta: 上游的完整元数据字典。

        Returns:
            适配 Zvec Doc.fields 的字典。
        """
        # 固定字段直接映射
        fields = {
            "memory_id": str(meta.get("memory_id", "")),
            "user_id": str(meta.get("user_id", "")),
            "memory_type": str(meta.get("memory_type", "")),
            "content": str(meta.get("content", "")),
            "importance": float(meta.get("importance", 0.5)),
            "timestamp": int(meta.get("timestamp", 0)),
            # RAG 管线专用字段
            "is_rag_data": str(meta.get("is_rag_data", "")),
            "data_source": str(meta.get("data_source", "")),
            "rag_namespace": str(meta.get("rag_namespace", "")),
            "modality": str(meta.get("modality", "")),
        }

        # 不在固定字段中的额外数据 → payload_json（JSON 序列化）
        extra = {k: v for k, v in meta.items() if k not in _SCALAR_FIELD_NAMES}
        fields["payload_json"] = json.dumps(extra, ensure_ascii=False, default=str)
        return fields

    def _fields_to_meta(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """将 Zvec 标量字段字典还原为上游元数据字典。

        还原逻辑:
        1. 固定字段直接取出。
        2. payload_json 反序列化 → 合并到元数据中。

        Args:
            fields: Zvec Doc.fields。

        Returns:
            与上游格式兼容的元数据字典。
        """
        # 固定字段直接取出
        meta = {
            "memory_id": fields.get("memory_id", ""),
            "user_id": fields.get("user_id", ""),
            "memory_type": fields.get("memory_type", ""),
            "content": fields.get("content", ""),
            "importance": fields.get("importance", 0.5),
            "timestamp": fields.get("timestamp", 0),
            # RAG 管线专用字段
            "is_rag_data": fields.get("is_rag_data", ""),
            "data_source": fields.get("data_source", ""),
            "rag_namespace": fields.get("rag_namespace", ""),
            "modality": fields.get("modality", ""),
        }

        # 还原 payload_json 中的额外字段
        payload_str = fields.get("payload_json", "{}")
        if payload_str:
            try:
                extra = json.loads(payload_str)
                if isinstance(extra, dict):
                    meta.update(extra)  # 合并回元数据
            except (json.JSONDecodeError, TypeError):
                pass  # 解析失败时丢弃额外字段，不影响核心数据
        return meta

    # ========================================================================
    # VectorStoreBase 九大接口实现
    # ========================================================================

    # ---- 1. add_vectors ----

    def add_vectors(
        self,
        vectors: List[List[float]],
        metadata: List[Dict[str, Any]],
        ids: Optional[List[str]] = None,
    ) -> bool:
        """添加向量到 Zvec。

        内部步骤:
        1. 校验输入（空列表、维度匹配）。
        2. 将每个 (vector, metadata, id) 三元组转换为 zvec.Doc 对象。
        3. 调用 self._collection.upsert() 批量写入。
           upsert 语义: ID 已存在则更新（覆盖），不存在则插入。
           这与 Qdrant 的 upsert 行为一致。
        4. Zvec 的 upsert 会先将数据写入 WAL（预写日志），
           保证进程崩溃或断电后数据不丢失。

        Args:
            vectors: 向量列表。
            metadata: 元数据列表（与 vectors 一一对应）。
            ids: 可选的文档 ID 列表。

        Returns:
            bool: 全部成功返回 True。
        """
        try:
            if not vectors:
                logger.warning("向量列表为空")
                return False

            # 生成 ID（如果未提供）
            if ids is None:
                import uuid
                from datetime import datetime
                ids = [
                    f"vec_{i}_{int(datetime.now().timestamp() * 1000000)}"
                    for i in range(len(vectors))
                ]

            # 逐条构建 zvec.Doc 对象
            docs = []
            for vector, meta, doc_id in zip(vectors, metadata, ids):
                # 维度校验：Zvec Schema 中定义了固定的向量维度，不匹配会报错
                vlen = len(vector)
                if vlen != self._vector_size:
                    logger.warning(
                        f"向量维度不匹配: 期望{self._vector_size}, 实际{vlen}，跳过此条"
                    )
                    continue

                # 元数据 → 标量字段（固定字段 + JSON 载荷）
                fields = self._meta_to_fields(meta)
                safe_id = str(doc_id)

                # 构建文档: id + 命名向量 + 标量字段
                doc = zvec.Doc(
                    id=safe_id,
                    vectors={"embedding": vector},  # 向量名必须与 Schema 匹配
                    fields=fields,
                )
                docs.append(doc)

            if not docs:
                logger.warning("没有有效的文档可插入")
                return False

            # 批量 upsert（insert or update）
            # Zvec 内部机制: 数据先写入 WAL → 内存缓冲区 → 定期合并到索引
            # 如果需要立即搜索到，可调用 self._collection.optimize() 强制构建索引
            result = self._collection.upsert(docs)
            logger.info(f"成功添加 {len(docs)} 个向量到 Zvec")
            return True

        except Exception as e:
            logger.error(f"添加向量失败: {e}")
            return False

    # ---- 2. search_similar ----

    def search_similar(
        self,
        query_vector: List[float],
        limit: int = 10,
        score_threshold: Optional[float] = None,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """搜索相似向量。

        内部步骤:
        1. 校验查询向量维度。
        2. 将 where 字典转换为 Zvec SQL 过滤字符串。
        3. 构造 VectorQuery 对象，调用 collection.query()。
        4. 将 Zvec 返回的 Doc 列表转换为统一的 {id, score, metadata} 格式。
        5. 如果指定了 score_threshold，过滤低分结果。

        Args:
            query_vector: 查询向量。
            limit: 返回结果数量上限。
            score_threshold: 相似度阈值（可选）。
            where: 过滤条件字典。

        Returns:
            统一格式的搜索结果列表。
        """
        try:
            # 维度校验
            if len(query_vector) != self._vector_size:
                logger.error(
                    f"查询向量维度错误: 期望{self._vector_size}, 实际{len(query_vector)}"
                )
                return []

            # 字典 → SQL 过滤字符串
            # 例如: {"memory_type": "episodic"} → "memory_type = 'episodic'"
            filter_str = _dict_to_filter_string(where) if where else None

            # 执行向量查询
            result = self._collection.query(
                zvec.VectorQuery(
                    field_name="embedding",    # 指定搜索哪个向量字段
                    vector=query_vector,       # 查询向量
                ),
                topk=limit,                    # 返回前 K 个最相似结果
                filter=filter_str,             # SQL 风格过滤条件
            )

            # 将 Zvec 结果转换为统一格式
            results = []
            for doc in result:
                score = getattr(doc, "score", 0.0)

                # 分数阈值过滤
                if score_threshold is not None and score < score_threshold:
                    continue

                results.append({
                    "id": doc.id,
                    "score": score,
                    "metadata": self._fields_to_meta(doc.fields or {}),
                })

            logger.debug(f"Zvec 搜索返回 {len(results)} 个结果")
            return results

        except Exception as e:
            logger.error(f"向量搜索失败: {e}")
            return []

    # ---- 3. delete_vectors ----

    def delete_vectors(self, ids: List[str]) -> bool:
        """按主键 ID 删除向量（直接调用 Zvec 原生 delete API）。

        Args:
            ids: 要删除的文档主键 ID 列表。

        Returns:
            bool: 是否成功。
        """
        try:
            if not ids:
                return True

            self._collection.delete(ids=ids)
            logger.info(f"成功删除 {len(ids)} 个向量")
            return True

        except Exception as e:
            logger.error(f"删除向量失败: {e}")
            return False

    # ---- 4. delete_by_filter ----

    def delete_by_filter(self, where: Dict[str, Any]) -> bool:
        """按条件过滤删除向量。

        将 where 字典转换为 Zvec SQL 过滤字符串，
        然后调用 Zvec 原生的 delete_by_filter。

        Args:
            where: 过滤条件字典。

        Returns:
            bool: 是否成功。
        """
        try:
            if not where:
                return True

            filter_str = _dict_to_filter_string(where)
            self._collection.delete_by_filter(filter=filter_str)
            logger.info(f"按条件删除成功: {where}")
            return True

        except Exception as e:
            logger.error(f"按条件删除失败: {e}")
            return False

    # ---- 5. delete_memories（覆盖基类默认实现） ----

    def delete_memories(self, memory_ids: List[str]) -> None:
        """按 memory_id 批量删除记忆。

        覆盖基类的默认实现。基类默认调用 delete_by_filter，
        这里直接用 Zvec 原生 delete_by_filter 配合 IN 语法，
        效率更高（单次网络/IO 调用）。

        生成的过滤字符串示例:
          memory_id IN ('id1', 'id2', 'id3')

        Args:
            memory_ids: 记忆 ID 列表。
        """
        if not memory_ids:
            return
        self.delete_by_filter({"memory_id": memory_ids})

    # ---- 6. clear_collection ----

    def clear_collection(self) -> bool:
        """清空集合（物理删除数据目录后重建）。

        步骤:
        1. 释放当前 collection 对象。
        2. 递归删除整个数据目录（{path}/{collection_name}/）。
        3. 重建空目录。
        4. 重新创建 collection（Schema 不变）。

        Returns:
            bool: 是否成功。
        """
        try:
            # 释放 collection 对象（否则文件被占用无法删除）
            self._collection = None

            # 物理删除整个数据目录
            import shutil
            if os.path.exists(self._path):
                shutil.rmtree(self._path)
            # 不重建目录 —— _init_collection 内部会调用 create_and_open 自动创建

            # 重建 collection
            self._init_collection()
            logger.info(f"成功清空 Zvec 集合: {self._collection_name}")
            return True

        except Exception as e:
            logger.error(f"清空集合失败: {e}")
            return False

    # ---- 7. get_collection_info ----

    def get_collection_info(self) -> Dict[str, Any]:
        """获取集合基本信息。

        Zvec 的 stats 对象包含 rows 属性（文档总数）。
        其余字段为兼容上层接口的映射。

        Returns:
            集合信息字典。
        """
        try:
            stats = self._collection.stats
            return {
                "name": self._collection_name,
                "vectors_count": getattr(stats, "rows", 0),
                "indexed_vectors_count": getattr(stats, "rows", 0),
                "points_count": getattr(stats, "rows", 0),
                "segments_count": 0,  # Zvec 无此概念，填 0 兼容
                "config": {
                    "vector_size": self._vector_size,
                    "distance": self._distance,
                    "path": self._path,
                },
            }
        except Exception as e:
            logger.error(f"获取集合信息失败: {e}")
            return {}

    # ---- 8. get_collection_stats ----

    def get_collection_stats(self) -> Dict[str, Any]:
        """获取集合统计信息（带 store_type 标识）。

        Returns:
            包含 store_type="zvec" 的统计信息字典。
        """
        info = self.get_collection_info()
        if not info:
            return {"store_type": "zvec", "name": self._collection_name}
        info["store_type"] = "zvec"
        return info

    # ---- 9. health_check ----

    def health_check(self) -> bool:
        """健康检查。

        对于 Zvec 这种进程内数据库，健康检查很简单：
        能正常访问 collection.stats 即视为健康。

        Returns:
            bool: 正常返回 True。
        """
        try:
            _ = self._collection.stats
            return True
        except Exception as e:
            logger.error(f"Zvec 健康检查失败: {e}")
            return False

    # ========== 资源清理 ==========

    def close(self):
        """显式关闭 collection，释放文件句柄。

        调用此方法后，可以安全地删除数据目录。
        关闭后不能再进行任何操作，需要重新 open 或 create。
        """
        if self._collection is not None:
            # Zvec collection 对象内部持有 RocksDB 的 LOCK 文件句柄。
            # 将引用置 None 后，Python GC 会触发 __del__ 释放底层 C++ 资源。
            self._collection = None
            logger.debug(f"Zvec 集合已关闭: {self._collection_name}")

    def __del__(self):
        """析构函数，确保资源释放。"""
        self.close()


"""
Qdrant向量数据库存储实现
使用专业的Qdrant向量数据库替代ChromaDB
"""

import logging
import os
import uuid
import threading
from typing import Dict, List, Optional, Any, Union
import numpy as np
from datetime import datetime

from .vector_store_base import VectorStoreBase

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
    from qdrant_client.http.models import (
        Distance, VectorParams, PointStruct,
        Filter, FieldCondition, MatchValue, SearchRequest
    )
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantClient = None
    models = None

logger = logging.getLogger(__name__)

class QdrantConnectionManager:
    """Qdrant连接管理器 - 防止重复连接和初始化"""
    _instances = {}  # key: (url, collection_name) -> QdrantVectorStore instance
    _lock = threading.Lock()
    
    @classmethod
    def get_instance(
        cls, 
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        collection_name: str = "hello_agents_vectors",
        vector_size: int = 384,
        distance: str = "cosine",
        timeout: int = 30,
        **kwargs
    ) -> 'QdrantVectorStore':
        """获取或创建Qdrant实例（单例模式）"""
        # 创建唯一键
        key = (url or "local", collection_name)
        
        if key not in cls._instances:
            with cls._lock:
                # 双重检查锁定
                if key not in cls._instances:
                    logger.debug(f"🔄 创建新的Qdrant连接: {collection_name}")
                    cls._instances[key] = QdrantVectorStore(
                        url=url,
                        api_key=api_key,
                        collection_name=collection_name,
                        vector_size=vector_size,
                        distance=distance,
                        timeout=timeout,
                        **kwargs
                    )
                else:
                    logger.debug(f"♻️ 复用现有Qdrant连接: {collection_name}")
        else:
            logger.debug(f"♻️ 复用现有Qdrant连接: {collection_name}")
            
        return cls._instances[key]

class QdrantVectorStore(VectorStoreBase):
    """Qdrant向量数据库存储实现

    实现 VectorStoreBase 的全部抽象方法，封装 Qdrant 客户端 API。

    架构角色:
      这是 VectorStoreBase 在 Qdrant 后端的具体实现。
      它与 ZvecVectorStore 共享完全相同的接口，可以无缝互换。

    Qdrant 特点:
      - 客户端-服务器架构，需要运行 Qdrant 服务（Docker 或云）。
      - 支持分布式部署，可扩展到十亿级向量。
      - Payload 是灵活的 JSON，无需预定义 Schema。
      - 过滤使用 Python 对象模型（Filter/FieldCondition），而非字符串表达式。
    """

    # ================================================================
    # VectorStoreBase 要求的三个只读属性
    # ================================================================
    # 这些属性通过 @property 实现，值在 __init__ 中存入私有变量。
    # 上层代码通过 store.collection_name / store.vector_size / store.store_type 访问。

    @property
    def collection_name(self) -> str:
        """集合名称，只读。等价于 Qdrant 的 collection name。"""
        return self._collection_name

    @property
    def vector_size(self) -> int:
        """向量维度，只读。创建 collection 时写入，之后不可变。"""
        return self._vector_size

    @property
    def store_type(self) -> str:
        """存储类型标识，固定返回 "qdrant"。用于日志和统计。"""
        return "qdrant"

    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        collection_name: str = "hello_agents_vectors",
        vector_size: int = 384,
        distance: str = "cosine",
        timeout: int = 30,
        **kwargs
    ):
        """
        初始化Qdrant向量存储 (支持本地、自定义URL、云服务三种模式)。

        === 初始化流程 ===

        1. 保存配置参数到实例变量。
        2. 读取 HNSW/搜索相关的环境变量调优参数。
        3. 映射距离度量字符串到 Qdrant Distance 枚举。
        4. 调用 _initialize_client() 建立连接 + 确保 collection 存在。

        === 三种连接模式 ===

          url + api_key → Qdrant 云服务 (https://cloud.qdrant.io)
          url           → 自定义部署 (自建服务器)
          都不传         → 本地 localhost:6333 (Docker 运行)

        === 向量维度契约 ===

        vector_size 必须与嵌入模型输出维度一致:
          - all-MiniLM-L6-v2 → 384
          - text-embedding-3-small → 1536
          - qwen3-embedding → 用户配置
        写入时会对每条向量做维度校验。

        Args:
            url: Qdrant 服务 URL (云服务或自定义部署)。None 则用本地。
            api_key: Qdrant 云服务 API 密钥。
            collection_name: 集合名称。不同记忆类型使用不同 collection。
            vector_size: 向量维度。
            distance: 距离度量 (cosine, dot, euclidean)。
            timeout: 网络请求超时时间（秒）。
        """
        if not QDRANT_AVAILABLE:
            raise ImportError(
                "qdrant-client未安装。请运行: pip install qdrant-client>=1.6.0"
            )

        # ---- 保存配置 ----
        self.url = url
        self.api_key = api_key
        self._collection_name = collection_name   # 私有变量，供 property 返回
        self._vector_size = vector_size           # 私有变量，供 property 返回
        self.timeout = timeout

        # ---- HNSW / 搜索参数（可通过环境变量覆盖默认值） ----
        # hnsw_m: HNSW 图中每个节点的最大连接数（越大越精确但越占内存，默认32）
        try:
            self.hnsw_m = int(os.getenv("QDRANT_HNSW_M", "32"))
        except Exception:
            self.hnsw_m = 32
        # hnsw_ef_construct: 索引构建时的搜索深度（越大越精确但构建越慢，默认256）
        try:
            self.hnsw_ef_construct = int(os.getenv("QDRANT_HNSW_EF_CONSTRUCT", "256"))
        except Exception:
            self.hnsw_ef_construct = 256
        # search_ef: 查询时的搜索深度（越大召回率越高但越慢，默认128）
        try:
            self.search_ef = int(os.getenv("QDRANT_SEARCH_EF", "128"))
        except Exception:
            self.search_ef = 128
        # search_exact: 是否使用精确搜索（默认关闭，使用近似搜索）
        self.search_exact = os.getenv("QDRANT_SEARCH_EXACT", "0") == "1"

        # ---- 距离度量映射 ----
        # 统一接口使用字符串 (cosine/dot/euclidean)，转为 Qdrant 枚举
        distance_map = {
            "cosine": Distance.COSINE,      # 余弦相似度（推荐用于文本嵌入）
            "dot": Distance.DOT,            # 点积/内积
            "euclidean": Distance.EUCLID,   # 欧几里得距离
        }
        self.distance = distance_map.get(distance.lower(), Distance.COSINE)

        # ---- 建立连接 ----
        self.client = None
        self._initialize_client()
        
    def _initialize_client(self):
        """初始化Qdrant客户端和集合"""
        try:
            # 根据配置创建客户端连接
            if self.url and self.api_key:
                # 使用云服务API
                self.client = QdrantClient(
                    url=self.url,
                    api_key=self.api_key,
                    timeout=self.timeout
                )
                logger.info(f"✅ 成功连接到Qdrant云服务: {self.url}")
            elif self.url:
                # 使用自定义URL（无API密钥）
                self.client = QdrantClient(
                    url=self.url,
                    timeout=self.timeout
                )
                logger.info(f"✅ 成功连接到Qdrant服务: {self.url}")
            else:
                # 使用本地服务（默认）
                self.client = QdrantClient(
                    host="localhost",
                    port=6333,
                    timeout=self.timeout
                )
                logger.info("✅ 成功连接到本地Qdrant服务: localhost:6333")
            
            # 检查连接
            collections = self.client.get_collections()
            
            # 创建或获取集合
            self._ensure_collection()
            
        except Exception as e:
            logger.error(f"❌ Qdrant连接失败: {e}")
            if not self.url:
                logger.info("💡 本地连接失败，可以考虑使用Qdrant云服务")
                logger.info("💡 或启动本地服务: docker run -p 6333:6333 qdrant/qdrant")
            else:
                logger.info("💡 请检查URL和API密钥是否正确")
            raise
    
    def _ensure_collection(self):
        """确保集合存在，不存在则创建"""
        try:
            # 检查集合是否存在
            collections = self.client.get_collections().collections
            collection_names = [c.name for c in collections]
            
            if self.collection_name not in collection_names:
                # 创建新集合
                hnsw_cfg = None
                try:
                    hnsw_cfg = models.HnswConfigDiff(m=self.hnsw_m, ef_construct=self.hnsw_ef_construct)
                except Exception:
                    hnsw_cfg = None
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.vector_size,
                        distance=self.distance
                    ),
                    hnsw_config=hnsw_cfg
                )
                logger.info(f"✅ 创建Qdrant集合: {self.collection_name}")
            else:
                logger.info(f"✅ 使用现有Qdrant集合: {self.collection_name}")
                # 尝试更新 HNSW 配置
                try:
                    self.client.update_collection(
                        collection_name=self.collection_name,
                        hnsw_config=models.HnswConfigDiff(m=self.hnsw_m, ef_construct=self.hnsw_ef_construct)
                    )
                except Exception as ie:
                    logger.debug(f"跳过更新HNSW配置: {ie}")
            # 确保必要的payload索引
            self._ensure_payload_indexes()
                
        except Exception as e:
            logger.error(f"❌ 集合初始化失败: {e}")
            raise

    def _ensure_payload_indexes(self):
        """为常用过滤字段创建payload索引"""
        try:
            index_fields = [
                ("memory_type", models.PayloadSchemaType.KEYWORD),
                ("user_id", models.PayloadSchemaType.KEYWORD),
                ("memory_id", models.PayloadSchemaType.KEYWORD),
                ("timestamp", models.PayloadSchemaType.INTEGER),
                ("modality", models.PayloadSchemaType.KEYWORD),  # 感知记忆模态筛选
                ("source", models.PayloadSchemaType.KEYWORD),
                ("external", models.PayloadSchemaType.BOOL),
                ("namespace", models.PayloadSchemaType.KEYWORD),
                # RAG相关字段索引
                ("is_rag_data", models.PayloadSchemaType.BOOL),
                ("rag_namespace", models.PayloadSchemaType.KEYWORD),
                ("data_source", models.PayloadSchemaType.KEYWORD),
            ]
            for field_name, schema_type in index_fields:
                try:
                    self.client.create_payload_index(
                        collection_name=self.collection_name,
                        field_name=field_name,
                        field_schema=schema_type,
                    )
                except Exception as ie:
                    # 索引已存在会报错，忽略
                    logger.debug(f"索引 {field_name} 已存在或创建失败: {ie}")
        except Exception as e:
            logger.debug(f"创建payload索引时出错: {e}")
    
    def add_vectors(
        self, 
        vectors: List[List[float]], 
        metadata: List[Dict[str, Any]], 
        ids: Optional[List[str]] = None
    ) -> bool:
        """
        添加向量到Qdrant
        
        Args:
            vectors: 向量列表
            metadata: 元数据列表
            ids: 可选的ID列表
        
        Returns:
            bool: 是否成功
        """
        try:
            if not vectors:
                logger.warning("⚠️ 向量列表为空")
                return False
                
            # 生成ID（如果未提供）
            if ids is None:
                ids = [f"vec_{i}_{int(datetime.now().timestamp() * 1000000)}" 
                       for i in range(len(vectors))]
            
            # 构建点数据
            logger.info(f"[Qdrant] add_vectors start: n_vectors={len(vectors)} n_meta={len(metadata)} collection={self.collection_name}")
            points = []
            for i, (vector, meta, point_id) in enumerate(zip(vectors, metadata, ids)):
                # 确保向量是正确的维度
                try:
                    vlen = len(vector)
                except Exception:
                    logger.error(f"[Qdrant] 非法向量类型: index={i} type={type(vector)} value={vector}")
                    continue
                if vlen != self.vector_size:
                    logger.warning(f"⚠️ 向量维度不匹配: 期望{self.vector_size}, 实际{len(vector)}")
                    continue
                    
                # 添加时间戳到元数据
                meta_with_timestamp = meta.copy()
                meta_with_timestamp["timestamp"] = int(datetime.now().timestamp())
                meta_with_timestamp["added_at"] = int(datetime.now().timestamp())
                if "external" in meta_with_timestamp and not isinstance(meta_with_timestamp.get("external"), bool):
                    # normalize to bool
                    val = meta_with_timestamp.get("external")
                    meta_with_timestamp["external"] = True if str(val).lower() in ("1", "true", "yes") else False
                # 确保点ID是Qdrant接受的类型（无符号整数或UUID字符串）
                safe_id: Any
                if isinstance(point_id, int):
                    safe_id = point_id
                elif isinstance(point_id, str):
                    try:
                        uuid.UUID(point_id)
                        safe_id = point_id
                    except Exception:
                        safe_id = str(uuid.uuid4())
                else:
                    safe_id = str(uuid.uuid4())

                point = PointStruct(
                    id=safe_id,
                    vector=vector,
                    payload=meta_with_timestamp
                )
                points.append(point)
            
            if not points:
                logger.warning("⚠️ 没有有效的向量点")
                return False
            
            # 批量插入
            logger.info(f"[Qdrant] upsert begin: points={len(points)}")
            operation_info = self.client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True
            )
            logger.info("[Qdrant] upsert done")
            
            logger.info(f"✅ 成功添加 {len(points)} 个向量到Qdrant")
            return True
            
        except Exception as e:
            logger.error(f"❌ 添加向量失败: {e}")
            return False
    
    def search_similar(
        self, 
        query_vector: List[float], 
        limit: int = 10, 
        score_threshold: Optional[float] = None,
        where: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        搜索相似向量
        
        Args:
            query_vector: 查询向量
            limit: 返回结果数量限制
            score_threshold: 相似度阈值
            where: 过滤条件
        
        Returns:
            List[Dict]: 搜索结果
        """
        try:
            if len(query_vector) != self.vector_size:
                logger.error(f"❌ 查询向量维度错误: 期望{self.vector_size}, 实际{len(query_vector)}")
                return []
            
            # 构建过滤器
            query_filter = None
            if where:
                conditions = []
                for key, value in where.items():
                    if isinstance(value, (str, int, float, bool)):
                        conditions.append(
                            FieldCondition(
                                key=key,
                                match=MatchValue(value=value)
                            )
                        )
                
                if conditions:
                    query_filter = Filter(must=conditions)
            
            # 执行搜索
            # 搜索参数
            search_params = None
            try:
                search_params = models.SearchParams(hnsw_ef=self.search_ef, exact=self.search_exact)
            except Exception:
                search_params = None
            search_result = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=False,
                search_params=search_params
            )
            
            # 转换结果格式
            results = []
            for hit in search_result:
                result = {
                    "id": hit.id,
                    "score": hit.score,
                    "metadata": hit.payload or {}
                }
                results.append(result)
            
            logger.debug(f"🔍 Qdrant搜索返回 {len(results)} 个结果")
            return results
            
        except Exception as e:
            logger.error(f"❌ 向量搜索失败: {e}")
            return []
    
    def delete_vectors(self, ids: List[str]) -> bool:
        """
        删除向量（按主键ID）

        Args:
            ids: 要删除的向量ID列表

        Returns:
            bool: 是否成功
        """
        try:
            if not ids:
                return True

            operation_info = self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(
                    points=ids
                ),
                wait=True
            )

            logger.info(f"成功删除 {len(ids)} 个向量")
            return True

        except Exception as e:
            logger.error(f"删除向量失败: {e}")
            return False

    def delete_by_filter(self, where: Dict[str, Any]) -> bool:
        """
        按条件过滤删除向量（VectorStoreBase 抽象方法实现）。

        这是基类要求的通用过滤删除接口。与 delete_memories 的关系:
          - delete_by_filter: 通用接口，接受任意过滤条件。
          - delete_memories:  业务便捷方法，专用于按 memory_id 删除。

        实现细节:
          1. 遍历 where 字典的每个 key-value。
          2. 如果 value 是 list → 构建 should (OR) 过滤器，单独一条 delete 调用。
          3. 如果 value 是标量 → 累积到 conditions 中，最后用 must (AND) 一次删除。
          4. 同时包含 list 和标量时会分多次调用（Qdrant 不支持混合逻辑的单一 filter）。

        Args:
            where: 过滤条件字典。
                   例如 {"memory_type": "episodic"} → 删除所有情景记忆。
                   例如 {"memory_id": ["id1", "id2"]} → 批量删除指定记忆。

        Returns:
            bool: 是否成功。
        """
        try:
            if not where:
                return True

            conditions = []
            for key, value in where.items():
                if isinstance(value, list):
                    # 多值匹配：使用 should (OR)
                    self.client.delete(
                        collection_name=self.collection_name,
                        points_selector=models.FilterSelector(
                            filter=Filter(should=[
                                FieldCondition(key=key, match=MatchValue(value=v))
                                for v in value
                            ])
                        ),
                        wait=True,
                    )
                elif isinstance(value, (str, int, float, bool)):
                    conditions.append(
                        FieldCondition(key=key, match=MatchValue(value=value))
                    )

            if conditions:
                self.client.delete(
                    collection_name=self.collection_name,
                    points_selector=models.FilterSelector(
                        filter=Filter(must=conditions)
                    ),
                    wait=True,
                )

            logger.info(f"按条件删除成功: {where}")
            return True

        except Exception as e:
            logger.error(f"按条件删除失败: {e}")
            return False

    def delete_memories(self, memory_ids: List[str]):
        """
        删除指定记忆（覆盖基类默认实现，使用 Qdrant 原生 should 过滤器）。

        注意：由于写入时可能将非UUID的点ID转换为UUID，这里不再依赖点ID，
        而是通过payload中的memory_id来匹配删除，确保一致性。

        与基类默认实现的区别:
          基类默认实现调用 self.delete_by_filter({"memory_id": memory_ids})，
          这里直接使用 Qdrant 原生 Filter(should=...) + FilterSelector，
          单次 API 调用即可完成批量删除，更高效。

        Args:
            memory_ids: 要删除的记忆 ID 列表（业务层 memory_id，非点 ID）。
        """
        try:
            if not memory_ids:
                return
            # 构建 should 过滤条件：memory_id 等于任一给定值
            conditions = [
                FieldCondition(key="memory_id", match=MatchValue(value=mid))
                for mid in memory_ids
            ]
            query_filter = Filter(should=conditions)
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=query_filter),
                wait=True,
            )
            logger.info(f"成功按memory_id删除 {len(memory_ids)} 个Qdrant向量")
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            raise

    def clear_collection(self) -> bool:
        """
        清空集合

        Returns:
            bool: 是否成功
        """
        try:
            # 删除并重新创建集合
            self.client.delete_collection(collection_name=self.collection_name)
            self._ensure_collection()

            logger.info(f"成功清空Qdrant集合: {self.collection_name}")
            return True

        except Exception as e:
            logger.error(f"清空集合失败: {e}")
            return False
    
    def get_collection_info(self) -> Dict[str, Any]:
        """
        获取集合信息
        
        Returns:
            Dict: 集合信息
        """
        try:
            collection_info = self.client.get_collection(self.collection_name)
            
            info = {
                "name": self.collection_name,
                "vectors_count": collection_info.vectors_count,
                "indexed_vectors_count": collection_info.indexed_vectors_count,
                "points_count": collection_info.points_count,
                "segments_count": collection_info.segments_count,
                "config": {
                    "vector_size": self.vector_size,
                    "distance": self.distance.value,
                }
            }
            
            return info
            
        except Exception as e:
            logger.error(f"❌ 获取集合信息失败: {e}")
            return {}
    
    def get_collection_stats(self) -> Dict[str, Any]:
        """
        获取集合统计信息（兼容抽象接口）
        """
        info = self.get_collection_info()
        if not info:
            return {"store_type": "qdrant", "name": self.collection_name}
        info["store_type"] = "qdrant"
        return info
    
    def health_check(self) -> bool:
        """
        健康检查
        
        Returns:
            bool: 服务是否健康
        """
        try:
            # 尝试获取集合列表
            collections = self.client.get_collections()
            return True
        except Exception as e:
            logger.error(f"❌ Qdrant健康检查失败: {e}")
            return False
    
    def __del__(self):
        """析构函数，清理资源"""
        if hasattr(self, 'client') and self.client:
            try:
                self.client.close()
            except:
                pass

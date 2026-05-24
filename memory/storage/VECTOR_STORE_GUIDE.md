# 向量数据库存储系统 — 使用与扩展指南

## 目录

1. [系统概览](#1-系统概览)
2. [快速开始](#2-快速开始)
3. [架构设计](#3-架构设计)
4. [后端选择机制](#4-后端选择机制)
5. [如何切换后端](#5-如何切换后端)
6. [如何新增向量数据库](#6-如何新增向量数据库)
7. [API 接口参考](#7-api-接口参考)
8. [常见问题](#8-常见问题)

---

## 1. 系统概览

### 1.1 什么是向量存储系统？

向量存储系统是记忆模块的底层基础设施，负责：

- **写入**: 将业务层编码好的向量和元数据持久化存储。
- **搜索**: 给定查询向量，返回最相似的文档列表。
- **过滤**: 在搜索时按元数据条件（如记忆类型、用户ID）过滤结果。
- **删除**: 按主键或条件批量删除文档。
- **管理**: 健康检查、统计信息、清空等。

### 1.2 当前支持的后端

| 后端 | 类型 | 适用场景 |
|---|---|---|
| **Zvec** (默认) | 进程内嵌入库 | 开发、CI、边缘设备、轻量部署 |
| **Qdrant** | 客户端-服务器 | 生产环境、大规模数据、分布式部署 |

### 1.3 文件清单

```
memory/storage/
  vector_store_base.py       ← 抽象基类（接口定义）
  vector_store_manager.py    ← 统一管理器（工厂 + 单例）
  qdrant_store.py            ← Qdrant 后端实现
  zvec_store.py              ← Zvec 后端实现
  VECTOR_STORE_GUIDE.md      ← 本文档
  __init__.py                ← 模块导出
```

---

## 2. 快速开始

### 2.1 零配置使用（默认 Zvec）

```python
from memory.storage import VectorStoreManager

# 无需任何环境变量或外部服务
store = VectorStoreManager.get_instance(
    collection_name="my_memories",
    vector_size=384,
)

# 写入向量
store.add_vectors(
    vectors=[[0.1, 0.2, ..., 0.5]],   # 384维向量
    metadata=[{
        "memory_id": "mem_001",
        "user_id": "user_123",
        "memory_type": "episodic",
        "content": "今天学习了Python",
        "importance": 0.8,
    }],
    ids=["mem_001"],
)

# 搜索相似向量
results = store.search_similar(
    query_vector=[0.3, 0.1, ..., 0.7],
    limit=10,
    where={"memory_type": "episodic"},
)

# 查看统计
print(store.get_collection_stats())
```

### 2.2 使用 Qdrant 后端

Qdrant 需要额外的服务进程。两种启动方式：

```bash
# 本地开发: Docker 启动 Qdrant
docker run -p 6333:6333 qdrant/qdrant

# 或使用 Qdrant 云服务: 获取 URL 和 API Key
# https://cloud.qdrant.io/
```

**重要**: 设置 `VECTOR_STORE_TYPE=qdrant` 才会启用 Qdrant。仅设置 `QDRANT_URL` 不会自动切换。

```bash
export VECTOR_STORE_TYPE=qdrant
export QDRANT_URL=http://localhost:6333
```

```python
from memory.storage import VectorStoreManager

# 方式 A: 显式传入 store_type
store = VectorStoreManager.get_instance(
    "qdrant",                              # ← 必须显式指定
    url="http://localhost:6333",
    collection_name="my_memories",
    vector_size=384,
)

# 方式 B: 通过环境变量 VECTOR_STORE_TYPE=qdrant 控制
store = VectorStoreManager.get_instance(
    url="http://localhost:6333",
    collection_name="my_memories",
    vector_size=384,
)

# 所有 API 调用完全一致
results = store.search_similar(query_vector=..., limit=10)
```

### 2.3 在业务代码中使用

三个记忆类型（EpisodicMemory、SemanticMemory、PerceptualMemory）已经全部接入 `VectorStoreManager`，你不需要直接操作向量存储。只需通过环境变量控制后端：

```bash
# 默认 Zvec（推荐，零配置）
python app.py

# 切换到 Qdrant
export VECTOR_STORE_TYPE=qdrant
export QDRANT_URL=http://localhost:6333
python app.py
```

---

## 3. 架构设计

### 3.1 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│                    MemoryManager                             │
│                  (业务层 — manager.py)                        │
│                                                              │
│   add_memory() → retrieve_memories() → get_memory_stats()   │
└────────┬──────────┬──────────┬───────────────────────────────┘
         │          │          │
    ┌────▼───┐ ┌───▼────┐ ┌──▼──────────┐
    │Working │ │Episodic│ │  Semantic    │  ← 记忆类型
    │Memory  │ │Memory  │ │  Memory      │    (types/*.py)
    └────┬───┘ └───┬────┘ └──┬──────────┘
         │         │         │
         │    ┌────▼─────────▼─────┐
         │    │  VectorStoreBase  │  ← 抽象接口
         │    │  (统一 API)        │    (vector_store_base.py)
         │    └──┬────────────┬───┘
         │       │            │
         │  ┌────▼────┐  ┌───▼──────────┐
         │  │ Qdrant  │  │    Zvec      │  ← 具体实现
         │  │ Store   │  │    Store     │    (qdrant_store.py
         │  └─────────┘  └──────────────┘     zvec_store.py)
         │
    ┌────▼────────┐
    │  Working    │  ← 工作记忆不依赖向量存储
    │  (内存)     │    (纯内存实现)
    └─────────────┘
```

### 3.2 核心设计模式

#### 模板方法模式 (Template Method)

`VectorStoreBase` 定义了接口骨架，`QdrantVectorStore` 和 `ZvecVectorStore` 各自实现具体逻辑。

#### 工厂方法模式 (Factory Method)

`VectorStoreManager.get_instance()` 根据配置创建合适的具体实现。

#### 单例模式 (Singleton)

同一个 `(后端, collection)` 组合只创建一个实例，防止重复连接。

#### 适配器模式 (Adapter)

`ZvecVectorStore` 将 Zvec 原生的 SQL 风格过滤 API 适配为统一的字典格式过滤 API。

### 3.3 数据流

```
写入流程:

  MemoryItem ──► BaseMemory.add()
                    │
                    ├──► embedder.encode(content)  → 向量
                    │
                    └──► vector_store.add_vectors(
                            vectors=[...],
                            metadata=[{memory_id, user_id, memory_type, ...}],
                            ids=[...]
                        )
                            │
                            ▼ (Qdrant)
                        PointStruct(id, vector, payload)
                            │
                            ▼ (Zvec)
                        _meta_to_fields() → Doc(id, vectors, fields)


查询流程:

  query ──► embedder.encode(query) → 查询向量
                │
                ▼
  vector_store.search_similar(
      query_vector=...,
      limit=10,
      where={"memory_type": "episodic"}
  )
                │
                ▼ (Qdrant: Filter/FieldCondition 对象)
                ▼ (Zvec:  "memory_type = 'episodic'" 字符串)
                │
                ▼
  [{id, score, metadata}, ...]  ← 统一返回格式
```

---

## 4. 后端选择机制

### 4.1 决策流程

`VectorStoreManager` 按以下优先级决定使用哪个后端：

```
调用 get_instance(store_type=None, ...)
        │
        ▼
1. store_type 参数显式指定?  ──是──▶ 使用指定后端 ("qdrant" / "zvec")
        │ 否
        ▼
2. 环境变量 VECTOR_STORE_TYPE 有值? ──是──▶ 使用环境变量指定的后端
        │ 否
        ▼
3. 默认使用 Zvec（零依赖、纯本地、开箱即用）
```

**url/api_key/path 等连接参数不会影响后端选择。** 它们仅作为选定后端的连接配置。例如 `.env` 中设置了 `QDRANT_URL` 但不设置 `VECTOR_STORE_TYPE`，系统仍使用 Zvec。

### 4.2 关键环境变量

| 变量 | 作用 | 示例 |
|---|---|---|
| `VECTOR_STORE_TYPE` | **唯一**切换后端的开关（不设置则默认 Zvec） | `qdrant` / `zvec` |
| `QDRANT_URL` | Qdrant 服务地址（仅当后端为 Qdrant 时读取） | `http://localhost:6333` |
| `QDRANT_API_KEY` | Qdrant 云服务密钥（仅当后端为 Qdrant 时读取） | `sk-xxx` |
| `QDRANT_COLLECTION` | Qdrant 集合名 | `hello_agents_vectors` |
| `QDRANT_DISTANCE` | 距离度量 | `cosine` / `dot` / `euclidean` |
| `QDRANT_HNSW_M` | HNSW M 参数 | `32` |
| `QDRANT_SEARCH_EF` | 搜索深度 | `128` |
| `ZVEC_DATA_PATH` | Zvec 数据存储根目录（仅当后端为 Zvec 时读取）。不设置则默认 `./zvec_data/` | `./zvec_data` |

### 4.3 单例 Key 规则

为了避免不同场景的实例互相覆盖，每种 `(后端, 连接参数, collection)` 组合生成唯一 key：

| 后端 | Key 格式 | 示例 |
|---|---|---|
| Qdrant | `qdrant:{url}:{collection_name}` | `qdrant:http://localhost:6333:memories` |
| Zvec | `zvec:{path}:{collection_name}` | `zvec:./zvec_data:memories` |

---

## 5. 如何切换后端

### 5.1 全局切换（推荐）

设置 `VECTOR_STORE_TYPE` 环境变量。不设置则默认 Zvec。

```bash
# 默认 Zvec（什么都不用设，数据存 ./zvec_data/）
python app.py

# Zvec + 自定义数据目录
export ZVEC_DATA_PATH=/data/my_vectors
python app.py

# 切换到 Qdrant
export VECTOR_STORE_TYPE=qdrant
export QDRANT_URL=http://localhost:6333
python app.py
```

### 5.2 代码中显式切换

```python
from memory.storage import VectorStoreManager

# 显式使用 Zvec
store = VectorStoreManager.get_instance(
    "zvec",
    path="./my_data",
    collection_name="memories",
)

# 显式使用 Qdrant
store = VectorStoreManager.get_instance(
    "qdrant",
    url="http://localhost:6333",
    collection_name="memories",
)
```

### 5.3 混合部署

同一个进程中可以同时使用多个后端（不同 collection 用不同后端）：

```python
# 情景记忆用 Zvec（本地快速）
episodic_store = VectorStoreManager.get_instance(
    "zvec", collection_name="episodic_memories"
)

# 语义记忆用 Qdrant（共享服务）
semantic_store = VectorStoreManager.get_instance(
    "qdrant", collection_name="semantic_memories"
)
```

---

## 6. 如何新增向量数据库

### 6.1 概述

新增后端只需 3 步：

1. **创建新的 Store 类** — 继承 `VectorStoreBase`，实现全部抽象方法。
2. **在 Manager 中注册** — 在 `VectorStoreManager._create_instance()` 添加一个分支。
3. **更新 `__init__.py`** — 导出新类（可选但推荐）。

### 6.2 详细步骤（以添加 Milvus 为例）

#### 步骤 1: 创建 `milvus_store.py`

```python
# memory/storage/milvus_store.py

"""Milvus向量数据库存储实现"""

import logging
from typing import Dict, List, Optional, Any
from .vector_store_base import VectorStoreBase

# 检查依赖是否安装
try:
    from pymilvus import Collection, connections, FieldSchema, CollectionSchema, DataType
    MILVUS_AVAILABLE = True
except ImportError:
    MILVUS_AVAILABLE = False

logger = logging.getLogger(__name__)


class MilvusVectorStore(VectorStoreBase):
    """Milvus 向量数据库存储实现"""

    # === 1. 实现三个必须属性 ===

    @property
    def collection_name(self) -> str:
        return self._collection_name

    @property
    def vector_size(self) -> int:
        return self._vector_size

    @property
    def store_type(self) -> str:
        return "milvus"               # ← 唯一标识符

    # === 2. 实现构造函数 ===

    def __init__(
        self,
        host: str = "localhost",
        port: int = 19530,
        collection_name: str = "hello_agents_vectors",
        vector_size: int = 384,
        distance: str = "cosine",
        **kwargs
    ):
        if not MILVUS_AVAILABLE:
            raise ImportError("pymilvus 未安装。请运行: pip install pymilvus")

        self._collection_name = collection_name
        self._vector_size = vector_size
        self.host = host
        self.port = port

        # 建立连接
        connections.connect(host=host, port=port)

        # 确保 collection 存在
        self.collection = self._ensure_collection()

    def _ensure_collection(self):
        """确保 collection 存在，不存在则创建。"""
        # ... Milvus 特定的 collection 创建逻辑
        pass

    # === 3. 实现九个抽象方法 ===

    def add_vectors(
        self,
        vectors: List[List[float]],
        metadata: List[Dict[str, Any]],
        ids: Optional[List[str]] = None,
    ) -> bool:
        # ... Milvus insert 实现
        pass

    def search_similar(
        self,
        query_vector: List[float],
        limit: int = 10,
        score_threshold: Optional[float] = None,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        # ... Milvus search 实现
        pass

    def delete_vectors(self, ids: List[str]) -> bool:
        # ... 按主键删除
        pass

    def delete_by_filter(self, where: Dict[str, Any]) -> bool:
        # ... 按条件删除。
        # Milvus 使用布尔表达式字符串，如 'memory_type == "episodic"'
        # 需要将 where 字典转换为此格式（参考 zvec_store.py 的 _dict_to_filter_string）
        pass

    def clear_collection(self) -> bool:
        # ... 清空逻辑
        pass

    def get_collection_info(self) -> Dict[str, Any]:
        # ... 获取信息
        pass

    def get_collection_stats(self) -> Dict[str, Any]:
        # ... 获取统计
        pass

    def health_check(self) -> bool:
        # ... 健康检查
        pass
```

#### 步骤 2: 在 `VectorStoreManager` 中注册

编辑 `vector_store_manager.py` 的 `_create_instance` 方法，添加新分支：

```python
@classmethod
def _create_instance(cls, store_type, collection_name, vector_size, distance, **kwargs):
    # ... 前面代码不变 ...

    elif store_type == "milvus":           # ← 新增分支
        from .milvus_store import MilvusVectorStore

        host = kwargs.get("host", "localhost")
        port = int(kwargs.get("port", 19530))

        return MilvusVectorStore(
            host=host,
            port=port,
            collection_name=collection_name,
            vector_size=vector_size,
            distance=distance,
        )

    # elif store_type == "your_new_backend":
    #     ...

    else:
        raise ValueError(f"不支持的向量存储类型: {store_type}")
```

同时更新 `_build_instance_key` 以支持新的 key 格式：

```python
@classmethod
def _build_instance_key(cls, store_type, collection_name, **kwargs):
    # ...
    elif store_type == "milvus":
        host = kwargs.get("host", "localhost")
        port = kwargs.get("port", 19530)
        return f"milvus:{host}:{port}:{collection_name}"
    # ...
```

以及 `_resolve_store_type` 的自动探测逻辑（可选，但一般不需要）：

```python
@classmethod
def _resolve_store_type(cls, store_type=None, **kwargs):
    # 注意: kwargs 中的连接参数不应该影响后端选择。
    # 后端选择应仅通过 store_type 参数或 VECTOR_STORE_TYPE 环境变量控制。
    # 下面的代码仅为示例，实际不推荐在 _resolve_store_type 中根据 kwargs 推断后端。
    ...
```

#### 步骤 3: 更新 `__init__.py`（可选）

```python
# memory/storage/__init__.py

from .milvus_store import MilvusVectorStore

__all__ = [
    # ... 原有导出 ...
    "MilvusVectorStore",
]
```

### 6.3 实现检查清单

开发新后端时，请确保：

- [ ] 继承 `VectorStoreBase`
- [ ] 实现 3 个 property: `collection_name`, `vector_size`, `store_type`
- [ ] 实现 9 个方法: `add_vectors`, `search_similar`, `delete_vectors`, `delete_by_filter`, `clear_collection`, `get_collection_info`, `get_collection_stats`, `health_check`
- [ ] 可选覆盖: `delete_memories`（基类有默认实现，但如果后端有更高效的批量过滤删除可以覆盖）
- [ ] `store_type` 返回值唯一，不与现有后端冲突
- [ ] `add_vectors` 使用 **upsert 语义**（ID 已存在时覆盖，不报错）
- [ ] `search_similar` 返回 **统一格式**: `[{id, score, metadata}]`
- [ ] `search_similar` 的 `where` 参数接受 **字典格式**（可内部转换）
- [ ] 检查依赖是否安装，未安装时给出清晰的错误提示
- [ ] 在 `VectorStoreManager._create_instance` 中注册
- [ ] 在 `VectorStoreManager._build_instance_key` 中定义单例 key 规则

### 6.4 接口契约详情

#### add_vectors — 写入

```
输入:
  vectors: List[List[float]]    ← 每个是 vector_size 维的浮点列表
  metadata: List[Dict]          ← 与 vectors 一一对应，至少包含 memory_id, user_id, memory_type
  ids: Optional[List[str]]      ← 可选的主键，未提供则自动生成

输出:
  bool                          ← True=成功, False=失败

行为:
  - 空列表 → 返回 False
  - 维度不匹配的向量 → 跳过（不阻塞其他向量）
  - ID 已存在 → 覆盖（upsert）
  - metadata 中所有字段都要能完整还原（重要！）
```

#### search_similar — 搜索

```
输入:
  query_vector: List[float]     ← 查询向量
  limit: int                    ← 返回上限
  score_threshold: float | None ← 分数阈值
  where: Dict | None            ← 过滤条件 {"field": value}

输出:
  List[Dict]                    ← [{"id": ..., "score": ..., "metadata": {...}}, ...]

行为:
  - 返回结果按 score 降序排列（高分在前）
  - where 为 None → 不过滤
  - where 有值 → 只返回匹配过滤条件的文档
  - metadata 必须完整还原（所有字段）
```

---

## 7. API 接口参考

### 7.1 VectorStoreBase（抽象接口）

所有后端都实现此接口。

```python
class VectorStoreBase(ABC):
    # === 属性 ===
    collection_name: str        # 只读，集合名称
    vector_size: int            # 只读，向量维度
    store_type: str             # 只读，后端类型 ("qdrant" / "zvec" / ...)

    # === 写入 ===
    def add_vectors(
        self,
        vectors: List[List[float]],
        metadata: List[Dict[str, Any]],
        ids: Optional[List[str]] = None,
    ) -> bool: ...

    # === 查询 ===
    def search_similar(
        self,
        query_vector: List[float],
        limit: int = 10,
        score_threshold: Optional[float] = None,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]: ...

    # === 删除 ===
    def delete_vectors(self, ids: List[str]) -> bool: ...
    def delete_by_filter(self, where: Dict[str, Any]) -> bool: ...
    def delete_memories(self, memory_ids: List[str]) -> None: ...  # 已有默认实现

    # === 管理 ===
    def clear_collection(self) -> bool: ...
    def get_collection_info(self) -> Dict[str, Any]: ...
    def get_collection_stats(self) -> Dict[str, Any]: ...
    def health_check(self) -> bool: ...
```

### 7.2 VectorStoreManager（管理器）

```python
class VectorStoreManager:
    # === 核心方法 ===
    @classmethod
    def get_instance(
        cls,
        store_type: Optional[str] = None,      # "qdrant" / "zvec" / None(自动)
        collection_name: str = "hello_agents_vectors",
        vector_size: int = 384,
        distance: str = "cosine",
        **kwargs,                               # url, api_key, path, timeout 等
    ) -> VectorStoreBase: ...

    # === 管理方法 ===
    @classmethod
    def list_instances(cls) -> Dict[str, str]: ...      # 列出所有缓存的实例
    @classmethod
    def remove_instance(cls, ...) -> None: ...           # 移除指定实例
    @classmethod
    def clear_all(cls) -> None: ...                      # 清除所有实例
```

### 7.3 结果格式

```python
# search_similar 返回格式
[
    {
        "id": "mem_001",
        "score": 0.95,
        "metadata": {
            "memory_id": "mem_001",
            "user_id": "user_123",
            "memory_type": "episodic",
            "content": "...",
            "importance": 0.8,
            "timestamp": 1717000000,
            # ... 其他字段
        }
    },
    # ...
]

# get_collection_stats 返回格式
{
    "name": "hello_agents_vectors",
    "vectors_count": 1234,
    "points_count": 1234,
    "store_type": "zvec",     # 或 "qdrant"
    "config": {
        "vector_size": 384,
        "distance": "cosine",
        "path": "./zvec_data/hello_agents_vectors",  # Zvec 专属
    }
}
```

---

## 8. 常见问题

### Q: 默认用 Zvec 还是 Qdrant？

**默认 Zvec。** 原因：零依赖、零配置、pip install 即可。适合开发和中小规模部署。

要切换到 Qdrant，需**显式**设置环境变量 `VECTOR_STORE_TYPE=qdrant`（或在代码中传 `store_type="qdrant"`）。仅设置 `QDRANT_URL` 不会自动切换 —— 连接参数不影响后端选择。

### Q: Zvec 和 Qdrant 的性能差异？

| 维度 | Zvec | Qdrant |
|---|---|---|
| 启动速度 | 即时（进程内） | 需等待服务启动 |
| 百万级数据 | 良好 | 良好 |
| 十亿级数据 | 需测试 | 支持（分布式） |
| 并发读取 | 支持（多进程） | 支持（多客户端） |
| 并发写入 | 单进程独占 | 支持 |

### Q: 数据存在哪里？

- **Zvec**: `{ZVEC_DATA_PATH}/{collection_name}/`，默认 `./zvec_data/{collection_name}/`。路径可通过环境变量 `ZVEC_DATA_PATH` 或代码中的 `path` 参数指定。优先级: 显式传参 > 环境变量 > 默认值。
- **Qdrant**: 由 Qdrant 服务管理（Docker volume 或云存储）

### Q: 如何迁移数据？

目前需要自行编写迁移脚本。基本思路：

```python
# 从 Qdrant 导出
old_store = VectorStoreManager.get_instance("qdrant", ...)
# 遍历所有数据...

# 导入 Zvec
new_store = VectorStoreManager.get_instance("zvec", ...)
new_store.add_vectors(vectors=exported_vectors, metadata=exported_metadata)
```

### Q: 新增后端后需要重启吗？

不需要。`VectorStoreManager` 在进程内管理实例，新增后端代码后重新运行 Python 进程即可生效。

### Q: 元数据中的自定义字段能正常存取吗？

能。两个后端都完整保留所有元数据字段：
- **Qdrant**: payload 是灵活的 JSON，直接存取。
- **Zvec**: 固定字段之外的字段序列化到 `payload_json`，存取时自动序列化/反序列化，对上层透明。

### Q: 如何调试后端选择问题？

```python
from memory.storage import VectorStoreManager

store = VectorStoreManager.get_instance(...)
print(store.store_type)  # 查看实际使用的后端
print(VectorStoreManager.list_instances())  # 查看所有缓存的实例
```

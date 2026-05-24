# 图数据库存储系统 — 使用与扩展指南

## 目录

1. [系统概览](#1-系统概览)
2. [快速开始](#2-快速开始)
3. [架构设计](#3-架构设计)
4. [后端选择机制](#4-后端选择机制)
5. [如何切换后端](#5-如何切换后端)
6. [如何新增图数据库](#6-如何新增图数据库)
7. [API 接口参考](#7-api-接口参考)
8. [常见问题](#8-常见问题)

---

## 1. 系统概览

### 1.1 什么是图存储系统？

图存储系统是语义记忆（SemanticMemory）的底层基础设施。当记忆内容被添加时，系统从中抽取实体（人物、地点、技能等）和关系，构建**知识图谱**：

- **节点（Node）**: 实体。如 "张三"(PERSON)、"Python"(SKILL)、"北京"(LOC)。
- **边（Edge）**: 关系。如 "张三 -[HAS_MEMORY]-> 记忆1"、"记忆1 -[MENTIONS]-> 北京"。
- **图遍历**: 从某个实体出发，沿边 N 跳找到所有关联实体，从而发现相关记忆。

### 1.2 当前支持的后端

| 后端 | 类型 | 查询方式 | 适用场景 |
|---|---|---|---|
| **SQLite** (默认) | 进程内嵌入 | 递归 CTE（WITH RECURSIVE） | 开发、桌面端、App、单机部署 |
| **Neo4j** | 客户端-服务器 | Cypher | 生产环境、大规模数据、分布式部署 |

### 1.3 文件清单

```
memory/storage/
  graph_store_base.py         ← 抽象基类（接口定义）
  graph_store_manager.py      ← 统一管理器（工厂 + 单例）
  neo4j_store.py              ← Neo4j 后端实现（Cypher 查询）
  sqlite_graph_store.py       ← SQLite 后端实现（递归 CTE 图遍历）
  GRAPH_STORE_GUIDE.md        ← 本文档
  __init__.py                 ← 模块导出
```

### 1.4 与向量数据库体系的对称关系

```
向量数据库:  VectorStoreBase → Zvec(嵌入)  / Qdrant(远程)
图数据库:    GraphStoreBase  → SQLite(嵌入) / Neo4j(远程)

共同模式:
  - 基类定义接口，后端各自实现
  - Manager 统一管理，单例缓存
  - 环境变量一键切换，嵌入为默认
  - 上层业务代码只依赖基类接口
```

---

## 2. 快速开始

### 2.1 零配置使用（默认 SQLite）

SQLite 是 Python 内置模块，**不需要安装任何包**。

```python
from memory.storage import GraphStoreManager

# 零配置即可使用
store = GraphStoreManager.get_instance(name="semantic_graph")

# 添加实体（节点）
store.add_entity("entity_1", "Python", "SKILL", {"description": "编程语言"})
store.add_entity("entity_2", "机器学习", "CONCEPT")
store.add_entity("entity_3", "TensorFlow", "TOOL")

# 添加关系（边）
store.add_relationship("entity_1", "entity_2", "RELATED_TO")
store.add_relationship("entity_2", "entity_3", "USES", {"strength": 0.9})

# 图遍历：从 entity_1 出发，2 跳内找到所有相关实体
related = store.find_related_entities("entity_1", max_depth=2)
for r in related:
    print(f"{r['name']} ({r['type']})  distance={r['distance']}")

# 按名称模糊搜索
results = store.search_entities_by_name("Tensor")
# → [{"id": "entity_3", "name": "TensorFlow", "type": "TOOL"}]

# 获取某个实体的所有关系
rels = store.get_entity_relationships("entity_1")

# 查看统计
print(store.get_stats())
# → {"total_nodes": 3, "total_relationships": 2, ...}
```

### 2.2 使用 Neo4j 后端

```bash
# 1. 启动 Neo4j 服务（或使用 Aura 云服务）
docker run -p 7474:7474 -p 7687:7687 neo4j:5.14

# 2. 设置环境变量
export GRAPH_STORE_TYPE=neo4j
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=your-password
```

```python
from memory.storage import GraphStoreManager

# GRAPH_STORE_TYPE=neo4j 环境变量已设置 → 自动使用 Neo4j
store = GraphStoreManager.get_instance(
    uri="bolt://localhost:7687",
    username="neo4j",
    password="your-password",
)

# 所有 API 调用完全一致
store.add_entity("e1", "Python", "SKILL")
results = store.find_related_entities("e1", max_depth=2)
```

### 2.3 在业务代码中使用

SemanticMemory 已经接入 `GraphStoreManager`，你不需要直接操作图数据库。只需在 `.env` 中配置：

```bash
# 默认 SQLite（什么都不用设）
python app.py

# 切换到 Neo4j
GRAPH_STORE_TYPE=neo4j python app.py
```

---

## 3. 架构设计

### 3.1 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│                    SemanticMemory                             │
│                  (业务层 — types/semantic.py)                  │
│                                                              │
│   _extract_entities() → _graph_search() → get_stats()       │
└───────────────────────┬──────────────────────────────────────┘
                        │
                   ┌────▼────────────┐
                   │ GraphStoreBase  │  ← 抽象接口
                   │ (统一 API)       │    (graph_store_base.py)
                   └──┬──────────┬───┘
                      │          │
                 ┌────▼────┐ ┌──▼──────────────┐
                 │ Neo4j   │ │    SQLite       │  ← 具体实现
                 │ Store   │ │    Store        │    (neo4j_store.py
                 └─────────┘ └─────────────────┘     sqlite_graph_store.py)
```

### 3.2 SQLite 图遍历原理

图遍历是图数据库最核心的能力。SQLite 没有内置图查询语言，但可以用**递归 CTE**（Common Table Expression）实现。

以 `find_related_entities("u1", max_depth=2)` 为例：

```
    u1 ──[HAS_MEMORY]──→ m1 ──[MENTIONS]──→ c1
    │                    │
    └──[HAS_MEMORY]──→ m2 ──[INVOLVES]──→ c2
                                        ↑
                         c1 ──[CO_OCCURS]─┘
```

```sql
WITH RECURSIVE related(id, name, depth) AS (
    -- 第1层: 从 u1 出发直接到达的节点
    SELECT n.id, n.name, 1
    FROM edges e JOIN nodes n ON n.id = e.to_id
    WHERE e.from_id = 'u1'

    UNION ALL

    -- 第N层: 从上一层结果继续向外扩展
    SELECT n.id, n.name, r.depth + 1
    FROM edges e JOIN related r ON e.from_id = r.id
    JOIN nodes n ON n.id = e.to_id
    WHERE r.depth < 2
)
SELECT DISTINCT id, name, depth FROM related ORDER BY depth;
```

结果：

| id | name | depth |
|---|---|---|
| m1 | 记忆1 | 1 |
| m2 | 记忆2 | 1 |
| c1 | 实体1 | 2 |
| c2 | 实体2 | 2 |

### 3.3 数据流

```
写入流程:

  text ──► _extract_entities() → [Entity, Entity, ...]
              │
              ├──► graph_store.add_entity(id, name, type, props)
              │         │
              │         ▼ (Neo4j)
              │    MERGE (e:Entity {id}) SET e += props
              │         │
              │         ▼ (SQLite)
              │    INSERT OR REPLACE INTO nodes (id, name, type, props_json)
              │
              └──► graph_store.add_relationship(from, to, type, props)
                        │
                        ▼
                   (同上: MERGE / INSERT OR REPLACE)


查询流程:

  query ──► _graph_search(query)
              │
              ├──► search_entities_by_name(pattern)  ← 从查询文本找匹配实体
              │
              └──► find_related_entities(entity_id, max_depth=2)
                        │
                        ▼ (Neo4j: MATCH path = (start)-[*1..2]-(related))
                        ▼ (SQLite: WITH RECURSIVE CTE)
                        │
                        ▼
                   [{id, name, type, distance}, ...]
```

---

## 4. 后端选择机制

### 4.1 决策流程

`GraphStoreManager` 按以下优先级决定使用哪个后端：

```
调用 get_instance(store_type=None, ...)
        │
        ▼
1. store_type 参数显式指定?  ──是──▶ 使用指定后端 ("neo4j" / "sqlite")
        │ 否
        ▼
2. 环境变量 GRAPH_STORE_TYPE 有值? ──是──▶ 使用环境变量指定的后端
        │ 否
        ▼
3. 默认使用 SQLite（零依赖、纯本地、开箱即用）
```

**uri/path 等连接参数不会影响后端选择。** 它们仅作为选定后端的连接配置。例如 `.env` 中设置了 `NEO4J_URI` 但不设置 `GRAPH_STORE_TYPE`，系统仍使用 SQLite。

### 4.2 关键环境变量

| 变量 | 作用 | 示例 |
|---|---|---|
| `GRAPH_STORE_TYPE` | **唯一**切换后端的开关（不设置则默认 SQLite） | `neo4j` / `sqlite` |
| `SQLITE_GRAPH_PATH` | SQLite 数据存储根目录（仅当后端为 SQLite 时读取） | `./graph_data` |
| `NEO4J_URI` | Neo4j 连接 URI（仅当后端为 Neo4j 时读取） | `bolt://localhost:7687` |
| `NEO4J_USERNAME` | Neo4j 用户名（仅当后端为 Neo4j 时读取） | `neo4j` |
| `NEO4J_PASSWORD` | Neo4j 密码（仅当后端为 Neo4j 时读取） | `your-password` |
| `NEO4J_DATABASE` | Neo4j 数据库名 | `neo4j` |

### 4.3 单例 Key 规则

| 后端 | Key 格式 | 示例 |
|---|---|---|
| SQLite | `sqlite:{path}:{name}` | `sqlite:./graph_data:semantic_graph` |
| Neo4j | `neo4j:{uri}:{database}` | `neo4j:bolt://localhost:7687:neo4j` |

---

## 5. 如何切换后端

### 5.1 全局切换（推荐）

设置 `GRAPH_STORE_TYPE` 环境变量。不设置则默认 SQLite。

```bash
# 默认 SQLite（什么都不用设，数据存 ./graph_data/semantic_graph.db）
python app.py

# SQLite + 自定义数据目录
export SQLITE_GRAPH_PATH=/data/graphs
python app.py

# 切换到 Neo4j
export GRAPH_STORE_TYPE=neo4j
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=your-password
python app.py
```

### 5.2 代码中显式切换

```python
from memory.storage import GraphStoreManager

# 显式使用 SQLite
store = GraphStoreManager.get_instance(
    "sqlite",
    path="./my_graph_data",
    name="semantic_graph",
)

# 显式使用 Neo4j
store = GraphStoreManager.get_instance(
    "neo4j",
    uri="bolt://localhost:7687",
    username="neo4j",
    password="your-password",
)
```

### 5.3 混合部署

同一个进程中可以同时使用多个后端（不同 name 用不同后端）：

```python
# 主知识图谱用 SQLite（本地快速）
main_store = GraphStoreManager.get_instance("sqlite", name="semantic_graph")

# 共享知识库用 Neo4j（团队共享）
shared_store = GraphStoreManager.get_instance("neo4j", name="shared_knowledge")
```

---

## 6. 如何新增图数据库

### 6.1 概述

新增后端只需 3 步：

1. **创建新的 Store 类** — 继承 `GraphStoreBase`，实现 9 个抽象方法。
2. **在 Manager 中注册** — 在 `GraphStoreManager._create_instance()` 添加一个分支。
3. **更新 `__init__.py`** — 导出新类（可选但推荐）。

### 6.2 详细步骤（以添加 NetworkX 为例）

#### 步骤 1: 创建 `networkx_graph_store.py`

```python
# memory/storage/networkx_graph_store.py

"""NetworkX 图数据库存储实现"""

import os
import logging
from typing import Dict, List, Optional, Any
from .graph_store_base import GraphStoreBase

try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False

logger = logging.getLogger(__name__)


class NetworkXGraphStore(GraphStoreBase):
    """NetworkX 图数据库存储实现

    使用 NetworkX 在内存中存储图，通过序列化实现持久化。
    """

    @property
    def store_type(self) -> str:
        return "networkx"

    def __init__(self, path=None, name="semantic_graph", **kwargs):
        if not NETWORKX_AVAILABLE:
            raise ImportError("networkx 未安装。pip install networkx")

        self._name = name
        if path is None:
            path = os.path.join(os.getcwd(), "graph_data")
        os.makedirs(path, exist_ok=True)
        self._path = os.path.join(path, f"{name}.graphml")

        self.G = nx.DiGraph()
        self._load()

    def _save(self):
        """持久化到文件。每次写入后自动调用。"""
        nx.write_graphml(self.G, self._path)

    def _load(self):
        """从文件恢复图。启动时自动调用。"""
        if os.path.exists(self._path):
            self.G = nx.read_graphml(self._path)

    # === 实现 9 个抽象方法 ===

    def add_entity(self, entity_id, name, entity_type, properties=None):
        try:
            self.G.add_node(entity_id, name=name, type=entity_type,
                           **(properties or {}))
            self._save()
            return True
        except Exception as e:
            logger.error(f"添加实体失败: {e}")
            return False

    def add_relationship(self, from_id, to_id, rel_type, properties=None):
        try:
            self.G.add_edge(from_id, to_id, type=rel_type,
                           **(properties or {}))
            self._save()
            return True
        except Exception as e:
            logger.error(f"添加关系失败: {e}")
            return False

    def find_related_entities(self, entity_id, relationship_types=None,
                              max_depth=2, limit=50):
        try:
            if entity_id not in self.G:
                return []
            # BFS 遍历
            from collections import deque
            visited = {entity_id: 0}
            queue = deque([entity_id])
            while queue:
                current = queue.popleft()
                depth = visited[current]
                if depth >= max_depth:
                    continue
                for _, neighbor in self.G.out_edges(current):
                    if neighbor not in visited:
                        visited[neighbor] = depth + 1
                        queue.append(neighbor)
                for neighbor, _ in self.G.in_edges(current):
                    if neighbor not in visited:
                        visited[neighbor] = depth + 1
                        queue.append(neighbor)

            results = []
            for nid, dist in visited.items():
                if nid == entity_id:
                    continue
                data = self.G.nodes[nid]
                results.append({
                    "id": nid, "name": data.get("name", nid),
                    "type": data.get("type", ""), "distance": dist,
                })
            return sorted(results, key=lambda x: x["distance"])[:limit]
        except Exception as e:
            logger.error(f"图遍历失败: {e}")
            return []

    def search_entities_by_name(self, name_pattern, entity_types=None, limit=20):
        try:
            results = []
            for nid, data in self.G.nodes(data=True):
                if name_pattern.lower() in data.get("name", "").lower():
                    if entity_types and data.get("type") not in entity_types:
                        continue
                    results.append({"id": nid, **data})
            return results[:limit]
        except Exception as e:
            logger.error(f"搜索失败: {e}")
            return []

    def get_entity_relationships(self, entity_id):
        try:
            rels = []
            for _, neighbor, data in self.G.out_edges(entity_id, data=True):
                rels.append({
                    "relationship": {"type": data.get("type", "")},
                    "other_entity": {
                        "id": neighbor,
                        "name": self.G.nodes[neighbor].get("name", ""),
                        "type": self.G.nodes[neighbor].get("type", ""),
                    },
                    "direction": "outgoing",
                })
            for neighbor, _, data in self.G.in_edges(entity_id, data=True):
                rels.append({
                    "relationship": {"type": data.get("type", "")},
                    "other_entity": {
                        "id": neighbor,
                        "name": self.G.nodes[neighbor].get("name", ""),
                        "type": self.G.nodes[neighbor].get("type", ""),
                    },
                    "direction": "incoming",
                })
            return rels
        except Exception as e:
            logger.error(f"获取关系失败: {e}")
            return []

    def delete_entity(self, entity_id):
        try:
            if entity_id in self.G:
                self.G.remove_node(entity_id)
                self._save()
                return True
            return False
        except Exception as e:
            logger.error(f"删除实体失败: {e}")
            return False

    def clear_all(self):
        try:
            self.G.clear()
            self._save()
            return True
        except Exception as e:
            logger.error(f"清空失败: {e}")
            return False

    def get_stats(self):
        return {
            "total_nodes": self.G.number_of_nodes(),
            "total_relationships": self.G.number_of_edges(),
            "entity_nodes": self.G.number_of_nodes(),
            "memory_nodes": 0,
        }

    def health_check(self):
        return True

    def close(self):
        self._save()
```

#### 步骤 2: 在 `GraphStoreManager` 中注册

编辑 `graph_store_manager.py`：

```python
@classmethod
def _create_instance(cls, store_type, name, **kwargs):
    # ... 前面代码不变 ...

    elif store_type == "networkx":           # ← 新增分支
        from .networkx_graph_store import NetworkXGraphStore
        return NetworkXGraphStore(name=name, **kwargs)

    else:
        raise ValueError(f"不支持的图数据库类型: {store_type}")
```

#### 步骤 3: 更新 `__init__.py`（可选）

```python
from .networkx_graph_store import NetworkXGraphStore
```

### 6.3 实现检查清单

- [ ] 继承 `GraphStoreBase`
- [ ] 实现 `store_type` 属性（返回值唯一，不与现有后端冲突）
- [ ] 实现 9 个方法：`add_entity`, `add_relationship`, `find_related_entities`, `search_entities_by_name`, `get_entity_relationships`, `delete_entity`, `clear_all`, `get_stats`, `health_check`
- [ ] `find_related_entities` 返回格式: `[{"id": ..., "name": ..., "type": ..., "distance": ...}]`
- [ ] `get_entity_relationships` 返回格式: `[{"relationship": {...}, "other_entity": {...}, "direction": "outgoing"/"incoming"}]`
- [ ] `add_entity` 和 `add_relationship` 使用 **upsert 语义**（ID 已存在时覆盖）
- [ ] 检查依赖是否安装，未安装时给出清晰的错误提示
- [ ] 在 `GraphStoreManager._create_instance` 中注册
- [ ] 在 `GraphStoreManager._build_instance_key` 中定义单例 key 规则

---

## 7. API 接口参考

### 7.1 GraphStoreBase（抽象接口）

```python
class GraphStoreBase(ABC):
    # === 属性 ===
    store_type: str             # 只读，后端类型 ("neo4j" / "sqlite" / ...)

    # === 写入 ===
    def add_entity(self, entity_id: str, name: str, entity_type: str,
                   properties: Dict = None) -> bool: ...
    def add_relationship(self, from_entity_id: str, to_entity_id: str,
                         relationship_type: str, properties: Dict = None) -> bool: ...

    # === 查询 ===
    def find_related_entities(self, entity_id: str, relationship_types: List[str] = None,
                              max_depth: int = 2, limit: int = 50) -> List[Dict]: ...
    def search_entities_by_name(self, name_pattern: str, entity_types: List[str] = None,
                                limit: int = 20) -> List[Dict]: ...
    def get_entity_relationships(self, entity_id: str) -> List[Dict]: ...

    # === 删除 ===
    def delete_entity(self, entity_id: str) -> bool: ...
    def clear_all(self) -> bool: ...

    # === 管理 ===
    def get_stats(self) -> Dict[str, Any]: ...
    def health_check(self) -> bool: ...
```

### 7.2 GraphStoreManager（管理器）

```python
class GraphStoreManager:
    @classmethod
    def get_instance(cls, store_type: str = None, name: str = "semantic_graph",
                     **kwargs) -> GraphStoreBase: ...

    @classmethod
    def list_instances(cls) -> Dict[str, str]: ...
    @classmethod
    def remove_instance(cls, ...) -> None: ...
    @classmethod
    def clear_all(cls) -> None: ...
```

### 7.3 返回格式

```python
# find_related_entities 返回
[
    {"id": "entity_1", "name": "Python", "type": "SKILL", "distance": 1},
    {"id": "entity_2", "name": "机器学习", "type": "CONCEPT", "distance": 1},
    {"id": "entity_3", "name": "TensorFlow", "type": "TOOL", "distance": 2},
]

# search_entities_by_name 返回
[
    {"id": "entity_3", "name": "TensorFlow", "type": "TOOL", "memory_id": "m1"},
]

# get_entity_relationships 返回
[
    {
        "relationship": {"type": "HAS_MEMORY", "created_at": "..."},
        "other_entity": {"id": "m1", "name": "Python学习", "type": "MEMORY"},
        "direction": "outgoing"
    },
]

# get_stats 返回
{
    "total_nodes": 42,
    "total_relationships": 67,
    "entity_nodes": 35,
    "memory_nodes": 7,
}
```

---

## 8. 常见问题

### Q: 默认用 SQLite 还是 Neo4j？

**默认 SQLite。** 原因：Python 内置 sqlite3，零 pip 安装，零配置，单个 .db 文件。适合开发、桌面端、App、小规模部署。

要切换到 Neo4j，需**显式**设置 `GRAPH_STORE_TYPE=neo4j`（或在代码中传 `store_type="neo4j"`）。

### Q: SQLite 能处理多大的图？

递归 CTE 在百万级节点以下表现良好。语义记忆场景（每个记忆抽取 3-5 个实体，几千条记忆总共也就几万节点）完全够用。如果数据量超过百万节点，建议切换到 Neo4j。

### Q: 数据存在哪里？

- **SQLite**: `{SQLITE_GRAPH_PATH}/{name}.db`，默认 `./graph_data/semantic_graph.db`。路径优先级: 显式传参 > 环境变量 > 默认值。
- **Neo4j**: 由 Neo4j 服务管理（Docker volume 或 Aura 云存储）。

### Q: 如何迁移数据？

```python
# 从 Neo4j 导出 → 导入 SQLite
old = GraphStoreManager.get_instance("neo4j", ...)
new = GraphStoreManager.get_instance("sqlite", ...)

# 遍历所有节点和边...
for entity in old_nodes:
    new.add_entity(entity["id"], entity["name"], entity["type"])
```

### Q: SQLite 支持并发吗？

WAL 模式下多进程可读，写入需独占。桌面端/App 场景（单用户单进程）完全没问题。

### Q: 递归 CTE 支持多深？

`max_depth` 参数默认值为 2（2 跳），这足够覆盖绝大多数关联查询。SQLite 的递归深度上限为 1000，远超实际需求。

### Q: 如何调试后端选择问题？

```python
from memory.storage import GraphStoreManager

store = GraphStoreManager.get_instance()
print(store.store_type)                        # 查看实际后端
print(GraphStoreManager.list_instances())      # 查看所有缓存实例
```

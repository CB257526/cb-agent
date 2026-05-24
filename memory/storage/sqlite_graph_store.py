"""SQLite图数据库存储实现

使用 SQLite3 实现图数据库功能，通过递归 CTE（公共表表达式）进行图遍历。
SQLite3 是 Python 内置模块，零额外依赖，数据存为单个 .db 文件。

=== 设计理念 ===

  SQLite 本身不支持图查询语言，但图本质上就是"节点表 + 边表"。
  通过两张核心表和递归 CTE，可以完整实现 Neo4j 的核心功能:

    表结构:
      nodes  (id TEXT PK, name TEXT, type TEXT, props_json TEXT)
      edges  (from_id TEXT, to_id TEXT, type TEXT, props_json TEXT)

    核心查询（递归 CTE）:
      WITH RECURSIVE related AS (
          SELECT to_id, 1 AS depth FROM edges WHERE from_id = ?
          UNION ALL
          SELECT e.to_id, r.depth + 1
          FROM edges e JOIN related r ON e.from_id = r.to_id
          WHERE r.depth < ?
      )
      SELECT DISTINCT to_id FROM related

=== 与 Neo4j 的对应 ===

  Neo4j                    →  SQLite
  ─────────────────────────────────────────────
  MERGE (e:Entity {id})    →  INSERT OR REPLACE INTO nodes
  MATCH (a)-[r]->(b)       →  SELECT FROM edges WHERE from_id=?
  -[*1..N]-                →  WITH RECURSIVE ... CTE
  =~ '.*pattern.*'         →  LIKE '%pattern%'
  DETACH DELETE            →  DELETE FROM nodes; DELETE FROM edges WHERE ...
  RETURN count(n)          →  SELECT COUNT(*) FROM nodes

=== 数据目录结构 ===

  {path}                    ← 默认为 ./graph_data/
    └── {name}.db           ← 每个图数据库一个 SQLite 文件

=== 桌面端/App 部署优势 ===

  - Python 内置 sqlite3，零 pip 安装
  - 单个 .db 文件，拷贝即备份
  - WAL 模式自动崩溃恢复
  - 多进程可读，写入需独占（与 Zvec 一致）
"""

import json
import logging
import os
import sqlite3
from typing import Dict, List, Optional, Any

from .graph_store_base import GraphStoreBase

logger = logging.getLogger(__name__)


class SQLiteGraphStore(GraphStoreBase):
    """SQLite 图数据库存储实现。

    使用 SQLite3 + 递归 CTE 实现图数据库的全部功能。
    数据持久化到单个 .db 文件，适合嵌入、桌面端、App 场景。

    使用示例:
        store = SQLiteGraphStore(path="./graph_data", name="semantic_graph")
        store.add_entity("e1", "Python", "SKILL", {"memory_id": "m1"})
        store.add_relationship("e1", "e2", "RELATED")
        related = store.find_related_entities("e1", max_depth=2)
    """

    # ================================================================
    # GraphStoreBase 要求的属性
    # ================================================================

    @property
    def store_type(self) -> str:
        """存储类型标识，固定返回 "sqlite"。用于日志和统计。"""
        return "sqlite"

    # ================================================================
    # 构造与初始化
    # ================================================================

    def __init__(
        self,
        path: Optional[str] = None,
        name: str = "semantic_graph",
        **kwargs
    ):
        """初始化 SQLite 图存储。

        === 初始化流程 ===

        1. 确定数据库文件路径: {path}/{name}.db
        2. 连接 SQLite（文件不存在则自动创建）
        3. 开启 WAL 模式（提升并发读写性能 + 崩溃恢复）
        4. 创建 nodes 和 edges 表（IF NOT EXISTS）
        5. 创建索引（加速名称搜索和图遍历）

        Args:
            path: 数据根目录。默认为 ./graph_data。
            name: 数据库名称（决定文件名）。
            **kwargs: 预留扩展参数。
        """
        # ---- 确定数据库文件路径 ----
        if path is None:
            path = os.path.join(os.getcwd(), "graph_data")
        os.makedirs(path, exist_ok=True)
        self._path = os.path.join(path, f"{name}.db")

        # ---- 连接 SQLite ----
        # check_same_thread=False 允许跨线程使用（SQLite 本身是线程安全的）
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # 让查询结果支持按列名访问

        # ---- 开启 WAL 模式 ----
        # WAL (Write-Ahead Logging): 写入不阻塞读取，且崩溃后可自动恢复
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        # ---- 创建表和索引 ----
        self._init_schema()

        logger.info(
            f"创建/打开 SQLite 图数据库: {name} "
            f"(路径: {self._path})"
        )

    def _init_schema(self):
        """创建图数据库的表结构和索引。

        两张核心表:
          nodes: 存储实体节点。
          edges: 存储实体间的关系边。

        索引:
          - nodes.type:  加速按类型过滤（如 search_entities_by_name 的类型过滤）
          - nodes.name:  加速按名称搜索
          - edges.from_id / edges.to_id: 加速图遍历查询
        """
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'MISC',
                props_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'RELATED',
                props_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (from_id, to_id, type)
            )
        """)
        # 索引
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id)")
        self._conn.commit()

    # ================================================================
    # 工具方法
    # ================================================================

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """将 SQLite 行对象转为字典，并反序列化 props_json。"""
        d = dict(row)
        # 反序列化 JSON 属性
        props_str = d.pop("props_json", "{}")
        try:
            props = json.loads(props_str)
        except (json.JSONDecodeError, TypeError):
            props = {}
        d.update(props)
        return d

    def _props_to_json(self, properties: Optional[Dict[str, Any]]) -> str:
        """将属性字典序列化为 JSON 字符串。"""
        if not properties:
            return "{}"
        return json.dumps(properties, ensure_ascii=False, default=str)

    # ================================================================
    # GraphStoreBase 接口实现
    # ================================================================

    def add_entity(
        self,
        entity_id: str,
        name: str,
        entity_type: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """添加实体节点。

        使用 INSERT OR REPLACE 语义: ID 已存在则更新，不存在则创建。
        这与 Neo4j 的 MERGE 行为一致。
        """
        try:
            props_json = self._props_to_json(properties)
            self._conn.execute(
                """INSERT OR REPLACE INTO nodes (id, name, type, props_json)
                   VALUES (?, ?, ?, ?)""",
                (entity_id, name, entity_type, props_json),
            )
            self._conn.commit()
            logger.debug(f"添加实体: {name} ({entity_type})")
            return True
        except Exception as e:
            logger.error(f"添加实体失败: {e}")
            return False

    def add_relationship(
        self,
        from_entity_id: str,
        to_entity_id: str,
        relationship_type: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """添加实体间关系（边）。

        使用 INSERT OR REPLACE 语义: 同 (from, to, type) 组合已存在则更新。
        """
        try:
            props_json = self._props_to_json(properties)
            self._conn.execute(
                """INSERT OR REPLACE INTO edges (from_id, to_id, type, props_json)
                   VALUES (?, ?, ?, ?)""",
                (from_entity_id, to_entity_id, relationship_type, props_json),
            )
            self._conn.commit()
            logger.debug(f"添加关系: {from_entity_id} -{relationship_type}-> {to_entity_id}")
            return True
        except Exception as e:
            logger.error(f"添加关系失败: {e}")
            return False

    def find_related_entities(
        self,
        entity_id: str,
        relationship_types: Optional[List[str]] = None,
        max_depth: int = 2,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """从指定实体出发，沿边 N 跳找到所有相关实体。

        使用 SQLite 递归 CTE（WITH RECURSIVE）实现图遍历。

        CTE 逻辑说明:
          基础查询（第1层）: SELECT 所有从 entity_id 出发能直接到达的节点。
          递归查询（第N层）: 从上一层的结果继续向外扩展，depth < max_depth。
          最终收集所有不重复的节点。

        SQL 等价于 Neo4j 的 MATCH path = (start)-[*1..N]-(related)。

        注意: 此查询同时匹配出边和入边（无向遍历），以匹配 Neo4j 的变长路径语义。
        """
        try:
            # 构建关系类型过滤
            type_clause = ""
            params: List[Any] = [entity_id, max_depth, limit]
            if relationship_types:
                placeholders = ",".join("?" for _ in relationship_types)
                type_clause = f"AND e.type IN ({placeholders})"
                params = [entity_id, max_depth] + relationship_types + [limit]

            # 递归 CTE 图遍历（双向: 同时追踪出边和入边）
            sql = f"""
                WITH RECURSIVE related(id, name, type, props_json, depth) AS (
                    -- 基础: 从 entity_id 出发能直接到达的节点（出边 + 入边）
                    SELECT n.id, n.name, n.type, n.props_json, 1
                    FROM edges e
                    JOIN nodes n ON n.id = e.to_id
                    WHERE e.from_id = ? {type_clause}
                    UNION
                    SELECT n.id, n.name, n.type, n.props_json, 1
                    FROM edges e
                    JOIN nodes n ON n.id = e.from_id
                    WHERE e.to_id = ? {type_clause}
                    UNION ALL
                    -- 递归: 从上一层结果继续扩展
                    SELECT n.id, n.name, n.type, n.props_json, r.depth + 1
                    FROM edges e
                    JOIN related r ON e.from_id = r.id
                    JOIN nodes n ON n.id = e.to_id
                    WHERE r.depth < ? {type_clause}
                    UNION ALL
                    SELECT n.id, n.name, n.type, n.props_json, r.depth + 1
                    FROM edges e
                    JOIN related r ON e.to_id = r.id
                    JOIN nodes n ON n.id = e.from_id
                    WHERE r.depth < ? {type_clause}
                )
                SELECT DISTINCT id, name, type, props_json, depth
                FROM related
                WHERE id != ?
                ORDER BY depth, name
                LIMIT ?
            """
            # 参数顺序: entity_id, entity_id(入边), max_depth(出边), max_depth(入边), entity_id, limit
            # 如果有关系类型过滤，参数顺序会更复杂。简化处理：
            if relationship_types:
                # 带关系类型过滤的版本
                sql = f"""
                    WITH RECURSIVE related(id, name, type, props_json, depth) AS (
                        SELECT n.id, n.name, n.type, n.props_json, 1
                        FROM edges e
                        JOIN nodes n ON n.id = e.to_id
                        WHERE e.from_id = ? AND e.type IN ({placeholders})
                        UNION
                        SELECT n.id, n.name, n.type, n.props_json, 1
                        FROM edges e
                        JOIN nodes n ON n.id = e.from_id
                        WHERE e.to_id = ? AND e.type IN ({placeholders})
                        UNION ALL
                        SELECT n.id, n.name, n.type, n.props_json, r.depth + 1
                        FROM edges e
                        JOIN related r ON e.from_id = r.id
                        JOIN nodes n ON n.id = e.to_id
                        WHERE r.depth < ? AND e.type IN ({placeholders})
                        UNION ALL
                        SELECT n.id, n.name, n.type, n.props_json, r.depth + 1
                        FROM edges e
                        JOIN related r ON e.to_id = r.id
                        JOIN nodes n ON n.id = e.from_id
                        WHERE r.depth < ? AND e.type IN ({placeholders})
                    )
                    SELECT DISTINCT id, name, type, props_json, depth
                    FROM related
                    WHERE id != ?
                    ORDER BY depth, name
                    LIMIT ?
                """
                params = (
                    [entity_id] + list(relationship_types) +      # 基础出边
                    [entity_id] + list(relationship_types) +      # 基础入边
                    [max_depth] + list(relationship_types) +      # 递归出边
                    [max_depth] + list(relationship_types) +      # 递归入边
                    [entity_id, limit]
                )
            else:
                # 无类型过滤版本
                sql = """
                    WITH RECURSIVE related(id, name, type, props_json, depth) AS (
                        SELECT n.id, n.name, n.type, n.props_json, 1
                        FROM edges e JOIN nodes n ON n.id = e.to_id
                        WHERE e.from_id = ?
                        UNION
                        SELECT n.id, n.name, n.type, n.props_json, 1
                        FROM edges e JOIN nodes n ON n.id = e.from_id
                        WHERE e.to_id = ?
                        UNION ALL
                        SELECT n.id, n.name, n.type, n.props_json, r.depth + 1
                        FROM edges e
                        JOIN related r ON e.from_id = r.id
                        JOIN nodes n ON n.id = e.to_id
                        WHERE r.depth < ?
                        UNION ALL
                        SELECT n.id, n.name, n.type, n.props_json, r.depth + 1
                        FROM edges e
                        JOIN related r ON e.to_id = r.id
                        JOIN nodes n ON n.id = e.from_id
                        WHERE r.depth < ?
                    )
                    SELECT DISTINCT id, name, type, props_json, depth
                    FROM related
                    WHERE id != ?
                    ORDER BY depth, name
                    LIMIT ?
                """
                params = [entity_id, entity_id, max_depth, max_depth, entity_id, limit]

            rows = self._conn.execute(sql, params).fetchall()

            results = []
            for row in rows:
                d = self._row_to_dict(row)
                results.append({
                    "id": row["id"],
                    "name": row["name"],
                    "type": row["type"],
                    "distance": row["depth"],
                    **{k: v for k, v in d.items() if k not in ("id", "name", "type")},
                })

            logger.debug(f"找到 {len(results)} 个相关实体")
            return results

        except Exception as e:
            logger.error(f"查找相关实体失败: {e}")
            return []

    def search_entities_by_name(
        self,
        name_pattern: str,
        entity_types: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按名称模糊搜索实体。

        使用 SQL LIKE 进行模糊匹配。
        """
        try:
            if entity_types:
                placeholders = ",".join("?" for _ in entity_types)
                sql = f"""
                    SELECT * FROM nodes
                    WHERE name LIKE ? AND type IN ({placeholders})
                    ORDER BY name LIMIT ?
                """
                params = [f"%{name_pattern}%"] + list(entity_types) + [limit]
            else:
                sql = "SELECT * FROM nodes WHERE name LIKE ? ORDER BY name LIMIT ?"
                params = [f"%{name_pattern}%", limit]

            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_dict(row) for row in rows]

        except Exception as e:
            logger.error(f"按名称搜索实体失败: {e}")
            return []

    def get_entity_relationships(self, entity_id: str) -> List[Dict[str, Any]]:
        """获取实体的所有直接关系（出边和入边）。"""
        try:
            results = []

            # 出边: entity_id → other
            sql_out = """
                SELECT e.type, e.props_json, n.id, n.name, n.type as node_type, 'outgoing' as direction
                FROM edges e JOIN nodes n ON n.id = e.to_id
                WHERE e.from_id = ?
            """
            for row in self._conn.execute(sql_out, (entity_id,)):
                props = self._row_to_dict(row)
                results.append({
                    "relationship": {
                        "type": row["type"],
                        **json.loads(row["props_json"] or "{}"),
                    },
                    "other_entity": {
                        "id": row["id"],
                        "name": row["name"],
                        "type": row["node_type"],
                    },
                    "direction": "outgoing",
                })

            # 入边: other → entity_id
            sql_in = """
                SELECT e.type, e.props_json, n.id, n.name, n.type as node_type, 'incoming' as direction
                FROM edges e JOIN nodes n ON n.id = e.from_id
                WHERE e.to_id = ?
            """
            for row in self._conn.execute(sql_in, (entity_id,)):
                props = self._row_to_dict(row)
                results.append({
                    "relationship": {
                        "type": row["type"],
                        **json.loads(row["props_json"] or "{}"),
                    },
                    "other_entity": {
                        "id": row["id"],
                        "name": row["name"],
                        "type": row["node_type"],
                    },
                    "direction": "incoming",
                })

            return results

        except Exception as e:
            logger.error(f"获取实体关系失败: {e}")
            return []

    def delete_entity(self, entity_id: str) -> bool:
        """删除实体及其所有关联关系。

        先删边再删节点，实现 Neo4j DETACH DELETE 的效果。
        """
        try:
            self._conn.execute("DELETE FROM edges WHERE from_id = ? OR to_id = ?", (entity_id, entity_id))
            cursor = self._conn.execute("DELETE FROM nodes WHERE id = ?", (entity_id,))
            self._conn.commit()
            deleted = cursor.rowcount > 0
            logger.info(f"删除实体: {entity_id} (删除 {'成功' if deleted else '失败'})")
            return deleted
        except Exception as e:
            logger.error(f"删除实体失败: {e}")
            return False

    def clear_all(self) -> bool:
        """清空图数据库的所有节点和边。"""
        try:
            self._conn.execute("DELETE FROM edges")
            self._conn.execute("DELETE FROM nodes")
            self._conn.commit()
            logger.info("清空 SQLite 图数据库")
            return True
        except Exception as e:
            logger.error(f"清空数据库失败: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """获取图数据库统计信息。"""
        try:
            total_nodes = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            total_edges = self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            entity_nodes = self._conn.execute("SELECT COUNT(*) FROM nodes WHERE type != 'MEMORY'").fetchone()[0]
            memory_nodes = self._conn.execute("SELECT COUNT(*) FROM nodes WHERE type = 'MEMORY'").fetchone()[0]
            return {
                "total_nodes": total_nodes,
                "total_relationships": total_edges,
                "entity_nodes": entity_nodes,
                "memory_nodes": memory_nodes,
            }
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {}

    def health_check(self) -> bool:
        """健康检查：执行一条简单查询验证数据库可访问。"""
        try:
            self._conn.execute("SELECT 1")
            return True
        except Exception as e:
            logger.error(f"SQLite 图数据库健康检查失败: {e}")
            return False

    def close(self):
        """显式关闭数据库连接，释放文件锁。"""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.debug(f"SQLite 图数据库已关闭: {self._path}")

    def __del__(self):
        """析构函数，确保连接关闭。"""
        self.close()

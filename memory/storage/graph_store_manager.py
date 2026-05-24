"""图数据库管理器

统一的图数据库工厂和管理层，负责:
  - 根据配置自动选择或手动指定后端（Neo4j / SQLite / 未来扩展）
  - 单例管理，防止同一图数据库被重复创建连接
  - 对上层业务代码完全屏蔽底层差异

=== 后端选择逻辑 ===

  优先级（从高到低）:
    1. 显式传入 store_type 参数                  → 直接使用
    2. 环境变量 GRAPH_STORE_TYPE                 → 显式但全局
    3. 默认值                                   → sqlite（零依赖、零配置）

  注意: 与向量数据库一样，uri/path 等连接参数不影响后端选择。
        仅通过 store_type 参数或 GRAPH_STORE_TYPE 环境变量控制。

=== 单例管理 ===

  同一个 (后端, 连接参数, 数据库名) 组合只创建一次。

  唯一键规则:
    Neo4j:  "neo4j:{uri}:{database}"
    SQLite: "sqlite:{path}:{name}"

=== 使用示例 ===

  # 方式1: 全自动（默认 SQLite，零配置）
  store = GraphStoreManager.get_instance(name="semantic_graph")

  # 方式2: 通过环境变量切换
  # export GRAPH_STORE_TYPE=neo4j
  store = GraphStoreManager.get_instance(name="semantic_graph")

  # 方式3: 显式指定后端
  store = GraphStoreManager.get_instance("neo4j", uri="bolt://...", ...)
"""

import logging
import os
import threading
from typing import Dict, Optional

from .graph_store_base import GraphStoreBase

logger = logging.getLogger(__name__)


class GraphStoreManager:
    """图数据库统一管理器。

    与 VectorStoreManager 对称设计，提供完全一致的 API 风格。
    """

    _instances: Dict[str, "GraphStoreBase"] = {}
    _lock = threading.Lock()

    # ========================================================================
    # 后端自动探测
    # ========================================================================

    @classmethod
    def _resolve_store_type(cls, store_type: Optional[str] = None, **kwargs) -> str:
        """解析最终使用哪种后端。

        决策流程:
          1. store_type 参数有值 → 直接返回
          2. 环境变量 GRAPH_STORE_TYPE 有值 → 返回
          3. 默认返回 "sqlite"

        uri/path 等连接参数不影响后端选择。
        """
        if store_type:
            return store_type.lower()
        env_type = os.getenv("GRAPH_STORE_TYPE", "").lower()
        if env_type:
            return env_type
        return "sqlite"

    # ========================================================================
    # 单例 Key 构建
    # ========================================================================

    @classmethod
    def _build_instance_key(cls, store_type: str, name: str = "semantic_graph", **kwargs) -> str:
        """构建单例缓存 key。"""
        if store_type == "neo4j":
            uri = kwargs.get("uri") or "bolt://localhost:7687"
            return f"neo4j:{uri}:{kwargs.get('database', 'neo4j')}"
        elif store_type == "sqlite":
            path = (
                kwargs.get("path")
                or os.getenv("SQLITE_GRAPH_PATH")
                or os.path.join(os.getcwd(), "graph_data")
            )
            return f"sqlite:{path}:{name}"
        else:
            return f"{store_type}:{name}"

    # ========================================================================
    # 核心工厂方法
    # ========================================================================

    @classmethod
    def get_instance(
        cls,
        store_type: Optional[str] = None,
        name: str = "semantic_graph",
        **kwargs,
    ) -> GraphStoreBase:
        """获取或创建图数据库实例（单例模式，线程安全）。

        Args:
            store_type: 后端类型 ("neo4j", "sqlite")。None 则自动探测。
            name: 数据库名称。
            **kwargs: 后端特定参数。
                Neo4j:  uri, username, password, database
                SQLite: path

        Returns:
            GraphStoreBase 实例。
        """
        store_type = cls._resolve_store_type(store_type, **kwargs)
        key = cls._build_instance_key(store_type, name, **kwargs)

        if key not in cls._instances:
            with cls._lock:
                if key not in cls._instances:
                    logger.info(f"创建新的 {store_type} 图数据库: {name}")
                    cls._instances[key] = cls._create_instance(
                        store_type=store_type,
                        name=name,
                        **kwargs,
                    )
                else:
                    logger.debug(f"复用现有 {store_type} 图数据库: {name}")
        else:
            logger.debug(f"复用现有 {store_type} 图数据库: {name}")

        return cls._instances[key]

    @classmethod
    def _create_instance(cls, store_type: str, name: str, **kwargs) -> GraphStoreBase:
        """根据类型分派到对应的构造函数。"""
        if store_type == "neo4j":
            from .neo4j_store import Neo4jGraphStore
            return Neo4jGraphStore(name=name, **kwargs)

        elif store_type == "sqlite":
            from .sqlite_graph_store import SQLiteGraphStore
            path = kwargs.get("path") or os.getenv("SQLITE_GRAPH_PATH")
            return SQLiteGraphStore(path=path, name=name)

        else:
            raise ValueError(
                f"不支持的图数据库类型: {store_type}。"
                f"当前支持: neo4j, sqlite。"
            )

    # ========================================================================
    # 管理方法
    # ========================================================================

    @classmethod
    def list_instances(cls) -> Dict[str, str]:
        """列出所有已缓存的实例。"""
        return {key: store.store_type for key, store in cls._instances.items()}

    @classmethod
    def remove_instance(cls, store_type: Optional[str] = None, name: str = "semantic_graph", **kwargs):
        """移除并清理指定实例。"""
        store_type = cls._resolve_store_type(store_type, **kwargs)
        key = cls._build_instance_key(store_type, name, **kwargs)
        with cls._lock:
            if key in cls._instances:
                del cls._instances[key]
                logger.info(f"移除图数据库实例: {key}")

    @classmethod
    def clear_all(cls):
        """清除所有实例缓存。"""
        with cls._lock:
            count = len(cls._instances)
            cls._instances.clear()
            logger.info(f"清除所有图数据库实例（共 {count} 个）")

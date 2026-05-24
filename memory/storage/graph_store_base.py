"""图数据库存储基类

定义所有图数据库实现必须遵循的接口契约。
所有图存储实现（Neo4j、SQLite 等）必须继承此基类并实现全部抽象方法。

架构位置:
  memory/types/semantic.py
          │
          ▼
    GraphStoreBase          ← 抽象接口（本文件）
          │
    ┌─────┴─────┐
    ▼           ▼
  Neo4j       SQLite        ← 具体实现
  (远程)      (嵌入)
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any


class GraphStoreBase(ABC):
    """图数据库存储抽象基类

    定义了所有图数据库操作类必须实现的方法与属性。
    子类必须实现全部抽象方法。

    图数据库在记忆系统中的角色:
      语义记忆（SemanticMemory）从文本中抽取实体和关系，
      构建知识图谱。图数据库负责存储节点（实体）和边（关系），
      并提供图遍历能力以发现相关记忆。

    核心概念:
      - 节点（Node/Entity）: 实体，如人物、地点、技能、概念等
      - 边（Edge/Relationship）: 实体间的关系，如 HAS_MEMORY、MENTIONS、CO_OCCURS
      - 图遍历: 从某个实体出发，沿边 N 跳找到所有相关实体
    """

    @property
    @abstractmethod
    def store_type(self) -> str:
        """存储类型标识。如 "neo4j", "sqlite"。"""
        ...

    @abstractmethod
    def add_entity(
        self,
        entity_id: str,
        name: str,
        entity_type: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """添加实体节点。

        Args:
            entity_id: 实体唯一 ID。
            name: 实体名称（如 "Python", "张三"）。
            entity_type: 实体类型（如 "SKILL", "PERSON", "CONCEPT"）。
            properties: 附加属性字典（可包含 memory_id 等关联信息）。

        Returns:
            bool: 是否成功。
        """
        ...

    @abstractmethod
    def add_relationship(
        self,
        from_entity_id: str,
        to_entity_id: str,
        relationship_type: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """添加实体间关系（边）。

        Args:
            from_entity_id: 源实体 ID。
            to_entity_id: 目标实体 ID。
            relationship_type: 关系类型（如 "HAS_MEMORY", "CO_OCCURS", "INVOLVES"）。
            properties: 关系附加属性。

        Returns:
            bool: 是否成功。
        """
        ...

    @abstractmethod
    def find_related_entities(
        self,
        entity_id: str,
        relationship_types: Optional[List[str]] = None,
        max_depth: int = 2,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """从指定实体出发，沿边 N 跳找到所有相关实体（图遍历）。

        这是图数据库最核心的查询能力。从起始节点沿边逐层扩展，
        收集沿途遇到的所有节点。

        Args:
            entity_id: 起始实体 ID。
            relationship_types: 关系类型过滤，None 表示不过滤。
            max_depth: 最大搜索深度（跳数），默认 2。
            limit: 返回结果数量上限。

        Returns:
            List[Dict]: 相关实体列表，每项包含 id, name, type, distance 等字段。
        """
        ...

    @abstractmethod
    def search_entities_by_name(
        self,
        name_pattern: str,
        entity_types: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按名称模糊搜索实体。

        当查询文本本身提取不到实体时（如用户问 "python"），
        直接用 name_pattern 在已存储的实体中搜索匹配的节点。

        Args:
            name_pattern: 名称搜索模式。
            entity_types: 实体类型过滤，None 表示不过滤。
            limit: 返回结果数量上限。

        Returns:
            List[Dict]: 匹配的实体列表。
        """
        ...

    @abstractmethod
    def get_entity_relationships(
        self,
        entity_id: str,
    ) -> List[Dict[str, Any]]:
        """获取指定实体的所有直接关系。

        返回与该实体相连的所有边及对端节点信息。

        Args:
            entity_id: 实体 ID。

        Returns:
            List[Dict]: 关系列表，每项包含 relationship, other_entity, direction 字段。
        """
        ...

    @abstractmethod
    def delete_entity(self, entity_id: str) -> bool:
        """删除实体及其所有关联关系。

        Args:
            entity_id: 要删除的实体 ID。

        Returns:
            bool: 是否成功。
        """
        ...

    @abstractmethod
    def clear_all(self) -> bool:
        """清空图数据库的所有节点和边。

        Returns:
            bool: 是否成功。
        """
        ...

    @abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """获取图数据库统计信息。

        Returns:
            Dict: 包含 total_nodes, total_relationships 等统计字段。
        """
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """健康检查。

        Returns:
            bool: 数据库是否正常可访问。
        """
        ...

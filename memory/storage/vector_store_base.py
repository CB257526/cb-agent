"""向量数据库存储基类

定义所有向量数据库实现必须遵循的接口契约。
所有向量存储实现（Qdrant、Zvec 等）必须继承此基类并实现全部抽象方法。

设计理念:
  上层业务代码（episodic.py、semantic.py 等）只依赖此基类的接口，
  不关心底层具体是哪个向量数据库。这样可以随时切换后端而无需修改业务代码。

  类比: 这个基类就像 Java 的 interface 或 C++ 的纯虚类，
        QdrantVectorStore 和 ZvecVectorStore 是它的两个具体实现。
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class VectorStoreBase(ABC):
    """向量数据库存储抽象基类

    定义了所有向量数据库操作类必须实现的方法与属性。
    子类必须实现全部抽象方法，否则实例化时将抛出 TypeError。

    架构位置:
      memory/types/episodic.py ─┐
      memory/types/semantic.py  ─┤  调用 VectorStoreBase 接口
      memory/types/perceptual.py─┘       │
                                          │ 继承实现
                    ┌─────────────────────┼─────────────────────┐
                    │                     │                     │
            QdrantVectorStore      ZvecVectorStore       (未来: Milvus等)
            (客户端-服务器)         (进程内嵌入)

    Attributes:
        collection_name: 集合/表名称，只读属性
        vector_size: 向量维度，只读属性
        store_type: 存储类型标识（如 "qdrant", "zvec"），只读属性，用于日志和统计
    """

    # ========== 必须实现的三个只读属性 ==========

    @property
    @abstractmethod
    def collection_name(self) -> str:
        """集合名称

        子类必须在 __init__ 中将此值存入实例变量（如 self._collection_name），
        然后在此属性中返回。
        """
        ...

    @property
    @abstractmethod
    def vector_size(self) -> int:
        """向量维度

        所有存入此集合的向量必须与此维度一致。搜索时也会校验查询向量的维度。
        """
        ...

    @property
    @abstractmethod
    def store_type(self) -> str:
        """存储类型标识

        返回值示例: "qdrant", "zvec", "milvus" 等。
        用于日志输出、统计信息和 VectorStoreManager 的管理。
        """
        ...

    # ========== 必须实现的九个抽象方法 ==========

    @abstractmethod
    def add_vectors(
        self,
        vectors: List[List[float]],
        metadata: List[Dict[str, Any]],
        ids: Optional[List[str]] = None,
    ) -> bool:
        """添加向量到存储

        这是最核心的写入方法。业务代码调用此方法将编码后的向量和元数据存入数据库。

        Args:
            vectors: 向量列表，每个向量是 float 列表。长度必须与 metadata 一致。
                     例如: [[0.1, 0.2, ...], [0.3, 0.4, ...]]
            metadata: 元数据列表，与 vectors 一一对应。
                      每个元素是一个字典，至少包含 memory_id, user_id, memory_type。
            ids: 可选的文档主键 ID 列表，未提供则由实现自动生成。

        Returns:
            bool: 全部成功返回 True，否则返回 False。

        注意:
          实现应使用 upsert（insert or update）语义，即 ID 已存在时覆盖而非报错。
        """
        ...

    @abstractmethod
    def search_similar(
        self,
        query_vector: List[float],
        limit: int = 10,
        score_threshold: Optional[float] = None,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """搜索相似向量

        这是最核心的读取方法。给定一个查询向量，返回最相似的文档列表。

        Args:
            query_vector: 查询向量，维度必须与 vector_size 一致。
            limit: 返回结果数量上限，默认 10。
            score_threshold: 相似度阈值（可选）。低于此分数的结果会被过滤。
                            注意: 各后端的分数语义可能不同（余弦距离、内积等）。
            where: 过滤条件字典。
                   格式: {"字段名": 值} 或 {"字段名": [值1, 值2]}。
                   例如: {"memory_type": "episodic", "user_id": "abc"}
                   子类负责将此字典转换为各自后端的过滤语法。

        Returns:
            List[Dict]: 搜索结果列表，每项包含:
                - id: 文档主键
                - score: 相似度分数（越高越相似）
                - metadata: 原始元数据字典
        """
        ...

    @abstractmethod
    def delete_vectors(self, ids: List[str]) -> bool:
        """按主键 ID 删除向量

        直接按数据库内部的主键 ID 删除文档。

        Args:
            ids: 要删除的向量主键 ID 列表。

        Returns:
            bool: 是否成功。
        """
        ...

    @abstractmethod
    def delete_by_filter(self, where: Dict[str, Any]) -> bool:
        """按条件过滤删除向量

        根据元数据字段的条件匹配删除文档。
        与 delete_vectors 的区别：这个按元数据内容匹配，那个按主键 ID 精确删除。

        Args:
            where: 过滤条件字典，格式与 search_similar 中的 where 相同。
                   例如: {"memory_type": "episodic"} 删除所有情景记忆。
                   例如: {"memory_id": ["id1", "id2"]} 批量删除指定记忆。

        Returns:
            bool: 是否成功。
        """
        ...

    @abstractmethod
    def clear_collection(self) -> bool:
        """清空集合（删除所有数据后重建）

        用于 reset 操作，清空整个集合的所有数据。

        Returns:
            bool: 是否成功。
        """
        ...

    @abstractmethod
    def get_collection_info(self) -> Dict[str, Any]:
        """获取集合基本信息

        Returns:
            Dict: 包含以下字段:
                - name: 集合名称
                - vectors_count: 向量总数
                - points_count: 文档总数
                - config: 配置信息（vector_size, distance 等）
        """
        ...

    @abstractmethod
    def get_collection_stats(self) -> Dict[str, Any]:
        """获取集合统计信息（兼容抽象接口）

        比 get_collection_info 多一个 store_type 字段，
        用于 MemoryManager.get_memory_stats() 的汇总展示。

        Returns:
            Dict: 包含 store_type 在内的统计信息。
        """
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """健康检查

        用于验证数据库连接是否正常。在 SemanticMemory 初始化时会调用此方法。

        Returns:
            bool: 服务正常返回 True，否则返回 False。
        """
        ...

    # ========== 具体方法（子类可选择性覆盖） ==========

    def delete_memories(self, memory_ids: List[str]) -> None:
        """按 memory_id 批量删除记忆

        这是业务层面的便捷方法。默认实现通过 delete_by_filter 完成。
        子类可以覆盖以提供更高效的实现（例如直接使用后端原生批量过滤删除）。

        Args:
            memory_ids: 记忆 ID 列表。这些是业务层的 memory_id，
                       不是数据库内部的主键 ID。
        """
        if not memory_ids:
            return
        self.delete_by_filter({"memory_id": memory_ids})

"""向量数据库管理器

统一的向量存储工厂和管理层，负责:
  - 根据配置自动选择或手动指定后端（Qdrant / Zvec / 未来扩展）
  - 单例管理，防止同一 collection 被重复创建连接
  - 对上层业务代码完全屏蔽底层差异

=== 设计理念 ===

  上层业务代码（episodic.py, semantic.py, perceptual.py）只需要:
    from ..storage import VectorStoreManager
    store = VectorStoreManager.get_instance(...)
    store.add_vectors(...)     # 这些方法在 VectorStoreBase 中定义
    store.search_similar(...)

  至于底层是 Qdrant 还是 Zvec，由 VectorStoreManager 根据配置决定。
  上层完全感知不到差异。

=== 后端选择逻辑 ===

  优先级（从高到低）:
    1. 显式传入 store_type 参数         →  直接使用
    2. 环境变量 VECTOR_STORE_TYPE       →  显式但全局
    3. 根据 kwargs 自动推断             →  有 url/api_key → qdrant, 有 path → zvec
    4. 默认值                          →  zvec（零依赖、零配置）

=== 单例管理 ===

  同一个 (后端, collection) 组合只创建一次。
  例如: 三处代码都调用 VectorStoreManager.get_instance(collection_name="memories")
        → 第一次创建 ZvecVectorStore 实例
        → 后续两次直接返回已有实例
        → 避免了重复打开文件/连接

  唯一键规则:
    Qdrant: "qdrant:{url}:{collection_name}"
    Zvec:   "zvec:{path}:{collection_name}"

=== 使用示例 ===

  # 方式1: 全自动（推荐）—— 默认 Zvec，零配置
  store = VectorStoreManager.get_instance(
      collection_name="memories",
      vector_size=384,
  )

  # 方式2: 通过环境变量切换为 Qdrant
  # export VECTOR_STORE_TYPE=qdrant
  store = VectorStoreManager.get_instance(
      url="http://localhost:6333",
      collection_name="memories",
  )

  # 方式3: 显式指定后端
  store = VectorStoreManager.get_instance(
      "qdrant",
      url="http://localhost:6333",
      collection_name="memories",
  )
"""

import logging
import os
import threading
from typing import Dict, Optional

from .vector_store_base import VectorStoreBase

logger = logging.getLogger(__name__)


class VectorStoreManager:
    """向量数据库统一管理器。

    这是一个静态类（所有方法都是 classmethod），不需要实例化。
    直接通过类调用: VectorStoreManager.get_instance(...)

    核心职责:
      1. 后端选择:    根据配置决定用 Qdrant 还是 Zvec（或其他）。
      2. 实例创建:    调用对应后端的构造函数。
      3. 单例维护:    缓存已创建的实例，按需返回。
      4. 生命周期:    提供移除实例、列出实例等管理方法。
    """

    # ========== 类级别状态 ==========

    # 实例缓存: key → VectorStoreBase 实例
    # key 格式: "qdrant:{url}:{collection_name}" 或 "zvec:{path}:{collection_name}"
    _instances: Dict[str, "VectorStoreBase"] = {}

    # 线程锁: 保证单例创建时的线程安全
    _lock = threading.Lock()

    # ========================================================================
    # 后端自动探测
    # ========================================================================

    @classmethod
    def _resolve_store_type(cls, store_type: Optional[str] = None, **kwargs) -> str:
        """解析最终使用哪种后端。

        === 决策流程 ===

        调用 get_instance(store_type=None, ...)
                │
                ▼
        1. store_type 参数有值?  ──是──▶ 直接返回（转小写）
                │ 否
                ▼
        2. 环境变量 VECTOR_STORE_TYPE 有值? ──是──▶ 返回（转小写）
                │ 否
                ▼
        3. 默认返回 "zvec"（零依赖、纯本地）

        === 设计说明 ===

        url/api_key/path 等参数只是后端连接的配置信息，不作为后端选择的依据。
        例如 .env 中设置了 QDRANT_URL，但 VECTOR_STORE_TYPE 未设置或不等于 "qdrant"，
        系统仍然使用 Zvec。这避免了"设了环境变量就被动切换后端"的意外行为。

        显式切换 Qdrant 的两种方式:
          方式A: store_type="qdrant" 显式传参
          方式B: 设置环境变量 VECTOR_STORE_TYPE=qdrant
        """
        # 优先级 1: 显式参数
        if store_type:
            return store_type.lower()

        # 优先级 2: 环境变量
        env_type = os.getenv("VECTOR_STORE_TYPE", "").lower()
        if env_type:
            return env_type

        # 优先级 3: 默认 Zvec（零配置、纯本地）
        return "zvec"

    # ========================================================================
    # 单例 Key 构建
    # ========================================================================

    @classmethod
    def _build_instance_key(
        cls, store_type: str, collection_name: str = "hello_agents_vectors", **kwargs
    ) -> str:
        """构建单例缓存 key。

        每个唯一的 (后端, 连接参数, collection) 组合生成一个唯一 key。
        这样不同的 collection 之间不会互相干扰，
        但同一个 collection 的多次请求会复用同一实例。

        Key 格式:
          Qdrant: "qdrant:{url}:{collection_name}"
                  例如: "qdrant:http://localhost:6333:memories"
                  如果 url 为空: "qdrant:local:memories"

          Zvec:   "zvec:{path}:{collection_name}"
                  例如: "zvec:./zvec_data:memories"

        Args:
            store_type: 后端类型。
            collection_name: 集合名称。
            **kwargs: 后端特定参数。

        Returns:
            单例缓存 key 字符串。
        """
        if store_type == "qdrant":
            # Qdrant: 以 (url, collection_name) 区分
            url = kwargs.get("url") or "local"
            return f"qdrant:{url}:{collection_name}"
        elif store_type == "zvec":
            # Zvec: 以 (path, collection_name) 区分
            path = kwargs.get("path") or os.path.join(os.getcwd(), "zvec_data")
            return f"zvec:{path}:{collection_name}"
        else:
            # 未来扩展: 其他后端
            return f"{store_type}:{collection_name}"

    # ========================================================================
    # 核心工厂方法 — 获取实例
    # ========================================================================

    @classmethod
    def get_instance(
        cls,
        store_type: Optional[str] = None,
        collection_name: str = "hello_agents_vectors",
        vector_size: int = 384,
        distance: str = "cosine",
        **kwargs,
    ) -> VectorStoreBase:
        """获取或创建向量存储实例（单例模式，线程安全）。

        这是整个管理器最核心的入口方法。所有上层代码都通过它获取实例。

        === 调用流程 ===

        1. 解析 store_type（显式 > 环境变量 > 推断 > 默认）
        2. 构建单例 key
        3. 双重检查锁定: key 不存在 → 创建新实例 → 缓存 → 返回
           key 已存在 → 直接返回缓存的实例

        === 线程安全 ===

        使用双重检查锁定（Double-Checked Locking）:
          - 第一次检查不加锁（快速路径，命中缓存的常见情况）
          - 第二次检查加锁（慢速路径，创建实例的罕见情况）
        这样在绝大多数情况下（缓存命中）不需要加锁。

        Args:
            store_type: 后端类型 ("qdrant", "zvec")。
                        None 则自动探测。
            collection_name: 集合名称。
                             不同记忆类型通常使用不同的 collection。
            vector_size: 向量维度。必须与嵌入模型输出一致。
            distance: 距离度量。cosine / dot / euclidean。
            **kwargs: 后端特定参数。
                Qdrant 专用: url, api_key, timeout
                Zvec 专用:   path

        Returns:
            VectorStoreBase 实例（实际类型取决于 store_type）。

        Raises:
            ValueError: store_type 不在支持列表中。
            ImportError: 对应后端的包未安装。
            ConnectionError: 无法连接到后端服务（仅 Qdrant）。
        """
        # 步骤1: 解析后端类型
        store_type = cls._resolve_store_type(store_type, **kwargs)

        # 步骤2: 构建单例 key
        key = cls._build_instance_key(store_type, collection_name, **kwargs)

        # 步骤3: 双重检查锁定
        if key not in cls._instances:
            with cls._lock:
                # 加锁后再次检查（可能其他线程已经创建了）
                if key not in cls._instances:
                    logger.info(
                        f"创建新的 {store_type} 向量存储实例: "
                        f"collection={collection_name}, vector_size={vector_size}"
                    )
                    cls._instances[key] = cls._create_instance(
                        store_type=store_type,
                        collection_name=collection_name,
                        vector_size=vector_size,
                        distance=distance,
                        **kwargs
                    )
                else:
                    logger.debug(f"复用现有 {store_type} 向量存储: {collection_name}")
        else:
            logger.debug(f"复用现有 {store_type} 向量存储: {collection_name}")

        return cls._instances[key]

    # ========================================================================
    # 实例创建 — 根据类型分派到具体构造函数
    # ========================================================================

    @classmethod
    def _create_instance(
        cls,
        store_type: str,
        collection_name: str,
        vector_size: int,
        distance: str,
        **kwargs,
    ) -> VectorStoreBase:
        """根据 store_type 分派到对应的构造函数。

        如果要新增后端支持，只需在这里添加一个 elif 分支即可。

        Args:
            store_type: 后端类型标识。
            collection_name: 集合名称。
            vector_size: 向量维度。
            distance: 距离度量。
            **kwargs: 后端特定参数。

        Returns:
            构造好的 VectorStoreBase 实例。
        """
        if store_type == "qdrant":
            # ---- Qdrant 后端 ----
            from .qdrant_store import QdrantVectorStore

            url = kwargs.get("url")
            api_key = kwargs.get("api_key")
            timeout = int(kwargs.get("timeout", 30))

            return QdrantVectorStore(
                url=url,
                api_key=api_key,
                collection_name=collection_name,
                vector_size=vector_size,
                distance=distance,
                timeout=timeout,
            )

        elif store_type == "zvec":
            # ---- Zvec 后端 ----
            from .zvec_store import ZvecVectorStore

            path = kwargs.get("path")

            return ZvecVectorStore(
                path=path,
                collection_name=collection_name,
                vector_size=vector_size,
                distance=distance,
            )

        else:
            # ---- 未知后端 ----
            raise ValueError(
                f"不支持的向量存储类型: {store_type}。"
                f"当前支持: qdrant, zvec。"
                f"如需添加新后端，请在 VectorStoreManager._create_instance 中增加分支。"
            )

    # ========================================================================
    # 管理方法 — 实例的查询、移除、清理
    # ========================================================================

    @classmethod
    def list_instances(cls) -> Dict[str, str]:
        """列出所有已缓存的实例。

        调试用。显示当前有哪些实例、各是什么类型。

        Returns:
            Dict[key, store_type] 映射。
        """
        result = {}
        for key, store in cls._instances.items():
            result[key] = store.store_type
        return result

    @classmethod
    def remove_instance(
        cls,
        store_type: Optional[str] = None,
        collection_name: str = "hello_agents_vectors",
        **kwargs
    ):
        """移除并清理指定实例。

        用于需要强制重建连接或释放资源的场景。

        Args:
            store_type: 后端类型。
            collection_name: 集合名称。
            **kwargs: 用于定位实例的额外参数。
        """
        store_type = cls._resolve_store_type(store_type, **kwargs)
        key = cls._build_instance_key(store_type, collection_name, **kwargs)
        with cls._lock:
            if key in cls._instances:
                del cls._instances[key]
                logger.info(f"移除向量存储实例: {key}")

    @classmethod
    def clear_all(cls):
        """清除所有实例缓存。

        慎用！会断开所有活动的向量数据库连接。

        主要用于:
          - 测试清理
          - 进程关闭前的资源释放
        """
        with cls._lock:
            count = len(cls._instances)
            cls._instances.clear()
            logger.info(f"清除所有向量存储实例（共 {count} 个）")

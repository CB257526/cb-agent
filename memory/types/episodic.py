"""情景记忆实现

按照第8章架构设计的情景记忆，提供：
- 具体交互事件存储
- 时间序列组织
- 上下文丰富的记忆
- 模式识别能力
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
import os
import math
import json
import logging

logger = logging.getLogger(__name__)

from ..base import BaseMemory, MemoryItem, MemoryConfig
from ..storage import SQLiteDocumentStore, QdrantVectorStore
from ..embedding import get_dimension
from memory.embedding import get_text_embedder_model

class Episode:
    """情景记忆中的单个情景"""
    
    def __init__(
        self,
        episode_id: str,
        user_id: str,
        session_id: str,
        timestamp: datetime,
        content: str,
        context: Dict[str, Any],
        outcome: Optional[str] = None,
        importance: float = 0.5
    ):
        self.episode_id = episode_id
        self.user_id = user_id
        self.session_id = session_id
        self.timestamp = timestamp
        self.content = content
        self.context = context
        self.outcome = outcome
        self.importance = importance

class EpisodicMemory(BaseMemory):
    """情景记忆实现
    
    特点：
    - 存储具体的交互事件
    - 包含丰富的上下文信息
    - 按时间序列组织
    - 支持模式识别和回溯
    """
    
    def __init__(self, config: MemoryConfig, storage_backend=None):
        super().__init__(config, storage_backend)
        
        # 本地缓存（内存）
        self.episodes: List[Episode] = []
        self.sessions: Dict[str, List[str]] = {}  # session_id -> episode_ids
        
        # 模式识别缓存
        self.patterns_cache = {}
        self.last_pattern_analysis = None

        # 权威文档存储（SQLite）
        db_dir = self.config.storage_path if hasattr(self.config, 'storage_path') else "./memory_data"
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "memory.db")
        self.doc_store = SQLiteDocumentStore(db_path=db_path)

        # 统一嵌入模型（多语言，默认384维）
        self.embedder = get_text_embedder_model()

        # 向量存储（Qdrant - 使用连接管理器避免重复连接）
        from ..storage.qdrant_store import QdrantConnectionManager
        qdrant_url = os.getenv("QDRANT_URL")
        qdrant_api_key = os.getenv("QDRANT_API_KEY")
        self.vector_store = QdrantConnectionManager.get_instance(
            url=qdrant_url,
            api_key=qdrant_api_key,
            collection_name=os.getenv("QDRANT_COLLECTION", "hello_agents_vectors"),
            vector_size=get_dimension(getattr(self.embedder, 'dimension', 384)),
            distance=os.getenv("QDRANT_DISTANCE", "cosine")
        )
    
    def add(self, memory_item: MemoryItem) -> str:
        """添加情景记忆"""
        # 从元数据中提取情景信息
        session_id = memory_item.metadata.get("session_id", "default_session")
        context = memory_item.metadata.get("context", {})
        outcome = memory_item.metadata.get("outcome")
        participants = memory_item.metadata.get("participants", [])
        tags = memory_item.metadata.get("tags", [])
        
        # 创建情景（内存缓存）
        episode = Episode(
            episode_id=memory_item.id,
            user_id=memory_item.user_id,
            session_id=session_id,
            timestamp=memory_item.timestamp,
            content=memory_item.content,
            context=context,
            outcome=outcome,
            importance=memory_item.importance
        )
        self.episodes.append(episode)
        if session_id not in self.sessions:
            self.sessions[session_id] = []
        self.sessions[session_id].append(episode.episode_id)

        # 1) 权威存储（SQLite）
        ts_int = int(memory_item.timestamp.timestamp())
        self.doc_store.add_memory(
            memory_id=memory_item.id,
            user_id=memory_item.user_id,
            content=memory_item.content,
            memory_type="episodic",
            timestamp=ts_int,
            importance=memory_item.importance,
            properties={
                "session_id": session_id,
                "context": context,
                "outcome": outcome,
                "participants": participants,
                "tags": tags
            }
        )

        # 2) 向量索引（Qdrant）
        try:
            embedding = self.embedder.encode(memory_item.content)
            if hasattr(embedding, "tolist"):
                embedding = embedding.tolist()
            self.vector_store.add_vectors(
                vectors=[embedding],
                metadata=[{
                    "memory_id": memory_item.id,
                    "user_id": memory_item.user_id,
                    "memory_type": "episodic",
                    "importance": memory_item.importance,
                    "session_id": session_id,
                    "content": memory_item.content
                }],
                ids=[memory_item.id]
            )
        except Exception:
            # 向量入库失败不影响权威存储
            pass

        return memory_item.id
    
    def retrieve(self, query: str, limit: int = 5, **kwargs) -> List[MemoryItem]:
        """检索情景记忆（结构化过滤 + 语义向量检索）"""
        user_id = kwargs.get("user_id")
        session_id = kwargs.get("session_id")
        time_range: Optional[Tuple[datetime, datetime]] = kwargs.get("time_range")
        importance_threshold: Optional[float] = kwargs.get("importance_threshold")

        # 结构化过滤候选（来自权威库）
        candidate_ids: Optional[set] = None
        if time_range is not None or importance_threshold is not None:
            start_ts = int(time_range[0].timestamp()) if time_range else None
            end_ts = int(time_range[1].timestamp()) if time_range else None
            docs = self.doc_store.search_memories(
                user_id=user_id,
                memory_type="episodic",
                start_time=start_ts,
                end_time=end_ts,
                importance_threshold=importance_threshold,
                limit=1000
            )
            candidate_ids = {d["memory_id"] for d in docs}

        # 向量检索（Qdrant）
        try:
            query_vec = self.embedder.encode(query)
            if hasattr(query_vec, "tolist"):
                query_vec = query_vec.tolist()
            where = {"memory_type": "episodic"}
            if user_id:
                where["user_id"] = user_id
            hits = self.vector_store.search_similar(
                query_vector=query_vec,
                limit=max(limit * 5, 20),
                where=where
            )
        except Exception:
            hits = []

        # 过滤与重排
        now_ts = int(datetime.now().timestamp())
        results: List[Tuple[float, MemoryItem]] = []
        seen = set()
        for hit in hits:
            meta = hit.get("metadata", {})
            mem_id = meta.get("memory_id")
            if not mem_id or mem_id in seen:
                continue
            
            # 检查是否已遗忘
            episode = next((e for e in self.episodes if e.episode_id == mem_id), None)
            if episode and episode.context.get("forgotten", False):
                continue  # 跳过已遗忘的记忆
                
            if candidate_ids is not None and mem_id not in candidate_ids:
                continue
            if session_id and meta.get("session_id") != session_id:
                continue

            # 从权威库读取完整记录
            doc = self.doc_store.get_memory(mem_id)
            if not doc:
                continue

            # 计算综合分数：向量0.6 + 近因0.2 + 重要性0.2
            vec_score = float(hit.get("score", 0.0))
            age_days = max(0.0, (now_ts - int(doc["timestamp"])) / 86400.0)
            recency_score = 1.0 / (1.0 + age_days)
            imp = float(doc.get("importance", 0.5))
            
            # 新评分算法：向量检索纯基于相似度，重要性作为加权因子
            # 基础相似度得分（不受重要性影响）
            base_relevance = vec_score * 0.8 + recency_score * 0.2
            
            # 重要性作为乘法加权因子，范围 [0.8, 1.2]
            importance_weight = 0.8 + (imp * 0.4)
            
            # 最终得分：相似度 * 重要性权重
            combined = base_relevance * importance_weight

            item = MemoryItem(
                id=doc["memory_id"],
                content=doc["content"],
                memory_type=doc["memory_type"],
                user_id=doc["user_id"],
                timestamp=datetime.fromtimestamp(doc["timestamp"]),
                importance=doc.get("importance", 0.5),
                metadata={
                    **doc.get("properties", {}),
                    "relevance_score": combined,
                    "vector_score": vec_score,
                    "recency_score": recency_score
                }
            )
            results.append((combined, item))
            seen.add(mem_id)

        # 若向量检索无结果，回退到简单关键词匹配（内存缓存）
        if not results:
            fallback = super()._generate_id  # 占位以避免未使用警告
            query_lower = query.lower()
            for ep in self._filter_episodes(user_id, session_id, time_range):
                if query_lower in ep.content.lower():
                    recency_score = 1.0 / (1.0 + max(0.0, (now_ts - int(ep.timestamp.timestamp())) / 86400.0))
                    # 回退匹配：新评分算法
                    keyword_score = 0.5  # 简单关键词匹配的基础分数
                    base_relevance = keyword_score * 0.8 + recency_score * 0.2
                    importance_weight = 0.8 + (ep.importance * 0.4)
                    combined = base_relevance * importance_weight
                    item = MemoryItem(
                        id=ep.episode_id,
                        content=ep.content,
                        memory_type="episodic",
                        user_id=ep.user_id,
                        timestamp=ep.timestamp,
                        importance=ep.importance,
                        metadata={
                            "session_id": ep.session_id,
                            "context": ep.context,
                            "outcome": ep.outcome,
                            "relevance_score": combined
                        }
                    )
                    results.append((combined, item))

        results.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in results[:limit]]
    
    def update(
        self,
        memory_id: str,
        content: str = None,
        importance: float = None,
        metadata: Dict[str, Any] = None
    ) -> bool:
        """更新情景记忆（SQLite为权威，Qdrant按需重嵌入）"""
        updated = False
        for episode in self.episodes:
            if episode.episode_id == memory_id:
                if content is not None:
                    episode.content = content
                if importance is not None:
                    episode.importance = importance
                if metadata is not None:
                    episode.context.update(metadata.get("context", {}))
                    if "outcome" in metadata:
                        episode.outcome = metadata["outcome"]
                updated = True
                break

        # 更新SQLite
        doc_updated = self.doc_store.update_memory(
            memory_id=memory_id,
            content=content,
            importance=importance,
            properties=metadata
        )

        # 如内容变更，重嵌入并upsert到Qdrant
        if content is not None:
            try:
                embedding = self.embedder.encode(content)
                if hasattr(embedding, "tolist"):
                    embedding = embedding.tolist()
                # 获取更新后的记录以同步payload
                doc = self.doc_store.get_memory(memory_id)
                payload = {
                    "memory_id": memory_id,
                    "user_id": doc["user_id"] if doc else "",
                    "memory_type": "episodic",
                    "importance": (doc.get("importance") if doc else importance) or 0.5,
                    "session_id": (doc.get("properties", {}) or {}).get("session_id"),
                    "content": content
                }
                self.vector_store.add_vectors(
                    vectors=[embedding],
                    metadata=[payload],
                    ids=[memory_id]
                )
            except Exception:
                pass

        return updated or doc_updated
    
    def remove(self, memory_id: str) -> bool:
        """删除情景记忆（SQLite + Qdrant）"""
        removed = False
        for i, episode in enumerate(self.episodes):
            if episode.episode_id == memory_id:
                removed_episode = self.episodes.pop(i)
                session_id = removed_episode.session_id
                if session_id in self.sessions:
                    self.sessions[session_id].remove(memory_id)
                    if not self.sessions[session_id]:
                        del self.sessions[session_id]
                removed = True
                break

        # 权威库删除
        doc_deleted = self.doc_store.delete_memory(memory_id)
        
        # 向量库删除
        try:
            self.vector_store.delete_memories([memory_id])
        except Exception:
            pass
        
        return removed or doc_deleted
    
    def has_memory(self, memory_id: str) -> bool:
        """检查记忆是否存在"""
        return any(episode.episode_id == memory_id for episode in self.episodes)
    
    def clear(self):
        """清空所有情景记忆（仅清理episodic，不影响其他类型）"""
        # 内存缓存
        self.episodes.clear()
        self.sessions.clear()
        self.patterns_cache.clear()

        # SQLite内的episodic全部删除
        docs = self.doc_store.search_memories(memory_type="episodic", limit=10000)
        ids = [d["memory_id"] for d in docs]
        for mid in ids:
            self.doc_store.delete_memory(mid)

        # Qdrant按ID删除对应向量
        try:
            if ids:
                self.vector_store.delete_memories(ids)
        except Exception:
            pass

    def forget(self, strategy: str = "importance_based", threshold: float = 0.1, max_age_days: int = 30) -> int:
        """情景记忆遗忘机制（硬删除）"""
        forgotten_count = 0
        current_time = datetime.now()
        
        to_remove = []  # 收集要删除的记忆ID
        
        for episode in self.episodes:
            should_forget = False
            
            if strategy == "importance_based":
                # 基于重要性遗忘
                if episode.importance < threshold:
                    should_forget = True
            elif strategy == "time_based":
                # 基于时间遗忘
                cutoff_time = current_time - timedelta(days=max_age_days)
                if episode.timestamp < cutoff_time:
                    should_forget = True
            elif strategy == "capacity_based":
                # 基于容量遗忘（保留最重要的）
                if len(self.episodes) > self.config.max_capacity:
                    sorted_episodes = sorted(self.episodes, key=lambda e: e.importance)
                    excess_count = len(self.episodes) - self.config.max_capacity
                    if episode in sorted_episodes[:excess_count]:
                        should_forget = True
            
            if should_forget:
                to_remove.append(episode.episode_id)
        
        # 执行硬删除
        for episode_id in to_remove:
            if self.remove(episode_id):
                forgotten_count += 1
                logger.info(f"情景记忆硬删除: {episode_id[:8]}... (策略: {strategy})")
        
        return forgotten_count

    def get_all(self) -> List[MemoryItem]:
        """获取所有情景记忆（转换为MemoryItem格式）"""
        memory_items = []
        for episode in self.episodes:
            memory_item = MemoryItem(
                id=episode.episode_id,
                content=episode.content,
                memory_type="episodic",
                user_id=episode.user_id,
                timestamp=episode.timestamp,
                importance=episode.importance,
                metadata=episode.metadata
            )
            memory_items.append(memory_item)
        return memory_items
    
    def get_stats(self) -> Dict[str, Any]:
        """获取情景记忆统计信息（合并SQLite与Qdrant）"""
        # 硬删除模式：所有episodes都是活跃的
        active_episodes = self.episodes
        
        db_stats = self.doc_store.get_database_stats()
        try:
            vs_stats = self.vector_store.get_collection_stats()
        except Exception:
            vs_stats = {"store_type": "qdrant"}
        return {
            "count": len(active_episodes),  # 活跃记忆数量
            "forgotten_count": 0,  # 硬删除模式下已遗忘的记忆会被直接删除
            "total_count": len(self.episodes),  # 总记忆数量
            "sessions_count": len(self.sessions),
            "avg_importance": sum(e.importance for e in active_episodes) / len(active_episodes) if active_episodes else 0.0,
            "time_span_days": self._calculate_time_span(),
            "memory_type": "episodic",
            "vector_store": vs_stats,
            "document_store": {k: v for k, v in db_stats.items() if k.endswith("_count") or k in ["store_type", "db_path"]}
        }
    
    def get_session_episodes(self, session_id: str) -> List[Episode]:
        """获取指定会话的所有情景"""
        if session_id not in self.sessions:
            return []
        
        episode_ids = self.sessions[session_id]
        return [e for e in self.episodes if e.episode_id in episode_ids]
    
    def find_patterns(
        self,
        user_id: str = None,
        recent_n: int = 100,
        batch_size: int = 20,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """发现用户行为模式（LLM 驱动：先分片标注，再聚合总结）。

        Args:
            user_id: 用户过滤，None 表示不过滤
            recent_n: 只取最近的 N 条 episodes 参与分析
            batch_size: 每次 LLM 调用的批量大小
            top_k: 高频标签截取数量
        """
        cache_key = f"{user_id}_{recent_n}"
        if (
            cache_key in self.patterns_cache
            and self.last_pattern_analysis
            and (datetime.now() - self.last_pattern_analysis).seconds < 3600
        ):
            return self.patterns_cache[cache_key]

        # ---- Step 1: 数据采样 ----
        episodes = [e for e in self.episodes if user_id is None or e.user_id == user_id]
        episodes.sort(key=lambda e: e.timestamp, reverse=True)
        episodes = episodes[:recent_n]

        if len(episodes) < 3:
            return []  # 样本太少，不分析

        # ---- Step 2: 批量语义标注 ----
        try:
            tag_counts = self._label_episodes(episodes, batch_size)
        except Exception:
            # LLM 不可用时回退到旧版关键词统计
            return self._find_patterns_keyword_fallback(user_id)

        if not tag_counts:
            return []

        # ---- Step 3: 频率统计 ----
        freq_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # ---- Step 4: 模式深度总结 ----
        summary = self._summarize_patterns(freq_tags, episodes[:30])

        patterns = [
            {"type": "summary", "content": summary},
            {
                "type": "tags",
                "items": [{"tag": t, "count": c} for t, c in freq_tags],
            },
        ]

        self.patterns_cache[cache_key] = patterns
        self.last_pattern_analysis = datetime.now()
        return patterns

    # ---- LLM 辅助方法 ----

    def _label_episodes(self, episodes: List[Episode], batch_size: int) -> Dict[str, int]:
        """Step 2: 分片交给 LLM 打行为标签，返回标签频率字典。"""
        from agent.cb_agent_basic import CbAgentsLLMBasic

        llm = CbAgentsLLMBasic()
        tag_counts: Dict[str, int] = {}

        for i in range(0, len(episodes), batch_size):
            batch = episodes[i : i + batch_size]

            # 构造每条的摘要行
            lines = []
            for idx, ep in enumerate(batch):
                ctx = ""
                if ep.context:
                    ctx_items = [f"{k}={v}" for k, v in list(ep.context.items())[:3]]
                    ctx = " | " + ", ".join(ctx_items)
                lines.append(f"[{idx}] {ep.content[:120]}{ctx}")

            prompt = (
                "你是一个用户行为分析师。下面是一批用户的操作记录，"
                "请为每条记录提炼 1-3 个行为标签（中文，2-5 字），"
                "例如：旅游规划、票务查询、技术学习、代码调试。\n\n"
                "输出格式：每条一行，格式为 [编号] 标签1, 标签2, 标签3\n\n"
                + "\n".join(lines)
            )

            result = llm.ask(
                prompt,
                system="你是一个精确的用户行为标签提取器。只输出标签，不输出任何解释。",
                temperature=0.0,
            )

            # 解析 LLM 返回的标签
            for line in result.split("\n"):
                line = line.strip()
                if not line or "[" not in line:
                    continue
                # 提取 ] 后面的部分
                tag_part = line.split("]", 1)[-1].strip()
                if not tag_part:
                    continue
                for tag in tag_part.split(","):
                    tag = tag.strip()
                    if tag:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1

        return tag_counts

    def _summarize_patterns(
        self, freq_tags: List[tuple], sample_episodes: List[Episode]
    ) -> str:
        """Step 4: 将高频标签和原始上下文交给 LLM，生成人类可读的模式总结。"""
        from agent.cb_agent_basic import CbAgentsLLMBasic

        llm = CbAgentsLLMBasic()

        tag_lines = "\n".join(f"- {tag}（出现 {cnt} 次）" for tag, cnt in freq_tags)
        sample_lines = "\n".join(
            f"- [{ep.timestamp.strftime('%m-%d %H:%M')}] {ep.content[:150]}"
            for ep in sample_episodes[:20]
        )

        prompt = (
            f"高频行为标签：\n{tag_lines}\n\n"
            f"近期操作记录样本：\n{sample_lines}\n\n"
            "请基于以上数据，用 3-5 句话总结用户的行为模式，"
            "包括：偏好领域、时间规律、交互风格等。语言简洁。"
        )

        result = llm.ask(prompt, system="你是一个用户行为模式分析师。", temperature=0.0)
        return result.strip()

    def _find_patterns_keyword_fallback(self, user_id: str = None) -> List[Dict[str, Any]]:
        """LLM 不可用时的关键词回退方案。"""
        episodes = [e for e in self.episodes if user_id is None or e.user_id == user_id]
        keyword_counts: Dict[str, int] = {}
        for ep in episodes:
            for word in ep.content.lower().split():
                if len(word) > 3:
                    keyword_counts[word] = keyword_counts.get(word, 0) + 1
        freq = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)[:15]
        return [
            {"type": "keyword", "items": [{"tag": w, "count": c} for w, c in freq if c >= 2]}
        ]
    
    def get_timeline(self, user_id: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        """获取时间线视图"""
        episodes = [e for e in self.episodes if user_id is None or e.user_id == user_id]
        episodes.sort(key=lambda x: x.timestamp, reverse=True)
        
        timeline = []
        for episode in episodes[:limit]:
            timeline.append({
                "episode_id": episode.episode_id,
                "timestamp": episode.timestamp.isoformat(),
                "content": episode.content[:100] + "..." if len(episode.content) > 100 else episode.content,
                "session_id": episode.session_id,
                "importance": episode.importance,
                "outcome": episode.outcome
            })
        
        return timeline
    
    def _filter_episodes(
        self,
        user_id: str = None,
        session_id: str = None,
        time_range: Tuple[datetime, datetime] = None
    ) -> List[Episode]:
        """过滤情景"""
        filtered = self.episodes
        
        if user_id:
            filtered = [e for e in filtered if e.user_id == user_id]
        
        if session_id:
            filtered = [e for e in filtered if e.session_id == session_id]
        
        if time_range:
            start_time, end_time = time_range
            filtered = [e for e in filtered if start_time <= e.timestamp <= end_time]
        
        return filtered
    
    def _calculate_time_span(self) -> float:
        """计算记忆时间跨度（天）"""
        if not self.episodes:
            return 0.0
        
        timestamps = [e.timestamp for e in self.episodes]
        min_time = min(timestamps)
        max_time = max(timestamps)
        
        return (max_time - min_time).days
    
    def _persist_episode(self, episode: Episode):
        """持久化情景到存储后端"""
        if self.storage and hasattr(self.storage, 'add_memory'):
            self.storage.add_memory(
                memory_id=episode.episode_id,
                user_id=episode.user_id,
                content=episode.content,
                memory_type="episodic",
                timestamp=int(episode.timestamp.timestamp()),
                importance=episode.importance,
                properties={
                    "session_id": episode.session_id,
                    "context": episode.context,
                    "outcome": episode.outcome
                }
            )
    
    def _remove_from_storage(self, memory_id: str):
        """从存储后端删除"""
        if self.storage and hasattr(self.storage, 'delete_memory'):
            self.storage.delete_memory(memory_id)

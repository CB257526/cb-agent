"""工作记忆实现

按照第8章架构设计的工作记忆，提供：
- 短期上下文管理
- 容量和时间限制
- 优先级管理
- 自动清理机制
"""

from typing import List, Dict, Any
from datetime import datetime, timedelta
import heapq
import numpy as np

from ..base import BaseMemory, MemoryItem, MemoryConfig
from utils.common import count_tokens

class WorkingMemory(BaseMemory):
    """工作记忆实现
    
    特点：
    - 容量有限（通常10-20条记忆）
    - 时效性强（会话级别）
    - 优先级管理
    - 自动清理过期记忆
    """
    
    def __init__(self, config: MemoryConfig, storage_backend=None):
        super().__init__(config, storage_backend)
        
        # 工作记忆特定配置
        self.max_capacity = self.config.working_memory_capacity
        self.max_tokens = self.config.working_memory_tokens
        # 纯内存TTL（分钟），可通过在 MemoryConfig 上挂载 working_memory_ttl_minutes 覆盖
        self.max_age_minutes = getattr(self.config, 'working_memory_ttl_minutes', 120)
        self.current_tokens = 0
        self.session_start = datetime.now()
        
        # 内存存储（工作记忆不需要持久化）
        self.memories: List[MemoryItem] = []
        
        # 使用优先级队列管理记忆
        self.memory_heap = []  # (priority, timestamp, memory_item)
    
    def add(self, memory_item: MemoryItem) -> str:
        """添加工作记忆"""
        self._expire_old_memories()

        # 预计算嵌入向量：add 时编码一次，后续 retrieve 直接复用，避免每次检索重复编码
        if memory_item.vector is None:
            try:
                from memory.embedding import get_text_embedder_model
                embedder = get_text_embedder_model()
                vec = embedder.encode(memory_item.content)
                memory_item.vector = vec.tolist() if hasattr(vec, 'tolist') else list(vec)
            except Exception:
                pass  # 嵌入模型不可用时跳过，检索时回退到关键词匹配

        priority = self._calculate_priority(memory_item)
        heapq.heappush(self.memory_heap, (-priority, memory_item.timestamp, memory_item))
        self.memories.append(memory_item)
        self.current_tokens += count_tokens(memory_item.content)
        self._enforce_capacity_limits()
        return memory_item.id
    
    def retrieve(self, query: str, limit: int = 5, user_id: str = None, **kwargs) -> List[MemoryItem]:
        """检索工作记忆 - 使用嵌入模型做语义向量检索"""
        # 1. 惰性清理：先踢掉过期的记忆，避免脏数据参与检索
        self._expire_old_memories()
        if not self.memories:
            return []

        # 2. 过滤已标记为"遗忘"的记忆（逻辑删除，不物理删除）
        active_memories = [m for m in self.memories if not m.metadata.get("forgotten", False)]

        # 3. 多用户隔离：按 user_id 过滤，不跨用户检索
        filtered_memories = active_memories
        if user_id:
            filtered_memories = [m for m in active_memories if m.user_id == user_id]

        if not filtered_memories:
            return []

        # 4. 语义检索：编码 query 和所有候选记忆，用余弦相似度排序
        scored_memories = self._semantic_retrieve(query, filtered_memories)

        # 5. 按最终得分降序排列，截取 top-N 返回
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        return [memory for _, memory in scored_memories[:limit]]

    def _semantic_retrieve(self, query: str, memories: List[MemoryItem]) -> List[tuple]:
        """使用嵌入模型计算语义相似度，失败时回退到关键词匹配"""
        try:
            from memory.embedding import get_text_embedder_model

            embedder = get_text_embedder_model()

            # 只编码 query（记忆的向量在 add 时已预计算并缓存）
            query_vec = embedder.encode(query)

            # 收集已缓存的记忆向量，缺失的当场补编码
            vecs = []
            for m in memories:
                if m.vector is not None:
                    vecs.append(np.array(m.vector))
                else:
                    vecs.append(embedder.encode(m.content))

            memory_vecs = np.array(vecs)

            # L2 归一化后点积 = 余弦相似度（+1e-8 防止除零）
            # 计算L2 范数归一化 v = v / ||v||_2   ||v||_2 = sqrt(sum(v_i^2))
            query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
            memory_norms = memory_vecs / (np.linalg.norm(memory_vecs, axis=1, keepdims=True) + 1e-8)
            # 计算余弦相似度：v_i * v_j / ||v_i||_2 * ||v_j||_2
            similarities = np.dot(memory_norms, query_norm)  #列向量 (m x 1)，包含了查询句与所有记忆的相似度得分

            query_lower = query.lower()
            scored = []
            for i, memory in enumerate(memories):
                # 语义分数：嵌入向量余弦相似度（主信号）
                semantic_score = float(similarities[i])

                # 关键词加分：精确子串命中 +0.15，分词重叠 +0.1（辅助信号）
                content_lower = memory.content.lower()
                keyword_boost = 0.0
                if query_lower in content_lower:
                    keyword_boost = 0.15
                else:
                    query_words = set(query_lower.split())
                    content_words = set(content_lower.split())
                    overlap = query_words & content_words
                    if overlap:
                        keyword_boost = len(overlap) / len(query_words | content_words) * 0.1

                # 基础相关性 = 语义分数 + 关键词加分
                base_relevance = semantic_score + keyword_boost

                # 时间衰减：越久远的记忆权重越低（指数衰减）
                time_decay = self._calculate_time_decay(memory.timestamp)
                base_relevance *= time_decay

                # 重要性加权：用户/系统标记的重要记忆获得加成（0.8~1.2 倍）
                importance_weight = 0.8 + (memory.importance * 0.4)
                final_score = base_relevance * importance_weight

                if final_score > 0:
                    scored.append((final_score, memory))

            return scored

        except Exception:
            # 嵌入模型不可用时回退到纯关键词匹配
            return self._keyword_retrieve(query, memories)

    def _keyword_retrieve(self, query: str, memories: List[MemoryItem]) -> List[tuple]:
        """关键词匹配回退 — 不依赖嵌入模型，纯文本匹配"""
        query_lower = query.lower()
        scored = []
        for memory in memories:
            content_lower = memory.content.lower()
            # 完全子串匹配：query 完整出现在 content 中的比例
            if query_lower in content_lower:
                keyword_score = len(query_lower) / len(content_lower)
            else:
                # 分词交集匹配：query 和 content 的词语重叠度（Jaccard 系数 x0.8）
                query_words = set(query_lower.split())
                content_words = set(content_lower.split())
                overlap = query_words & content_words
                if overlap:
                    keyword_score = len(overlap) / len(query_words | content_words) * 0.8
                else:
                    keyword_score = 0.0

            if keyword_score > 0:
                # 与语义检索使用相同的时间衰减和重要性加权公式
                time_decay = self._calculate_time_decay(memory.timestamp)
                importance_weight = 0.8 + (memory.importance * 0.4)
                final_score = keyword_score * time_decay * importance_weight
                scored.append((final_score, memory))

        return scored
    
    def update(
        self,
        memory_id: str,
        content: str = None,
        importance: float = None,
        metadata: Dict[str, Any] = None
    ) -> bool:
        """更新工作记忆"""
        for memory in self.memories:
            if memory.id == memory_id:
                old_tokens = count_tokens(memory.content)

                if content is not None:
                    memory.content = content
                    new_tokens = count_tokens(content)
                    self.current_tokens = self.current_tokens - old_tokens + new_tokens
                    # 内容变了，重新编码向量以保证检索时拿到的向量与实际内容一致
                    memory.vector = None
                    try:
                        from memory.embedding import get_text_embedder_model
                        embedder = get_text_embedder_model()
                        vec = embedder.encode(content)
                        memory.vector = vec.tolist() if hasattr(vec, 'tolist') else list(vec)
                    except Exception:
                        pass

                if importance is not None:
                    memory.importance = importance

                if metadata is not None:
                    memory.metadata.update(metadata)

                self._update_heap_priority(memory)
                return True
        return False
    
    def remove(self, memory_id: str) -> bool:
        """删除工作记忆"""
        for i, memory in enumerate(self.memories):
            if memory.id == memory_id:
                # 从列表中删除
                removed_memory = self.memories.pop(i)
                
                # 从堆中删除（标记删除）
                self._mark_deleted_in_heap(memory_id)
                
                # 更新token计数
                self.current_tokens -= count_tokens(removed_memory.content)
                self.current_tokens = max(0, self.current_tokens)
                
                return True
        return False
    
    def has_memory(self, memory_id: str) -> bool:
        """检查记忆是否存在"""
        return any(memory.id == memory_id for memory in self.memories)
    
    def clear(self):
        """清空所有工作记忆"""
        self.memories.clear()
        self.memory_heap.clear()
        self.current_tokens = 0
    
    def get_stats(self) -> Dict[str, Any]:
        """获取工作记忆统计信息"""
        # 过期清理（惰性）
        self._expire_old_memories()
        
        # 工作记忆中的记忆都是活跃的（已遗忘的记忆会被直接删除）
        active_memories = self.memories
        
        return {
            "count": len(active_memories),  # 活跃记忆数量
            "forgotten_count": 0,  # 工作记忆中已遗忘的记忆会被直接删除
            "total_count": len(self.memories),  # 总记忆数量
            "current_tokens": self.current_tokens,
            "max_capacity": self.max_capacity,
            "max_tokens": self.max_tokens,
            "max_age_minutes": self.max_age_minutes,
            "session_duration_minutes": (datetime.now() - self.session_start).total_seconds() / 60,
            "avg_importance": sum(m.importance for m in active_memories) / len(active_memories) if active_memories else 0.0,
            "capacity_usage": len(active_memories) / self.max_capacity if self.max_capacity > 0 else 0.0,
            "token_usage": self.current_tokens / self.max_tokens if self.max_tokens > 0 else 0.0,
            "memory_type": "working"
        }
    
    def get_recent(self, limit: int = 10) -> List[MemoryItem]:
        """获取最近的记忆"""
        sorted_memories = sorted(
            self.memories, 
            key=lambda x: x.timestamp, 
            reverse=True
        )
        return sorted_memories[:limit]
    
    def get_important(self, limit: int = 10) -> List[MemoryItem]:
        """获取重要记忆"""
        sorted_memories = sorted(
            self.memories,
            key=lambda x: x.importance,
            reverse=True
        )
        return sorted_memories[:limit]

    def get_all(self) -> List[MemoryItem]:
        """获取所有记忆"""
        return self.memories.copy()
    
    def get_context_summary(self, max_length: int = 500) -> str:
        """获取上下文摘要"""
        if not self.memories:
            return "No working memories available."
        
        # 按重要性和时间排序
        sorted_memories = sorted(
            self.memories,
            key=lambda m: (m.importance, m.timestamp),
            reverse=True
        )
        
        summary_parts = []
        current_length = 0
        
        for memory in sorted_memories:
            content = memory.content
            if current_length + len(content) <= max_length:
                summary_parts.append(content)
                current_length += len(content)
            else:
                # 截断最后一个记忆
                remaining = max_length - current_length
                if remaining > 50:  # 至少保留50个字符
                    summary_parts.append(content[:remaining] + "...")
                break
        
        return "Working Memory Context:\n" + "\n".join(summary_parts)
    
    def forget(self, strategy: str = "importance_based", threshold: float = 0.1, max_age_days: int = 1) -> int:
        """工作记忆遗忘机制"""
        forgotten_count = 0
        current_time = datetime.now()
        
        to_remove = set()

        # 始终先执行TTL过期（分钟级）
        cutoff_ttl = current_time - timedelta(minutes=self.max_age_minutes)
        for memory in self.memories:
            if memory.timestamp < cutoff_ttl:
                to_remove.add(memory.id)

        if strategy == "importance_based":
            # 删除低重要性记忆
            for memory in self.memories:
                if memory.importance < threshold:
                    to_remove.add(memory.id)

        elif strategy == "time_based":
            # 删除过期记忆（工作记忆通常以小时计算）
            cutoff_time = current_time - timedelta(hours=max_age_days * 24)
            for memory in self.memories:
                if memory.timestamp < cutoff_time:
                    to_remove.add(memory.id)

        elif strategy == "capacity_based":
            # 删除超出容量的记忆
            if len(self.memories) > self.max_capacity:
                # 按优先级排序，删除最低的
                sorted_memories = sorted(
                    self.memories,
                    key=lambda m: self._calculate_priority(m)
                )
                excess_count = len(self.memories) - self.max_capacity
                for memory in sorted_memories[:excess_count]:
                    to_remove.add(memory.id)
        
        # 执行删除
        for memory_id in to_remove:
            if self.remove(memory_id):
                forgotten_count += 1
        
        return forgotten_count
    
    def _calculate_priority(self, memory: MemoryItem) -> float:
        """计算记忆优先级"""
        # 基础优先级 = 重要性
        priority = memory.importance
        
        # 时间衰减
        time_decay = self._calculate_time_decay(memory.timestamp)
        priority *= time_decay
        
        return priority
    
    def _calculate_time_decay(self, timestamp: datetime) -> float:
        """计算时间衰减因子"""
        time_diff = datetime.now() - timestamp
        hours_passed = time_diff.total_seconds() / 3600
        
        # 指数衰减（工作记忆衰减更快）
        decay_factor = self.config.decay_factor ** (hours_passed / 6)  # 每6小时衰减
        return max(0.1, decay_factor)  # 最小保持10%的权重
    
    def _enforce_capacity_limits(self):
        """强制执行容量限制"""
        # 检查记忆数量限制
        while len(self.memories) > self.max_capacity:
            self._remove_lowest_priority_memory()
        
        # 检查token限制
        while self.current_tokens > self.max_tokens:
            self._remove_lowest_priority_memory()

    def _expire_old_memories(self):
        """按TTL清理过期记忆，并同步更新堆与token计数"""
        if not self.memories:
            return
        cutoff_time = datetime.now() - timedelta(minutes=self.max_age_minutes)
        # 过滤保留的记忆
        kept: List[MemoryItem] = []
        removed_token_sum = 0
        for m in self.memories:
            if m.timestamp >= cutoff_time:
                kept.append(m)
            else:
                removed_token_sum += count_tokens(m.content)
        if len(kept) == len(self.memories):
            return
        # 覆盖列表与token
        self.memories = kept
        self.current_tokens = max(0, self.current_tokens - removed_token_sum)
        # 重建堆
        self.memory_heap = []
        for mem in self.memories:
            priority = self._calculate_priority(mem)
            heapq.heappush(self.memory_heap, (-priority, mem.timestamp, mem))
    
    def _remove_lowest_priority_memory(self):
        """删除优先级最低的记忆"""
        if not self.memories:
            return
        
        # 找到优先级最低的记忆
        lowest_priority = float('inf')
        lowest_memory = None
        
        for memory in self.memories:
            priority = self._calculate_priority(memory)
            if priority < lowest_priority:
                lowest_priority = priority
                lowest_memory = memory
        
        if lowest_memory:
            self.remove(lowest_memory.id)
    
    def _update_heap_priority(self, memory: MemoryItem):
        """更新堆中记忆的优先级"""
        # 简单实现：重建堆
        self.memory_heap = []
        for mem in self.memories:
            priority = self._calculate_priority(mem)
            heapq.heappush(self.memory_heap, (-priority, mem.timestamp, mem))
    
    def _mark_deleted_in_heap(self, memory_id: str):
        """在堆中标记删除的记忆"""
        # 由于heapq不支持直接删除，我们标记为已删除
        # 在后续操作中会被清理
        pass

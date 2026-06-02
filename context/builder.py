"""上下文构建器 - GSSC 流水线

实现 Gather-Select-Structure-Compress 上下文构建流程：
1. Gather: 从多源（系统指令、Markdown 记忆、旧记忆、RAG、对话历史、外部包）收集候选信息
2. Select: 基于相关性、新近性、MMR 多样性、token 预算筛选
3. Structure: 按优先级组织成结构化上下文模板
4. Compress: 在预算内压缩与规范化（保结构整段丢弃）

提供同步 build() 与异步 abuild() 两套入口，异步版本对 Markdown memory / memory / rag 检索做并发触发。
"""

from __future__ import annotations

import asyncio
import logging
import math
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, TYPE_CHECKING

from core.message import Message, MessageRole
from context.markdown_memory import MarkdownMemoryProvider
from utils.common import (
    _get_encoding,
    count_tokens,
    jaccard,
    tokenize_for_relevance,
)

if TYPE_CHECKING:
    from tools.tools.memory_tool import MemoryTool
    from tools.tools.rag_tool import RAGTool


logger = logging.getLogger(__name__)


# ---------- 模块级 helper（与 utils.common 共用底层 tiktoken 工具） ----------


def _now_utc() -> datetime:
    """当前 UTC 时间，带时区信息以避免 naive/aware 混用。"""
    return datetime.now(timezone.utc)


def _extract_message_text(msg: Message) -> str:
    """把单条 Message 转成可读文本。

    正确处理：
    - role 是 MessageRole 枚举（取 .value 而非整个枚举的字面）
    - user 角色的 content 可能是 List[Dict]（多模态），抽出 text/image_url/audio_url
    - tool 角色额外标注 tool_name
    """
    role = msg.role.value if isinstance(msg.role, MessageRole) else str(msg.role)

    # 提取正文
    body_parts: List[str] = []
    content = msg.content
    if isinstance(content, str):
        if content:
            body_parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "")
                if text:
                    body_parts.append(text)
            elif item_type == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                body_parts.append(f"[image: {url}]" if url else "[image]")
            elif item_type == "audio_url":
                url = (item.get("audio_url") or {}).get("url", "")
                body_parts.append(f"[audio: {url}]" if url else "[audio]")

    # 工具调用结果带 tool_name 标注
    if role == "tool" and msg.tool_name:
        prefix = f"[tool:{msg.tool_name}] "
    else:
        prefix = ""

    body = " ".join(body_parts).strip()
    return f"[{role}] {prefix}{body}".rstrip()


def messages_to_text(
    messages: Sequence[Message],
    max_messages: Optional[int] = None,
) -> str:
    """把一组 Message 转成多行文本，仅保留最近 max_messages 条。"""
    if not messages:
        return ""
    chunk = messages[-max_messages:] if max_messages else list(messages)
    return "\n".join(_extract_message_text(m) for m in chunk)


# ---------- 优先级枚举 ----------


class ContextPriority(IntEnum):
    """上下文片段的优先级，决定进入哪一节、按何种顺序选择。

    数值越小优先级越高（系统指令永不被截断）。
    """
    P0_SYSTEM = 0     # [Role & Policies]
    P1_STATE = 1      # [State]
    P2_EVIDENCE = 2   # [Evidence]
    P3_HISTORY = 3    # [Context]


# ---------- 数据类 ----------


@dataclass
class ContextPacket:
    """上下文信息包。

    token_count 改为按需懒计算（property），避免批量构造时频繁调用编码器。
    """
    content: str
    priority: ContextPriority = ContextPriority.P2_EVIDENCE
    timestamp: datetime = field(default_factory=_now_utc)
    metadata: Dict[str, Any] = field(default_factory=dict)
    relevance_score: float = 0.0
    _token_count: Optional[int] = field(default=None, repr=False)

    @property
    def token_count(self) -> int:
        """懒计算 token 数，仅在第一次访问时编码。"""
        if self._token_count is None:
            self._token_count = count_tokens(self.content)
        return self._token_count

    @token_count.setter
    def token_count(self, value: int) -> None:
        self._token_count = value


@dataclass
class ContextConfig:
    """上下文构建配置。"""

    # 预算
    max_tokens: int = 8000
    reserve_ratio: float = 0.15  # 给生成留出的余量比例（约 15%）

    # 筛选
    min_relevance: float = 0.05  # 调低阈值以适配 token 集合 Jaccard（中文短查询交集天然较小）
    enable_mmr: bool = True
    mmr_lambda: float = 0.7      # 0=纯多样性, 1=纯相关性

    # 评分权重与衰减
    relevance_weight: float = 0.7
    recency_weight: float = 0.3
    recency_tau_seconds: float = 3600.0  # 新近性指数衰减时间尺度

    # 压缩
    enable_compression: bool = True

    # 其他
    system_prompt_template: str = ""

    # 记忆检索参数（之前硬编码，现在暴露）
    memory_state_query: str = "任务状态 子目标 结论 阻塞"
    memory_state_min_importance: float = 0.7
    memory_state_limit: int = 5
    memory_state_types: Tuple[str, ...] = ("working", "episodic", "semantic")
    memory_related_limit: int = 5

    # RAG 参数
    rag_top_k: int = 5

    # 历史
    history_max_messages: int = 10

    def get_available_tokens(self) -> int:
        """扣除生成余量后的可用预算。"""
        return int(self.max_tokens * (1 - self.reserve_ratio))


@dataclass
class ContextResult:
    """详细构建结果，用于调试和可观测性。"""
    context: str
    selected: List[ContextPacket]
    dropped: List[Tuple[ContextPacket, str]]  # (packet, 丢弃原因)
    truncated: bool
    total_tokens: int


# ---------- ContextBuilder ----------


class ContextBuilder:
    """上下文构建器（GSSC 流水线）。

    用法：
        builder = ContextBuilder(memory_tool, rag_tool, ContextConfig(max_tokens=8000))
        ctx = builder.build(user_query="...", conversation_history=[...])
        # 或异步并发触发 memory/rag：
        ctx = await builder.abuild(user_query="...", conversation_history=[...])
    """

    def __init__(
        self,
        memory_tool: Optional["MemoryTool"] = None,
        rag_tool: Optional["RAGTool"] = None,
        md_memory_provider: Optional[MarkdownMemoryProvider] = None,
        config: Optional[ContextConfig] = None,
    ):
        # 兼容旧构造方式：新增 md_memory_provider 参数前，第三个位置参数就是
        # ContextConfig。若调用方仍写 ContextBuilder(memory, rag, ContextConfig(...))，
        # 这里主动把第三参挪回 config，避免后续把配置对象当 Markdown provider 调用。
        if isinstance(md_memory_provider, ContextConfig) and config is None:
            config = md_memory_provider
            md_memory_provider = None

        self.memory_tool = memory_tool
        self.rag_tool = rag_tool
        self.md_memory_provider = md_memory_provider
        self.config = config or ContextConfig()

    # ---------- 公开入口 ----------

    def build(
        self,
        user_query: str,
        conversation_history: Optional[List[Message]] = None,
        system_instructions: Optional[str] = None,
        additional_packets: Optional[List[ContextPacket]] = None,
    ) -> str:
        """同步构建上下文，返回结构化字符串。"""
        return self.build_detailed(
            user_query=user_query,
            conversation_history=conversation_history,
            system_instructions=system_instructions,
            additional_packets=additional_packets,
        ).context

    async def abuild(
        self,
        user_query: str,
        conversation_history: Optional[List[Message]] = None,
        system_instructions: Optional[str] = None,
        additional_packets: Optional[List[ContextPacket]] = None,
    ) -> str:
        """异步构建上下文：memory 与 rag 检索并发触发。"""
        result = await self.abuild_detailed(
            user_query=user_query,
            conversation_history=conversation_history,
            system_instructions=system_instructions,
            additional_packets=additional_packets,
        )
        return result.context

    # ---------- OpenAI messages 适配 ----------

    def to_messages(
        self,
        user_query: str,
        conversation_history: Optional[List[Message]] = None,
        system_instructions: Optional[str] = None,
        additional_packets: Optional[List[ContextPacket]] = None,
    ) -> List[Dict[str, str]]:
        """构建上下文并直接转成 OpenAI 风格的 messages 列表。

        约定：整段拼好的 prompt 放在一条 system message 里，user_query 再作为
        最后一条 user message 出现一次（冗余但符合 OpenAI 风格，多数厂商对
        "最后一条 user 消息为当前问题" 这种格式表现更稳）。
        """
        ctx = self.build(
            user_query=user_query,
            conversation_history=conversation_history,
            system_instructions=system_instructions,
            additional_packets=additional_packets,
        )
        return [
            {"role": "system", "content": ctx},
            {"role": "user", "content": user_query},
        ]

    async def ato_messages(
        self,
        user_query: str,
        conversation_history: Optional[List[Message]] = None,
        system_instructions: Optional[str] = None,
        additional_packets: Optional[List[ContextPacket]] = None,
    ) -> List[Dict[str, str]]:
        """`to_messages` 的异步版本（走 `abuild`，memory/rag 并发触发）。"""
        ctx = await self.abuild(
            user_query=user_query,
            conversation_history=conversation_history,
            system_instructions=system_instructions,
            additional_packets=additional_packets,
        )
        return [
            {"role": "system", "content": ctx},
            {"role": "user", "content": user_query},
        ]

    def build_detailed(
        self,
        user_query: str,
        conversation_history: Optional[List[Message]] = None,
        system_instructions: Optional[str] = None,
        additional_packets: Optional[List[ContextPacket]] = None,
    ) -> ContextResult:
        """同步构建并返回详细结果（含被丢弃的片段、是否截断等）。"""
        packets = self._gather_sync(
            user_query=user_query,
            conversation_history=conversation_history or [],
            system_instructions=system_instructions,
            additional_packets=additional_packets or [],
        )
        return self._finalize(packets, user_query)

    async def abuild_detailed(
        self,
        user_query: str,
        conversation_history: Optional[List[Message]] = None,
        system_instructions: Optional[str] = None,
        additional_packets: Optional[List[ContextPacket]] = None,
    ) -> ContextResult:
        """异步构建并返回详细结果。"""
        packets = await self._gather_async(
            user_query=user_query,
            conversation_history=conversation_history or [],
            system_instructions=system_instructions,
            additional_packets=additional_packets or [],
        )
        return self._finalize(packets, user_query)

    # ---------- 内部流水线 ----------

    def _finalize(self, packets: List[ContextPacket], user_query: str) -> ContextResult:
        """统一的 Select -> Structure -> Compress 收尾，sync/async 共用。"""
        selected, dropped = self._select(packets, user_query)
        structured = self._structure(selected, user_query)
        final_text, truncated = self._compress(structured)
        return ContextResult(
            context=final_text,
            selected=selected,
            dropped=dropped,
            truncated=truncated,
            total_tokens=count_tokens(final_text),
        )

    # ---------- Gather ----------

    def _gather_sync(
        self,
        user_query: str,
        conversation_history: List[Message],
        system_instructions: Optional[str],
        additional_packets: List[ContextPacket],
    ) -> List[ContextPacket]:
        """同步收集候选 packet。"""
        packets: List[ContextPacket] = []
        state_packets, extra_packets = self._split_local_state_packets(additional_packets)

        if system_instructions:
            packets.append(self._make_system_packet(system_instructions))

        # 本地 session state 是跨轮工作上下文的滚动摘要，优先级高于所有记忆来源。
        # AgentSession 通过 additional_packets 传入它；这里识别 metadata.source 后
        # 提前插入，剩余 additional_packets 仍按调用方原意在 Gather 末尾追加。
        packets.extend(state_packets)

        # Markdown 记忆：轻量模式使用，不依赖 embedding/RAG。先注入状态类记忆，
        # 再注入与当前 query 相关的片段；full 模式如果同时传入旧 memory_tool，
        # 后面的旧记忆/RAG 路径仍照常工作。
        md_state, md_related = self._search_markdown_memory(user_query)
        if md_state:
            packets.append(md_state)
        if md_related:
            packets.append(md_related)

        # 旧记忆：任务状态
        state = self._search_memory_state()
        if state:
            packets.append(state)

        # 旧记忆：与查询相关
        related = self._search_memory_related(user_query)
        if related:
            packets.append(related)

        # RAG
        rag = self._search_rag(user_query)
        if rag:
            packets.append(rag)

        # 对话历史
        if conversation_history:
            packets.append(self._make_history_packet(conversation_history))

        # 外部传入
        packets.extend(extra_packets)

        return packets

    async def _gather_async(
        self,
        user_query: str,
        conversation_history: List[Message],
        system_instructions: Optional[str],
        additional_packets: List[ContextPacket],
    ) -> List[ContextPacket]:
        """异步收集候选 packet：memory/rag 用 to_thread 并发触发。"""
        packets: List[ContextPacket] = []
        state_packets, extra_packets = self._split_local_state_packets(additional_packets)

        if system_instructions:
            packets.append(self._make_system_packet(system_instructions))

        # 同步版本同理：本地 session state 先进入 [State]，再拼 Markdown/full 记忆。
        packets.extend(state_packets)

        # 四路并发触发同步检索。Markdown provider 是纯文件扫描，旧 memory/rag
        # 是 full 模式才会传入的工具；各自 helper 内部会在未配置时快速返回 None。
        md_task = asyncio.to_thread(self._search_markdown_memory, user_query)
        state_task = asyncio.to_thread(self._search_memory_state)
        related_task = asyncio.to_thread(self._search_memory_related, user_query)
        rag_task = asyncio.to_thread(self._search_rag, user_query)

        md_result, state, related, rag = await asyncio.gather(
            md_task, state_task, related_task, rag_task, return_exceptions=False
        )

        md_state, md_related = md_result
        for p in (md_state, md_related):
            if p is not None:
                packets.append(p)
        for p in (state, related, rag):
            if p is not None:
                packets.append(p)

        if conversation_history:
            packets.append(self._make_history_packet(conversation_history))

        packets.extend(extra_packets)

        return packets

    # ---------- 工具调用（已抽出，便于 mock 单测） ----------

    @staticmethod
    def _split_local_state_packets(
        packets: List[ContextPacket],
    ) -> Tuple[List[ContextPacket], List[ContextPacket]]:
        """把 AgentSession 注入的本地滚动状态从 additional_packets 中提前。

        这个 helper 专门服务轻量/持久化上下文的组合：LocalSessionStore 的
        state.json 代表“当前会话已经确认的任务状态”，应该排在 Markdown 记忆、
        full MemoryTool 与 RAG 之前。其它 additional packet 仍保留原顺序，并在
        Gather 末尾追加，避免破坏调用方手动传入证据的旧语义。
        """
        state_packets: List[ContextPacket] = []
        extra_packets: List[ContextPacket] = []
        for packet in packets:
            source = packet.metadata.get("source") if isinstance(packet.metadata, dict) else None
            if source == "local_session_state":
                state_packets.append(packet)
            else:
                extra_packets.append(packet)
        return state_packets, extra_packets

    def _make_system_packet(self, instructions: str) -> ContextPacket:
        return ContextPacket(
            content=instructions,
            priority=ContextPriority.P0_SYSTEM,
            metadata={"source": "system_instructions"},
        )

    def _make_history_packet(self, history: List[Message]) -> ContextPacket:
        text = messages_to_text(history, max_messages=self.config.history_max_messages)
        return ContextPacket(
            content=text,
            priority=ContextPriority.P3_HISTORY,
            metadata={"source": "history", "count": min(len(history), self.config.history_max_messages)},
        )

    def _search_markdown_memory(self, user_query: str) -> Tuple[Optional[ContextPacket], Optional[ContextPacket]]:
        """检索轻量 Markdown 记忆。

        这个入口只依赖 ``MarkdownMemoryProvider``，不会 import 旧 memory/RAG 包。
        返回两个 packet：任务态记忆进 [State]，相关记忆进 [Evidence]。任一为空时
        返回 None，保持 ContextBuilder 对未配置轻量记忆的兼容。
        """
        if not self.md_memory_provider:
            return None, None
        try:
            result = self.md_memory_provider.recall(user_query)
        except Exception:
            logger.exception("Markdown 记忆检索失败")
            return None, None

        state_packet = None
        related_packet = None
        if result.state_text:
            state_packet = ContextPacket(
                content=result.state_text,
                priority=ContextPriority.P1_STATE,
                metadata={"source": "markdown_memory.state"},
            )
        if result.related_text:
            related_packet = ContextPacket(
                content=result.related_text,
                priority=ContextPriority.P2_EVIDENCE,
                metadata={"source": "markdown_memory.related"},
            )
        return state_packet, related_packet

    def _search_memory_state(self) -> Optional[ContextPacket]:
        """搜索任务态：覆盖 working/episodic/semantic 三种类型，结果合并。"""
        if not self.memory_tool:
            return None
        merged: List[str] = []
        for mem_type in self.config.memory_state_types:
            try:
                result = self.memory_tool.execute(
                    "search",
                    query=self.config.memory_state_query,
                    memory_type=mem_type,
                    limit=self.config.memory_state_limit,
                    min_importance=self.config.memory_state_min_importance,
                )
            except Exception:
                logger.exception("记忆检索（任务态/%s）失败", mem_type)
                continue
            text = self._normalize_tool_output(result)
            if text:
                merged.append(f"<{mem_type}>\n{text}")
        if not merged:
            return None
        return ContextPacket(
            content="\n\n".join(merged),
            priority=ContextPriority.P1_STATE,
            metadata={"source": "memory.state", "types": list(self.config.memory_state_types)},
        )

    def _search_memory_related(self, user_query: str) -> Optional[ContextPacket]:
        """按用户查询召回相关记忆。"""
        if not self.memory_tool or not user_query:
            return None
        try:
            result = self.memory_tool.execute(
                "search",
                query=user_query,
                limit=self.config.memory_related_limit,
            )
        except Exception:
            logger.exception("记忆检索（相关）失败")
            return None
        text = self._normalize_tool_output(result)
        if not text:
            return None
        return ContextPacket(
            content=text,
            priority=ContextPriority.P2_EVIDENCE,
            metadata={"source": "memory.related"},
        )

    def _search_rag(self, user_query: str) -> Optional[ContextPacket]:
        """RAG 知识库检索。"""
        if not self.rag_tool or not user_query:
            return None
        try:
            result = self.rag_tool.run({
                "action": "search",
                "query": user_query,
                "top_k": self.config.rag_top_k,
            })
        except Exception:
            logger.exception("RAG 检索失败")
            return None
        text = self._normalize_tool_output(result)
        if not text:
            return None
        return ContextPacket(
            content=text,
            priority=ContextPriority.P2_EVIDENCE,
            metadata={"source": "rag"},
        )

    @staticmethod
    def _normalize_tool_output(raw: Any) -> str:
        """工具结果归一化：None/空字符串视为无结果，去除首尾空白。

        不再做 "未找到"/"错误" 字面量匹配——那种判断在工具改提示语后会失效，
        让工具自己负责返回明确的空结果即可。
        """
        if raw is None:
            return ""
        text = str(raw).strip()
        return text

    # ---------- Select ----------

    def _select(
        self,
        packets: List[ContextPacket],
        user_query: str,
    ) -> Tuple[List[ContextPacket], List[Tuple[ContextPacket, str]]]:
        """筛选 + 排序 + 预算填充。

        返回：(选中列表, 被丢弃及原因列表)
        """
        cfg = self.config
        dropped: List[Tuple[ContextPacket, str]] = []
        if not packets:
            return [], dropped

        # 1. 计算每个 packet 的相关性 + 新近性
        query_tokens = tokenize_for_relevance(user_query)
        now = _now_utc()

        for p in packets:
            content_tokens = tokenize_for_relevance(p.content)
            p.relevance_score = jaccard(query_tokens, content_tokens)
            # 把综合分缓存进 metadata，避免 select 内部再算
            recency = self._recency_score(p.timestamp, now)
            p.metadata["_score"] = (
                cfg.relevance_weight * p.relevance_score
                + cfg.recency_weight * recency
            )

        # 2. 系统指令固定纳入，不参与排序与过滤
        system_packets = [p for p in packets if p.priority == ContextPriority.P0_SYSTEM]
        non_system = [p for p in packets if p.priority != ContextPriority.P0_SYSTEM]

        # 3. 历史和任务态也固定纳入（priority 高于证据），按预算填
        forced = [p for p in non_system if p.priority in (ContextPriority.P1_STATE, ContextPriority.P3_HISTORY)]
        evidence = [p for p in non_system if p.priority == ContextPriority.P2_EVIDENCE]

        # 4. 证据按 min_relevance 过滤
        kept_evidence: List[ContextPacket] = []
        for p in evidence:
            if p.relevance_score >= cfg.min_relevance:
                kept_evidence.append(p)
            else:
                dropped.append((p, f"relevance {p.relevance_score:.3f} < {cfg.min_relevance}"))

        # 5. 证据排序：MMR 或纯综合分
        if cfg.enable_mmr and kept_evidence:
            kept_evidence = self._mmr_rerank(kept_evidence, query_tokens, cfg.mmr_lambda)
        else:
            kept_evidence.sort(key=lambda p: p.metadata.get("_score", 0.0), reverse=True)

        # 6. 系统指令必须纳入（即使超预算，由 _compress 兜底处理）
        available = cfg.get_available_tokens()
        used = 0
        selected: List[ContextPacket] = []
        for p in system_packets:
            selected.append(p)
            used += p.token_count

        # 7. P1/P3 按顺序填，超预算则丢但记录原因
        for p in [q for q in forced if q.priority == ContextPriority.P1_STATE] + \
                 [q for q in forced if q.priority == ContextPriority.P3_HISTORY]:
            tc = p.token_count
            if used + tc <= available:
                selected.append(p)
                used += tc
            else:
                # 高优先级超预算：仍然纳入（_compress 会按节丢弃保结构），
                # 但只取一份避免无限堆积
                if not any(s.priority == p.priority and s.content == p.content for s in selected):
                    selected.append(p)
                    used += tc
                    dropped.append((p, f"高优先级超预算但保留（已用 {used}/{available}）"))

        # 8. P2 证据严格按预算填
        for p in kept_evidence:
            tc = p.token_count
            if used + tc <= available:
                selected.append(p)
                used += tc
            else:
                dropped.append((p, f"超预算（已用 {used}/{available}, 该包 {tc}）"))

        return selected, dropped

    def _recency_score(self, ts: datetime, now: datetime) -> float:
        """指数衰减：tau 时间尺度内分值 ~ 1/e。"""
        # 兼容 naive datetime（旧调用方可能传无时区）
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = max((now - ts).total_seconds(), 0.0)
        tau = max(self.config.recency_tau_seconds, 1.0)
        return math.exp(-delta / tau)

    def _mmr_rerank(
        self,
        packets: List[ContextPacket],
        query_tokens: FrozenSet[int],
        mmr_lambda: float,
    ) -> List[ContextPacket]:
        """MMR 贪心重排：每步选 λ*相关 - (1-λ)*max(与已选相似)。

        base 用纯 query-content 相关性（query_tokens vs packet_tokens 的 jaccard），
        而不是 _score（综合分含新近性）。这样 MMR 才是"相关 vs 多样"权衡，
        新近性已在前置预算填充顺序里体现。
        """
        # 预计算每个候选的 token 集合
        token_map = {id(p): tokenize_for_relevance(p.content) for p in packets}
        # 预计算每个候选与 query 的相关性（不复用 p.relevance_score 防止外部已篡改）
        base_map = {id(p): jaccard(query_tokens, token_map[id(p)]) for p in packets}

        remaining = list(packets)
        ordered: List[ContextPacket] = []

        while remaining:
            best_p = None
            best_score = -math.inf
            for p in remaining:
                base = base_map[id(p)]
                if not ordered:
                    mmr = mmr_lambda * base
                else:
                    p_tokens = token_map[id(p)]
                    sim_to_selected = max(
                        jaccard(p_tokens, token_map[id(s)]) for s in ordered
                    )
                    mmr = mmr_lambda * base - (1 - mmr_lambda) * sim_to_selected
                if mmr > best_score:
                    best_score = mmr
                    best_p = p
            ordered.append(best_p)
            remaining.remove(best_p)
        return ordered

    # ---------- Structure ----------

    def _structure(
        self,
        selected: List[ContextPacket],
        user_query: str,
    ) -> str:
        """按优先级组织成结构化模板。"""
        sections: List[str] = []

        # [Role & Policies]
        p0 = [p for p in selected if p.priority == ContextPriority.P0_SYSTEM]
        if p0:
            sections.append("[Role & Policies]\n" + "\n".join(p.content for p in p0))

        # [Task]
        sections.append(f"[Task]\n用户问题：{user_query}")

        # [State]
        p1 = [p for p in selected if p.priority == ContextPriority.P1_STATE]
        if p1:
            body = "\n".join(p.content for p in p1)
            sections.append(f"[State]\n关键进展与未决问题：\n{body}")

        # [Evidence]
        p2 = [p for p in selected if p.priority == ContextPriority.P2_EVIDENCE]
        if p2:
            body = "\n\n".join(p.content for p in p2)
            sections.append(f"[Evidence]\n事实与引用：\n{body}")

        # [Context]
        p3 = [p for p in selected if p.priority == ContextPriority.P3_HISTORY]
        if p3:
            body = "\n".join(p.content for p in p3)
            sections.append(f"[Context]\n对话历史与背景：\n{body}")

        # [Output] —— 用 dedent 处理多行字符串缩进
        sections.append(textwrap.dedent("""\
            [Output]
            请按以下格式回答：
            1. 结论（简洁明确）
            2. 依据（列出支撑证据及来源）
            3. 风险与假设（如有）
            4. 下一步行动建议（如适用）""").rstrip())

        return "\n\n".join(sections)

    # ---------- Compress ----------

    def _compress(
        self,
        context: str,
    ) -> Tuple[str, bool]:
        """超预算时整段（按节）丢弃，保结构。

        返回 (压缩后文本, 是否发生截断)。
        丢弃顺序（从可丢到不可丢）：[Context] -> [Evidence] -> [State]。
        [Role & Policies] / [Task] / [Output] 始终保留。
        """
        if not self.config.enable_compression:
            return context, False

        available = self.config.get_available_tokens()
        if count_tokens(context) <= available:
            return context, False

        # 把已生成文本按节切回，按可丢顺序逐节剥离
        sections = self._split_sections(context)
        droppable = ["[Context]", "[Evidence]", "[State]"]

        for header in droppable:
            if header in sections:
                logger.warning("上下文超预算，整段丢弃 %s 节", header)
                del sections[header]
                rebuilt = "\n\n".join(sections.values())
                if count_tokens(rebuilt) <= available:
                    return rebuilt, True

        # 全丢完仍超预算（极端场景）：截断 [Task] 后内容
        rebuilt = "\n\n".join(sections.values())
        if count_tokens(rebuilt) <= available:
            return rebuilt, True

        # 兜底：硬截断到 token 上限
        encoding = _get_encoding()
        try:
            ids = encoding.encode(rebuilt)[:available]
            return encoding.decode(ids), True
        except Exception:
            # 字符级兜底
            return rebuilt[: available * 4], True

    # 由 _structure 生成的固定节标题（按顺序），_split_sections 据此精确切分
    _SECTION_HEADERS: Tuple[str, ...] = (
        "[Role & Policies]",
        "[Task]",
        "[State]",
        "[Evidence]",
        "[Context]",
        "[Output]",
    )

    @classmethod
    def _split_sections(cls, context: str) -> Dict[str, str]:
        """把 _structure 输出按已知节标题切回有序字典。

        用白名单匹配避免把内容里偶然出现的方括号文本误判为节标题。
        """
        from collections import OrderedDict
        sections: "OrderedDict[str, str]" = OrderedDict()
        current_header: Optional[str] = None
        current_lines: List[str] = []
        header_set = set(cls._SECTION_HEADERS)
        for line in context.split("\n"):
            stripped = line.strip()
            if stripped in header_set:
                if current_header is not None:
                    sections[current_header] = "\n".join(current_lines).rstrip()
                current_header = stripped
                current_lines = [line]
            else:
                current_lines.append(line)
        if current_header is not None:
            sections[current_header] = "\n".join(current_lines).rstrip()
        return sections

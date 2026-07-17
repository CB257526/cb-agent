"""跨轮工作上下文压缩与本地会话持久化。

这个模块刻意和 OpenAI 协议里的 ``messages`` 分开：

- ``messages`` 是"本轮协议上下文"，里面可以有 assistant.tool_calls、
  role=tool、tool_call_id，以及完整工具返回。它只适合在同一轮 tool loop
  中继续回灌给模型，不能跨轮直接复用。
- 本模块维护的是"跨轮工作上下文"，只保存被裁剪后的工具事实和工作摘要。
  它可以安全加入普通对话历史，也可以写入 ``.cbagent/sessions`` 后在重启
  时恢复。

这里的核心原则是：保留"下一轮继续任务需要知道什么"，而不是保留"工具
原始输出是什么"。因此 file_read 的正文、bash 的 stdout/stderr、file_write
的 content 参数都会被严格截断或完全排除，避免本地 transcript 体积膨胀，
也降低敏感内容被长期保存的概率。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.message import Message, MessageRole
from agent.message_protocol import drop_orphan_tool_message_objects
from utils.common import count_tokens

logger = logging.getLogger(__name__)

# 单个工具结果最多进入 trace 的字符数。注意这不是给 LLM 的本轮消息上限；
# 本轮 ``messages`` 仍然保留完整工具结果，只有跨轮 trace 被限制。
TRACE_RESULT_LIMIT = 100

# 一轮 trace 超过以下阈值时，AgentSession 会优先走 TraceSummarizer 进行
# 静默压缩。两个阈值同时存在，是因为中文短文本按字符看可能不长，但 token
# 已经偏高；英文/JSON 则经常相反。
TRACE_SUMMARIZE_CHARS = 1000
TRACE_SUMMARIZE_TOKENS = 800

# ``【工作记录】`` 最终进入 history 的长度上限。这个值需要明显小于普通回答，
# 否则工作记录本身会挤占后续对话窗口。
WORK_RECORD_LIMIT = 600

# state.json 中滚动摘要和单文件摘要的上限。state 会作为 P1 State 注入，
# 优先级高于普通历史，所以必须保持紧凑。
ROLLING_SUMMARY_LIMIT = 2000
FILE_SUMMARY_LIMIT = 300
RECENT_COMMANDS_LIMIT = 10

# compact 后 state 里的结构化集合继续保留，但要做边界裁剪。/compact 的目标是
# 释放上下文，而不是把 state.json 变成无限增长的第二份 transcript。
FILES_SEEN_LIMIT = 50
FILES_MODIFIED_LIMIT = 30
DECISIONS_LIMIT = 20
PENDING_LIMIT = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, limit: int) -> str:
    """把任意值压成单行短文本。

    trace 和 state 的目标是"可读摘要"，不是精确复现原输出，所以这里会：
    1. 把 Windows/Unix 换行统一掉；
    2. 把连续空白折叠成单个空格；
    3. 超过上限时用省略号截断。

    这样可以避免大段 stdout、文件正文或 JSON 缩进把 transcript 撑爆。
    """
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _json_loads_maybe(raw: Any) -> Any:
    """尽量把工具结果解析成结构化 JSON，解析失败则保留原值。

    绝大多数内置工具返回 JSON 字符串，但 MCP 或第三方工具可能返回纯文本。
    上层摘要逻辑只在 dict 形态下做字段级提取，纯文本则走通用截断。
    """
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _safe_json_dumps(data: Any, limit: int = 180) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(data)
    return _clip(text, limit)


def _tail_mapping(data: Any, limit: int) -> Dict[str, Any]:
    """保留 dict 的尾部若干项。

    Python 3.7+ dict 保持插入顺序；这里利用这个特性保留最近写入/更新的项目。
    compact 时不重新排序，是为了不引入额外的时间字段假设：有些旧 state 里的
    文件项可能没有 last_seen_at/last_modified_at，按现有顺序裁剪最稳妥。
    """
    if not isinstance(data, dict):
        return {}
    items = list(data.items())[-limit:]
    return {str(k): v for k, v in items}


def _tail_list(data: Any, limit: int) -> List[Any]:
    """保留 list 的尾部若干项；非 list 输入统一降级为空列表。"""
    if not isinstance(data, list):
        return []
    return data[-limit:]


def _message_kind(message: Message) -> str:
    """读取 Message.metadata.kind，缺失时返回空串。

    work_record/compact_record 都是普通 assistant message，真正区分它们的不是
    OpenAI role，而是本地 metadata。这个 helper 让恢复、裁剪和 UI 导出都能
    用同一套语义判断。
    """
    meta = message.metadata if isinstance(message.metadata, dict) else {}
    return str(meta.get("kind") or "")


def _message_to_persist_payload(message: Message) -> Dict[str, Any]:
    """把 Message 序列化成可落盘且可往返还原的 dict。

    与旧的 _history_message_to_payload（仅返回 role/content/kind 的轻量 UI 视图）
    不同，这个 helper 用于 transcript.jsonl 与 compact.json 的持久化：
    - 保留 assistant.tool_calls（含 OpenAI function 字段）
    - 保留 assistant.reasoning_content，兼容要求跨轮回传思考内容的模型
    - 保留 tool 消息的 tool_call_id 与 name
    - 保留 tool 消息的本地错误状态，供恢复后的 UI 正确展示
    - 保留 metadata.kind 用于识别 compact_boundary
    这样跨轮恢复时可以原样把 tool_use + tool_result 块塞回 messages，让模型
    看到上一轮真实工具调用细节（CC 同款累积模式）。
    """
    role = message.role.value if hasattr(message.role, "value") else str(message.role)
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    payload: Dict[str, Any] = {"role": role}
    # content 可能是字符串或多模态数组（仅 user 角色）。tool 消息的 content
    # 必须保留为字符串。assistant 没有 content 但有 tool_calls 时 content=None。
    payload["content"] = message.content
    if message.tool_calls:
        payload["tool_calls"] = message.tool_calls
    if message.reasoning_content:
        payload["reasoning_content"] = message.reasoning_content
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_name:
        payload["tool_name"] = message.tool_name
    if role == "tool":
        payload["is_error"] = bool(message.is_error)
    kind = metadata.get("kind")
    if kind:
        payload["kind"] = str(kind)
    context_fingerprints = metadata.get("context_fingerprints")
    if isinstance(context_fingerprints, dict):
        # 指纹基线必须跟随 context update 一起落盘，重启后才能继续做增量 diff。
        payload["context_fingerprints"] = {
            str(name): str(fingerprint)
            for name, fingerprint in context_fingerprints.items()
            if name and fingerprint
        }
    # 中断标记需要跟随归档后的消息继续保留，否则 active_turn 一旦转存到
    # transcript，UI 下次恢复时就无法区分正常完成轮和异常中断轮。
    if metadata.get("interrupted"):
        payload["interrupted"] = True
    return payload


def _message_payload_to_message(payload: Dict[str, Any]) -> Optional[Message]:
    """把持久化 payload 还原为 Message。

    支持的 role：user / system / assistant（可带 tool_calls）/ tool。
    旧 compact.json 里的轻量结构（仅 role/content/kind）也仍能恢复成普通文本
    消息——这条路径主要服务破坏性更新前可能残留的旧快照。
    """
    if not isinstance(payload, dict):
        return None
    role = str(payload.get("role") or "")
    content = payload.get("content")
    kind = payload.get("kind")
    tool_calls = payload.get("tool_calls")
    reasoning_content = payload.get("reasoning_content")
    tool_call_id = payload.get("tool_call_id") or ""
    tool_name = payload.get("tool_name") or payload.get("name") or ""
    is_error = bool(payload.get("is_error"))

    if role == "user":
        # user 消息可能是多模态 list，也可能是字符串。空内容跳过。
        if isinstance(content, list):
            if not content:
                return None
            msg = Message(role="user", content=content)
        else:
            text = str(content or "")
            if not text:
                return None
            # 字符串 user 消息必须按字符串原样恢复，不能转换成多模态 text 数组。
            # 否则重启前后语义虽相同，请求 JSON 前缀却不再逐字一致。
            msg = Message(role=MessageRole.USER, content=text)
    elif role == "system":
        text = str(content or "")
        if not text:
            return None
        msg = Message.create_system_message(text)
    elif role == "tool":
        # tool 消息必须有 tool_call_id 才能跟 assistant.tool_calls 配对。
        # 缺失时无法回灌（OpenAI 协议会 400），直接丢弃。
        if not tool_call_id:
            return None
        msg = Message.create_tool_message(
            tool_call_id=str(tool_call_id),
            tool_name=str(tool_name),
            tool_output=str(content or ""),
            is_error=is_error,
        )
    elif role == "assistant":
        # assistant 至少要有 content 或 tool_calls 之一才有意义。
        text = content if isinstance(content, str) else (str(content) if content else None)
        if not text and not tool_calls:
            return None
        msg = Message.create_assistant_message(
            input_text=text,
            tool_calls=tool_calls if isinstance(tool_calls, list) else None,
            reasoning_content=(
                str(reasoning_content)
                if reasoning_content is not None else None
            ),
        )
    else:
        return None

    metadata: Dict[str, Any] = {}
    if kind:
        metadata["kind"] = str(kind)
    context_fingerprints = payload.get("context_fingerprints")
    if isinstance(context_fingerprints, dict):
        # 空字典也是有效基线，表示上一轮显式删除了全部动态 section。
        metadata["context_fingerprints"] = {
            str(name): str(fingerprint)
            for name, fingerprint in context_fingerprints.items()
            if name and fingerprint
        }
    if payload.get("interrupted"):
        metadata["interrupted"] = True
    if metadata:
        msg.metadata = metadata
    return msg


def _messages_from_transcript_item(item: Dict[str, Any]) -> List[Message]:
    """把 transcript.jsonl 的单轮记录还原成 history 消息序列。

    新格式：item["messages"] 是本轮提交到 history 的完整消息列表（含 user、
    assistant 含 tool_calls、role=tool、final assistant）。直接逐条还原即可。

    旧格式（破坏性更新前）会有 user_query/final_answer/work_record 字段——
    这条路径不再支持，旧 session 启动时会被破坏性清理。
    """
    raw_messages = item.get("messages")
    if not isinstance(raw_messages, list):
        return []
    out: List[Message] = []
    for payload in raw_messages:
        msg = _message_payload_to_message(payload)
        if msg is not None:
            out.append(msg)
    return out


def _mark_interrupted_message(message: Message) -> Message:
    """给 active_turn 恢复出的可见消息加本地标记。

    metadata 不会进入 Message.to_dict()，因此不会影响发给模型的 OpenAI 协议；
    它只服务 export_history/UI，让前端可以选择显示“上次中断前恢复”提示。
    """
    metadata = dict(message.metadata or {})
    metadata["interrupted"] = True
    message.metadata = metadata
    return message


def _messages_from_active_turn_events(events: List[Dict[str, Any]]) -> List[Message]:
    """把运行中检查点还原成一段协议合法的 history。

    active_turn.jsonl 记录未完成回合的安全边界：用户输入、assistant 规划出的
    tool_calls、已经完成的 tool 结果，以及已经完整生成的最终回答。恢复工具轨迹
    时最重要的约束是 OpenAI tool calling 协议合法性：每条 role=tool 前面必须
    有声明同一 tool_call_id 的 assistant.tool_calls。因此这里会主动丢弃没有
    完成结果的 tool_call，只恢复“assistant 声明 + 对应 tool 结果”成对出现的部分。
    """
    if not events:
        return []

    # 如果文件里意外出现多段 turn_started（例如开发期手工追加），只采用最后一段。
    # active_turn 的语义是“当前正在执行的一轮”，不能把多个半成品轮次拼在一起。
    start_index = -1
    for idx, event in enumerate(events):
        if isinstance(event, dict) and event.get("type") == "turn_started":
            start_index = idx
    if start_index < 0:
        return []
    scoped_events = events[start_index:]
    started = scoped_events[0]

    out: List[Message] = []
    context_payload = started.get("context_update_payload")
    if isinstance(context_payload, dict):
        context_message = _message_payload_to_message(context_payload)
        if context_message is not None:
            out.append(context_message)

    user_payload = started.get("user_payload")
    if not isinstance(user_payload, dict):
        user_payload = {
            "role": "user",
            "content": str(started.get("user_query") or ""),
        }
    user_message = _message_payload_to_message(user_payload)
    if user_message is None:
        return out
    out.append(_mark_interrupted_message(user_message))

    completed_by_round: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for event in scoped_events[1:]:
        if not isinstance(event, dict) or event.get("type") != "tool_completed":
            continue
        tool_payload = event.get("tool_payload")
        if not isinstance(tool_payload, dict):
            continue
        call_id = str(
            tool_payload.get("tool_call_id")
            or event.get("tool_call_id")
            or ""
        )
        if not call_id:
            continue
        round_key = str(event.get("round_idx") or "")
        restored_tool_payload = deepcopy(tool_payload)
        # 兼容本提交早期格式：错误状态最初只写在事件顶层，没有放进 tool_payload。
        restored_tool_payload["is_error"] = bool(
            restored_tool_payload.get("is_error")
            or event.get("is_error")
        )
        completed_by_round.setdefault(round_key, {})[call_id] = restored_tool_payload

    for event in scoped_events[1:]:
        if not isinstance(event, dict) or event.get("type") != "assistant_tool_calls":
            continue
        assistant_payload = event.get("assistant_payload")
        if not isinstance(assistant_payload, dict):
            continue
        raw_calls = assistant_payload.get("tool_calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            continue
        round_key = str(event.get("round_idx") or "")
        completed = completed_by_round.get(round_key, {})
        filtered_calls: List[Dict[str, Any]] = []
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            if call_id and call_id in completed:
                filtered_calls.append(deepcopy(call))
        if not filtered_calls:
            continue

        # 只把已完成的 call 放回 assistant.tool_calls。这样即使模型一次规划了
        # 多个工具，崩溃时只完成了一部分，恢复后的协议消息仍然完全配对。
        paired_assistant_payload = deepcopy(assistant_payload)
        paired_assistant_payload["tool_calls"] = filtered_calls
        assistant_message = _message_payload_to_message(paired_assistant_payload)
        if assistant_message is None:
            continue
        out.append(_mark_interrupted_message(assistant_message))

        # tool 结果按原 assistant.tool_calls 顺序回放，而不是按完成先后。这样恢复
        # 出来的 history 更贴近 provider 原始协议顺序，也便于后续孤儿清理兜底。
        for call in filtered_calls:
            call_id = str(call.get("id") or "")
            tool_message = _message_payload_to_message(completed.get(call_id, {}))
            if tool_message is not None:
                out.append(_mark_interrupted_message(tool_message))

    # 最终回答是本轮已经完整生成的安全边界。即使 transcript 尚未来得及提交，
    # 重启后也应恢复用户已经看到的回答，而不是只留下一个未回答的 user 消息。
    final_event = next(
        (
            event for event in reversed(scoped_events[1:])
            if isinstance(event, dict) and event.get("type") == "assistant_final"
        ),
        None,
    )
    if isinstance(final_event, dict):
        final_payload = final_event.get("assistant_payload")
        if isinstance(final_payload, dict):
            final_message = _message_payload_to_message(final_payload)
            if final_message is not None:
                out.append(_mark_interrupted_message(final_message))

    return out


def _active_turn_started_event(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """取 active_turn.jsonl 中最后一个 turn_started 事件。"""
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "turn_started":
            return event
    return {}


def _trim_restored_history(messages: List[Message], max_messages: int) -> List[Message]:
    """按恢复窗口裁剪 history，并尽量保留最近一次 compact_boundary。

    普通恢复直接取尾部 max_messages 即可；但 compact 后的第一条消息是
    `【上下文压缩】` boundary，它承担早期上下文摘要的职责。如果后续新消息
    很多，简单尾裁剪会把 boundary 挤掉，导致早期任务状态丢失。因此这里
    优先保留最近一个 compact_boundary，再用剩余窗口装其后的最新消息。

    CC 模式下 history 累积的是原始协议消息(assistant.tool_calls / role=tool),
    任何尾裁剪/anchor+tail 都可能把 assistant.tool_calls 切掉而留下它的 tool
    响应,形成"孤儿 tool"。跨进程恢复后第一轮就把它发给 LLM 会触发 OpenAI
    兼容协议 400。因此截断后统一过一遍孤儿清理——这是 _build_chat_messages
    切片清理之外的第二道保险(防止恢复进内存的 history 本身就不合法)。
    """
    if max_messages <= 0:
        return []
    if len(messages) <= max_messages:
        return drop_orphan_tool_message_objects(messages)

    boundary_idx = None
    for idx, message in enumerate(messages):
        if _message_kind(message) == "compact_boundary":
            boundary_idx = idx

    if boundary_idx is None:
        return drop_orphan_tool_message_objects(messages[-max_messages:])

    anchor = messages[boundary_idx]
    tail_capacity = max_messages - 1
    if tail_capacity <= 0:
        return [anchor]
    trimmed = [anchor] + messages[boundary_idx + 1:][-tail_capacity:]
    return drop_orphan_tool_message_objects(trimmed)


def _extract_tool_call_name(call: Dict[str, Any]) -> str:
    return str((call.get("function") or {}).get("name") or "")


def _extract_tool_call_args(call: Dict[str, Any]) -> Dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments", "{}")
    parsed = _json_loads_maybe(raw)
    return parsed if isinstance(parsed, dict) else {}


def _summarize_arguments(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """生成可落盘的参数摘要。

    这里不能简单保存完整 arguments：
    - file_write.content / file_edit.old_string / file_edit.new_string 可能很长，必须丢弃；
    - bash stdout 不在 arguments 里，但 command 可能很长，也要截断；
    - 其他工具若带 content/stdout/stderr/result 这类高噪声字段，也不落盘。

    这些限制只影响跨轮 trace，不影响本轮真实工具调用。
    """
    if name == "file_write":
        keep = ("path",)
        return {k: _clip(arguments.get(k), 160) for k in keep if k in arguments}
    if name == "file_edit":
        keep = ("path", "replace_all")
        summary = {k: _clip(arguments.get(k), 160) for k in keep if k in arguments}
        if "old_string" in arguments:
            summary["old_string_preview"] = _clip(arguments.get("old_string"), 160)
        if "new_string" in arguments:
            summary["new_string_preview"] = _clip(arguments.get("new_string"), 160)
        return summary
    if name == "file_read":
        keep = (
            "path",
            "head",
            "tail",
            "start_line",
            "end_line",
            "start_char",
            "end_char",
        )
        return {k: _clip(arguments.get(k), 160) for k in keep if k in arguments}
    if name == "bash":
        keep = ("command", "cwd", "timeout", "background")
        return {k: _clip(arguments.get(k), 160) for k in keep if k in arguments}

    noisy_keys = {"content", "stdout", "stderr", "result"}
    return {
        k: _clip(v, 160)
        for k, v in arguments.items()
        if k not in noisy_keys
    }


@dataclass
class TraceEntry:
    """单次工具调用的跨轮摘要。

    TraceEntry 不是 OpenAI Message，也不会作为 role=tool 直接发给模型。
    它是一条"工具事实记录"：工具名、参数摘要、结果摘要、错误标记、
    轮次和少量结构化元数据。这样既能保留"读过哪个文件/跑过什么命令"，
    又避免跨轮携带 tool_call_id 造成协议污染。
    """
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""
    is_error: bool = False
    round_idx: int = 0
    timestamp: str = field(default_factory=_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "result_summary": self.result_summary,
            "is_error": self.is_error,
            "round_idx": self.round_idx,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraceEntry":
        return cls(
            name=str(data.get("name") or ""),
            arguments=data.get("arguments") if isinstance(data.get("arguments"), dict) else {},
            result_summary=str(data.get("result_summary") or ""),
            is_error=bool(data.get("is_error")),
            round_idx=int(data.get("round_idx") or 0),
            timestamp=str(data.get("timestamp") or _now_iso()),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )

    def to_line(self) -> str:
        """把 TraceEntry 渲染成给 summarizer 使用的单行文本。

        content_preview/stdout_preview/stderr_preview 这些字段已经在
        result_summary 中体现，metadata 再输出一次会重复占 token，所以这里过滤。
        """
        parts = [f"round={self.round_idx}", f"tool={self.name}"]
        if self.arguments:
            parts.append(f"args={_safe_json_dumps(self.arguments, 140)}")
        if self.metadata:
            meta = {
                k: v for k, v in self.metadata.items()
                if k not in {"content_preview", "stdout_preview", "stderr_preview"}
            }
            if meta:
                parts.append(f"meta={_safe_json_dumps(meta, 180)}")
        if self.result_summary:
            parts.append(f"result={self.result_summary}")
        if self.is_error:
            parts.append("error=true")
        return "- " + " | ".join(parts)


@dataclass
class WorkRecord:
    """一轮对话结束后提取的结构化工作记录。

    Claude Code 对齐重构后，``text`` 字段保留但不再被注入 history。原始
    assistant.tool_calls / role=tool 消息会按累积模式直接进入下一轮 prompt，
    上一轮工具细节不再依赖文本摘要。

    其他字段（files_seen / files_modified / recent_commands / decisions / pending）
    继续用于更新 state.json，state.json 仍作为独立 user message 注入。这层
    结构化提取与原始消息累积互补，不冲突。
    """
    text: str = ""
    trace_entries: List[TraceEntry] = field(default_factory=list)
    files_seen: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    files_modified: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    recent_commands: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "trace_entries": [e.to_dict() for e in self.trace_entries],
            "files_seen": self.files_seen,
            "files_modified": self.files_modified,
            "recent_commands": self.recent_commands,
            "decisions": self.decisions,
            "pending": self.pending,
        }


class RuleTraceSummarizer:
    """纯规则结构化提取器。

    Claude Code 对齐重构后，这个类不再生成 `【工作记录】` 文本注入 history。
    它只从工具轨迹里抽取结构化字段（files_seen / files_modified /
    recent_commands），用于更新 state.json。state.json 通过独立 user message
    注入下一轮 prompt，与原始消息累积模式互补。

    text 字段保留但置空，是为了保持 WorkRecord 接口稳定（trace_entries 仍可
    被 LocalSessionStore 选择性落盘做审计）。
    """

    def summarize(
        self,
        *,
        user_query: str,
        final_answer: str,
        trace_entries: Sequence[TraceEntry],
    ) -> WorkRecord:
        del user_query, final_answer
        files_seen: Dict[str, Dict[str, Any]] = {}
        files_modified: Dict[str, Dict[str, Any]] = {}
        recent_commands: List[Dict[str, Any]] = []

        for entry in trace_entries:
            meta = entry.metadata
            if entry.name == "file_read":
                path = str(meta.get("path") or entry.arguments.get("path") or "")
                if path:
                    files_seen[path] = {
                        "last_mode": meta.get("mode"),
                        "total_lines": meta.get("total_lines"),
                        "returned_lines": meta.get("returned_lines"),
                        "truncated": meta.get("truncated"),
                        "summary": _clip(meta.get("content_preview") or entry.result_summary, FILE_SUMMARY_LIMIT),
                        "last_seen_at": entry.timestamp,
                    }
                continue

            if entry.name == "file_write":
                path = str(meta.get("path") or entry.arguments.get("path") or "")
                if path:
                    files_modified[path] = {
                        "lines_added": meta.get("lines_added"),
                        "lines_removed": meta.get("lines_removed"),
                        "summary": _clip(entry.result_summary or meta.get("message"), FILE_SUMMARY_LIMIT),
                        "last_modified_at": entry.timestamp,
                    }
                continue

            if entry.name in {"bash", "bash_task"}:
                command = str(meta.get("command") or entry.arguments.get("command") or "")
                if not command and entry.name == "bash_task":
                    command = f"bash_task {entry.arguments.get('action', '')}".strip()
                item = {
                    "command": _clip(command, 180),
                    "cwd": meta.get("cwd"),
                    "exit_code": meta.get("exit_code"),
                    "summary": _clip(entry.result_summary, TRACE_RESULT_LIMIT),
                    "ts": entry.timestamp,
                }
                if meta.get("output_file"):
                    item["output_file"] = meta.get("output_file")
                recent_commands.append(item)
                continue

        if not trace_entries:
            return WorkRecord(text="", trace_entries=[])

        return WorkRecord(
            text="",
            trace_entries=list(trace_entries),
            files_seen=files_seen,
            files_modified=files_modified,
            recent_commands=recent_commands[-RECENT_COMMANDS_LIMIT:],
        )


class TraceSummarizer:
    """LLM 静默工作记录压缩器（已退役）。

    Claude Code 对齐重构后，跨轮 history 直接累积原始 tool_use + tool_result
    消息，不再注入【工作记录】文本。这个类只剩下"返回结构化字段"的语义，
    实现上等同 RuleTraceSummarizer。

    保留类名是为了兼容 run_agent.py 的现有装配代码——外部依赖注入还是会
    传 llm 进来，构造器忽略它即可，未来可彻底删除这个类。
    """

    def __init__(
        self,
        llm: Optional[Any] = None,
        fallback: Optional[RuleTraceSummarizer] = None,
    ) -> None:
        del llm  # 不再使用 LLM 静默调用
        self.fallback = fallback or RuleTraceSummarizer()

    def summarize(
        self,
        *,
        user_query: str,
        final_answer: str,
        trace_entries: Sequence[TraceEntry],
    ) -> WorkRecord:
        return self.fallback.summarize(
            user_query=user_query,
            final_answer=final_answer,
            trace_entries=trace_entries,
        )


class TraceCollector:
    """一次 chat 内的压缩工具轨迹收集器。

    AgentSession._tool_loop 会同时维护两份数据：
    - ``messages``：完整协议消息，用于本轮继续 tool calling；
    - ``TraceCollector.entries``：压缩后的工作事实，用于跨轮 history/落盘。

    Collector 生命周期只覆盖一次 chat，结束后由 AgentSession 把它总结成
    WorkRecord。
    """

    def __init__(self, result_limit: int = TRACE_RESULT_LIMIT) -> None:
        self.result_limit = result_limit
        self.entries: List[TraceEntry] = []

    def add_tool_result(
        self,
        *,
        call: Dict[str, Any],
        name: str,
        result: Any,
        is_error: bool,
        round_idx: int,
    ) -> TraceEntry:
        # call 里有模型请求工具时的原始 arguments；exec_result 里有实际工具名、
        # 结果和错误标记。两者合并后才能得到一条完整 trace。
        arguments = _extract_tool_call_args(call)
        entry = trace_entry_from_tool_result(
            name=name or _extract_tool_call_name(call),
            arguments=arguments,
            result=result,
            is_error=is_error,
            round_idx=round_idx,
            result_limit=self.result_limit,
        )
        self.entries.append(entry)
        return entry

    def as_text(self) -> str:
        return "\n".join(e.to_line() for e in self.entries)

    def needs_summary(self) -> bool:
        # 同时用字符数和 token 数判断是否需要 LLM 压缩。字符数便宜直观，
        # token 数更贴近实际上下文预算。
        text = self.as_text()
        return len(text) > TRACE_SUMMARIZE_CHARS or count_tokens(text) > TRACE_SUMMARIZE_TOKENS


def trace_entry_from_tool_result(
    *,
    name: str,
    arguments: Dict[str, Any],
    result: Any,
    is_error: bool,
    round_idx: int,
    result_limit: int = TRACE_RESULT_LIMIT,
) -> TraceEntry:
    """把一次工具执行结果转换成可跨轮保存的 TraceEntry。

    这里是安全边界最重要的一层：
    - 可以解析 JSON 时，按工具类型白名单提取字段；
    - 不认识的工具走通用摘要，但仍过滤 content/stdout/stderr/result 等大字段；
    - 无论工具返回多大，result_summary 都会被裁到 result_limit。

    注意：这不改变 ``messages`` 里的 tool content。模型在本轮仍能看到完整
    工具输出；只有跨轮 trace 被压缩。
    """
    parsed = _json_loads_maybe(result)
    metadata: Dict[str, Any] = {}
    summary = ""

    if isinstance(parsed, dict):
        if name == "file_read":
            # file_read 的 content 是最容易膨胀的字段，所以只取短预览。
            # path/mode/line 信息比正文更适合长期保存。
            content_preview = _clip(parsed.get("content"), result_limit)
            metadata = {
                "path": parsed.get("path"),
                "mode": parsed.get("mode"),
                "total_lines": parsed.get("total_lines"),
                "returned_lines": parsed.get("returned_lines"),
                "truncated": parsed.get("truncated"),
                "content_preview": content_preview,
            }
            error = parsed.get("error")
            summary = _clip(error or content_preview, result_limit)
            is_error = is_error or bool(error)
        elif name in {"file_write", "file_edit"}:
            # file_write/file_edit 不保存写入内容，只保存结果状态和行数变化。
            metadata = {
                "path": parsed.get("path"),
                "ok": parsed.get("ok"),
                "type": parsed.get("type"),
                "bytes_written": parsed.get("bytes_written"),
                "lines_added": parsed.get("lines_added"),
                "lines_removed": parsed.get("lines_removed"),
                "message": parsed.get("message"),
            }
            if name == "file_edit":
                metadata["replacements"] = parsed.get("replacements")
                metadata["replace_all"] = parsed.get("replace_all")
            summary = _clip(parsed.get("error") or parsed.get("message") or parsed, result_limit)
            is_error = is_error or bool(parsed.get("error")) or parsed.get("ok") is False
        elif name == "bash":
            # BashTool 已经对超大输出做过落盘处理；这里再压一层，只保留
            # stdout/stderr 预览和 output_file 引用。
            stdout_preview = _clip(parsed.get("stdout"), result_limit)
            stderr_preview = _clip(parsed.get("stderr"), result_limit)
            metadata = {
                "command": arguments.get("command"),
                "cwd": parsed.get("cwd") or arguments.get("cwd"),
                "exit_code": parsed.get("exit_code"),
                "output_file": parsed.get("output_file"),
                "stdout_preview": stdout_preview,
                "stderr_preview": stderr_preview,
            }
            summary = _clip(
                parsed.get("error") or stderr_preview or stdout_preview or parsed.get("output_file"),
                result_limit,
            )
            is_error = is_error or bool(parsed.get("error")) or parsed.get("exit_code") not in (0, None)
        elif name == "bash_task":
            # bash_task 既可能返回任务状态，也可能返回后台任务输出。
            # 两种情况都统一成 task_id/status/output_file/content_preview。
            task = parsed.get("task") if isinstance(parsed.get("task"), dict) else {}
            content_preview = _clip(parsed.get("content") or parsed.get("stdout"), result_limit)
            metadata = {
                "command": task.get("command") or arguments.get("command"),
                "cwd": task.get("cwd"),
                "exit_code": task.get("exit_code") or parsed.get("exit_code"),
                "output_file": task.get("output_path") or parsed.get("output_file"),
                "content_preview": content_preview,
                "task_id": task.get("id") or arguments.get("task_id"),
                "status": task.get("status") or parsed.get("status"),
            }
            summary = _clip(parsed.get("error") or content_preview or task or parsed, result_limit)
            is_error = is_error or bool(parsed.get("error"))
        else:
            # 其他工具无法逐个建模，采用保守策略：结构化 metadata 去掉大字段，
            # result_summary 从 error/summary/message/result/content/stdout 里择一。
            metadata = {
                k: v for k, v in parsed.items()
                if k not in {"content", "stdout", "stderr", "result"}
            }
            summary_source = (
                parsed.get("error")
                or parsed.get("summary")
                or parsed.get("message")
                or parsed.get("result")
                or parsed.get("content")
                or parsed.get("stdout")
                or parsed
            )
            summary = _clip(summary_source, result_limit)
            is_error = is_error or bool(parsed.get("error"))
    else:
        summary = _clip(parsed, result_limit)

    return TraceEntry(
        name=name,
        arguments=_summarize_arguments(name, arguments),
        result_summary=summary,
        is_error=bool(is_error),
        round_idx=round_idx,
        metadata={k: v for k, v in metadata.items() if v not in (None, "")},
    )


class LocalSessionStore:
    """项目级本地会话存储。

    默认目录是 ``./.cbagent/sessions``。选择项目级而不是用户级，是因为这些
    状态强依赖当前仓库：读过哪些文件、改过哪些文件、命令 cwd、任务摘要等。

    文件结构：
    - index.json：只保存 active_session_id，用于启动时自动恢复最近会话；
    - <session_id>/transcript.jsonl：每轮 user/final/work_record/trace_entries；
    - <session_id>/compact.json：最近一次 /compact 的恢复锚点；
    - <session_id>/compactions.jsonl：每次 /compact 的审计记录；
    - <session_id>/state.json：滚动摘要、已读文件、已改文件、最近命令等。

    多会话隔离的关键点也在这里：同一时刻只有一个 active session，但每个
    session 都拥有独立目录。切换会话只会改 index 指针并重载该目录的 state；
    不会把 A 会话的 transcript/state 合并进 B 会话。

    Store 被 AgentSession 以依赖注入方式使用；单测不传 store 时完全不落盘。
    """

    def __init__(
        self,
        root: Optional[Path | str] = None,
        *,
        persist_trace_entries: bool = True,
    ) -> None:
        self.root = Path(root or Path.cwd() / ".cbagent" / "sessions")
        self.index_path = self.root / "index.json"
        self.active_session_id: Optional[str] = None
        self.state: Dict[str, Any] = {}
        # work_record 是已经压缩过的跨轮工作事实，应该继续落盘和恢复；trace_entries
        # 是逐工具明细，QQ/微信这类通讯平台可关闭它，避免 transcript 长期膨胀。
        self.persist_trace_entries = bool(persist_trace_entries)
        self._load_or_create()

    @property
    def active_dir(self) -> Path:
        """当前 active session 的目录。

        clear_active_session() 后 active_session_id 会被置空；此时访问 active_dir
        是调用方错误。写入路径会先走 ensure_active() 自动创建新 session。
        """
        if not self.active_session_id:
            raise RuntimeError("active_session_id is not set")
        return self.root / self.active_session_id

    def current_session_summary(self) -> Optional[Dict[str, Any]]:
        """返回当前 active session 的摘要；没有 active 时返回 None。

        摘要是给前端/RPC 展示用的轻量对象，不包含 transcript 正文，
        也不包含完整工具输出。这样前端可以安全列出会话，而不会把历史文件内容
        一股脑重新读进 UI 或网络协议。
        """
        if not self.active_session_id or not self._is_valid_session_id(self.active_session_id):
            return None
        target = self.root / self.active_session_id
        if not target.exists():
            return None
        return self._session_summary_from_dir(target)

    def ensure_active(self) -> None:
        """确保有可写的 active session。

        /clear 会删除当前 session 并清掉 index。如果用户不重启、直接继续聊天，
        下一轮 append_turn/save_state 需要自动创建一个新 session，否则会因为
        active_session_id=None 写不进去。
        """
        if (
            self.active_session_id
            and self._is_valid_session_id(self.active_session_id)
            and (self.root / self.active_session_id).exists()
        ):
            return
        self._load_or_create()

    def _load_or_create(self) -> None:
        """加载最近 session；不存在或损坏时创建新 session。

        这里遵循 index.json 的 active 指针。没有 active 指针时创建空白新会话，
        而不是自动挑一个旧目录恢复；这样 /clear 删除 active 后，重启不会偷偷
        回到其它旧会话。旧会话仍可通过 list/switch 显式找回。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        index = self._read_json(self.index_path, {})
        active = index.get("active_session_id") if isinstance(index, dict) else None
        if (
            isinstance(active, str)
            and self._is_valid_session_id(active)
            and (self.root / active).exists()
        ):
            self.active_session_id = active
            self.state = self._read_json(self.active_dir / "state.json", {})
            return
        self.create_session()

    def list_sessions(self) -> List[Dict[str, Any]]:
        """列出当前项目下的所有本地会话摘要。

        只扫描 ``root/session_*`` 子目录，并且只读取每个目录里的 ``state.json``
        与 transcript 行数。这里不读取 transcript 的正文，是为了避免 TUI 打开
        会话列表时把大量历史内容加载进内存，也避免把旧工具摘要误注入当前上下文。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        sessions: List[Dict[str, Any]] = []
        for child in self.root.iterdir():
            if not child.is_dir() or not self._is_valid_session_id(child.name):
                continue
            sessions.append(self._session_summary_from_dir(child))
        sessions.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        return sessions

    def create_session(self) -> Dict[str, Any]:
        """创建一个新的空白会话并立即设为 active。

        新会话不会继承旧会话的 history/state。AgentSession 调用本方法后会同步
        清空内存 history，从而保证下一轮 prompt 只看到这个新会话自己的上下文。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self.active_session_id = self._new_session_id()
        self.state = self._new_state()
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(self.active_dir / "state.json", self.state)
        self._write_json(self.active_dir / "usage.json", self._empty_usage())
        self._write_index()
        return self._session_summary_from_dir(self.active_dir)

    @staticmethod
    def _empty_usage() -> Dict[str, Any]:
        """返回新会话的累计用量初始值。"""
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_prompt_tokens": 0,
            "cache_miss_tokens": 0,
            "requests": 0,
            "updated_at": _now_iso(),
        }

    def load_usage(self) -> Dict[str, Any]:
        """读取当前会话累计用量；旧会话缺文件时按零值兼容。"""
        if not self.active_session_id:
            return self._empty_usage()
        raw = self._read_json(self.active_dir / "usage.json", {})
        usage = self._empty_usage()
        if isinstance(raw, dict):
            for key in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens", "cache_miss_tokens", "requests"):
                usage[key] = max(0, int(raw.get(key) or 0))
            usage["updated_at"] = str(raw.get("updated_at") or usage["updated_at"])
        return usage

    def add_token_usage(self, event: Any) -> Dict[str, Any]:
        """把一次 provider usage 原子累加到当前会话。"""
        self.ensure_active()
        usage = self.load_usage()
        prompt = max(0, int(getattr(event, "prompt_tokens", 0) or 0))
        completion = max(0, int(getattr(event, "completion_tokens", 0) or 0))
        cached = max(0, int(
            getattr(event, "cached_prompt_tokens", None)
            or getattr(event, "prompt_cache_hit_tokens", None)
            or 0
        ))
        explicit_miss = getattr(event, "prompt_cache_miss_tokens", None)
        miss = max(0, int(explicit_miss if explicit_miss is not None else max(0, prompt - cached)))
        usage["prompt_tokens"] += prompt
        usage["completion_tokens"] += completion
        usage["cached_prompt_tokens"] += min(cached, prompt) if prompt else cached
        usage["cache_miss_tokens"] += miss
        usage["requests"] += 1
        usage["updated_at"] = _now_iso()
        self._write_json(self.active_dir / "usage.json", usage)
        return usage

    def load_token_calibration(self, key: str) -> Optional[float]:
        """读取项目级 provider/model 估算校准系数。"""
        raw = self._read_json(self.root / "token-calibration.json", {})
        item = raw.get(key) if isinstance(raw, dict) else None
        try:
            return float((item or {}).get("ratio")) if isinstance(item, dict) else None
        except (TypeError, ValueError):
            return None

    def save_token_calibration(self, key: str, ratio: float, samples: int) -> None:
        """持久化项目级 provider/model 估算校准系数。"""
        path = self.root / "token-calibration.json"
        raw = self._read_json(path, {})
        data = raw if isinstance(raw, dict) else {}
        data[key] = {
            "ratio": round(float(ratio), 6),
            "samples": max(1, int(samples)),
            "updated_at": _now_iso(),
        }
        self._write_json(path, data)

    def switch_session(self, session_id: str) -> Dict[str, Any]:
        """切换 active session，并返回切换后的摘要。

        ``session_id`` 只能是本 store 自己创建的目录名形式，不能包含路径分隔符
        或 ``..``。这一步很重要：会话切换接口会读写 state/transcript，如果不做
        白名单校验，就可能被误用成任意路径访问。
        """
        if not self._is_valid_session_id(session_id):
            raise ValueError(f"invalid session_id: {session_id!r}")
        target = self.root / session_id
        if not target.exists() or not target.is_dir():
            raise ValueError(f"session not found: {session_id}")

        self.active_session_id = session_id
        state = self._read_json(target / "state.json", {})
        if not isinstance(state, dict) or not state:
            # 兼容极少数只有 transcript、没有 state 的旧/损坏目录：补一份空 state，
            # 但不合并其它会话内容，仍然保持这个目录自己的隔离边界。
            state = self._new_state()
        state["session_id"] = session_id
        state.setdefault("created_at", self._mtime_iso(target))
        state["updated_at"] = state.get("updated_at") or self._mtime_iso(target)
        self.state = state
        self._write_json(target / "state.json", self.state)
        self._write_index()
        return self._session_summary_from_dir(target)

    def _new_session_id(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"session_{stamp}_{uuid.uuid4().hex[:8]}"

    def _new_state(self) -> Dict[str, Any]:
        """创建 state.json 的初始结构。

        字段设计保持简单 JSON，避免引入 pydantic 或迁移系统。后续新增字段时，
        旧 state 缺字段也能通过 state.get()/setdefault() 兼容。
        """
        now = _now_iso()
        return {
            "session_id": self.active_session_id,
            "project_root": str(self.root.parent.parent.resolve()),
            "created_at": now,
            "updated_at": now,
            "turn_count": 0,
            "rolling_summary": "",
            "active_task": "",
            "files_seen": {},
            "files_modified": {},
            "recent_commands": [],
            "decisions": [],
            "pending": [],
        }

    def load_latest_history(self, max_messages: Optional[int] = None) -> List[Message]:
        """从 transcript 恢复最近 history（CC 模式：raw messages 累积）。

        新格式：每条 transcript 记录的 ``messages`` 字段是本轮 commit 到 history
        的完整消息列表，含 user / assistant(可带 tool_calls) / role=tool / final
        assistant。恢复时按顺序还原即可，模型在新一轮就能看到上一轮真实工具
        调用细节。

        执行过 /compact 的会话仍然优先读 compact.json：里面的 ``history``
        字段保存了 compact 时刻的 boundary 之后消息（含 boundary system 消息
        本身）。transcript_offset 之后的新轮再追加。

        旧格式（user_query/final_answer/work_record 三段式）已不再支持；
        破坏性更新已确认，旧目录在启动期清空。
        """
        if not self.active_session_id:
            return []

        transcript_items = self._read_transcript_items(self.active_dir)
        compact = self._read_json(self.active_dir / "compact.json", {})
        messages: List[Message] = []
        committed_turn_ids = {
            str(item.get("turn_id"))
            for item in transcript_items
            if isinstance(item, dict) and item.get("turn_id")
        }

        start_idx = 0
        if isinstance(compact, dict) and compact:
            raw_history = compact.get("history")
            if isinstance(raw_history, list):
                for payload in raw_history:
                    msg = _message_payload_to_message(payload)
                    if msg is not None:
                        messages.append(msg)
            start_idx = int(compact.get("transcript_offset") or 0)

        for item in transcript_items[max(0, start_idx):]:
            messages.extend(_messages_from_transcript_item(item))

        # active_turn.jsonl 是 pending_user.json 的增强版：它除了用户输入，还能
        # 保存已经完成的工具配对。active 与 pending 属于同一 turn_id 时只恢复
        # active；属于不同 turn_id 时二者是连续两轮，必须按“旧 active、新 pending”
        # 的顺序同时恢复，不能用新输入覆盖尚未归档的中断轮。
        active_events = self._read_active_turn_events(self.active_dir)
        active_started = _active_turn_started_event(active_events)
        active_turn_id = str(active_started.get("turn_id") or "")
        active_messages = _messages_from_active_turn_events(active_events)
        pending = self._read_json(self.active_dir / "pending_user.json", {})
        pending_turn_id = str(pending.get("turn_id") or "") if isinstance(pending, dict) else ""
        if pending_turn_id and pending_turn_id in committed_turn_ids:
            pending = {}
            pending_turn_id = ""
        if active_turn_id and active_turn_id in committed_turn_ids:
            # transcript 已经包含这一轮时，active_turn 只是“提交后尚未来得及删除”的
            # 残留文件。恢复时必须跳过，否则同一轮会被展示/注入两次。
            active_messages = []
        if active_messages:
            messages.extend(active_messages)
        # 如果 pending 与 active 是同一轮，active 已经包含 user，不能重复追加。
        # turn_id 不同则 pending 是用户在恢复后发出的下一条消息，必须放在旧中断轮后。
        if (
            isinstance(pending, dict)
            and pending.get("user_query")
            and (not active_messages or pending_turn_id != active_turn_id)
        ):
            messages.append(Message.create_user_message(str(pending.get("user_query") or "")))

        if not messages:
            return []
        if max_messages is None:
            return drop_orphan_tool_message_objects(messages)
        return _trim_restored_history(messages, max_messages)

    @staticmethod
    def _new_turn_id() -> str:
        """生成本地回合 id。

        turn_id 只用于 pending/active/transcript 之间做崩溃恢复去重，不暴露给模型。
        """
        return f"turn_{uuid.uuid4().hex}"

    def _pending_turn_id_for_user(self, user_query: str) -> str:
        """如果 pending_user.json 属于同一条用户输入，返回它的 turn_id。"""
        if not self.active_session_id:
            return ""
        pending = self._read_json(self.active_dir / "pending_user.json", {})
        if not isinstance(pending, dict):
            return ""
        if str(pending.get("user_query") or "") != user_query:
            return ""
        return str(pending.get("turn_id") or "")

    def _active_turn_id_for_user(self, user_query: str) -> str:
        """如果 active_turn.jsonl 属于同一条用户输入，返回它的 turn_id。"""
        if not self.active_session_id:
            return ""
        started = _active_turn_started_event(self._read_active_turn_events(self.active_dir))
        if str(started.get("user_query") or "") != user_query:
            return ""
        return str(started.get("turn_id") or "")

    def begin_active_turn(
        self,
        *,
        user_query: str,
        turn_id: Optional[str] = None,
        context_update_message: Optional[Message] = None,
    ) -> None:
        """开始记录当前运行中的回合。

        active_turn.jsonl 的语义是“当前未完成的一轮”，所以新一轮开始时会重置
        旧文件，只写入第一条 turn_started。后续 assistant_tool_calls、
        tool_completed 和 assistant_final 仍然按 JSONL 追加，保证每个可恢复
        边界都能及时写入文件对象。
        """

        self.ensure_active()
        self.active_dir.mkdir(parents=True, exist_ok=True)
        resolved_turn_id = (
            turn_id
            or self._pending_turn_id_for_user(user_query)
            or self._new_turn_id()
        )
        # 恢复后的中断轮可能仍只存在于 active_turn.jsonl。开始下一轮前必须先把
        # 它归档进 transcript；直接用 "w" 覆盖会让用户输入和已完成工具永久丢失。
        self._archive_replaced_active_turn(next_turn_id=resolved_turn_id)
        event: Dict[str, Any] = {
            "type": "turn_started",
            "ts": _now_iso(),
            "session_id": self.active_session_id,
            "turn_id": resolved_turn_id,
            "user_query": user_query,
            "user_payload": _message_to_persist_payload(
                Message(role=MessageRole.USER, content=user_query),
            ),
        }
        if context_update_message is not None:
            event["context_update_payload"] = _message_to_persist_payload(context_update_message)
        path = self.active_dir / "active_turn.jsonl"
        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            f.flush()

    def _archive_replaced_active_turn(self, *, next_turn_id: str) -> None:
        """把即将被新回合替换的运行中检查点归档到 transcript。

        写入顺序刻意采用“先追加 transcript、再删除 active”：如果进程在两步
        之间退出，load_latest_history 会按 turn_id 去重；如果追加尚未成功，旧
        active 仍然保留，不会因为启动新回合而丢失。
        """

        events = self._read_active_turn_events(self.active_dir)
        started = _active_turn_started_event(events)
        active_turn_id = str(started.get("turn_id") or "")
        if not active_turn_id or active_turn_id == str(next_turn_id or ""):
            return

        transcript_items = self._read_transcript_items(self.active_dir)
        committed_turn_ids = {
            str(item.get("turn_id"))
            for item in transcript_items
            if isinstance(item, dict) and item.get("turn_id")
        }
        if active_turn_id in committed_turn_ids:
            self.clear_active_turn()
            return

        committed_messages = _messages_from_active_turn_events(events)
        if not committed_messages:
            # 无法还原的损坏检查点不能静默覆盖，让调用方保留 pending 并停止换轮。
            raise RuntimeError("无法归档旧 active turn：检查点中没有可恢复消息")

        user_query = str(started.get("user_query") or "")
        final_answer = ""
        for message in reversed(committed_messages):
            role = message.role.value if hasattr(message.role, "value") else str(message.role)
            if role == "assistant" and not message.tool_calls and message.content:
                final_answer = str(message.content)
                break

        item = {
            "ts": _now_iso(),
            "turn_id": active_turn_id,
            "user_query": user_query,
            "final_answer": final_answer,
            "messages": [_message_to_persist_payload(m) for m in committed_messages],
            "trace_entries": [],
            "interrupted": True,
        }
        with (self.active_dir / "transcript.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            f.flush()

        self._bump_turn(user_query=user_query)
        self.save_state(self.state)
        self._write_index()
        self.clear_active_turn()

    def record_active_assistant_tool_calls(
        self,
        *,
        round_idx: int,
        assistant_message: Message,
    ) -> None:
        """记录模型已经完整规划出的 assistant.tool_calls。

        这里只记录“规划完成”的 assistant 消息，不记录流式文本增量。真正恢复时
        还会按已完成 tool_result 过滤 tool_calls，避免留下“有声明无结果”的半截
        工具调用。
        """

        payload = _message_to_persist_payload(assistant_message)
        if not payload.get("tool_calls"):
            return
        self._append_active_turn_event({
            "type": "assistant_tool_calls",
            "round_idx": int(round_idx),
            "assistant_payload": payload,
        })

    def record_active_assistant_final(
        self,
        *,
        round_idx: int,
        assistant_message: Message,
    ) -> None:
        """记录已经完整生成、但尚未提交进 transcript 的最终回答。"""

        payload = _message_to_persist_payload(assistant_message)
        if not str(payload.get("content") or ""):
            return
        self._append_active_turn_event({
            "type": "assistant_final",
            "round_idx": int(round_idx),
            "assistant_payload": payload,
        })

    def record_active_tool_completed(
        self,
        *,
        round_idx: int,
        tool_message: Message,
        is_error: bool = False,
    ) -> None:
        """记录一个已经完成并回灌到 messages 的工具结果。

        这条事件是恢复边界：只有写到这里的工具，重启后才会重新进入 history。
        如果进程正在执行工具但尚未写入本事件，恢复时会按用户要求直接丢弃。
        """

        payload = _message_to_persist_payload(tool_message)
        call_id = str(payload.get("tool_call_id") or "")
        if not call_id:
            return
        self._append_active_turn_event({
            "type": "tool_completed",
            "round_idx": int(round_idx),
            "tool_call_id": call_id,
            "tool_name": str(payload.get("tool_name") or ""),
            "is_error": bool(is_error),
            "tool_payload": payload,
        })

    def clear_active_turn(self) -> None:
        """清理当前运行中回合检查点。"""

        if not self.active_session_id:
            return
        path = self.active_dir / "active_turn.jsonl"
        try:
            if path.exists():
                path.unlink()
        except Exception:
            logger.exception("清理 active_turn.jsonl 失败")

    def _append_active_turn_event(self, event: Dict[str, Any]) -> None:
        """向 active_turn.jsonl 追加一条事件并 flush。

        这里不 fsync，保持和 transcript 当前写入成本一致；但每条工具完成事件都会
        立即 flush 到 Python 文件对象，尽量缩小异常退出时丢失的窗口。
        """

        self.ensure_active()
        self.active_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(event)
        payload.setdefault("ts", _now_iso())
        payload.setdefault("session_id", self.active_session_id)
        with (self.active_dir / "active_turn.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            f.flush()

    def _read_active_turn_events(self, session_dir: Path) -> List[Dict[str, Any]]:
        """读取运行中检查点 JSONL，坏行直接跳过。"""

        path = session_dir / "active_turn.jsonl"
        if not path.exists():
            return []
        events: List[Dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    logger.warning("跳过损坏的 active_turn.jsonl 行: %s", path)
                    continue
                if isinstance(item, dict):
                    events.append(item)
        except Exception:
            logger.exception("failed to load active turn from %s", path)
            return []
        return events

    def save_pending_user_message(self, user_query: str, *, turn_id: Optional[str] = None) -> None:
        """收到用户消息后先写一份 pending 记录。

        transcript.jsonl 仍只记录完整回合；pending_user.json 只用于崩溃恢复。这样可以
        满足通讯平台“每条私聊消息先落盘”的需求，同时避免正常完成时 transcript 出现
        一条 user-only 记录和一条完整记录的重复。
        """

        self.ensure_active()
        self.active_dir.mkdir(parents=True, exist_ok=True)
        resolved_turn_id = turn_id or self._new_turn_id()
        payload = {
            "ts": _now_iso(),
            "session_id": self.active_session_id,
            "turn_id": resolved_turn_id,
            "user_query": user_query,
        }
        self._write_json(self.active_dir / "pending_user.json", payload)
        # 这里只保存最新输入，不清理旧 active。下一轮 begin_active_turn 会先归档
        # 旧中断轮再创建新检查点；若进程在此刻退出，恢复逻辑会同时展示两轮。

    def clear_pending_user_message(self) -> None:
        """清理当前会话的 pending 用户消息。"""

        if not self.active_session_id:
            return
        path = self.active_dir / "pending_user.json"
        try:
            if path.exists():
                path.unlink()
        except Exception:
            logger.exception("清理 pending 用户消息失败")

    def save_compaction(
        self,
        *,
        summary: str,
        history_payload: List[Dict[str, Any]],
        before_messages: int,
        after_messages: int,
    ) -> Dict[str, Any]:
        """保存当前会话的 /compact 快照，并更新滚动 state。

        注意这里不删除、不重写 transcript.jsonl。compact 的核心语义是“以后恢复
        时从这个快照开始”，不是“销毁旧审计”。因此 compact.json 记录压缩发生时
        transcript 已有多少行；load_latest_history() 只会补这个 offset 之后的新
        行，旧行仍留给人工审计和排障。
        """
        self.ensure_active()
        self.active_dir.mkdir(parents=True, exist_ok=True)

        compact_path = self.active_dir / "compact.json"
        compactions_path = self.active_dir / "compactions.jsonl"
        state_path = self.active_dir / "state.json"
        index_path = self.index_path
        paths_to_restore = [compact_path, compactions_path, state_path, index_path]
        file_snapshots = {
            path: path.read_text(encoding="utf-8") if path.exists() else None
            for path in paths_to_restore
        }
        state_snapshot = deepcopy(self.state)

        ts = _now_iso()
        compact = {
            "ts": ts,
            "session_id": self.active_session_id,
            # summary 已由 AgentSession 按 8K token 上限裁剪，这里必须完整保存。
            # compact.json 是恢复锚点，不能再做字符级二次截断。
            "summary": str(summary or ""),
            "transcript_offset": self._count_transcript_turns(self.active_dir),
            "history": history_payload,
            "before_messages": before_messages,
            "after_messages": after_messages,
        }
        try:
            self._write_json(compact_path, compact)

            # compactions.jsonl 是审计流：保留每次 compact 的时间、数量变化和摘要。
            # 它不像 compact.json 那样只保存最新快照；这样将来排查“什么时候压缩过”
            # 时，不需要翻 Git 或猜测 state 的 updated_at。
            with compactions_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(compact, ensure_ascii=False, default=str) + "\n")

            state = self.state if isinstance(self.state, dict) else self._new_state()
            state["updated_at"] = ts
            # 完整摘要通过 compact.json/history 的 boundary 恢复；state 只保留一份
            # 短审计预览，避免 SessionState 在下一轮重复注入完整摘要。
            state["last_compact_summary"] = _clip(summary, ROLLING_SUMMARY_LIMIT)
            state["rolling_summary"] = ""
            state["compacted_at"] = ts
            state["compact_count"] = int(state.get("compact_count") or 0) + 1
            state["compact_transcript_offset"] = compact["transcript_offset"]
            state["files_seen"] = _tail_mapping(state.get("files_seen"), FILES_SEEN_LIMIT)
            state["files_modified"] = _tail_mapping(state.get("files_modified"), FILES_MODIFIED_LIMIT)
            state["recent_commands"] = _tail_list(state.get("recent_commands"), RECENT_COMMANDS_LIMIT)
            state["decisions"] = _tail_list(state.get("decisions"), DECISIONS_LIMIT)
            state["pending"] = _tail_list(state.get("pending"), PENDING_LIMIT)
            self.save_state(state)
            self._write_index()
            return compact
        except Exception:
            self.state = state_snapshot if isinstance(state_snapshot, dict) else {}
            for path, text in file_snapshots.items():
                try:
                    if text is None:
                        if path.exists():
                            path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text, encoding="utf-8")
                except Exception:
                    logger.exception("failed to roll back compact snapshot file %s", path)
            raise

    def align_compaction_transcript_offset(
        self,
        *,
        history_payload: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """把最近 compact 锚点推进到当前 transcript 末尾并刷新 replacement history。

        mid-turn compact 发生时本轮 transcript 尚未提交；回合提交成功后必须推进
        offset，否则下次恢复会把已经包含在 replacement history 的本轮再追加一次。
        """
        if not self.active_session_id:
            return
        path = self.active_dir / "compact.json"
        compact = self._read_json(path, {})
        if not isinstance(compact, dict) or not compact:
            return
        compact["transcript_offset"] = self._count_transcript_turns(self.active_dir)
        if history_payload is not None:
            compact["history"] = history_payload
            compact["after_messages"] = len(history_payload)
        self._write_json(path, compact)

    def _read_transcript_items(self, session_dir: Path) -> List[Dict[str, Any]]:
        """读取 transcript.jsonl 为结构化行列表。

        这个 helper 只返回 JSON object 行，坏行会触发异常日志并让整次读取失败
        为空列表。保持保守语义的原因是：如果 transcript 损坏，宁愿少恢复上下文，
        也不要把半截 JSON 或错误数据伪装成可用 history 注入模型。
        """
        path = session_dir / "transcript.jsonl"
        if not path.exists():
            return []
        items: List[Dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if isinstance(item, dict):
                    items.append(item)
        except Exception:
            logger.exception("failed to load transcript from %s", path)
            return []
        return items

    def state_text(self) -> str:
        """把 state.json 渲染成可注入 ContextBuilder 的 P1 State 文本。

        state_text 是高优先级上下文，所以必须短而密：滚动摘要、最近看过/改过
        的文件、最近命令和待办。这里不输出 transcript 的逐轮细节。
        """
        state = self.state or {}
        parts: List[str] = []
        summary = _clip(state.get("rolling_summary"), ROLLING_SUMMARY_LIMIT)
        if summary:
            parts.append(summary)
        files_seen = state.get("files_seen") if isinstance(state.get("files_seen"), dict) else {}
        if files_seen:
            parts.append("已查看文件：")
            for path, info in list(files_seen.items())[-12:]:
                if isinstance(info, dict):
                    parts.append(f"- {path}: {_clip(info.get('summary'), 160)}")
        files_modified = state.get("files_modified") if isinstance(state.get("files_modified"), dict) else {}
        if files_modified:
            parts.append("已修改文件：")
            for path, info in list(files_modified.items())[-8:]:
                if isinstance(info, dict):
                    parts.append(
                        f"- {path}: +{info.get('lines_added')}/-{info.get('lines_removed')} "
                        f"{_clip(info.get('summary'), 120)}"
                    )
        commands = state.get("recent_commands") if isinstance(state.get("recent_commands"), list) else []
        if commands:
            parts.append("最近命令：")
            for cmd in commands[-5:]:
                if isinstance(cmd, dict):
                    parts.append(
                        f"- {cmd.get('command')} (exit={cmd.get('exit_code')}, cwd={cmd.get('cwd')}): "
                        f"{_clip(cmd.get('summary'), 100)}"
                    )
        pending = state.get("pending") if isinstance(state.get("pending"), list) else []
        if pending:
            parts.append("待办/阻塞：" + "；".join(_clip(x, 80) for x in pending[-5:]))
        return "\n".join(p for p in parts if p)

    def append_turn(
        self,
        *,
        user_query: str,
        final_answer: str,
        committed_messages: List[Message],
        work_record: Optional[WorkRecord] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        """追加一轮 transcript，并同步更新 state.json。

        新格式（CC 对齐）：transcript 行包含本轮提交进 history 的完整 messages
        序列（user / assistant 含 tool_calls / role=tool / final assistant），
        恢复时直接逐条还原即可。这意味着 transcript 行的体积会比旧格式大，但
        换来的是跨进程恢复时模型仍能看到原始 tool_use + tool_result 配对。

        work_record 仅用于驱动 state.json 的结构化字段更新（files_seen /
        recent_commands / decisions / pending）；它的 text 字段已废弃，不再
        参与 history 注入或 transcript 持久化。
        """
        self.ensure_active()
        self.active_dir.mkdir(parents=True, exist_ok=True)
        resolved_turn_id = (
            turn_id
            or self._active_turn_id_for_user(user_query)
            or self._pending_turn_id_for_user(user_query)
            or self._new_turn_id()
        )
        item = {
            "ts": _now_iso(),
            "turn_id": resolved_turn_id,
            "user_query": user_query,
            "final_answer": final_answer,
            "messages": [_message_to_persist_payload(m) for m in committed_messages],
            "trace_entries": (
                [e.to_dict() for e in work_record.trace_entries]
                if work_record and self.persist_trace_entries else []
            ),
        }
        with (self.active_dir / "transcript.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        self.clear_pending_user_message()
        self.clear_active_turn()
        if work_record:
            self.merge_work_record(work_record, user_query=user_query)
        else:
            self._bump_turn(user_query=user_query)
        self.save_state(self.state)
        self._write_index()

    def merge_work_record(self, record: WorkRecord, *, user_query: str = "") -> None:
        """把本轮 WorkRecord 合并进滚动 state。

        合并策略是"新事实覆盖旧事实"：
        - 同一路径的 files_seen/files_modified 用最新摘要覆盖；
        - recent_commands 只保留最近 N 条；
        - decisions/pending 追加后截尾。

        重构后 record.text 不再参与（它已经废弃，CC 模式下原始 tool_use +
        tool_result 直接存进 transcript），rolling_summary 只由 /compact 路径
        通过 last_compact_summary 维护。
        """
        state = self.state or self._new_state()
        state["updated_at"] = _now_iso()
        state["turn_count"] = int(state.get("turn_count") or 0) + 1
        if user_query:
            state["active_task"] = _clip(user_query, 200)

        files_seen = state.setdefault("files_seen", {})
        if isinstance(files_seen, dict):
            files_seen.update(record.files_seen)
        files_modified = state.setdefault("files_modified", {})
        if isinstance(files_modified, dict):
            files_modified.update(record.files_modified)
        commands = state.setdefault("recent_commands", [])
        if isinstance(commands, list):
            commands.extend(record.recent_commands)
            state["recent_commands"] = commands[-RECENT_COMMANDS_LIMIT:]
        for key in ("decisions", "pending"):
            cur = state.setdefault(key, [])
            additions = getattr(record, key)
            if isinstance(cur, list) and additions:
                cur.extend(additions)
                state[key] = cur[-20:]
        self.state = state

    def _bump_turn(self, *, user_query: str = "") -> None:
        """没有工具轨迹时也推进 turn_count/active_task。

        纯聊天轮次没有 WorkRecord，但 transcript 仍然会记录 user/final；state
        也需要更新时间，便于知道这个 session 仍然活跃。
        """
        state = self.state or self._new_state()
        state["updated_at"] = _now_iso()
        state["turn_count"] = int(state.get("turn_count") or 0) + 1
        if user_query:
            state["active_task"] = _clip(user_query, 200)
        self.state = state

    def save_state(self, state: Dict[str, Any]) -> None:
        self.ensure_active()
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self.state = state
        self._write_json(self.active_dir / "state.json", state)

    def clear_active_session(self) -> None:
        """彻底删除当前 active session。

        这对应用户确认后的 /clear 语义：不仅清内存 history，也删除本地
        transcript/state/index，保证重启后不会自动恢复旧上下文。
        """
        if self.active_session_id:
            target = self.root / self.active_session_id
            if target.exists():
                self._assert_safe_session_dir(target)
                shutil.rmtree(target)
        if self.index_path.exists():
            self.index_path.unlink()
        self.active_session_id = None
        self.state = {}

    def _write_index(self) -> None:
        """写入 active 指针。

        index 只放当前激活会话 id，不保存会话内容。多会话列表通过扫描目录获得，
        这样删除某个 session 目录后不会出现 index 里残留一大份旧元数据的问题。
        """
        self._write_json(
            self.index_path,
            {
                "active_session_id": self.active_session_id,
                "updated_at": _now_iso(),
            },
        )

    def _session_summary_from_dir(self, session_dir: Path) -> Dict[str, Any]:
        """从单个 session 目录提取列表页需要的摘要。

        这个方法故意只返回短 preview 和计数，不返回 transcript 全文。TUI 切换到
        该会话时才会通过 AgentSession.load_latest_history 恢复最近 history。
        """
        session_id = session_dir.name
        state = self._read_json(session_dir / "state.json", {})
        if not isinstance(state, dict):
            state = {}
        created_at = str(state.get("created_at") or self._mtime_iso(session_dir))
        updated_at = str(state.get("updated_at") or self._newest_mtime_iso(session_dir))
        return {
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "turn_count": int(state.get("turn_count") or self._count_transcript_turns(session_dir)),
            "active_task": _clip(state.get("active_task"), 120),
            "rolling_summary": _clip(
                state.get("rolling_summary") or state.get("last_compact_summary"),
                180,
            ),
            "is_active": session_id == self.active_session_id,
        }

    def _count_transcript_turns(self, session_dir: Path) -> int:
        """粗略统计 transcript 轮数，用于 state 缺 turn_count 时兜底。"""
        path = session_dir / "transcript.jsonl"
        try:
            if not path.exists():
                return 0
            return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except Exception:
            logger.exception("failed to count transcript turns from %s", path)
            return 0

    def _newest_mtime_iso(self, session_dir: Path) -> str:
        """取 session 目录内关键文件的最新 mtime，作为 updated_at 兜底。"""
        paths = [
            session_dir,
            session_dir / "state.json",
            session_dir / "transcript.jsonl",
            session_dir / "active_turn.jsonl",
            session_dir / "pending_user.json",
        ]
        newest = max((p.stat().st_mtime for p in paths if p.exists()), default=session_dir.stat().st_mtime)
        return datetime.fromtimestamp(newest, timezone.utc).isoformat()

    @staticmethod
    def _mtime_iso(path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()

    @staticmethod
    def _is_valid_session_id(session_id: str) -> bool:
        """校验会话目录名，防止 switch/delete 走出 sessions 根目录。"""
        return bool(re.fullmatch(r"session_\d{8}_\d{6}_[0-9a-f]{8}", session_id or ""))

    def _assert_safe_session_dir(self, target: Path) -> None:
        """删除前确认目标目录确实位于 sessions 根目录下。

        这是对 ``shutil.rmtree`` 的最后一道保险。即使 index.json 被手工写坏，
        也不能让 active_session_id 通过 ``..`` 之类的路径片段影响 sessions
        目录之外的文件。
        """
        root = self.root.resolve()
        resolved = target.resolve()
        if resolved == root or root not in resolved.parents:
            raise RuntimeError(f"refuse to remove unsafe session dir: {resolved}")

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            if not path.exists():
                return default
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("failed to read json %s", path)
            return default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


__all__ = [
    "LocalSessionStore",
    "RuleTraceSummarizer",
    "TraceCollector",
    "TraceEntry",
    "TraceSummarizer",
    "WorkRecord",
]

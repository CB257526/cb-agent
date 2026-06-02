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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.message import Message
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


def _extract_tool_call_name(call: Dict[str, Any]) -> str:
    return str((call.get("function") or {}).get("name") or "")


def _extract_tool_call_args(call: Dict[str, Any]) -> Dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments", "{}")
    parsed = _json_loads_maybe(raw)
    return parsed if isinstance(parsed, dict) else {}


def _summarize_arguments(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """生成可落盘的参数摘要。

    这里不能简单保存完整 arguments：
    - file_write.content 可能是整份文件内容，必须丢弃；
    - bash stdout 不在 arguments 里，但 command 可能很长，也要截断；
    - 其他工具若带 content/stdout/stderr/result 这类高噪声字段，也不落盘。

    这些限制只影响跨轮 trace，不影响本轮真实工具调用。
    """
    if name == "file_write":
        keep = ("path",)
        return {k: _clip(arguments.get(k), 160) for k in keep if k in arguments}
    if name == "file_read":
        keep = ("path", "head", "tail", "start_line", "end_line")
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
    """一轮对话结束后生成的工作记录。

    ``text`` 会作为普通 assistant 消息追加到 ``AgentSession.history``；
    其他字段用于更新 state.json。这样即时上下文和长期恢复使用同一份事实，
    但注入位置不同：text 进入 [Context]，state_text 进入更高优先级的 [State]。
    """
    text: str
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
    """纯规则工作记录生成器。

    它有两个用途：
    1. trace 较小时直接生成 ``【工作记录】``，避免额外 LLM 调用；
    2. trace 较大但静默 LLM 总结失败时作为兜底。

    规则总结只做字段提取和短句拼接，不尝试推理新结论，因此稳定、便宜、
    不会把 summarizer 的失败传染给主对话流程。
    """

    def summarize(
        self,
        *,
        user_query: str,
        final_answer: str,
        trace_entries: Sequence[TraceEntry],
    ) -> WorkRecord:
        del final_answer
        files_seen: Dict[str, Dict[str, Any]] = {}
        files_modified: Dict[str, Dict[str, Any]] = {}
        recent_commands: List[Dict[str, Any]] = []
        lines: List[str] = []

        if user_query:
            lines.append(f"用户任务：{_clip(user_query, 120)}")

        for entry in trace_entries:
            meta = entry.metadata
            if entry.name == "file_read":
                # file_read 是代码任务里最重要的上下文来源。这里保存路径、
                # 读取模式和行数信息，以及最多 100 字符的正文预览；完整正文
                # 只存在于本轮 messages，不进入 history/transcript/state。
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
                    lines.append(
                        "读取文件："
                        f"{path} ({meta.get('mode') or 'unknown'}, "
                        f"returned={meta.get('returned_lines')}) "
                        f"{_clip(meta.get('content_preview') or entry.result_summary, 120)}"
                    )
                continue

            if entry.name == "file_write":
                # file_write 的入参 content 已在 _summarize_arguments 中丢弃。
                # 这里仅记录写入结果和粗粒度行数变化，方便下一轮知道哪些文件
                # 已经被 agent 改过。
                path = str(meta.get("path") or entry.arguments.get("path") or "")
                if path:
                    files_modified[path] = {
                        "lines_added": meta.get("lines_added"),
                        "lines_removed": meta.get("lines_removed"),
                        "summary": _clip(entry.result_summary or meta.get("message"), FILE_SUMMARY_LIMIT),
                        "last_modified_at": entry.timestamp,
                    }
                    lines.append(
                        f"修改文件：{path} "
                        f"(+{meta.get('lines_added')}/-{meta.get('lines_removed')})"
                    )
                continue

            if entry.name in {"bash", "bash_task"}:
                # bash 输出通常很大，而且可能已经由 BashTool 单独落到
                # .cbagent/bash_outputs。跨轮上下文只需要命令、cwd、退出码、
                # 简短输出摘要和 output_file 引用。
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
                lines.append(
                    f"执行命令：{_clip(command, 120)} "
                    f"(exit={meta.get('exit_code')}, cwd={meta.get('cwd')}) "
                    f"{_clip(entry.result_summary, 100)}"
                )
                continue

            lines.append(
                f"调用工具：{entry.name} "
                f"{_clip(entry.result_summary, 120)}"
            )

        if not trace_entries:
            return WorkRecord(text="", trace_entries=[])

        text = "【工作记录】" + "\n".join(lines)
        if len(text) > WORK_RECORD_LIMIT:
            text = _clip(text, WORK_RECORD_LIMIT)

        return WorkRecord(
            text=text,
            trace_entries=list(trace_entries),
            files_seen=files_seen,
            files_modified=files_modified,
            recent_commands=recent_commands[-RECENT_COMMANDS_LIMIT:],
        )


class TraceSummarizer:
    """静默 LLM 工作记录压缩器。

    它直接调用 OpenAI-compatible client，且强制 ``stream=False``。这里绝对
    不走 ``llm.think()``，原因是 think() 是主回答路径，会向 EventBus 发
    TextDelta/ReasoningDelta，前端会把 summarizer 的内容误当成助手回答。

    如果没有传入 llm、llm 没有 client/model，或静默调用失败，就直接返回
    RuleTraceSummarizer 的结果。总结能力是增强项，不能影响主对话完成。
    """

    def __init__(
        self,
        llm: Optional[Any] = None,
        fallback: Optional[RuleTraceSummarizer] = None,
    ) -> None:
        self.llm = llm
        self.fallback = fallback or RuleTraceSummarizer()

    def summarize(
        self,
        *,
        user_query: str,
        final_answer: str,
        trace_entries: Sequence[TraceEntry],
    ) -> WorkRecord:
        # 先生成规则版记录。即使后面的 LLM 调用成功，也复用它里面提取好的
        # files_seen/files_modified/recent_commands 等结构化状态，只替换 text。
        fallback_record = self.fallback.summarize(
            user_query=user_query,
            final_answer=final_answer,
            trace_entries=trace_entries,
        )
        client = getattr(self.llm, "client", None)
        model = getattr(self.llm, "model", None)
        if client is None or not model:
            return fallback_record

        # 给 LLM 的输入只包含 trace 的单行摘要，不包含原始文件正文或完整 stdout。
        # 因此即便 trace_entries 来自 file_read/bash，也不会把大输出二次送入
        # summarizer。
        trace_text = "\n".join(e.to_line() for e in trace_entries)
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                stream=False,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是会话工作记录压缩器。把工具轨迹压缩成一条中文工作记录，"
                            "只保留对下一轮继续任务有帮助的事实：看过/改过的文件、命令、"
                            "关键结论、待办。不要编造。输出不超过600字，并以【工作记录】开头。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"用户任务：{user_query}\n\n"
                            f"最终回答：{_clip(final_answer, 500)}\n\n"
                            f"工具轨迹：\n{trace_text}"
                        ),
                    },
                ],
            )
            content = response.choices[0].message.content or ""
        except Exception:
            logger.exception("silent trace summary failed")
            return fallback_record

        # LLM 只负责把文字表达得更紧凑；结构化状态仍以规则提取为准。
        # 这样可以降低幻觉对 state.json 的影响。
        content = _clip(content, WORK_RECORD_LIMIT)
        if not content:
            return fallback_record
        if not content.startswith("【工作记录】"):
            content = "【工作记录】" + content
        fallback_record.text = _clip(content, WORK_RECORD_LIMIT)
        return fallback_record


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
        elif name == "file_write":
            # file_write 不保存写入内容，只保存结果状态和行数变化。
            metadata = {
                "path": parsed.get("path"),
                "ok": parsed.get("ok"),
                "type": parsed.get("type"),
                "bytes_written": parsed.get("bytes_written"),
                "lines_added": parsed.get("lines_added"),
                "lines_removed": parsed.get("lines_removed"),
                "message": parsed.get("message"),
            }
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
    - <session_id>/state.json：滚动摘要、已读文件、已改文件、最近命令等。

    多会话隔离的关键点也在这里：同一时刻只有一个 active session，但每个
    session 都拥有独立目录。切换会话只会改 index 指针并重载该目录的 state；
    不会把 A 会话的 transcript/state 合并进 B 会话。

    Store 被 AgentSession 以依赖注入方式使用；单测不传 store 时完全不落盘。
    """

    def __init__(self, root: Optional[Path | str] = None) -> None:
        self.root = Path(root or Path.cwd() / ".cbagent" / "sessions")
        self.index_path = self.root / "index.json"
        self.active_session_id: Optional[str] = None
        self.state: Dict[str, Any] = {}
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

        摘要是给 CLI/TUI/RPC 展示用的轻量对象，不包含 transcript 正文，
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
        self._write_index()
        return self._session_summary_from_dir(self.active_dir)

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

    def load_latest_history(self, max_messages: int = 12) -> List[Message]:
        """从 transcript 恢复最近 history。

        transcript 每轮最多恢复三条普通对话消息：
        1. user_query -> user message；
        2. final_answer -> assistant message；
        3. work_record -> assistant message，content 以【工作记录】开头。

        不恢复 role=tool，也不恢复 assistant.tool_calls。跨轮恢复的是对话和工作
        摘要，而不是上一轮工具协议状态。
        """
        if not self.active_session_id:
            return []
        path = self.active_dir / "transcript.jsonl"
        if not path.exists():
            return []
        messages: List[Message] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                user_query = item.get("user_query")
                final_answer = item.get("final_answer")
                work_record = item.get("work_record")
                if user_query:
                    messages.append(Message.create_user_message(str(user_query)))
                if final_answer:
                    messages.append(Message.create_assistant_message(str(final_answer)))
                if work_record:
                    messages.append(_create_work_record_message(str(work_record)))
        except Exception:
            logger.exception("failed to load transcript from %s", path)
            return []
        return messages[-max_messages:]

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
        work_record: Optional[WorkRecord],
    ) -> None:
        """追加一轮 transcript，并同步更新 state.json。

        transcript 是审计/恢复用的逐轮记录；state 是给模型下一轮快速使用的
        滚动摘要。两者都只保存压缩后的 work_record/trace_entries，不保存完整
        工具输出。
        """
        self.ensure_active()
        self.active_dir.mkdir(parents=True, exist_ok=True)
        item = {
            "ts": _now_iso(),
            "user_query": user_query,
            "final_answer": final_answer,
            "work_record": work_record.text if work_record else "",
            "trace_entries": (
                [e.to_dict() for e in work_record.trace_entries]
                if work_record else []
            ),
        }
        with (self.active_dir / "transcript.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
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
        - rolling_summary 采用追加后截断，避免无限增长。
        """
        state = self.state or self._new_state()
        state["updated_at"] = _now_iso()
        state["turn_count"] = int(state.get("turn_count") or 0) + 1
        if user_query:
            state["active_task"] = _clip(user_query, 200)
        if record.text:
            current = str(state.get("rolling_summary") or "")
            merged = (current + "\n" + record.text).strip() if current else record.text
            state["rolling_summary"] = _clip(merged, ROLLING_SUMMARY_LIMIT)

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
            "rolling_summary": _clip(state.get("rolling_summary"), 180),
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
        paths = [session_dir, session_dir / "state.json", session_dir / "transcript.jsonl"]
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


def _create_work_record_message(text: str) -> Message:
    """把工作记录包装成普通 assistant message。

    这里故意不用 role=tool：tool message 必须有对应的 tool_call_id，且只在
    同一轮 OpenAI tool calling 协议中合法。跨轮工作记录是普通文本背景。
    """
    msg = Message.create_assistant_message(text)
    msg.metadata = {"kind": "work_record"}
    return msg


def make_work_record_message(record: WorkRecord) -> Message:
    return _create_work_record_message(record.text)


__all__ = [
    "LocalSessionStore",
    "RuleTraceSummarizer",
    "TraceCollector",
    "TraceEntry",
    "TraceSummarizer",
    "WorkRecord",
    "make_work_record_message",
]

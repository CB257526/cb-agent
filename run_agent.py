"""cb-agent 完整能力 demo

一个交互式 REPL，把项目里所有能力都串起来：
- ContextBuilder（GSSC 上下文构建，自动召回 memory + RAG）
- 原生 Tools（memory / rag / todo / search / skill / run_skill_script）
- MCP（mcp.json 里声明的服务器自动展开为独立工具，按 OpenAI function calling 协议调用）
- Skills（.cbagent/skills/ 下的 SKILL.md 自动发现）
- LLM 工具调用循环（think → tool_calls → 执行 → 回灌 → 再 think，直到模型给最终回答）

跑法：
    cd c:/Users/cb135/Desktop/cbAgent/cb-agent
    ../venv/python.exe run_agent.py

运行时命令：
    /help       打印帮助
    /tools      列出所有已注册工具
    /skills     列出所有 Skill
    /history    查看当前会话历史
    /clear      清空会话历史
    /ctx on|off 开关 ContextBuilder（默认 on）
    /quit       退出
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

# 把 cb-agent 目录加到 sys.path（脚本本身就在 cb-agent/，理论上不需要，
# 但若用户从其它目录起 python，仍能跑）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Windows 控制台输出 UTF-8（避免 emoji/中文 GBK 编码异常）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from agent.cb_agents import CbAgentsLLM
from context import ContextBuilder, ContextConfig
from core.message import Message
from skills.skill_manager import SkillManager
from skills.skill_executor import SkillExecutor
from tools.tool import Tool
from tools.toolRegistry import ToolRegistry
from tools.tools.memory_tool import MemoryTool
from tools.tools.rag_tool import RAGTool
from tools.tools.search import SearchTool
from tools.tools.skill_tool import SkillTool
from tools.tools.run_skill_script_tool import RunSkillScriptTool
from tools.tools.todo_tool import TodoTool
from tools.tools.bash_tool import BashTool
from tools.tools.bash_task_tool import BashTaskTool
from tools.tools.bash_permission_tool import BashPermissionTool
from tools.tools.file_read_tool import FileReadTool
from tools.tools.file_write_tool import FileWriteTool
from tools.tools.bash_prompt import get_bash_prompt

try:
    from tools.mcp_tools.mcptools_add import load_mcp_tools
    _HAS_MCP = True
except Exception:
    _HAS_MCP = False


# 日志配置：Demo 默认只显示 WARNING 以上，避免被各模块的 INFO 刷屏
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# memory/rag 启动期信息量大，进一步压到 ERROR
for noisy in ("memory", "memory.types", "memory.storage", "memory.manager"):
    logging.getLogger(noisy).setLevel(logging.ERROR)


# ---------- 视觉辅助 ----------

# Windows 终端 ANSI：尝试启用 VT 模式；不行就用 colorama 兜底；再不行就降级为空串
_ANSI_OK = True
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        try:
            import colorama  # 可选依赖
            colorama.just_fix_windows_console()
        except Exception:
            _ANSI_OK = False


def _c(code: str) -> str:
    """生成 ANSI 控制序列；终端不支持时返回空串。"""
    return f"\033[{code}m" if _ANSI_OK else ""


# 颜色简写
_BOLD = _c("1")
_DIM = _c("2")
_RESET = _c("0")
_RED = _c("31")
_GREEN = _c("32")
_YELLOW = _c("33")
_MAGENTA = _c("35")
_CYAN = _c("36")
_GRAY = _c("90")


def _hr(char: str = "─", width: int = 60) -> str:
    return char * width


def _section(title: str) -> None:
    print(f"\n{_hr()}\n{title}\n{_hr()}")


def _info(msg: str) -> None:
    print(f"[*] {msg}")


def _err(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)


def _render_todo_panel(tool_result: str) -> Optional[str]:
    """把 todo 工具的 JSON 输出渲染成 Claude Code 风格的彩色面板。

    解析失败或结构不符则返回 None，由调用方走默认输出。
    """
    try:
        data = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "todos" not in data:
        return None

    todos = data.get("todos") or []
    summary = data.get("summary") or {}

    # 状态 → (标记, 颜色, 内容样式)
    style_map = {
        "completed":   ("☒", _GREEN,   _DIM),         # 已完成：灰字 + 删除感
        "in_progress": ("◐", _YELLOW,  _BOLD),        # 进行中：黄底加粗
        "pending":     ("☐", _GRAY,    ""),           # 待办：灰
        "cancelled":   ("✗", _GRAY,    _DIM),         # 取消
    }

    lines = [f"{_BOLD}{_MAGENTA}● Update Todos{_RESET}"]
    if not todos:
        lines.append(f"  {_GRAY}（当前没有任务）{_RESET}")
    else:
        for item in todos:
            status = (item.get("status") or "pending").lower()
            marker, color, body_style = style_map.get(
                status, ("·", _GRAY, "")
            )
            content = item.get("content") or "(无描述)"
            lines.append(f"  {color}{marker}{_RESET} {body_style}{content}{_RESET}")

    if summary:
        total = summary.get("total", len(todos))
        bits = []
        if summary.get("in_progress"): bits.append(f"{_YELLOW}进行中 {summary['in_progress']}{_RESET}")
        if summary.get("pending"):     bits.append(f"{_GRAY}待办 {summary['pending']}{_RESET}")
        if summary.get("completed"):   bits.append(f"{_GREEN}完成 {summary['completed']}{_RESET}")
        if summary.get("cancelled"):   bits.append(f"{_GRAY}取消 {summary['cancelled']}{_RESET}")
        tail = "  ".join(bits)
        lines.append(f"  {_DIM}── 共 {total} 项{_RESET}" + (f"  {tail}" if tail else ""))

    return "\n".join(lines)


def _render_thought(reason: str, elapsed_seconds: Optional[float] = None) -> str:
    """把模型 reasoning_content 渲染成 'Thought for Xs' 风格块。

    输出形如：
        ▸ Thought for 12.3s
          逐段缩进的思考内容（灰色）
    """
    head_time = ""
    if elapsed_seconds is not None and elapsed_seconds >= 0:
        head_time = f" for {elapsed_seconds:.1f}s"
    head = f"{_DIM}{_CYAN}▸ Thought{head_time}{_RESET}"
    body_lines = (reason or "").strip().splitlines()
    if not body_lines:
        return head
    indented = "\n".join(f"  {_GRAY}{ln}{_RESET}" for ln in body_lines)
    return f"{head}\n{indented}"


def _render_bash_output(tool_result: str) -> Optional[str]:
    """把 bash 工具的 JSON 输出渲染成彩色面板。

    命令分类不同显示策略：
    - silent: 成功时只一行 "Done."，无输出
    - search/read: 输出可折叠（由 dump 机制处理）
    - 错误: 红字 stderr
    - 警告: 黄字前缀
    解析失败返回 None，走默认截断预览。
    """
    try:
        data = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "stdout" not in data:
        return None

    stdout = (data.get("stdout") or "").rstrip()
    stderr = (data.get("stderr") or "").rstrip()
    exit_code = data.get("exit_code", 0)
    is_error = data.get("is_error", exit_code != 0)
    classification = data.get("classification", {}) or {}
    kind = classification.get("kind", "normal")
    semantic = data.get("semantic")
    interrupted = data.get("interrupted", False)
    timed_out = data.get("timeout", False)
    background = data.get("background", False)

    lines = []

    # 后台模式
    if background:
        task_id = data.get("background_task_id", "?")
        lines.append(f"{_DIM}{_CYAN}  ⟳ 后台运行中 (task {task_id}){_RESET}")
        return "\n".join(lines)

    # 超时 / 中断
    if timed_out:
        lines.append(f"{_BOLD}{_YELLOW}  ⏱ 命令超时{_RESET}")
    if interrupted:
        lines.append(f"{_YELLOW}  ✗ 命令被中断{_RESET}")

    # silent 命令成功 → 简洁一行
    if kind == "silent" and not is_error and not timed_out and not interrupted:
        lines.append(f"  {_DIM}Done.{_RESET}")
        return "\n".join(lines) if lines else None

    # 错误输出
    if is_error and not timed_out and not interrupted:
        stderr_preview = (stderr or f"exit code {exit_code}")[:200]
        lines.append(f"  {_BOLD}{_RED}✗ {stderr_preview}{_RESET}")

    # 语义化退出码
    if not is_error and semantic:
        lines.append(f"  {_DIM}({semantic.get('message', '')}){_RESET}")

    # stdout（截取前 500 字符到面板，完整输出存在于 tool_result 传给模型）
    if stdout:
        preview = stdout[:500]
        trimmed = "..." if len(stdout) > 500 else ""
        for line in preview.split("\n"):
            lines.append(f"  {_DIM}{line}{_RESET}")
        if trimmed:
            lines.append(f"  {_GRAY}... [{len(stdout)} 字符, 已截断显示]{_RESET}")

    # stderr 警告级（非致命）
    if stderr and not is_error:
        for line in stderr.split("\n")[:5]:
            lines.append(f"  {_YELLOW}{line}{_RESET}")

    if not lines:
        return None

    # 折叠标记：search/read/list 类命令
    if kind in ("search", "read", "list"):
        label = {"search": "搜索结果", "read": "读取内容", "list": "目录列表"}.get(kind, "输出")
        header = f"{_BOLD}{_MAGENTA}  ▸ {label}{_RESET}"
        lines.insert(0, header)

    return "\n".join(lines)


# ---------- 主类 ----------


class AgentRunner:
    """REPL 主控。

    职责：
    - 启动期注册所有工具（原生 + MCP）
    - 构建 ContextBuilder
    - 维护会话历史（List[Message]）
    - 主循环里执行 think → tool_calls → 执行工具 → 回灌的多轮调用
    """

    # 工具调用循环最大轮数，防止模型陷入死循环
    MAX_TOOL_ROUNDS = 20

    def __init__(self, use_mcp: bool = True, ctx_enabled: bool = True):
        self.use_mcp = use_mcp and _HAS_MCP
        self.ctx_enabled = ctx_enabled
        # 是否在每轮 think 前 dump 当前 messages（默认开，便于观察提示词怎么拼的）
        self.dump_messages = True
        # dump 增量游标：已打印过的 messages 数量，每次 _chat_once 重置为 0
        self._dump_seen_count = 0
        self.history: List[Message] = []

        _section("初始化 cb-agent")

        # 1. 实例化 LLM
        _info("初始化 LLM 客户端")
        self.llm = CbAgentsLLM()
        _info(f"模型: {self.llm.model}  function_calling={self.llm.is_Function_Calling}")

        # 2. 工具注册
        self.registry = ToolRegistry()
        self._register_native_tools()
        if self.use_mcp:
            self._register_mcp_tools()

        # 3. 上下文构建器（复用 memory/rag 实例）
        self.builder = ContextBuilder(
            memory_tool=self._memory_tool,
            rag_tool=self._rag_tool,
            config=ContextConfig(
                max_tokens=8000,
                min_relevance=0.05,
                history_max_messages=8,
            ),
        )

        _section("就绪")
        _info(f"已注册工具 {len(self.registry.list_tools())} 个: {', '.join(self.registry.list_tools())}")
        _info(f"Skill 数量 {len(self._skill_manager.list_skills())}")
        _info(f"上下文构建器: {'开启' if self.ctx_enabled else '关闭'}")
        _info(f"messages dump: {'开启' if self.dump_messages else '关闭'} (用 /msg off 关闭)")
        print()

    # ---------- 启动期 ----------

    def _register_native_tools(self) -> None:
        """注册项目内置工具。"""
        _info("注册原生工具")

        # memory / rag 实例稍后还要交给 ContextBuilder，在此存为字段
        self._memory_tool = MemoryTool()
        self._rag_tool = RAGTool()
        self._skill_manager = SkillManager()
        skill_executor = SkillExecutor()

        for tool in [
            self._memory_tool,
            self._rag_tool,
            TodoTool(),
            SearchTool(),
            SkillTool(self._skill_manager),
            RunSkillScriptTool(self._skill_manager, skill_executor),
            BashTool(),
            BashTaskTool(),
            BashPermissionTool(),
            FileReadTool(),
            FileWriteTool(),
        ]:
            try:
                self.registry.register_tool(tool)
            except Exception as e:
                _err(f"工具 {tool.name} 注册失败: {e}")

    def _register_mcp_tools(self) -> None:
        """读取 mcp.json，把每个 MCP 服务器的子工具展开注册。"""
        _info("加载 MCP 配置")
        try:
            mcp_tools = load_mcp_tools()
        except Exception as e:
            _err(f"MCP 加载失败（跳过）: {e}")
            return

        for mcp_tool in mcp_tools:
            try:
                expanded = mcp_tool.get_expanded_tools()
                if not expanded:
                    # 未展开就把 MCP 主工具自己注册（按 action 分发）
                    self.registry.register_tool(mcp_tool)
                    continue
                for sub in expanded:
                    self.registry.register_tool(sub)
            except Exception as e:
                _err(f"MCP 工具 {getattr(mcp_tool, 'name', '?')} 展开失败: {e}")

    # ---------- 主循环 ----------

    def run(self) -> None:
        _section("交互模式")
        print("输入问题与我对话，输入 /help 看命令，/quit 退出。\n")

        while True:
            try:
                user_input = input("you > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                _info("再见")
                return

            if not user_input:
                continue

            if user_input.startswith("/"):
                if self._handle_command(user_input):
                    continue
                else:
                    return  # /quit

            try:
                self._chat_once(user_input)
            except Exception as e:
                _err(f"本轮对话异常: {e}")
                traceback.print_exc()

    def _handle_command(self, line: str) -> bool:
        """斜杠命令分派。返回 True 继续循环，False 退出。"""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit"):
            _info("再见")
            return False

        if cmd == "/help":
            print(
                "\n可用命令：\n"
                "  /help        打印帮助\n"
                "  /tools       列出所有已注册工具\n"
                "  /skills      列出所有 Skill\n"
                "  /history     查看当前会话历史\n"
                "  /clear       清空会话历史\n"
                "  /ctx on|off  开关 ContextBuilder (当前: "
                + ("on" if self.ctx_enabled else "off")
                + ")\n"
                "  /msg on|off  开关每轮 messages dump (当前: "
                + ("on" if self.dump_messages else "off")
                + ")\n"
                "  /quit        退出\n"
            )
        elif cmd == "/tools":
            names = self.registry.list_tools()
            print(f"\n已注册 {len(names)} 个工具：")
            for n in names:
                tool = self.registry.get_tool(n)
                desc = tool.description if tool else ""
                print(f"  - {n}: {desc[:80]}")
            print()
        elif cmd == "/skills":
            skills = self._skill_manager.list_skills()
            print(f"\n已发现 {len(skills)} 个 Skill：")
            for s in skills:
                print(f"  - {s.name}: {(s.description or '')[:80]}")
            print()
        elif cmd == "/history":
            print(f"\n会话历史 ({len(self.history)} 条)：")
            for i, m in enumerate(self.history, 1):
                role = m.role.value if hasattr(m.role, "value") else str(m.role)
                content = m.content if isinstance(m.content, str) else json.dumps(
                    m.content, ensure_ascii=False
                )
                preview = (content or "")[:120]
                print(f"  {i:2d}. [{role}] {preview}")
            print()
        elif cmd == "/clear":
            self.history.clear()
            _info("会话历史已清空")
        elif cmd == "/ctx":
            if arg in ("on", "off"):
                self.ctx_enabled = arg == "on"
                _info(f"ContextBuilder = {arg}")
            else:
                _info(f"用法: /ctx on|off  (当前: {'on' if self.ctx_enabled else 'off'})")
        elif cmd == "/msg":
            if arg in ("on", "off"):
                self.dump_messages = arg == "on"
                _info(f"messages dump = {arg}")
            else:
                _info(f"用法: /msg on|off  (当前: {'on' if self.dump_messages else 'off'})")
        else:
            _err(f"未知命令 {cmd}，/help 查看可用命令")
        return True

    # ---------- 单轮对话 ----------

    def _chat_once(self, user_query: str) -> None:
        """处理一次用户输入：构 messages → think → 工具循环 → 落历史。"""
        # 每次新用户输入都重置 dump 增量游标，让本轮第一次能打全量
        self._dump_seen_count = 0
        # 拉取后台任务完成通知，挂到 user_query 前作为系统提示
        user_query = self._prepend_background_notifications(user_query)
        # 系统指令（项目角色定位 + Skill 概览）
        system_instructions = self._build_system_instructions()

        # 构建 messages
        if self.ctx_enabled:
            # ContextBuilder 模式：把 memory/rag/历史都揉进 system prompt
            messages = self.builder.to_messages(
                user_query=user_query,
                conversation_history=self.history,
                system_instructions=system_instructions,
            )
        else:
            # 朴素模式：让历史以独立 message 形式出现
            messages = [{"role": "system", "content": system_instructions}]
            for m in self.history[-10:]:
                messages.append(m.to_dict())
            messages.append({"role": "user", "content": user_query})

        # 工具 schema
        tools_schema = (
            self.registry.get_tools_description_openai_schema()
            if self.llm.is_Function_Calling
            else None
        )

        # 工具调用循环
        final_answer = self._tool_loop(messages, tools_schema)

        # 落入历史（用 user_query 而不是整段 system，避免历史无限膨胀）
        self.history.append(Message.create_user_message(user_query))
        if final_answer:
            self.history.append(Message.create_assistant_message(final_answer))

    def _prepend_background_notifications(self, user_query: str) -> str:
        """每轮 think 前，把"上轮还在跑、本轮已经结束"的后台任务结果作为系统提示
        塞到 user_query 前面。让模型知道结果已就绪，可主动用 bash_task(action=output)
        拉详情。

        见 [[bash_background]] 的 drain_notifications 行为约定。
        """
        try:
            from tools.tools.bash_background import get_background_registry
            done = get_background_registry().drain_notifications()
        except Exception:
            return user_query
        if not done:
            return user_query
        lines = ["<system-reminder>", "[后台任务完成通知]"]
        for t in done:
            lines.append(
                f"- task_id={t.id} status={t.status} exit={t.exit_code} "
                f"cmd={t.command!r} output={t.output_path}"
            )
        lines.append(
            "请在回答用户前主动用 bash_task(action=output, task_id=...) "
            "拉一下完成任务的结果，告知用户。"
        )
        lines.append("</system-reminder>")
        return "\n".join(lines) + "\n\n" + user_query

    def _build_system_instructions(self) -> str:
        """组装系统指令：角色 + 已注册工具清单 + Bash 使用规范 + Skill 概览。

        工具清单从 ToolRegistry 动态拉取，避免与实际注册情况脱节。
        """
        parts = [
            "你是 cb-agent 的智能助手。下面列出当前可用的能力，按需调用：",
            "遇到复杂的问题是请务必调用todo工具",
            "",
        ]

        # 工具清单从注册表动态生成
        tools_desc = self.registry.get_tools_description()
        if tools_desc and tools_desc != "暂无可用工具":
            parts.append(tools_desc)
        else:
            parts.append("（当前没有已注册的工具）")

        parts.extend([
            "",
            "调用工具时选最直接的那个，避免连续多轮无意义调用。",
            "回答用中文，简明扼要。",
        ])

        # Bash 工具使用规范（参考 Claude Code prompt.ts 的设计）
        try:
            bash_prompt = get_bash_prompt()
            if bash_prompt:
                parts.append("")
                parts.append(bash_prompt)
        except Exception:
            pass

        # Skill 概览（按使用频率 + 上下文预算自动降级，由 SkillManager 处理）
        try:
            overview = self._skill_manager.build_skills_overview(max_chars=1500)
            if overview:
                parts.append("")
                parts.append(overview)
        except Exception:
            pass
        return "\n".join(parts)

    def _dump_messages(self, messages: List[Dict[str, Any]], round_idx: int) -> None:
        """增量打印 messages：只输出本轮新追加的部分。

        - 第一轮（_dump_seen_count == 0）打印整段 messages
        - 后续每轮只打印 messages[_dump_seen_count:]
        """
        seen = getattr(self, "_dump_seen_count", 0)
        new_msgs = messages[seen:]
        total = len(messages)
        if not new_msgs:
            print(f"\n---- messages dump (round {round_idx}, 共 {total} 条，本轮新增 0) ----")
            print("---- end dump ----")
            return

        print(
            f"\n---- messages dump (round {round_idx}, 共 {total} 条，本轮新增 {len(new_msgs)}，"
            f"索引 [{seen}, {total - 1}]) ----"
        )
        try:
            print(json.dumps(new_msgs, ensure_ascii=False, indent=2, default=str))
        except Exception:
            for i, msg in enumerate(new_msgs, start=seen):
                print(f"[{i}] {msg!r}")
        print("---- end dump ----")
        self._dump_seen_count = total

    def _tool_loop(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
    ) -> str:
        """工具调用主循环。

        每轮：
        1. 调 llm.think
        2. 若有 tool_calls，全部执行后把结果回灌为 tool message，继续下一轮
        3. 若没有 tool_calls，把 answer 作为最终回答返回
        """
        for round_idx in range(1, self.MAX_TOOL_ROUNDS + 1):
            print(f"\n[round {round_idx}] 调用模型 ...")
            if self.dump_messages:
                self._dump_messages(messages, round_idx)
            t0 = time.perf_counter()
            result = self.llm.think(messages, tools=tools_schema)
            elapsed = time.perf_counter() - t0

            # think 在不支持 FC 的模型下返回 [text, None]
            if isinstance(result, list):
                # 流式输出已在 think 内打印过，这里直接收尾
                return result[0] or ""

            if not isinstance(result, dict):
                _err(f"模型返回非预期结构: {type(result)}")
                return ""

            answer = result.get("answer", "") or ""
            tool_calls = result.get("tool_calls") or []
            reasoning = result.get("reasoning_content")

            # 模型思考块（thinking 模式才有）：渲染成 Thought for Xs 折叠风格。
            # 流式下 reasoning 实际是先于 content 到的，但我们在 think 内只累积不打印，
            # 流完之后在这里统一渲染。视觉上仍呈现 "思考 → 回答" 的顺序。
            if reasoning and reasoning != answer:
                print()
                print(_render_thought(reasoning, elapsed_seconds=elapsed))

            if not tool_calls:
                # 流式版的 think 已经把 answer 边收边打过了，这里直接返回，不再重复打
                return answer

            # 把 assistant 的 tool_calls 消息加入 messages
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": answer or None,
                "tool_calls": tool_calls,
            }
            # thinking 模式（DeepSeek-V4 等）要求把 reasoning_content 原样回传，
            # 否则下一轮会 400: "reasoning_content ... must be passed back to the API."
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            messages.append(assistant_msg)

            # 顺序执行每个 tool_call
            for call in tool_calls:
                tool_call_id = call.get("id", "")
                fn = call.get("function") or {}
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}

                print(f"  → 调用工具 {tool_name}({_short_args(args)})")
                try:
                    tool_result = self.registry.execute_tool(tool_name, args)
                except Exception as e:
                    tool_result = f"[ERROR] 工具 {tool_name} 抛异常: {e}"

                # todo/bash 工具：用彩色面板替代单行预览；其它工具仍走截断预览
                if tool_name == "todo":
                    rendered = _render_todo_panel(tool_result)
                elif tool_name == "bash":
                    rendered = _render_bash_output(tool_result)
                else:
                    rendered = None
                if rendered:
                    print(rendered)
                else:
                    preview = (tool_result or "")[:200].replace("\n", " ")
                    suffix = "..." if tool_result and len(tool_result) > 200 else ""
                    print(f"     ← 结果: {preview}{suffix}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": tool_result if isinstance(tool_result, str) else str(tool_result),
                })

        _err(f"工具调用超过 {self.MAX_TOOL_ROUNDS} 轮，强制终止")
        return "（工具调用次数过多，已终止本轮）"


def _short_args(args: Dict[str, Any], limit: int = 80) -> str:
    """把工具参数压成一行短预览。"""
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    return s if len(s) <= limit else s[: limit - 3] + "..."


# ---------- 入口 ----------


def main() -> None:
    runner = AgentRunner(use_mcp=True, ctx_enabled=True)
    runner.run()


if __name__ == "__main__":
    main()

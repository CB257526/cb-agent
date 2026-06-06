"""cb-agent REPL 入口（Stage 3 拆分版）

跑法：
    cd c:/Users/cb135/Desktop/cbAgent/cb-agent
    ../venv/python.exe run_agent.py

架构：
    AgentRunner (本文件)
      ├── 启动期：装配 LLM / ToolRegistry / Executor / EventBus / Builder / Skill
      ├── 创建 AgentSession（纯逻辑，无 print）
      ├── 创建 CLIRenderer 并 attach 到 EventBus（订阅事件 → stdout）
      └── REPL：input → session.chat → 落历史 + slash 命令

  跟 Stage 2 之前的差别：
    - 所有运行时输出（流式正文、工具调用、Thought、todo/bash 面板）都搬到了
      [agent/renderers/cli.py](agent/renderers/cli.py)
    - 会话主循环搬到了 [agent/session.py](agent/session.py)
    - 本文件只剩"装配 + REPL 输入循环 + slash 命令"

运行时命令：
    /help       打印帮助
    /tools      列出所有已注册工具
    /skills     列出所有 Skill
    /history    查看当前会话历史
    /sessions   列出本项目的本地会话
    /new        新建并切换到空白会话
    /switch ID  切换到指定会话
    /clear      清空会话历史
    /ctx on|off 开关 ContextBuilder（默认 on）
    /msg on|off 开关每轮 messages dump（默认 on）
    /quit       退出
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List

# 把 cb-agent 目录加到 sys.path，允许从其它目录起 python
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Windows 控制台输出 UTF-8（避免 emoji/中文 GBK 异常）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from agent.logging_config import configure_logging

_LOG_SETTINGS = configure_logging(Path(_HERE))
logger = logging.getLogger(__name__)

from agent.cb_agents import CbAgentsLLM
from agent.event_bus import EventBus
from agent.events import Done, MCPStatus
from agent.buddy import BuddyManager
from agent.executor import ToolExecutor
from agent.renderers.cli import CLIRenderer
from agent.message_logger import MessageLogger
from agent.session import AgentSession
from agent.work_context import LocalSessionStore, TraceSummarizer
from constant.llm.constant_llm import ConstantLLM
from context import MemoryLoader, OpenAICompatibleAdapter
from skills.skill_manager import SkillManager
from skills.skill_executor import SkillExecutor
from tools.toolRegistry import ToolRegistry
from tools.tools.search import SearchTool
from tools.tools.local_search import GlobTool, GrepTool, LsTool
from tools.tools.skill_tool import SkillTool
from tools.tools.run_skill_script_tool import RunSkillScriptTool
from tools.tools.todo_tool import TodoTool
from tools.tools.bash_tool import BashTool
from tools.tools.bash_task_tool import BashTaskTool
from tools.tools.bash_permission_tool import BashPermissionTool
from tools.tools.file_read_tool import FileReadTool
from tools.tools.file_write_tool import FileWriteTool
from tools.tools.file_edit_tool import FileEditTool
from tools.tools.ask_user_question_tool import AskUserQuestionTool
from tools.tools.list_tools_tool import ListToolsTool

try:
    from tools.mcp_tools.mcptools_add import load_mcp_server_configs
    _HAS_MCP = True
except Exception:
    _HAS_MCP = False


# 日志：默认只显示 WARNING 以上，避免被各模块的 INFO 刷屏

# ========== 启动期纯字符输出 ==========


def _hr(char: str = "─", width: int = 60) -> str:
    """打印分隔线，用于分隔不同阶段的输出"""
    return char * width


def _section(title: str) -> None:
    """打印标题，用于分隔不同阶段的输出"""
    logger.info("section: %s", title)
    print(f"\n{_hr()}\n{title}\n{_hr()}")


def _info(msg: str) -> None:
    """打印信息，用于启动期的输出"""
    logger.info(msg)
    print(f"[*] {msg}")


def _err(msg: str) -> None:
    """打印错误信息，用于启动期的输出"""
    logger.error(msg)
    print(f"[!] {msg}", file=sys.stderr)


# ========== AgentRunner（装配 + REPL）==========


class AgentRunner:
    """装配所有依赖、跑 REPL。运行时的渲染交给 CLIRenderer，逻辑在 AgentSession。"""

    def __init__(
        self,
        use_mcp: bool = True,  # 是否开启 MCP 工具
        ctx_enabled: bool = True,  # 是否开启 ContextBuilder
        attach_cli_renderer: bool = True, # 是否 attach CLIRenderer 到 EventBus
        memory_system: str = "light",  # light=Markdown 记忆，full=旧 RAG/向量记忆，off=关闭记忆
    ) -> None:
        self.logging_settings = _LOG_SETTINGS
        self.use_mcp = use_mcp and _HAS_MCP
        self.ctx_enabled = ctx_enabled
        self.memory_system = memory_system
        logger.info(
            "AgentRunner init: use_mcp=%s has_mcp=%s ctx_enabled=%s attach_cli_renderer=%s memory_system=%s log_level=%s",
            use_mcp,
            _HAS_MCP,
            ctx_enabled,
            attach_cli_renderer,
            memory_system,
            self.logging_settings.verbosity,
        )
        # CLI 直接交互时保留 messages dump，方便开发者用 /msg on|off 看原始上下文；
        # TUI/jsonrpc 模式 attach_cli_renderer=False，此时 stderr 会被前端实时收集，
        # 如果默认 dump 完整 system prompt + 工具 schema，集成终端和 React 渲染都会承压。
        # 因此 TUI 默认关闭 dump，仍可在 CLI 模式下手动 /msg on 调试。
        self.dump_messages = bool(attach_cli_renderer)
        self._attach_cli_renderer = attach_cli_renderer
        self._md_memory_provider = self._create_markdown_memory_provider()
        self.buddy_manager = BuddyManager()
        # CLI 模式下的待发送附件队列。TUI 走 JSON-RPC attachments 字段；CLI 没有
        # 前端状态容器，所以在 Runner 内保留一份轻量队列，发送成功后清空。
        self.pending_attachments: List[Dict[str, Any]] = []
        self._mcp_lock = threading.RLock()
        self._mcp_thread: threading.Thread | None = None
        self._mcp_started = False
        self._mcp_status: Dict[str, Any] = self._initial_mcp_status()
        # dump 增量游标：每次 chat() 开始重置，让本轮第一次能打全量
        self._dump_seen_count = 0

        _section("初始化 cb-agent")

        # 1. LLM
        _info("初始化 LLM 客户端")
        self.llm = CbAgentsLLM()
        _info(f"模型: {self.llm.model}  function_calling={self.llm.is_Function_Calling}")

        # 2. 事件总线（要在工具注册前建好，TodoTool 等工具构造期就要拿到）
        self.event_bus = EventBus()

        # 3. 工具注册表
        self.registry = ToolRegistry()
        self._memory_tool = None
        self._rag_tool = None
        self._skill_manager: SkillManager = None  # type: ignore[assignment]
        self._register_native_tools()
        self._prepare_mcp_loading()

        # 4. 工具调度器（依赖 event_bus）
        self.executor = ToolExecutor(
            runner=self.registry.execute_tool,
            event_bus=self.event_bus,
            max_workers=4,
        )

        # 4. 上下文模块装配(对齐 Claude Code 重构后)
        # MemoryLoader 替代旧 ContextBuilder + MarkdownMemoryProvider 组合,
        # 走多级 CLAUDE.md 路径优先级链(Managed > User > Project > Local)。
        # OpenAICompatibleAdapter 把分段 system prompt join 成单 string,适配
        # 国内厂商的 OpenAI 兼容 API。
        memory_loader = MemoryLoader(cwd=Path(_HERE)) if self.ctx_enabled else None
        provider_adapter = OpenAICompatibleAdapter()
        # 跨轮工作上下文采用项目级持久化:这些状态和当前仓库文件强绑定,
        # 放在 .cbagent/sessions 比放到用户级 ~/.cb-agent 更容易理解和清理。
        session_store = LocalSessionStore(Path(_HERE) / ".cbagent" / "sessions")
        # trace_summarizer 只在工具轨迹超过阈值时静默调用;小 trace 走规则压缩。
        # 它不会走主回答的 llm.think 流式路径,因此不会向 UI 误发 text_delta。
        trace_summarizer = TraceSummarizer(self.llm)

        # 消息日志:将所有发送给 LLM 的消息全文记录到独立文件,
        # 包含 system/user/assistant/tool 全部角色的消息内容
        message_logger = None
        if self.logging_settings.message_log_mode != "off":
            message_logger = MessageLogger(
                self.logging_settings.log_dir / f"messages-{int(time.time())}.log",
                mode=self.logging_settings.message_log_mode,
            )
            logger.info(
                "message logger enabled: mode=%s path=%s",
                self.logging_settings.message_log_mode,
                message_logger.path,
            )
        else:
            logger.info("message logger disabled at log level=%s", self.logging_settings.verbosity)

        # 5. 会话核心(纯逻辑)
        self.session = AgentSession(
            llm=self.llm,
            registry=self.registry,
            executor=self.executor,
            event_bus=self.event_bus,
            memory_loader=memory_loader,
            provider_adapter=provider_adapter,
            skill_manager=self._skill_manager,
            bash_prompt_provider=self._memory_prompt_provider,
            ctx_enabled=self.ctx_enabled,
            messages_snapshot_hook=self._on_messages_snapshot,
            session_store=session_store,
            trace_summarizer=trace_summarizer,
            message_logger=message_logger,
            buddy_manager=self.buddy_manager,
        )
        # Gateway 只持有 AgentSession，不直接知道 AgentRunner。这里把 MCP 运行态
        # 以“可选回调”的形式挂到 session 上：旧代码不需要感知这些属性；JSON-RPC
        # 模式则可以在 gateway_ready 后启动后台连接，并通过 session.mcp_status
        # 查询当前进度。MCP 状态是运行时信息，不写入 history。
        self.session.mcp_status_provider = self.mcp_status
        self.session.mcp_background_loader = self.start_mcp_background_loading

        # 5b. 依赖 session 共享态的工具：AskUserQuestionTool 需要 session 的
        # question_registry + event_bus（跨工具线程同步），在 session 构造完后注册
        self.registry.register_tool(
            AskUserQuestionTool(
                question_registry=self.session.question_registry,
                event_bus=self.event_bus,
            )
        )

        # 5c. 给全局 PermissionGate 装上 question_channel，让 bash 权限弹框
        # 也能走 UI 而不是 stdin（TUI 模式下 stdin 被前端接管）
        from agent.question_channel import QuestionChannel
        from tools.tools.bash_permission import get_permission_gate
        get_permission_gate().question_channel = QuestionChannel(
            self.session.question_registry, self.event_bus,
        )

        # 6. CLI 渲染器（订阅事件 → stdout）。gateway 模式下不挂，由 transport 转发事件
        if self._attach_cli_renderer:
            self.renderer = CLIRenderer(self.event_bus)
            self.renderer.attach()
        else:
            self.renderer = None

        # 同时订阅 Done 事件，方便 REPL 自己拿到 final_answer / rounds_used
        self._last_done: Done | None = None
        self.event_bus.subscribe(self._on_done, Done)

        _section("就绪")
        _info(f"已注册工具 {len(self.registry.list_tools())} 个: {', '.join(self.registry.list_tools())}")
        _info(f"Skill 数量 {len(self._skill_manager.list_skills())}")
        _info(f"上下文构建器: {'开启' if self.ctx_enabled else '关闭'}")
        _info(f"记忆系统: {self.memory_system}")
        _info(f"MCP: {self._format_mcp_status_line(self.mcp_status())}")
        _info(f"messages dump: {'开启' if self.dump_messages else '关闭'} (用 /msg off 关闭)")
        print()

    # ---------- 启动期工具注册 ----------

    def _create_markdown_memory_provider(self):
        """重构后保留为兼容 stub —— Markdown 记忆改由 MemoryLoader 统一加载。

        旧的 MarkdownMemoryProvider 已删除,记忆加载现在走 context.memory.MemoryLoader
        的多级 CLAUDE.md 路径优先级链。这里仍然在 light 模式下保证全局/项目级
        memory 目录存在并写入模板 CLAUDE.md,以便用户首次打开 TUI 就能编辑。

        返回 None: bash_prompt_provider 调用方拿到 None 时会跳过 memory 提示段
        (memory 内容已经被 MemoryLoader 注入 system prompt 的 dynamic memory section)。
        """
        if self.memory_system != "light":
            return None
        try:
            global_dir = Path.home() / ".cbagent"
            project_dir = Path(_HERE) / ".cbagent" / "memory"
            for d in (global_dir, project_dir):
                d.mkdir(parents=True, exist_ok=True)
            user_memory = global_dir / "CLAUDE.md"
            if not user_memory.exists():
                user_memory.write_text(
                    "# cb-agent 用户全局记忆\n\n"
                    "这是 cb-agent 跨项目共享的全局指令。把对所有项目都适用的偏好、"
                    "规则、工具使用约定写在这里。具体项目的指令请放在项目根目录的 CLAUDE.md。\n",
                    encoding="utf-8",
                )
        except Exception as e:
            _err(f"轻量记忆目录初始化失败(继续启动): {e}")
        return None

    def _register_native_tools(self) -> None:
        """注册项目内置工具。"""
        _info("注册原生工具")
        self._skill_manager = SkillManager()
        skill_executor = SkillExecutor()

        tools = []
        if self.memory_system == "full":
            # 旧 RAG/向量记忆只在 full 模式懒加载。这样 light/off 的默认启动路径
            # 不会 import memory_tool/rag_tool，也就不需要 embedding、Qdrant 等依赖。
            try:
                from tools.tools.memory_tool import MemoryTool
                from tools.tools.rag_tool import RAGTool
                self._memory_tool = MemoryTool()
                self._rag_tool = RAGTool()
                tools.extend([self._memory_tool, self._rag_tool])
            except Exception as e:
                _err(f"full 记忆工具加载失败（跳过 memory/rag）: {e}")
                self._memory_tool = None
                self._rag_tool = None
        elif self.memory_system == "light":
            _info("使用轻量 Markdown 记忆：不注册 memory/rag 工具")
        else:
            _info("记忆系统关闭：不注册 memory/rag 工具")

        tools.extend([
            TodoTool(event_bus=self.event_bus),
            ListToolsTool(self.registry),
            SearchTool(),
            GlobTool(),
            GrepTool(),
            LsTool(),
            SkillTool(self._skill_manager),
            RunSkillScriptTool(self._skill_manager, skill_executor),
            BashTool(),
            BashTaskTool(),
            BashPermissionTool(),
            FileReadTool(),
            FileEditTool(),
            FileWriteTool(),
        ])

        for tool in tools:
            try:
                self.registry.register_tool(tool)
            except Exception as e:
                _err(f"工具 {tool.name} 注册失败: {e}")

    def _memory_prompt_provider(self) -> str:
        """返回轻量 Markdown 记忆的系统提示片段。

        轻量模式不注册专门的 memory tool，因此必须在系统提示里告诉模型两级记忆
        文件的位置和修改约束。模型若要保存记忆，会使用现有 file_read/file_write，
        仍然受文件写入工具的 read-before-write 保护。
        """
        if self._md_memory_provider is None:
            return ""
        return self._md_memory_provider.memory_instructions()

    def _initial_mcp_status(self) -> Dict[str, Any]:
        """创建 MCP 状态快照的初始值。

        这里不能做任何慢操作，只记录当前启动参数是否允许 MCP。真正读取 mcp.json
        和连接 server 都在后续步骤完成，保证 AgentRunner 构造尽量轻。
        """
        if not self.use_mcp:
            reason = "未安装 MCP 依赖" if not _HAS_MCP else "启动参数 --no-mcp 已关闭"
            return {
                "status": "disabled",
                "servers": [],
                "total": 0,
                "connected": 0,
                "failed": 0,
                "error": reason,
            }
        return {
            "status": "pending",
            "servers": [],
            "total": 0,
            "connected": 0,
            "failed": 0,
        }

    def _prepare_mcp_loading(self) -> None:
        """快速读取 MCP server 列表，但不连接任何 server。

        旧实现会在启动期同步调用 MCPTool(...)._discover_tools()，这会启动外部
        MCP 进程并等待 list_tools，配置多时 TUI 会迟迟收不到 gateway_ready。
        新实现只在这里读取 mcp.json 中的 server 名称和命令，把慢连接延后到
        start_mcp_background_loading()，由 Gateway 发出 ready 后触发。
        """
        if not self.use_mcp:
            return
        _info("读取 MCP 配置（后台连接）")
        try:
            server_configs = load_mcp_server_configs()
        except FileNotFoundError:
            with self._mcp_lock:
                self._mcp_status = {
                    "status": "disabled",
                    "servers": [],
                    "total": 0,
                    "connected": 0,
                    "failed": 0,
                    "error": "未找到 mcp.json",
                }
            return
        except Exception as e:
            with self._mcp_lock:
                self._mcp_status = {
                    "status": "error",
                    "servers": [],
                    "total": 0,
                    "connected": 0,
                    "failed": 1,
                    "error": str(e),
                }
            _err(f"MCP 配置读取失败（跳过）: {e}")
            return

        servers = []
        for item in server_configs:
            servers.append({
                "name": item.get("name", ""),
                "status": "pending",
                "tools_count": 0,
                "elapsed_seconds": 0.0,
                "error": None,
                "_config": item,
            })
        with self._mcp_lock:
            self._mcp_status = {
                "status": "pending" if servers else "ready",
                "servers": servers,
                "total": len(servers),
                "connected": 0,
                "failed": 0,
            }

    def mcp_status(self) -> Dict[str, Any]:
        """返回 MCP 状态快照，供 Gateway RPC/TUI 展示。

        返回值会剥掉内部使用的 ``_config``，避免把 command/env 这类实现细节或
        潜在敏感配置透给前端。前端只需要 name/status/tools_count/error。
        """
        with self._mcp_lock:
            snapshot = dict(self._mcp_status)
            servers = []
            for server in self._mcp_status.get("servers", []):
                public = {k: v for k, v in server.items() if not k.startswith("_")}
                servers.append(public)
            snapshot["servers"] = servers
        return snapshot

    def _emit_mcp_status(self) -> None:
        """把当前 MCP 快照作为事件发出；失败只记日志，不影响后台加载。"""
        snapshot = self.mcp_status()
        try:
            self.event_bus.emit(MCPStatus(
                status=str(snapshot.get("status") or "unknown"),
                servers=list(snapshot.get("servers") or []),
                total=int(snapshot.get("total") or 0),
                connected=int(snapshot.get("connected") or 0),
                failed=int(snapshot.get("failed") or 0),
            ))
        except Exception:
            logging.getLogger(__name__).exception("emit MCP status failed")

    def start_mcp_background_loading(self) -> Dict[str, Any]:
        """启动 MCP 后台连接线程，并立即返回当前状态。

        Gateway 在发出 gateway_ready 之后调用它，TUI 因此能先进入可输入状态；
        MCP 工具准备好后会动态注册到 ToolRegistry，下一轮 prompt 就会自然出现在
        tool schema 中。重复调用是幂等的，/mcp 查询不会重复启动连接。
        """
        with self._mcp_lock:
            status = self._mcp_status.get("status")
            servers = self._mcp_status.get("servers") or []
            if status in {"disabled", "ready", "error"} or not servers:
                return self.mcp_status()
            if self._mcp_started:
                return self.mcp_status()
            self._mcp_started = True
            self._mcp_status["status"] = "loading"

        self._emit_mcp_status()
        thread = threading.Thread(
            target=self._load_mcp_tools_background,
            name="cb-agent-mcp-loader",
            daemon=True,
        )
        with self._mcp_lock:
            self._mcp_thread = thread
        thread.start()
        return self.mcp_status()

    def _load_mcp_tools_background(self) -> None:
        """后台逐个连接 MCP server，并把展开后的工具注册进 registry。"""
        try:
            # MCPTool 的构造会导入 fastmcp 并同步发现工具；必须放在后台线程里。
            # 这样 TUI 首屏和第一句 prompt 不会因为 MCP 依赖导入或 server 握手而卡住。
            from tools.mcp_tools.mcptool import MCPTool
        except Exception as e:
            with self._mcp_lock:
                for item in self._mcp_status.get("servers", []):
                    if item.get("status") not in {"connected", "error"}:
                        item["status"] = "error"
                        item["error"] = f"MCP 依赖加载失败: {e}"
                self._mcp_status["failed"] = len(self._mcp_status.get("servers") or [])
                self._mcp_status["connected"] = 0
                self._mcp_status["status"] = "error"
                self._mcp_status["error"] = str(e)
            _err(f"MCP 依赖加载失败: {e}")
            self._emit_mcp_status()
            return

        with self._mcp_lock:
            servers = list(self._mcp_status.get("servers") or [])

        for index, server in enumerate(servers):
            started_at = time.monotonic()
            name = str(server.get("name") or f"mcp_{index + 1}")
            config = server.get("_config") if isinstance(server.get("_config"), dict) else {}
            with self._mcp_lock:
                current = self._mcp_status["servers"][index]
                current["status"] = "connecting"
                current["error"] = None
            self._emit_mcp_status()

            try:
                mcp_tool = MCPTool(
                    name=name,
                    server_command=config.get("server_command"),
                    env=config.get("env"),
                )
                expanded = mcp_tool.get_expanded_tools()
                registered_count = 0
                if not expanded:
                    self.registry.register_tool(mcp_tool)
                    registered_count = 1
                else:
                    for sub in expanded:
                        self.registry.register_tool(sub)
                        registered_count += 1
                elapsed = round(time.monotonic() - started_at, 2)
                with self._mcp_lock:
                    current = self._mcp_status["servers"][index]
                    current["status"] = "connected"
                    current["tools_count"] = registered_count
                    current["elapsed_seconds"] = elapsed
                    current["error"] = None
                    self._mcp_status["connected"] = sum(
                        1 for item in self._mcp_status["servers"]
                        if item.get("status") == "connected"
                    )
            except Exception as e:
                elapsed = round(time.monotonic() - started_at, 2)
                with self._mcp_lock:
                    current = self._mcp_status["servers"][index]
                    current["status"] = "error"
                    current["elapsed_seconds"] = elapsed
                    current["error"] = str(e)
                    self._mcp_status["failed"] = sum(
                        1 for item in self._mcp_status["servers"]
                        if item.get("status") == "error"
                    )
                _err(f"MCP 服务器 {name} 连接失败: {e}")
            self._emit_mcp_status()

        with self._mcp_lock:
            failed = sum(1 for item in self._mcp_status["servers"] if item.get("status") == "error")
            connected = sum(1 for item in self._mcp_status["servers"] if item.get("status") == "connected")
            self._mcp_status["failed"] = failed
            self._mcp_status["connected"] = connected
            self._mcp_status["status"] = "error" if failed and not connected else "ready"
        self._emit_mcp_status()

    def _format_mcp_status_line(self, status: Dict[str, Any]) -> str:
        """CLI 启动摘要里用的一行 MCP 状态。"""
        state = status.get("status", "unknown")
        total = int(status.get("total") or 0)
        connected = int(status.get("connected") or 0)
        failed = int(status.get("failed") or 0)
        if total:
            return f"{state} ({connected}/{total} connected, {failed} failed)"
        return str(status.get("error") or state)

    # ---------- 钩子 ----------

    def _on_messages_snapshot(self, messages: List[Dict[str, Any]], round_idx: int) -> None:
        """每轮 think 前增量打 messages dump。配合 self.dump_messages 开关。"""
        if not self.dump_messages:
            return
        seen = self._dump_seen_count
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

    def _on_done(self, e: Done) -> None:
        self._last_done = e

    # ---------- REPL ----------

    def run(self) -> None:
        """同步入口，内部跑一个 asyncio loop。

        为什么不直接 sync：要在用户按 Ctrl-C 时**取消当前 chat 但不退进程**。
        sync REPL 下 input() 阻塞主线程，KeyboardInterrupt 直接抛出 input
        外面，没办法区分"用户在输入态按 Ctrl-C 想退出"和"用户在 chat 中按
        Ctrl-C 想中断这次回答"。

        async 实现：input() 用 asyncio.to_thread 跑在 worker，主 loop 同时
        监听 signal。chat 跑在另一个线程（chat_async），收到 SIGINT 时调
        session.current_cancel_token.cancel()，chat 自然收尾后 await 返回。
        """
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            # 输入态下的二次 Ctrl-C 兜底
            print()
            _info("再见")

    async def _run_async(self) -> None:
        _section("交互模式")
        print(
            "输入问题与我对话，输入 /help 看命令，/quit 退出。\n"
            "对话进行中按 Ctrl-C 中断当前回答（不退出进程）；空闲时按 Ctrl-C 或 /quit 退出。\n"
        )
        # CLI 没有 gateway_ready 这个协议层事件，因此在进入输入循环前主动启动
        # 后台 MCP 连接。连接仍在 daemon 线程里跑，用户可以立刻输入，也可以用
        # /mcp 查看进度。
        self.start_mcp_background_loading()

        while True:
            try:
                user_input = (await asyncio.to_thread(input, "you > ")).strip()
            except EOFError:
                print()
                _info("再见")
                return
            except KeyboardInterrupt:
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

            self._dump_seen_count = 0
            attachments = list(self.pending_attachments)
            ok = await self._run_chat(user_input, attachments=attachments)
            if ok and attachments:
                self.pending_attachments.clear()

    async def _run_chat(self, user_input: str, attachments: List[Dict[str, Any]] | None = None) -> bool:
        """跑一次 chat，期间安装临时 SIGINT handler 实现"中断而不退出"。"""
        from agent.cancel import CancelToken

        token = CancelToken()
        prev_handler = signal.getsignal(signal.SIGINT)

        def _on_sigint(_signum, _frame):
            # signal handler 在主线程执行；调 token.cancel() 不阻塞
            # 过去只设置 token，若 SDK 正阻塞等待下一个 stream chunk，就必须等到
            # provider 再吐数据才会真正停下。现在同步 close 活跃 stream，让 Ctrl-C
            # 能打断底层流式连接；界面输出仍交给 CLIRenderer 处理。
            token.cancel()
            cancel_streams = getattr(self.llm, "cancel_active_streams", None)
            if callable(cancel_streams):
                try:
                    cancel_streams("cli_sigint")
                except Exception:
                    logging.getLogger(__name__).exception("failed to close stream on Ctrl-C")

        try:
            signal.signal(signal.SIGINT, _on_sigint)
        except (ValueError, OSError):
            # 某些环境（如非主线程、无控制台）signal.signal 会失败
            # 退化到无 Ctrl-C 中断；不影响其它路径
            prev_handler = None

        try:
            await self.session.chat_async(user_input, cancel_token=token, attachments=attachments or [])
            return True
        except Exception as e:
            _err(f"本轮对话异常: {e}")
            traceback.print_exc()
            return False
        finally:
            if prev_handler is not None:
                try:
                    signal.signal(signal.SIGINT, prev_handler)
                except (ValueError, OSError):
                    pass

    def _handle_command(self, line: str) -> bool:
        """斜杠命令分派。返回 True 继续 REPL，False 退出。"""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        # raw_arg 保留原文，供 /switch 这类需要完整 id 的命令使用；
        # arg_lower 只用于 on/off 这种固定关键字，避免把未来参数意外改写。
        raw_arg = parts[1].strip() if len(parts) > 1 else ""
        arg_lower = raw_arg.lower()

        if cmd in ("/quit", "/exit"):
            _info("再见")
            return False

        if cmd == "/help":
            print(
                "\n可用命令：\n"
                "  /help        打印帮助\n"
                "  /tools       列出所有已注册工具\n"
                "  /mcp         查看 MCP 后台连接状态\n"
                "  /buddy       查看/孵化/互动 Buddy 宠物\n"
                "  /attach PATH 添加图片或音频附件到下一轮\n"
                "  /attachments 查看待发送附件队列\n"
                "  /detach N|all 移除待发送附件\n"
                "  /skills      列出所有 Skill\n"
                "  /skill NAME  手动加载指定 Skill\n"
                "  /history     查看当前会话历史\n"
                "  /sessions    列出本项目的本地会话\n"
                "  /new         新建并切换到空白会话\n"
                "  /switch ID   切换到指定会话\n"
                "  /clear       清空会话历史\n"
                "  /ctx on|off  开关 ContextBuilder (当前: "
                + ("on" if self.session.ctx_enabled else "off")
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
        elif cmd == "/mcp":
            # /mcp 只读运行态，不参与当前对话，也不会写入本地 session。
            # start_mcp_background_loading 是幂等的：如果还没启动就启动，如果已经
            # 在连或已完成，就只是返回当前快照。
            status = self.start_mcp_background_loading()
            print(f"\nMCP 状态: {self._format_mcp_status_line(status)}")
            servers = status.get("servers") or []
            if not servers:
                reason = status.get("error")
                if reason:
                    print(f"  - {reason}")
            for item in servers:
                name = item.get("name", "unknown")
                state = item.get("status", "unknown")
                tools_count = item.get("tools_count", 0)
                elapsed = item.get("elapsed_seconds", 0)
                error = item.get("error")
                tail = f", tools={tools_count}" if tools_count else ""
                if elapsed:
                    tail += f", {elapsed}s"
                if error:
                    tail += f", error={error}"
                print(f"  - {name}: {state}{tail}")
            print()
        elif cmd == "/buddy":
            # Buddy 是独立 UI 附属状态，不进入当前会话 history。CLI 下没有输入框旁
            # sprite，但仍可以查看、孵化、摸摸、静音，TUI 会共享同一个配置文件。
            result = self.buddy_manager.handle_command(raw_arg)
            text = result.get("text") if isinstance(result, dict) else ""
            if text:
                print("\n" + str(text) + "\n")
        elif cmd == "/attach":
            self._handle_attach_command(raw_arg)
        elif cmd == "/attachments":
            self._print_pending_attachments()
        elif cmd == "/detach":
            self._handle_detach_command(raw_arg)
        elif cmd == "/skills":
            skills = self._skill_manager.list_skills()
            print(f"\n已发现 {len(skills)} 个 Skill：")
            for s in skills:
                print(f"  - {s.name}: {(s.description or '')[:80]}")
            print()
        elif cmd == "/skill":
            self._handle_skill_command(raw_arg)
        elif cmd == "/history":
            history = self.session.history
            print(f"\n会话历史 ({len(history)} 条)：")
            for i, m in enumerate(history, 1):
                role = m.role.value if hasattr(m.role, "value") else str(m.role)
                content = m.content if isinstance(m.content, str) else json.dumps(
                    m.content, ensure_ascii=False
                )
                preview = (content or "")[:120]
                print(f"  {i:2d}. [{role}] {preview}")
            print()
        elif cmd == "/sessions":
            # 多会话列表只展示 LocalSessionStore 提供的摘要，不读取 transcript 全文。
            # 这样 CLI 查看列表不会把旧对话的大段工作记录重新塞进当前上下文。
            sessions = self.session.list_sessions()
            if not sessions:
                _info("当前项目还没有本地会话")
                return True
            print(f"\n本地会话 ({len(sessions)} 个)：")
            for item in sessions:
                mark = "*" if item.get("is_active") else " "
                sid = item.get("session_id", "")
                turns = item.get("turn_count", 0)
                updated = str(item.get("updated_at") or "")[:19]
                preview = item.get("active_task") or item.get("rolling_summary") or "（空会话）"
                print(f" {mark} {sid}  turns={turns}  updated={updated}")
                print(f"     {str(preview)[:100]}")
            print("\n用 /switch <session_id> 切换；用 /new 新建空白会话。")
        elif cmd == "/new":
            # 新建会话会立即清空内存 history，并把 store active 指向新的独立目录。
            # 旧会话仍留在 .cbagent/sessions 下，可用 /sessions 找回。
            payload = self.session.create_session()
            session_info = payload.get("session") if isinstance(payload, dict) else None
            sid = session_info.get("session_id") if isinstance(session_info, dict) else "（未启用本地存储）"
            _info(f"已新建并切换到会话 {sid}")
        elif cmd == "/switch":
            if not raw_arg:
                _info("用法: /switch <session_id>")
                return True
            try:
                payload = self.session.switch_session(raw_arg)
            except Exception as e:
                _err(f"切换会话失败: {e}")
                return True
            session_info = payload.get("session") if isinstance(payload, dict) else None
            sid = session_info.get("session_id") if isinstance(session_info, dict) else raw_arg
            restored = payload.get("history") if isinstance(payload, dict) else []
            _info(f"已切换到会话 {sid}，恢复 history {len(restored)} 条")
        elif cmd == "/clear":
            # /clear 现在是彻底清理：内存 history + active session 本地文件。
            # 这样重启后不会因为 index.json 指向旧 session 而自动恢复旧上下文。
            self.session.clear_history()
            _info("会话历史与本地会话记录已删除")
        elif cmd == "/ctx":
            if arg_lower in ("on", "off"):
                self.session.ctx_enabled = arg_lower == "on"
                _info(f"ContextBuilder = {arg_lower}")
            else:
                _info(f"用法: /ctx on|off  (当前: {'on' if self.session.ctx_enabled else 'off'})")
        elif cmd == "/msg":
            if arg_lower in ("on", "off"):
                self.dump_messages = arg_lower == "on"
                _info(f"messages dump = {arg_lower}")
            else:
                _info(f"用法: /msg on|off  (当前: {'on' if self.dump_messages else 'off'})")
        else:
            if self._handle_named_skill_command(cmd, raw_arg):
                return True
            _err(f"未知命令 {cmd}，/help 查看可用命令")
        return True

    def _handle_attach_command(self, raw_arg: str) -> None:
        """把一个本地文件加入 CLI 待发送附件队列。

        这里故意只做轻量路径存在性提示，不在 CLI 里调用 OCR/ASR。真正的格式识别、
        大小限制、基模是否支持图片、以及 OCR/ASR 错误，都由
        agent.multimodal_input 统一处理，保证 CLI/TUI/Web 未来共用同一套规则。
        """
        raw_path = raw_arg.strip().strip('"').strip("'")
        if not raw_path:
            _info("用法: /attach <path>")
            return
        path = Path(raw_path)
        if path.is_absolute():
            resolved = path
        else:
            try:
                from tools.tools.bash_session import get_session
                base_dir = Path(get_session().cwd)
            except Exception:
                base_dir = Path.cwd()
            resolved = base_dir / path
        if not resolved.exists() or not resolved.is_file():
            _err(f"附件文件不存在或不是普通文件: {resolved}")
            return
        self.pending_attachments.append({
            "path": str(resolved.resolve()),
            "source": "direct",
        })
        _info(f"已添加附件 #{len(self.pending_attachments)}: {resolved.name} ({resolved.stat().st_size} bytes)")

    def _print_pending_attachments(self) -> None:
        """展示 CLI 下一轮会随 prompt 一起发送的附件。"""
        if not self.pending_attachments:
            _info("当前没有待发送附件。使用 /attach <path> 添加图片或音频。")
            return
        print("\n待发送附件：")
        for index, item in enumerate(self.pending_attachments, start=1):
            path = Path(str(item.get("path") or ""))
            size = path.stat().st_size if path.exists() else 0
            print(f"  {index}. {path.name}  {size} bytes  {path}")
        print()

    def _handle_detach_command(self, raw_arg: str) -> None:
        """从 CLI 待发送附件队列移除一个附件或全部清空。"""
        arg = raw_arg.strip().lower()
        if not arg:
            _info("用法: /detach <index|all>")
            return
        if arg == "all":
            count = len(self.pending_attachments)
            self.pending_attachments.clear()
            _info(f"已清空 {count} 个待发送附件。")
            return
        try:
            index = int(arg)
        except ValueError:
            _info("用法: /detach <index|all>")
            return
        if index < 1 or index > len(self.pending_attachments):
            _err(f"附件序号超出范围: {index}")
            return
        removed = self.pending_attachments.pop(index - 1)
        _info(f"已移除附件: {Path(str(removed.get('path') or '')).name}")

    def _handle_named_skill_command(self, cmd: str, raw_arg: str) -> bool:
        """处理 /pdf 这类按 Skill 名称直接触发的命令。"""
        if not cmd.startswith("/") or len(cmd) <= 1:
            return False
        skill_name = cmd[1:]
        skill = self._skill_manager.get_skill(skill_name)
        if skill is None:
            return False
        self._print_skill_content(skill_name, raw_arg)
        return True

    def _handle_skill_command(self, raw_arg: str) -> None:
        """处理 /skill NAME [args]。"""
        if not raw_arg:
            print("\n" + self._skill_manager.format_skill_list() + "\n")
            return
        parts = raw_arg.split(maxsplit=1)
        skill_name = parts[0].strip()
        args = parts[1].strip() if len(parts) > 1 else ""
        self._print_skill_content(skill_name, args)

    def _print_skill_content(self, skill_name: str, args: str = "") -> None:
        """用户显式触发 Skill：尊重 user_invocable，但不受模型禁用字段限制。"""
        self._skill_manager.check_for_changes()
        skill = self._skill_manager.get_skill(skill_name)
        if skill is None:
            available = ", ".join(s.name for s in self._skill_manager.list_skills())
            _err(f"未找到 Skill '{skill_name}'。可用 Skill: {available}")
            return
        if not skill.user_invocable:
            _err(f"Skill '{skill.name}' 不允许用户通过 slash 命令手动触发")
            return
        self._skill_manager.record_usage(skill.name)
        content = self._skill_manager.load_skill_content(skill.name, args)
        print(f"\n{content}\n")


# ========== 入口 ==========


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cb-agent",
        description="cb-agent 命令行入口。默认进 CLI 交互；--transport jsonrpc 切到 stdio 网关模式给外部 UI 用。",
    )
    parser.add_argument(
        "--transport",
        choices=["cli", "jsonrpc"],
        default="cli",
        help="cli=REPL 直接打印；jsonrpc=stdio NDJSON 网关模式",
    )
    parser.add_argument(
        "--no-mcp", action="store_true",
        help="跳过 MCP 工具注册（调试加速）",
    )
    parser.add_argument(
        "--no-ctx", action="store_true",
        help="禁用 ContextBuilder（裸跑，记忆不参与拼 system）",
    )
    parser.add_argument(
        "--memory-system",
        choices=["light", "full", "off"],
        default="light",
        help=(
            "记忆系统选择：light=Markdown 文件记忆（默认，无 RAG 依赖）；"
            "full=旧 MemoryTool/RAGTool；off=关闭记忆"
        ),
    )
    args = parser.parse_args()

    use_mcp = not args.no_mcp
    ctx_enabled = not args.no_ctx
    memory_system = args.memory_system

    if args.transport == "jsonrpc":
        # gateway 模式：先把 stdout 切到 stderr，AgentRunner 启动期 print 不会污染协议
        # 真 stdout 留给 Gateway 写 JSON 用
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        runner = AgentRunner(
            use_mcp=use_mcp,
            ctx_enabled=ctx_enabled,
            attach_cli_renderer=False,
            memory_system=memory_system,
        )
        from agent.transport import Gateway, StdioTransport
        gw = Gateway(
            session=runner.session,
            event_bus=runner.event_bus,
            transport=StdioTransport(stdin=sys.stdin, stdout=real_stdout),
            redirect_stdout_to_stderr=False,  # 上面已经切过了
        )
        gw.serve_forever()
        return

    runner = AgentRunner(use_mcp=use_mcp, ctx_enabled=ctx_enabled, memory_system=memory_system)
    runner.run()


if __name__ == "__main__":
    main()

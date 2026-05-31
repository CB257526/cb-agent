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
import traceback
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

from agent.cb_agents import CbAgentsLLM
from agent.event_bus import EventBus
from agent.events import Done
from agent.executor import ToolExecutor
from agent.renderers.cli import CLIRenderer
from agent.session import AgentSession
from context import ContextBuilder, ContextConfig
from skills.skill_manager import SkillManager
from skills.skill_executor import SkillExecutor
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

try:
    from tools.mcp_tools.mcptools_add import load_mcp_tools
    _HAS_MCP = True
except Exception:
    _HAS_MCP = False


# 日志：默认只显示 WARNING 以上，避免被各模块的 INFO 刷屏
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
for noisy in ("memory", "memory.types", "memory.storage", "memory.manager"):
    logging.getLogger(noisy).setLevel(logging.ERROR)


# ========== 启动期纯字符输出 ==========


def _hr(char: str = "─", width: int = 60) -> str:
    return char * width


def _section(title: str) -> None:
    print(f"\n{_hr()}\n{title}\n{_hr()}")


def _info(msg: str) -> None:
    print(f"[*] {msg}")


def _err(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)


# ========== AgentRunner（装配 + REPL）==========


class AgentRunner:
    """装配所有依赖、跑 REPL。运行时的渲染交给 CLIRenderer，逻辑在 AgentSession。"""

    def __init__(
        self,
        use_mcp: bool = True,
        ctx_enabled: bool = True,
        attach_cli_renderer: bool = True,
    ) -> None:
        self.use_mcp = use_mcp and _HAS_MCP
        self.ctx_enabled = ctx_enabled
        self.dump_messages = True
        self._attach_cli_renderer = attach_cli_renderer
        # dump 增量游标：每次 chat() 开始重置，让本轮第一次能打全量
        self._dump_seen_count = 0

        _section("初始化 cb-agent")

        # 1. LLM
        _info("初始化 LLM 客户端")
        self.llm = CbAgentsLLM()
        _info(f"模型: {self.llm.model}  function_calling={self.llm.is_Function_Calling}")

        # 2. 工具注册表
        self.registry = ToolRegistry()
        self._memory_tool: MemoryTool = None  # type: ignore[assignment]
        self._rag_tool: RAGTool = None        # type: ignore[assignment]
        self._skill_manager: SkillManager = None  # type: ignore[assignment]
        self._register_native_tools()
        if self.use_mcp:
            self._register_mcp_tools()

        # 3. 事件总线 + 工具调度器
        self.event_bus = EventBus()
        self.executor = ToolExecutor(
            runner=self.registry.execute_tool,
            event_bus=self.event_bus,
            max_workers=4,
        )

        # 4. 上下文构建器
        builder = ContextBuilder(
            memory_tool=self._memory_tool,
            rag_tool=self._rag_tool,
            config=ContextConfig(
                max_tokens=8000,
                min_relevance=0.05,
                history_max_messages=8,
            ),
        )

        # 5. 会话核心（纯逻辑）
        self.session = AgentSession(
            llm=self.llm,
            registry=self.registry,
            executor=self.executor,
            event_bus=self.event_bus,
            builder=builder,
            skill_manager=self._skill_manager,
            ctx_enabled=self.ctx_enabled,
            messages_snapshot_hook=self._on_messages_snapshot,
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
        _info(f"messages dump: {'开启' if self.dump_messages else '关闭'} (用 /msg off 关闭)")
        print()

    # ---------- 启动期工具注册 ----------

    def _register_native_tools(self) -> None:
        """注册项目内置工具。"""
        _info("注册原生工具")
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
                    self.registry.register_tool(mcp_tool)
                    continue
                for sub in expanded:
                    self.registry.register_tool(sub)
            except Exception as e:
                _err(f"MCP 工具 {getattr(mcp_tool, 'name', '?')} 展开失败: {e}")

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
            await self._run_chat(user_input)

    async def _run_chat(self, user_input: str) -> None:
        """跑一次 chat，期间安装临时 SIGINT handler 实现"中断而不退出"。"""
        from agent.cancel import CancelToken

        token = CancelToken()
        prev_handler = signal.getsignal(signal.SIGINT)

        def _on_sigint(_signum, _frame):
            # signal handler 在主线程执行；调 token.cancel() 不阻塞
            # cb_agents 流式循环每个 chunk 看 token.is_set()，下一个 chunk 边界停
            # 这里不直接 print；让 CLIRenderer 在收到 Cancelled 事件时打 ✗
            token.cancel()

        try:
            signal.signal(signal.SIGINT, _on_sigint)
        except (ValueError, OSError):
            # 某些环境（如非主线程、无控制台）signal.signal 会失败
            # 退化到无 Ctrl-C 中断；不影响其它路径
            prev_handler = None

        try:
            await self.session.chat_async(user_input, cancel_token=token)
        except Exception as e:
            _err(f"本轮对话异常: {e}")
            traceback.print_exc()
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
        elif cmd == "/skills":
            skills = self._skill_manager.list_skills()
            print(f"\n已发现 {len(skills)} 个 Skill：")
            for s in skills:
                print(f"  - {s.name}: {(s.description or '')[:80]}")
            print()
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
        elif cmd == "/clear":
            self.session.clear_history()
            _info("会话历史已清空")
        elif cmd == "/ctx":
            if arg in ("on", "off"):
                self.session.ctx_enabled = arg == "on"
                _info(f"ContextBuilder = {arg}")
            else:
                _info(f"用法: /ctx on|off  (当前: {'on' if self.session.ctx_enabled else 'off'})")
        elif cmd == "/msg":
            if arg in ("on", "off"):
                self.dump_messages = arg == "on"
                _info(f"messages dump = {arg}")
            else:
                _info(f"用法: /msg on|off  (当前: {'on' if self.dump_messages else 'off'})")
        else:
            _err(f"未知命令 {cmd}，/help 查看可用命令")
        return True


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
        help="禁用 ContextBuilder（裸跑，记忆/RAG 不参与拼 system）",
    )
    args = parser.parse_args()

    use_mcp = not args.no_mcp
    ctx_enabled = not args.no_ctx

    if args.transport == "jsonrpc":
        # gateway 模式：先把 stdout 切到 stderr，AgentRunner 启动期 print 不会污染协议
        # 真 stdout 留给 Gateway 写 JSON 用
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        runner = AgentRunner(
            use_mcp=use_mcp,
            ctx_enabled=ctx_enabled,
            attach_cli_renderer=False,
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

    runner = AgentRunner(use_mcp=use_mcp, ctx_enabled=ctx_enabled)
    runner.run()


if __name__ == "__main__":
    main()

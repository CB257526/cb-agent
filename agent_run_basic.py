"""agent_run_basic.py — 最简 Agent CLI

一个单文件的纯 CLI Agent，复用项目全部现有组件，只替换最外层调度逻辑。

复用的组件：
  - CbAgentsLLM          agent/cb_agents.py          LLM 调用（event_bus=None → print 模式）
  - ContextBuilder       context/builder.py          GSSC 上下文构建（Markdown 记忆 / full 记忆 + 历史）
  - ToolRegistry         tools/toolRegistry.py        工具注册/查找/执行
  - MemoryTool           tools/tools/memory_tool.py   记忆增删搜
  - RAGTool              tools/tools/rag_tool.py      知识库文档问答
  - SearchTool           tools/tools/search.py        网络搜索
  - TodoTool             tools/tools/todo_tool.py     任务管理
  - SkillTool            tools/tools/skill_tool.py    Skill 加载
  - RunSkillScriptTool   tools/tools/run_skill_script_tool.py  脚本执行
  - FileReadTool         tools/tools/file_read_tool.py     读文件
  - FileWriteTool        tools/tools/file_write_tool.py    写文件
  - BashTool             tools/tools/bash_tool.py          Shell 命令
  - BashTaskTool         tools/tools/bash_task_tool.py     后台任务管理
  - BashPermissionTool   tools/tools/bash_permission_tool.py 权限管理
  - SkillManager         skills/skill_manager.py      Skill 发现
  - Message              core/message.py              对话历史

去掉的（仅在 run_agent.py 的 TUI/事件驱动模式下需要）：
  - EventBus / 事件流
  - ToolExecutor 并发调度 → 改为串行 for 循环
  - QuestionChannel / 权限弹窗 → BashTool 的交互式权限会退化（返回 permission_unavailable）
  - AskUserQuestionTool → 依赖 EventBus + QuestionRegistry，跳过
  - MCP / JSON-RPC transport

跑法：
    cd cb-agent
    python agent_run_basic.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import textwrap
import traceback
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保能 import 项目模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Windows 终端 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 日志：只显示 WARNING 以上，压住 memory/rag 的启动噪声
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
for noisy in ("memory", "memory.types", "memory.storage", "memory.manager"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

from agent.cb_agents import CbAgentsLLM
from constant.llm.constant_llm import ConstantLLM
from context.builder import ContextBuilder, ContextConfig
from context.markdown_memory import MarkdownMemoryProvider
from core.message import Message
from tools.toolRegistry import ToolRegistry
from tools.tools.search import SearchTool
from tools.tools.todo_tool import TodoTool
from tools.tools.skill_tool import SkillTool
from tools.tools.run_skill_script_tool import RunSkillScriptTool
from tools.tools.file_read_tool import FileReadTool
from tools.tools.file_write_tool import FileWriteTool
from tools.tools.bash_tool import BashTool
from tools.tools.bash_task_tool import BashTaskTool
from tools.tools.bash_permission_tool import BashPermissionTool
from skills.skill_manager import SkillManager
from skills.skill_executor import SkillExecutor


# ============================================================
# 主 Agent
# ============================================================

class BasicAgent:
    """最简 Agent：复用全部现有组件，只替换最外层的 REPL + 工具循环。"""

    MAX_TOOL_ROUNDS = 8

    def __init__(self, memory_system: str = "light"):
        self.memory_system = memory_system
        self._md_memory_provider = self._create_markdown_memory_provider()
        print("=" * 50)
        print("Basic Agent — 初始化中…")
        print("=" * 50)

        # ── LLM ──
        self.llm = CbAgentsLLM()
        self._fc = self.llm.is_Function_Calling
        print(f"\n[LLM] {self.llm.model}  function_calling={self._fc}")

        # ── 工具注册 ──
        self.registry = ToolRegistry()
        self._skill_manager = SkillManager()
        self._register_tools()

        names = self.registry.list_tools()
        print(f"\n[工具] 已注册 {len(names)} 个: {', '.join(names)}")

        # ── 上下文构建器（复用 GSSC 流水线） ──
        context_max_tokens = ConstantLLM.context_window_tokens(self.llm.model)
        self.builder = ContextBuilder(
            memory_tool=self._memory_tool,
            rag_tool=self._rag_tool,
            md_memory_provider=self._md_memory_provider,
            config=ContextConfig(
                max_tokens=context_max_tokens,
                min_relevance=0.05,
                history_max_messages=8,
            ),
        )
        print(f"[上下文] ContextBuilder (GSSC) max_tokens={context_max_tokens}")
        print(f"[记忆] memory_system={self.memory_system}")

        # ── 对话历史 ──
        self.history: List[Message] = []

        print()

    # ========== 工具注册 ==========

    def _create_markdown_memory_provider(self):
        """按需创建并初始化轻量 Markdown 记忆目录。

        basic CLI 和主入口保持同一语义：light 模式启动后就能看到全局/项目两级
        MEMORY.md 模板，而不是等到第一轮 ContextBuilder 扫描时才被动创建。
        初始化失败只打印诊断，不影响继续跑 off/full 之外的核心功能。
        """
        if self.memory_system != "light":
            return None
        provider = MarkdownMemoryProvider(project_dir=Path(_HERE))
        try:
            provider.ensure_initialized()
        except Exception as e:
            print(f"  [!] 轻量 Markdown 记忆目录初始化失败（继续启动）: {e}")
        return provider

    def _register_tools(self) -> None:
        """注册所有项目内置工具。

        跳过的工具：
          - AskUserQuestionTool：依赖 EventBus + QuestionRegistry，CLI 模式下无 UI 弹窗
        """
        self._memory_tool = None
        self._rag_tool = None

        skill_executor = SkillExecutor()

        tools = []
        if self.memory_system == "full":
            # full 模式才懒加载旧 MemoryTool/RAGTool，避免 basic 入口在轻量安装下
            # 因为顶层 import 拉起 embedding/向量库依赖。
            try:
                from tools.tools.memory_tool import MemoryTool
                from tools.tools.rag_tool import RAGTool
                self._memory_tool = MemoryTool()
                self._rag_tool = RAGTool()
                tools.extend([self._memory_tool, self._rag_tool])
            except Exception as e:
                print(f"  [!] full 记忆工具加载失败（跳过 memory/rag）: {e}")
        elif self.memory_system == "light":
            print("  [*] 使用轻量 Markdown 记忆：不注册 memory/rag 工具")
        else:
            print("  [*] 记忆系统关闭：不注册 memory/rag 工具")

        tools.extend([
            SearchTool(),
            TodoTool(),                                  # event_bus=None，不发事件，功能正常
            SkillTool(self._skill_manager),
            RunSkillScriptTool(self._skill_manager, skill_executor),
            FileReadTool(),
            FileWriteTool(),
            BashTool(),                                  # 无 question_channel，交互式权限会退化
            BashTaskTool(),
            BashPermissionTool(),
        ])

        for tool in tools:
            try:
                self.registry.register_tool(tool)
            except Exception as e:
                print(f"  [!] 工具 {tool.name} 注册失败: {e}")

    # ========== 系统指令 ==========

    def _build_system_instructions(self) -> str:
        """组装系统指令：角色 + 工具清单 + Skill 概览。"""
        parts = [
            "你是 cb-agent 的智能助手。下面列出当前可用的能力，按需调用：",
            "",
            self.registry.get_tools_description(),
            "",
            "调用工具时选最直接的那个，避免连续多轮无意义调用。",
            "回答用中文，简明扼要。",
        ]

        if self._md_memory_provider is not None:
            parts.append("")
            parts.append(self._md_memory_provider.memory_instructions())

        # Skill 概览
        try:
            overview = self._skill_manager.build_skills_overview(max_chars=1500)
            if overview:
                parts.append("")
                parts.append(overview)
        except Exception:
            pass

        return "\n".join(parts)

    # ========== 主循环 ==========

    def run(self) -> None:
        print("=" * 50)
        print("输入问题开始对话  /help 看命令  /quit 退出")
        print("=" * 50 + "\n")

        while True:
            try:
                user_input = input("you > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见")
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
            except Exception:
                print(f"\n[!] 本轮异常:")
                traceback.print_exc()

    # ========== 斜杠命令 ==========

    def _handle_command(self, line: str) -> bool:
        """返回 True 继续循环，False 退出。"""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd in ("/quit", "/exit"):
            print("再见")
            return False

        if cmd == "/help":
            print(textwrap.dedent("""\
                命令：
                  /help        帮助
                  /tools       列出所有工具
                  /skills      列出所有 Skill
                  /history     查看对话历史
                  /clear       清空对话历史
                  /quit        退出
            """))
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
            self._print_history()
        elif cmd == "/clear":
            self.history.clear()
            print("[历史已清空]")
        else:
            print(f"未知命令 {cmd}，/help 查看可用命令")
        return True

    # ========== 单轮对话 ==========

    def _chat_once(self, user_query: str) -> None:
        """一次完整的 用户输入 → 上下文构建 → think → 工具循环 → 记录历史。"""

        # 1. ContextBuilder 构建上下文（GSSC 流水线）
        messages = self.builder.to_messages(
            user_query=user_query,
            conversation_history=self.history,
            system_instructions=self._build_system_instructions(),
        )

        # 2. 工具 schema（不支持 FC 的模型传 None）
        tools_schema = (
            self.registry.get_tools_description_openai_schema()
            if self._fc
            else None
        )

        # 3. 工具调用循环
        final_answer = self._tool_loop(messages, tools_schema)

        # 4. 写入历史
        self.history.append(Message.create_user_message(user_query))
        if final_answer:
            self.history.append(Message.create_assistant_message(final_answer))

        # 5. full 模式保留旧的自动记录逻辑；light/off 不注册旧 memory tool。
        if self._memory_tool is not None:
            try:
                self._memory_tool.auto_record_conversation(user_query, final_answer or "")
            except Exception:
                pass

    # ========== 工具循环 ==========

    def _tool_loop(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
    ) -> str:
        """think → 串行执行工具 → 回灌 → 重复，直到模型给最终回答。

        这是 run_agent.py 中 AgentSession._tool_loop + ToolExecutor 的最简替代：
        - 不判定并发/串行（全部串行）
        - 不发射事件（直接 print）
        - 没有 cancel_token
        """
        for round_idx in range(1, self.MAX_TOOL_ROUNDS + 1):
            print(f"\n--- round {round_idx} ---")

            # 调 LLM（event_bus=None → 流式输出直接 print 到终端）
            result = self.llm.think(messages, tools=tools_schema)

            # 不支持 FC 的模型返回 [text, None]
            if isinstance(result, list):
                return result[0] or ""

            if not isinstance(result, dict):
                print(f"[!] 模型返回异常类型: {type(result)}")
                return ""

            answer = result.get("answer", "") or ""
            tool_calls = result.get("tool_calls") or []
            reasoning = result.get("reasoning_content")

            # 没有工具调用 → 最终回答
            if not tool_calls:
                return answer

            # 追加 assistant 消息（含 tool_calls）
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": answer or None,
                "tool_calls": tool_calls,
            }
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            messages.append(assistant_msg)

            # 串行执行每个工具
            for call in tool_calls:
                tool_call_id = call.get("id", "")
                fn = call.get("function") or {}
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}

                print(f"  🔧 {tool_name}({_short_args(args)})")
                try:
                    tool_result = self.registry.execute_tool(tool_name, args)
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)}, ensure_ascii=False)

                # 打印结果预览
                preview = (tool_result or "")[:200].replace("\n", " ")
                suffix = "..." if len(tool_result or "") > 200 else ""
                print(f"     ← {preview}{suffix}")

                # 回灌 tool 消息
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": tool_result if isinstance(tool_result, str) else str(tool_result),
                })

        print(f"[!] 工具调用超过 {self.MAX_TOOL_ROUNDS} 轮，强制终止")
        return ""

    # ========== 辅助 ==========

    def _print_history(self) -> None:
        print(f"\n对话历史 ({len(self.history)} 条):")
        for i, m in enumerate(self.history, 1):
            role = m.role.value
            content = m.content
            if isinstance(content, list):
                content = " ".join(
                    item.get("text", "") for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            preview = (content or "")[:100]
            print(f"  {i:2d}. [{role}] {preview}")
        print()


def _short_args(args: Dict[str, Any], limit: int = 60) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    return s if len(s) <= limit else s[:limit - 3] + "..."


# ============================================================
# 入口
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="cb-agent basic CLI")
    parser.add_argument(
        "--memory-system",
        choices=["light", "full", "off"],
        default="light",
        help="light=Markdown 记忆（默认）；full=旧 MemoryTool/RAGTool；off=关闭记忆",
    )
    args = parser.parse_args()
    agent = BasicAgent(memory_system=args.memory_system)
    agent.run()


if __name__ == "__main__":
    main()

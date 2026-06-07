"""系统提示词常量与缓存边界测试。"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.session import AgentSession
from constant.system_prompt import ConstantSystemPrompt
from context.prompts.boundary import SYSTEM_PROMPT_DYNAMIC_BOUNDARY
from context.prompts.builder import get_system_prompt


class TestSystemPromptConstants(unittest.TestCase):
    def test_user_cosplay_prompt_is_loaded_before_dynamic_boundary(self):
        """用户长期风格提示属于稳定段，不能被工具列表/时间等动态段拖进低缓存命中区域。"""

        with (
            patch.object(ConstantSystemPrompt, "USER_COSPLAY_PROMPT", "请保持冷静的中文代码审查风格。"),
            patch.object(ConstantSystemPrompt, "USER_COSERPLAY_PROMPT", ""),
            patch("context.prompts.builder.should_use_global_cache_scope", return_value=True),
        ):
            parts = asyncio.run(get_system_prompt(
                enabled_tools=frozenset({"bash", "file_read"}),
                model="cache-test-model",
            ))

        boundary_index = parts.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        static_text = "\n\n".join(parts[:boundary_index])
        dynamic_text = "\n\n".join(parts[boundary_index + 1:])

        self.assertIn("# User cosplay / role style", static_text)
        self.assertIn("请保持冷静的中文代码审查风格。", static_text)
        self.assertNotIn("Available tools:", static_text)
        self.assertIn("Available tools: bash, file_read.", dynamic_text)
        self.assertIn("# Current time", dynamic_text)

    def test_user_coserplay_alias_is_supported(self):
        """兼容用户常见的 coserplay 拼写，避免改配置时找不到入口。"""

        with (
            patch.object(ConstantSystemPrompt, "USER_COSPLAY_PROMPT", ""),
            patch.object(ConstantSystemPrompt, "USER_COSERPLAY_PROMPT", "请以沉稳的架构师风格回答。"),
        ):
            section = ConstantSystemPrompt.get_user_cosplay_section()

        self.assertIn("请以沉稳的架构师风格回答。", section)

    def test_no_tools_guidance_remains_dynamic(self):
        """无工具注册时仍保留旧提示语，但它位于动态段，避免污染静态缓存前缀。"""

        with patch("context.prompts.builder.should_use_global_cache_scope", return_value=True):
            parts = asyncio.run(get_system_prompt(
                enabled_tools=frozenset(),
                model="cache-test-model",
            ))

        boundary_index = parts.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        static_text = "\n\n".join(parts[:boundary_index])
        dynamic_text = "\n\n".join(parts[boundary_index + 1:])

        self.assertNotIn("Available tools:", static_text)
        self.assertIn("Available tools: (no tools registered).", dynamic_text)

    def test_agent_session_runtime_instructions_do_not_duplicate_static_prompt(self):
        """AgentSession 运行时补充段不再重复固定身份和中文规则。"""

        session = object.__new__(AgentSession)
        session.bash_prompt_provider = None
        session.skill_manager = None
        session.buddy_manager = None

        self.assertEqual(session._build_system_instructions(), "")


if __name__ == "__main__":
    unittest.main()

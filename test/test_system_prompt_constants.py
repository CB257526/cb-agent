"""System prompt static/dynamic separation tests."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.session import AgentSession
from constant.system_prompt import ConstantSystemPrompt
from context import clear_system_prompt_sections
from context.prompts.builder import (
    get_dynamic_context_prompt,
    get_static_system_prompt,
)


class TestSystemPromptConstants(unittest.TestCase):
    def test_user_cosplay_prompt_stays_in_static_system_prefix(self):
        with (
            patch.object(ConstantSystemPrompt, "USER_COSPLAY_PROMPT", "stable reviewer voice"),
            patch.object(ConstantSystemPrompt, "USER_COSERPLAY_PROMPT", ""),
        ):
            static = get_static_system_prompt(enabled_tools=frozenset({"bash", "file_read"}))
            dynamic = asyncio.run(get_dynamic_context_prompt(
                enabled_tools=frozenset({"bash", "file_read"}),
                model="cache-test-model",
            ))

        static_text = "\n\n".join(static)
        dynamic_text = "\n\n".join(dynamic)

        self.assertIn("# User cosplay / role style", static_text)
        self.assertIn("stable reviewer voice", static_text)
        self.assertNotIn("# Current time", static_text)
        self.assertNotIn("# Environment", static_text)
        self.assertNotIn("Available tools:", static_text)
        self.assertIn("Available tools: bash, file_read.", dynamic_text)
        self.assertIn("# Current time", dynamic_text)

    def test_user_coserplay_alias_is_supported(self):
        with (
            patch.object(ConstantSystemPrompt, "USER_COSPLAY_PROMPT", ""),
            patch.object(ConstantSystemPrompt, "USER_COSERPLAY_PROMPT", "stable architect voice"),
        ):
            section = ConstantSystemPrompt.get_user_cosplay_section()

        self.assertIn("stable architect voice", section)

    def test_no_tools_guidance_remains_dynamic(self):
        clear_system_prompt_sections()

        static = get_static_system_prompt(enabled_tools=frozenset())
        dynamic = asyncio.run(get_dynamic_context_prompt(
            enabled_tools=frozenset(),
            model="cache-test-model",
        ))

        static_text = "\n\n".join(static)
        dynamic_text = "\n\n".join(dynamic)

        self.assertNotIn("Available tools:", static_text)
        self.assertIn("Available tools: (no tools registered).", dynamic_text)

    def test_agent_session_runtime_instructions_do_not_duplicate_static_prompt(self):
        session = object.__new__(AgentSession)
        session.bash_prompt_provider = None
        session.skill_manager = None
        session.pet_manager = None

        self.assertEqual(session._build_system_instructions(), "")


if __name__ == "__main__":
    unittest.main()

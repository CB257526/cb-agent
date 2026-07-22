from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from constant.llm.constant_llm import ConstantLLM
from constant.llm.model_config import ModelConfigManager


class TestModelConfigManager(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("IS_TOOL", "IS_REASONING", "MAX_TOKENS", "MAX_OUTPUT_TOKENS", "IMAGE_ABILITY", "CBAGENT_MODEL_CONFIG")
        }
        for key in self._saved_env:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_single_provider_shape_registers_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "models.json"
            manager = ModelConfigManager.from_provider_dicts(path, [
                {
                    "name": "Kimi",
                    "options": {
                        "baseURL": "https://api.moonshot.cn/v1",
                        "apiKey": "sk-test",
                    },
                    "models": {
                        "kimi-k2.7-code": {
                            "name": "Kimi K2.7 Code",
                            "is_tool": True,
                            "is_reasoning": True,
                            "max_tokens": 1000000,
                            "max_output_tokens": 32000,
                            "output_token_param": "max_completion_tokens",
                            "image_ability": True,
                        },
                    },
                },
            ])

        choice = manager.find("kimi:kimi-k2.7-code")
        self.assertIsNotNone(choice)
        assert choice is not None
        self.assertEqual(choice.display_name, "Kimi K2.7 Code")
        self.assertEqual(choice.base_url, "https://api.moonshot.cn/v1")
        # models.json 不再写回全局 llm_dict；能力以 ModelChoice/ActiveModelConfig 为准。
        active = choice.to_active_config()
        self.assertTrue(active.is_tool)
        self.assertTrue(active.image_ability)
        self.assertEqual(active.max_context_tokens, 1000000)
        self.assertEqual(choice.max_output_tokens, 32000)
        self.assertEqual(choice.output_token_param, "max_completion_tokens")

    def test_duplicate_provider_names_get_unique_keys(self) -> None:
        manager = ModelConfigManager.from_provider_dicts(Path("models.json"), [
            {"name": "OpenAI", "models": {"gpt-a": {}}},
            {"name": "OpenAI", "models": {"gpt-b": {}}},
        ])
        keys = [choice.key for choice in manager.choices]
        self.assertEqual(keys, ["openai:gpt-a", "openai-2:gpt-b"])

    def test_load_accepts_python_literal_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "models.json"
            path.write_text(
                """{
  "name": "Kimi",
  "options": {"baseURL": "https://api.moonshot.cn/v1", "apiKey": "sk-test"},
  "models": {
    "kimi-k2.7-code": {
      "is_tool": True,
      "is_reasoning": True,
      "image_ability": False,
      "max_tokens": 1000000
    }
  }
}""",
                encoding="utf-8",
            )
            os.environ["CBAGENT_MODEL_CONFIG"] = str(path)
            manager = ModelConfigManager.load(Path(td))
        choice = manager.find("kimi:kimi-k2.7-code")
        self.assertIsNotNone(choice)
        assert choice is not None
        self.assertTrue(choice.is_tool)
        self.assertFalse(choice.image_ability)

    def test_same_model_id_different_providers_keep_distinct_windows(self) -> None:
        manager = ModelConfigManager.from_provider_dicts(Path("models.json"), [
            {
                "name": "ProviderA",
                "models": {
                    "same-model": {
                        "max_tokens": 128000,
                        "max_output_tokens": 8000,
                        "is_tool": True,
                    }
                },
            },
            {
                "name": "ProviderB",
                "models": {
                    "same-model": {
                        "max_tokens": 32000,
                        "max_output_tokens": 4000,
                        "is_tool": False,
                    }
                },
            },
        ])
        a = manager.find("providera:same-model")
        b = manager.find("providerb:same-model")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        assert a is not None and b is not None
        self.assertEqual(a.model_id, b.model_id)
        self.assertEqual(a.to_active_config().max_context_tokens, 128000)
        self.assertEqual(b.to_active_config().max_context_tokens, 32000)
        self.assertTrue(a.to_active_config().is_tool)
        self.assertFalse(b.to_active_config().is_tool)
        # 全局 llm_dict 不应被后加载的同名配置污染。
        # 即使 resolve 仍可走内建/默认，两个 active config 也必须互不影响。
        self.assertNotEqual(
            a.to_active_config().max_context_tokens,
            b.to_active_config().max_context_tokens,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

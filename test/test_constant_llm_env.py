"""ConstantLLM env 优先能力解析层单测。

覆盖:
1. K/M 后缀 token 解析(_parse_token_count_env)
2. 布尔解析(_parse_bool_env)
3. resolve_is_tool / resolve_image_ability / resolve_is_reasoning 的
   env > llm_dict > default 三级优先级
4. model_max_tokens 的 env(MAX_TOKENS,含 K/M) > llm_dict > 默认
5. 换服务商场景:模型名对不上 llm_dict 时 env 兜底
6. window.get_context_window_for_model 复用 MAX_TOKENS
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from constant.llm.constant_llm import (
    ConstantLLM,
    _parse_bool_env,
    _parse_token_count_env,
)


# 解析层读的所有 env 键,每个用例前后清干净,避免相互污染。
_ENV_KEYS = ("IS_TOOL", "IS_REASONING", "MAX_TOKENS", "MAX_OUTPUT_TOKENS", "IMAGE_ABILITY",
             "CB_AGENT_MAX_CONTEXT_TOKENS")


class _EnvSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestParseHelpers(unittest.TestCase):
    def test_token_count_suffixes(self):
        self.assertEqual(_parse_token_count_env("1024K"), 1024 * 1024)
        self.assertEqual(_parse_token_count_env("1M"), 1024 * 1024)
        self.assertEqual(_parse_token_count_env("200000"), 200000)
        self.assertEqual(_parse_token_count_env("1m"), 1024 * 1024)  # 大小写不敏感
        self.assertEqual(_parse_token_count_env("1,000,000"), 1000000)  # 逗号分隔

    def test_token_count_invalid(self):
        self.assertIsNone(_parse_token_count_env(None))
        self.assertIsNone(_parse_token_count_env(""))
        self.assertIsNone(_parse_token_count_env("abc"))
        self.assertIsNone(_parse_token_count_env("0"))
        self.assertIsNone(_parse_token_count_env("-5"))

    def test_bool_parse(self):
        for s in ("true", "True", "1", "yes", "on", "  TRUE "):
            self.assertIs(_parse_bool_env(s), True)
        for s in ("false", "False", "0", "no", "off"):
            self.assertIs(_parse_bool_env(s), False)
        for s in (None, "", "maybe", "2"):
            self.assertIsNone(_parse_bool_env(s))


class TestResolveCapabilities(_EnvSandbox):
    def test_is_tool_env_over_registry(self):
        # llm_dict 里 deepseek-v4-flash 的 is_tool=True;env 设 False 应覆盖
        os.environ["IS_TOOL"] = "False"
        self.assertFalse(ConstantLLM.resolve_is_tool("deepseek-v4-flash"))
        os.environ["IS_TOOL"] = "True"
        self.assertTrue(ConstantLLM.resolve_is_tool("deepseek-v4-flash"))

    def test_is_tool_registry_when_no_env(self):
        # 不配 env,走 llm_dict
        self.assertTrue(ConstantLLM.resolve_is_tool("deepseek-v4-flash"))

    def test_is_tool_default_when_unknown(self):
        # 模型名对不上 llm_dict,也没 env,走 default
        self.assertTrue(ConstantLLM.resolve_is_tool("unknown/Model", default=True))
        self.assertFalse(ConstantLLM.resolve_is_tool("unknown/Model", default=False))

    def test_image_ability_env_over_registry(self):
        # deepseek-v4-flash 的 image_ability=False;env 设 True 应覆盖
        os.environ["IMAGE_ABILITY"] = "True"
        self.assertTrue(ConstantLLM.resolve_image_ability("deepseek-v4-flash"))

    def test_image_ability_registry(self):
        # gemini-3.5-flash image_ability=True,不配 env 走表
        self.assertTrue(ConstantLLM.resolve_image_ability("gemini-3.5-flash"))
        self.assertFalse(ConstantLLM.resolve_image_ability("deepseek-v4-flash"))

    def test_switch_provider_unknown_model_uses_env(self):
        # 换服务商场景:模型名 "deepseek-ai/DeepSeek-V4-Flash" 不在 llm_dict,
        # 但 env 指定了能力,应当全部生效。
        os.environ["IS_TOOL"] = "True"
        os.environ["IMAGE_ABILITY"] = "False"
        os.environ["MAX_TOKENS"] = "1024K"
        model = "deepseek-ai/DeepSeek-V4-Flash"
        self.assertTrue(ConstantLLM.resolve_is_tool(model))
        self.assertFalse(ConstantLLM.resolve_image_ability(model))
        self.assertEqual(ConstantLLM.model_max_tokens(model), 1024 * 1024)


class TestModelMaxTokens(_EnvSandbox):
    def test_env_max_tokens_over_registry(self):
        # deepseek-v4-flash 表里是 1000000;env 设 1024K 应覆盖
        os.environ["MAX_TOKENS"] = "1024K"
        self.assertEqual(ConstantLLM.model_max_tokens("deepseek-v4-flash"), 1024 * 1024)

    def test_registry_when_no_env(self):
        self.assertEqual(ConstantLLM.model_max_tokens("deepseek-v4-flash"), 1000000)
        self.assertEqual(ConstantLLM.model_max_tokens("qwen3-max"), 262144)

    def test_default_when_unknown(self):
        self.assertEqual(
            ConstantLLM.model_max_tokens("unknown/Model"),
            ConstantLLM.DEFAULT_MAX_TOKENS,
        )

    def test_context_window_tokens_respects_env(self):
        os.environ["MAX_TOKENS"] = "1M"
        os.environ["MAX_OUTPUT_TOKENS"] = "16K"
        limits = ConstantLLM.context_limits("anything")
        self.assertEqual(limits["full_window_tokens"], 1024 * 1024)
        self.assertEqual(limits["max_output_tokens"], 16 * 1024)
        self.assertEqual(limits["estimation_margin_tokens"], 16_000)
        self.assertEqual(limits["hard_limit_tokens"], 1024 * 1024 - 16 * 1024)
        self.assertEqual(ConstantLLM.context_window_tokens("anything"), limits["soft_limit_tokens"])

    def test_max_output_env_over_registry(self):
        os.environ["MAX_OUTPUT_TOKENS"] = "8K"
        self.assertEqual(ConstantLLM.model_max_output_tokens("deepseek-v4-flash"), 8 * 1024)


class TestWindowReusesEnv(_EnvSandbox):
    def test_window_uses_max_tokens_env(self):
        from context.budget.window import get_context_window_for_model
        os.environ["MAX_TOKENS"] = "512K"
        self.assertEqual(get_context_window_for_model("deepseek-v4-flash"), 512 * 1024)

    def test_window_registry_when_no_env(self):
        from context.budget.window import get_context_window_for_model
        self.assertEqual(get_context_window_for_model("deepseek-v4-flash"), 1_000_000)

    def test_window_1m_suffix_when_no_env(self):
        from context.budget.window import get_context_window_for_model
        self.assertEqual(get_context_window_for_model("foo[1m]"), 1_000_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""多模态输入处理层单测。

这些测试只验证 cb-agent 自己的协议边界，不调用真实 OCR/ASR API：
- 支持视觉的主模型：图片以 image_url 进入当前请求；
- 纯文本主模型：图片先转成文本摘要；
- 音频始终转 ASR 文本；
- history/token/log 侧只能看到文本摘要或脱敏占位符，不能保存 data URI。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.multimodal_input import (
    MultimodalInputError,
    process_multimodal_prompt,
    sanitize_multimodal_payload,
)
from constant.llm.constant_llm import ConstantLLM


class FakeProcessor:
    def __init__(self) -> None:
        self.images: list[str] = []
        self.audio: list[str] = []

    def process_image(self, file_path: str) -> dict:
        self.images.append(file_path)
        return {"text": "图像 OCR 文本"}

    def process_audio(self, file_path: str) -> dict:
        self.audio.append(file_path)
        return {"text": "音频 ASR 文本"}


class TestMultimodalInput(unittest.TestCase):
    # 能力覆盖类 env(ConstantLLM 会优先读它们覆盖 llm_dict)。cb_agents.py 顶部的
    # load_dotenv() 在 import 时把用户本地 .env 的 IMAGE_ABILITY/MAX_TOKENS 等灌进
    # os.environ,而其他测试也可能残留这些 env。本类依赖 mm-test/text-test 的
    # llm_dict.image_ability monkeypatch,必须先清掉这些 env,否则图片路由会被
    # env 的 IMAGE_ABILITY 覆盖,导致原生视觉用例错走 OCR。
    _CAPABILITY_ENV_KEYS = ("IS_TOOL", "IS_REASONING", "MAX_TOKENS", "IMAGE_ABILITY")

    def setUp(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in self._CAPABILITY_ENV_KEYS}

        def _restore() -> None:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore)

        self._old_mm = ConstantLLM.llm_dict.get("mm-test")
        self._old_text = ConstantLLM.llm_dict.get("text-test")
        ConstantLLM.llm_dict["mm-test"] = {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 100000,
            "image_ability": True,
        }
        ConstantLLM.llm_dict["text-test"] = {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 100000,
            "image_ability": False,
        }

    def tearDown(self) -> None:
        if self._old_mm is None:
            ConstantLLM.llm_dict.pop("mm-test", None)
        else:
            ConstantLLM.llm_dict["mm-test"] = self._old_mm
        if self._old_text is None:
            ConstantLLM.llm_dict.pop("text-test", None)
        else:
            ConstantLLM.llm_dict["text-test"] = self._old_text

    def test_image_native_route_keeps_history_text_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "shot.png"
            path.write_bytes(b"png bytes")

            prompt = process_multimodal_prompt(
                text="看图",
                attachments=[{"path": str(path), "source": "direct"}],
                model="mm-test",
                cwd=Path(td),
                processor=FakeProcessor(),
            )

        self.assertIsInstance(prompt.request_content, list)
        image_parts = [p for p in prompt.request_content if p.get("type") == "image_url"]
        self.assertEqual(len(image_parts), 1)
        self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn("图片已原生发送", prompt.history_text)
        self.assertNotIn("base64", prompt.history_text)
        self.assertEqual(prompt.attachments[0].routed_as, "image_url")

    def test_text_model_image_route_uses_processor_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "shot.jpg"
            path.write_bytes(b"jpg bytes")
            processor = FakeProcessor()

            prompt = process_multimodal_prompt(
                text="识别图片",
                attachments=[{"path": "shot.jpg"}],
                model="text-test",
                cwd=Path(td),
                processor=processor,
            )

        self.assertEqual(len(processor.images), 1)
        self.assertIn("图像 OCR 文本", str(prompt.request_content))
        self.assertIn("图像 OCR 文本", prompt.history_text)
        self.assertEqual(prompt.attachments[0].routed_as, "ocr")

    def test_audio_always_uses_asr_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "voice.mp3"
            path.write_bytes(b"mp3 bytes")
            processor = FakeProcessor()

            prompt = process_multimodal_prompt(
                text="",
                attachments=[{"path": str(path), "source": "direct"}],
                model="mm-test",
                cwd=Path(td),
                processor=processor,
            )

        self.assertEqual(len(processor.audio), 1)
        self.assertIn("请根据以下附件回答用户问题。", prompt.history_text)
        self.assertIn("音频 ASR 文本", prompt.history_text)
        self.assertEqual(prompt.attachments[0].routed_as, "asr")

    def test_text_attachment_uses_markdown_route(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "note.txt"
            path.write_text("hello", encoding="utf-8")

            with patch(
                "agent.multimodal_input._convert_attachment_to_markdown",
                return_value="# Note\n\nhello",
            ) as convert:
                prompt = process_multimodal_prompt(
                    text="总结附件",
                    attachments=[{"path": str(path), "source": "direct"}],
                    model="text-test",
                    cwd=Path(td),
                    processor=FakeProcessor(),
                )

        convert.assert_called_once()
        self.assertIn("# Note", str(prompt.request_content))
        self.assertIn("# Note", prompt.history_text)
        self.assertEqual(prompt.attachments[0].modality, "text")
        self.assertEqual(prompt.attachments[0].routed_as, "markdown")

    def test_document_attachment_uses_markdown_route(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "paper.pdf"
            path.write_bytes(b"%PDF fake")

            with patch(
                "agent.multimodal_input._convert_attachment_to_markdown",
                return_value="# Paper\n\nconverted markdown",
            ) as convert:
                prompt = process_multimodal_prompt(
                    text="",
                    attachments=[{"path": str(path), "source": "direct"}],
                    model="text-test",
                    cwd=Path(td),
                    processor=FakeProcessor(),
                )

        convert.assert_called_once()
        self.assertIn("# Paper", str(prompt.request_content))
        self.assertIn("# Paper", prompt.history_text)
        self.assertEqual(prompt.attachments[0].modality, "document")
        self.assertEqual(prompt.attachments[0].routed_as, "markdown")

    def test_sanitize_replaces_data_uri_without_mutating_original(self) -> None:
        original = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abcdef"}}]
        sanitized = sanitize_multimodal_payload(original)

        self.assertEqual(original[0]["image_url"]["url"], "data:image/png;base64,abcdef")
        self.assertIn("[data-uri omitted: image/png", sanitized[0]["image_url"]["url"])
        self.assertNotIn("abcdef", sanitized[0]["image_url"]["url"])

    def test_invalid_inputs_raise_clear_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.png"
            unsupported = Path(td) / "blob.bin"
            unsupported.write_bytes(b"hello")
            big = Path(td) / "big.png"
            big.write_bytes(b"xx")

            with self.assertRaisesRegex(MultimodalInputError, "不存在"):
                process_multimodal_prompt(text="x", attachments=[{"path": str(missing)}], cwd=Path(td))
            with self.assertRaisesRegex(MultimodalInputError, "不支持"):
                process_multimodal_prompt(text="x", attachments=[{"path": str(unsupported)}], cwd=Path(td))
            with patch.dict(os.environ, {"CBAGENT_ATTACHMENT_MAX_MB": "0.000001"}):
                with self.assertRaisesRegex(MultimodalInputError, "超过限制"):
                    process_multimodal_prompt(text="x", attachments=[{"path": str(big)}], cwd=Path(td))


if __name__ == "__main__":
    unittest.main()

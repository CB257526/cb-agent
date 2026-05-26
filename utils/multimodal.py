"""多模态数据处理器

为 RAG 管线提供 OCR 图像识别和 ASR 语音转录能力。
将图像/音频等非文本数据统一转换为文本描述 + 结构化元数据，
使向量检索管线可以统一处理所有模态的数据。

使用方式:
    processor = MultimodalProcessor()

    # 图像 OCR
    result = processor.process_image("photo.png")
    # → {"text": "识别出的文字...", "metadata": {"file_path": "...", "modality": "image", ...}}

    # 音频 ASR
    result = processor.process_audio("recording.mp3")
    # → {"text": "转录出的文字...", "metadata": {"file_path": "...", "modality": "audio", ...}}
"""

import os
import base64
import hashlib
import logging
from typing import Dict, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class MultimodalProcessor:
    """多模态数据处理器

    支持的操作:
    - process_image(file_path): OCR 识别图像中的文字
    - process_audio(file_path): ASR 转录音频中的语音
    - process_file(file_path):   自动检测文件类型并调用对应处理器
    """

    # 图像文件扩展名 → MIME 类型映射
    IMAGE_MIME_MAP = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }

    # 音频文件扩展名 → MIME 类型映射
    AUDIO_MIME_MAP = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".wma": "audio/x-ms-wma",
    }

    def __init__(self):
        """初始化多模态处理器，从环境变量读取 API 配置"""
        self._ocr_client = None
        self._asr_client = None

    # ── 公开接口 ──

    def process_image(self, file_path: str) -> Dict:
        """对图像文件执行 OCR 识别

        流程:
        1. 读取图像文件 → Base64 编码
        2. 调用视觉 LLM 提取图中的所有文字
        3. 返回识别文本 + 元数据

        参数:
            file_path: 图像文件路径

        返回:
            {
                "text": "识别出的文本内容",
                "metadata": {
                    "file_path": "原始文件路径",
                    "file_name": "文件名",
                    "file_size": 文件字节数,
                    "mime_type": "image/png",
                    "modality": "image",
                    "content_hash": "内容MD5哈希"
                }
            }
            失败时 text 为空字符串
        """
        path_obj = Path(file_path)
        if not path_obj.exists():
            logger.error(f"图像文件不存在: {file_path}")
            return self._empty_result(file_path, "image")

        ext = path_obj.suffix.lower()
        mime_type = self.IMAGE_MIME_MAP.get(ext)
        if mime_type is None:
            logger.error(f"不支持的图像格式: {ext}")
            return self._empty_result(file_path, "image")

        try:
            file_bytes = path_obj.read_bytes()
            content_hash = hashlib.md5(file_bytes).hexdigest()
            base64_str = base64.b64encode(file_bytes).decode("utf-8")
            data_uri = f"data:{mime_type};base64,{base64_str}"

            # 调用视觉 LLM 进行 OCR
            client = self._get_ocr_client()
            if client is None:
                logger.error("OCR 客户端未配置，请检查 OCR_API_KEY 等环境变量")
                return self._empty_result(file_path, "image")

            completion = client.chat.completions.create(
                model=os.getenv("OCR_MODEL_NAME", "qwen-vl-ocr-2025-11-20"),
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": data_uri},
                            },
                            {
                                "type": "text",
                                "text": (
                                    "请详细描述这张图片的内容，包括：\n"
                                    "1. 图片中的所有文字内容（逐字识别，不要遗漏）\n"
                                    "2. 图片的视觉内容描述（场景、物体、人物、颜色、布局等）\n"
                                    "3. 图片的类型（如：截图、照片、图表、海报等）\n"
                                    "请用中文回答，保持客观准确。"
                                ),
                            },
                        ],
                    },
                ],
            )

            text = completion.choices[0].message.content or ""
            logger.info(f"OCR 完成: {file_path} → {len(text)} 字符")

            return {
                "text": text.strip(),
                "metadata": {
                    "file_path": str(path_obj.absolute()),
                    "file_name": path_obj.name,
                    "file_size": len(file_bytes),
                    "mime_type": mime_type,
                    "modality": "image",
                    "content_hash": content_hash,
                },
            }

        except Exception as e:
            logger.error(f"OCR 处理失败 ({file_path}): {e}")
            return self._empty_result(file_path, "image")

    def process_audio(self, file_path: str) -> Dict:
        """对音频文件执行 ASR 语音转录

        流程:
        1. 读取音频文件 → Base64 编码
        2. 调用语音识别 LLM 转录语音内容
        3. 返回转录文本 + 元数据

        参数:
            file_path: 音频文件路径

        返回:
            {
                "text": "转录出的文本内容",
                "metadata": {
                    "file_path": "原始文件路径",
                    "file_name": "文件名",
                    "file_size": 文件字节数,
                    "mime_type": "audio/mpeg",
                    "duration_seconds": 时长(可能为None),
                    "modality": "audio",
                    "content_hash": "内容MD5哈希"
                }
            }
            失败时 text 为空字符串
        """
        path_obj = Path(file_path)
        if not path_obj.exists():
            logger.error(f"音频文件不存在: {file_path}")
            return self._empty_result(file_path, "audio")

        ext = path_obj.suffix.lower()
        mime_type = self.AUDIO_MIME_MAP.get(ext)
        if mime_type is None:
            logger.error(f"不支持的音频格式: {ext}")
            return self._empty_result(file_path, "audio")

        try:
            file_bytes = path_obj.read_bytes()
            content_hash = hashlib.md5(file_bytes).hexdigest()
            base64_str = base64.b64encode(file_bytes).decode("utf-8")
            data_uri = f"data:{mime_type};base64,{base64_str}"

            # 调用语音识别 LLM 进行转录
            client = self._get_asr_client()
            if client is None:
                logger.error("ASR 客户端未配置，请检查 ASR_API_KEY 等环境变量")
                return self._empty_result(file_path, "audio")

            # 音频文件可能较大，使用非流式模式更稳定
            completion = client.chat.completions.create(
                model=os.getenv("ASR_MODEL_NAME", "qwen3-asr-flash"),
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": data_uri},
                            }
                        ],
                    }
                ],
                stream=False,
                extra_body={
                    "asr_options": {
                        "enable_itn": False,  # 不启用逆文本正则化，保留原始识别结果
                    }
                },
            )

            text = completion.choices[0].message.content or ""
            logger.info(f"ASR 完成: {file_path} → {len(text)} 字符")

            return {
                "text": text.strip(),
                "metadata": {
                    "file_path": str(path_obj.absolute()),
                    "file_name": path_obj.name,
                    "file_size": len(file_bytes),
                    "mime_type": mime_type,
                    "modality": "audio",
                    "content_hash": content_hash,
                },
            }

        except Exception as e:
            logger.error(f"ASR 处理失败 ({file_path}): {e}")
            return self._empty_result(file_path, "audio")

    def process_file(self, file_path: str) -> Dict:
        """自动检测文件类型并调用对应处理器

        根据文件扩展名判断:
        - 图像 (png/jpg/webp/...) → process_image
        - 音频 (mp3/wav/m4a/...) → process_audio
        - 其他 → 返回空结果（由外部 MarkItDown 处理文本类文件）
        """
        ext = Path(file_path).suffix.lower()
        if ext in self.IMAGE_MIME_MAP:
            return self.process_image(file_path)
        elif ext in self.AUDIO_MIME_MAP:
            return self.process_audio(file_path)
        else:
            return self._empty_result(file_path, "unknown")

    # ── 内部方法 ──

    def _get_ocr_client(self):
        """获取 OCR 客户端（懒加载，单例）"""
        if self._ocr_client is None:
            api_key = os.getenv("OCR_API_KEY", "").strip()
            base_url = os.getenv("OCR_BASE_URL", "").strip()
            if not api_key:
                logger.warning("OCR_API_KEY 未设置，OCR 功能不可用")
                return None
            try:
                from openai import OpenAI
                self._ocr_client = OpenAI(api_key=api_key, base_url=base_url)
            except ImportError:
                logger.error("openai 库未安装，OCR 功能不可用")
                return None
        return self._ocr_client

    def _get_asr_client(self):
        """获取 ASR 客户端（懒加载，单例）"""
        if self._asr_client is None:
            api_key = os.getenv("ASR_API_KEY", "").strip()
            base_url = os.getenv("ASR_BASE_URL", "").strip()
            if not api_key:
                logger.warning("ASR_API_KEY 未设置，ASR 功能不可用")
                return None
            try:
                from openai import OpenAI
                self._asr_client = OpenAI(api_key=api_key, base_url=base_url)
            except ImportError:
                logger.error("openai 库未安装，ASR 功能不可用")
                return None
        return self._asr_client

    def _empty_result(self, file_path: str, modality: str) -> Dict:
        """构建空的处理结果（用于错误/不支持格式的兜底）"""
        path_obj = Path(file_path)
        return {
            "text": "",
            "metadata": {
                "file_path": str(path_obj.absolute()) if path_obj.exists() else file_path,
                "file_name": path_obj.name if path_obj.exists() else os.path.basename(file_path),
                "modality": modality,
            },
        }

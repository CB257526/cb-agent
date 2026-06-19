"""图像加载工具 load_image。

让模型在执行任务过程中按需读取图片。根据当前模型是否支持原生视觉输入，分两条路：

  1. 支持视觉（image_ability=True）：把图片编码成 data URI（本地路径）或直接透传
     http(s) URL，排进 pending_images 缓冲；工具返回一句文本确认。AgentSession 的
     工具循环随后把图片作为一条 role="user" 消息注入当轮请求，模型据原图回答。
     —— 这是 Chat Completions 协议下 codex view_image 的等价做法：图片只能进
     user/system 消息，不能塞进 role="tool"。

  2. 不支持视觉：调用现成的 OCR（MultimodalProcessor.process_image）把图片转成文字，
     直接作为 tool result 返回。为省 OCR 调用成本，这条路只接受本地文件，拒绝 URL。

图片类型沿用 MultimodalProcessor.IMAGE_MIME_MAP：png/jpg/jpeg/webp/gif/bmp/tiff。
分支判定读环境变量 LLM_MODEL_ID 在 llm_dict 里的 image_ability（可被 IMAGE_ABILITY
env 覆盖），与用户附件图片走的是同一套能力判定。
"""
#TODO: 当前在怎么将图片塞进message思路中由于本项目主要是chat协议，tool角色的message不支持传入图片智能传文本，所以当前是直接将图片塞进user字段中，等以后项目适配response协议时，tool角色才能直接传图片。
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tool import Tool, ToolParameter
from constant.llm.constant_llm import ConstantLLM
from utils.multimodal import MultimodalProcessor


# 复用多模态处理器的图片类型表（扩展名 → MIME）
IMAGE_MIME_MAP = MultimodalProcessor.IMAGE_MIME_MAP

# 本地图片大小上限，沿用附件同款 env；默认 20MB。
DEFAULT_IMAGE_MAX_MB = 20

# OCR 处理器进程内单例（内部客户端懒加载，复用以免每次重连）
_ocr_processor: Optional[MultimodalProcessor] = None


def _get_ocr_processor() -> MultimodalProcessor:
    global _ocr_processor
    if _ocr_processor is None:
        _ocr_processor = MultimodalProcessor()
    return _ocr_processor


def _is_http_url(value: str) -> bool:
    v = value.strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def _image_size_limit_bytes() -> int:
    raw = os.getenv("CBAGENT_ATTACHMENT_MAX_MB")
    try:
        limit_mb = float(raw) if raw else DEFAULT_IMAGE_MAX_MB
    except ValueError:
        limit_mb = DEFAULT_IMAGE_MAX_MB
    return max(1, int(limit_mb * 1024 * 1024))


class LoadImageTool(Tool):
    def __init__(self):
        super().__init__(
            name="load_image",
            description=(
                "加载一张图片供你查看。支持的格式：png/jpg/jpeg/webp/gif/bmp/tiff。\n"
                "- 视觉模型：图片会作为视觉输入直接发给你，调用后请根据图片内容回答；"
                "可传本地文件路径（绝对或相对当前工作目录），也可传 http(s) 图片 URL。\n"
                "- 非视觉模型：自动用 OCR 提取图片中的文字与描述并返回文本；"
                "此时只接受本地文件路径，不支持 URL。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description=(
                    "图片的本地路径（绝对或相对当前工作目录），"
                    "视觉模型下也可以是 http(s) 图片 URL。"
                ),
                required=True,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        path = parameters.get("path")
        return bool(path) and isinstance(path, str)

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps({"error": "参数验证失败：需要字符串 path。"}, ensure_ascii=False)

        raw_path = str(parameters["path"]).strip().strip('"').strip("'")
        image_native = ConstantLLM.resolve_image_ability(os.getenv("LLM_MODEL_ID"), default=False)

        if _is_http_url(raw_path):
            return self._handle_url(raw_path, image_native)
        return self._handle_local(raw_path, image_native)

    # ---------- URL 分支 ----------

    def _handle_url(self, url: str, image_native: bool) -> str:
        if not image_native:
            # 非视觉模型只能走 OCR，而 OCR 这里不下载网络图片（省调用成本，也避免
            # 后端发起任意外联请求）。直接告诉模型换本地路径。
            return json.dumps(
                {
                    "error": "当前模型不支持视觉输入，load_image 只能处理本地图片文件，"
                             "不支持网络 URL。请先把图片下载到本地再用本地路径调用。",
                },
                ensure_ascii=False,
            )
        # 视觉模型：URL 直接透传，不下载（由模型侧拉取）。
        image_part = {"type": "image_url", "image_url": {"url": url}}
        file_name = url.rsplit("/", 1)[-1] or url
        self._queue(image_part, file_name)
        return json.dumps(
            {
                "status": "ok",
                "routed_as": "image_url",
                "file": file_name,
                "message": f"图片 URL 已作为视觉输入发送给模型：{file_name}。请直接根据图片内容回答。",
            },
            ensure_ascii=False,
        )

    # ---------- 本地路径分支 ----------

    def _handle_local(self, raw_path: str, image_native: bool) -> str:
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            # 相对路径以 BashSession.cwd 为准，与 file_read / 附件一致；
            # 放函数内导入避免循环依赖。
            try:
                from tools.tools.bash_session import get_session
                p = Path(get_session().cwd) / p
            except Exception:
                p = Path.cwd() / p
        p = p.resolve()

        if not p.exists():
            return json.dumps({"error": f"图片文件不存在：{p}"}, ensure_ascii=False)
        if not p.is_file():
            return json.dumps({"error": f"不是文件：{p}"}, ensure_ascii=False)

        ext = p.suffix.lower()
        mime_type = IMAGE_MIME_MAP.get(ext)
        if mime_type is None:
            supported = "/".join(sorted({e.lstrip(".") for e in IMAGE_MIME_MAP}))
            return json.dumps(
                {"error": f"不支持的图片格式：{ext or p.name}。支持：{supported}。"},
                ensure_ascii=False,
            )

        size = p.stat().st_size
        limit = _image_size_limit_bytes()
        if size > limit:
            return json.dumps(
                {"error": f"图片 {p.name} 大小 {size} 字节，超过限制 {limit} 字节。"},
                ensure_ascii=False,
            )

        if image_native:
            return self._local_native(p, mime_type)
        return self._local_ocr(p)

    def _local_native(self, p: Path, mime_type: str) -> str:
        """视觉模型：本地图片编码成 data URI 排队，作为视觉输入注入。"""
        try:
            file_bytes = p.read_bytes()
        except OSError as e:
            return json.dumps({"error": f"读取图片失败：{e}"}, ensure_ascii=False)
        data_uri = "data:%s;base64,%s" % (
            mime_type,
            base64.b64encode(file_bytes).decode("utf-8"),
        )
        image_part = {"type": "image_url", "image_url": {"url": data_uri}}
        self._queue(image_part, p.name)
        return json.dumps(
            {
                "status": "ok",
                "routed_as": "image_url",
                "file": p.name,
                "message": f"图片 {p.name} 已作为视觉输入发送给模型。请直接根据图片内容回答。",
            },
            ensure_ascii=False,
        )

    def _local_ocr(self, p: Path) -> str:
        """非视觉模型：OCR 提取文字，直接作为 tool result 返回文本。"""
        result = _get_ocr_processor().process_image(str(p))
        text = str((result or {}).get("text") or "").strip()
        if not text:
            return json.dumps(
                {
                    "error": f"图片 {p.name} OCR 识别失败或无内容，"
                             "请检查 OCR_API_KEY / OCR_BASE_URL / OCR_MODEL_NAME 配置。",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "status": "ok",
                "routed_as": "ocr",
                "file": p.name,
                "text": text,
                "message": "当前模型不支持视觉输入，已用 OCR 提取图片内容如下（text 字段）。",
            },
            ensure_ascii=False,
        )

    # ---------- 排队 ----------

    @staticmethod
    def _queue(image_part: Dict[str, Any], file_name: str) -> None:
        # 放函数内导入：保持工具与缓冲模块的依赖方向清晰，也避免任何潜在循环。
        from tools.tools.pending_images import queue_image
        queue_image(call_id="", image_part=image_part, file_name=file_name)

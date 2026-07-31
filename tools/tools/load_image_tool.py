"""图像加载工具 load_image。

让模型在执行任务过程中按需读取图片。根据当前模型是否支持原生视觉输入，分两条路：

  1. 支持视觉（image_ability=True）：把本地文件或远程 URL 固化成 ImageRef，作为
     ToolModelResult 的结构化内容返回。AgentSession 随后把 ImageRef 作为正式
     role="user" 消息追加到 history，provider 请求边界才展开 data URI。
     这是 Chat Completions 协议下的桥接方式，因为 role="tool" 通常不能承载图片。

  2. 不支持视觉：调用现成的 OCR（MultimodalProcessor.process_image）把图片转成文字，
     直接作为 tool result 返回。为省 OCR 调用成本，这条路只接受本地文件，拒绝 URL。

图片类型沿用 MultimodalProcessor.IMAGE_MIME_MAP：png/jpg/jpeg/webp/gif/bmp/tiff。
分支判定读环境变量 LLM_MODEL_ID 在 llm_dict 里的 image_ability（可被 IMAGE_ABILITY
env 覆盖），与用户附件图片走的是同一套能力判定。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tool import Tool, ToolParameter
from constant.llm.constant_llm import ConstantLLM
from utils.multimodal import MultimodalProcessor
from agent.media_store import MediaBlobStore, get_current_media_store
from agent.tool_execution import ToolModelResult


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

    def run(self, parameters: Dict[str, Any]) -> str | ToolModelResult:
        if not self.validate_parameters(parameters):
            return json.dumps({"error": "参数验证失败：需要字符串 path。"}, ensure_ascii=False)

        raw_path = str(parameters["path"]).strip().strip('"').strip("'")
        image_native = ConstantLLM.resolve_image_ability(os.getenv("LLM_MODEL_ID"), default=False)

        if _is_http_url(raw_path):
            return self._handle_url(raw_path, image_native)
        return self._handle_local(raw_path, image_native)

    # ---------- URL 分支 ----------

    def _handle_url(self, url: str, image_native: bool) -> str | ToolModelResult:
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
        # URL 必须先固化到内容寻址存储；直接透传远程地址无法保证下一轮内容相同。
        try:
            ref = self._media_store().put_url(url)
        except Exception as error:
            return json.dumps({"error": f"下载并固化图片 URL 失败：{error}"}, ensure_ascii=False)
        image_part = {"type": "image_ref", "image_ref": ref.to_dict()}
        file_name = ref.file_name or url.rsplit("/", 1)[-1] or url
        return ToolModelResult(
            text=json.dumps(
                {
                    "status": "ok",
                    "routed_as": "image_ref",
                    "file": file_name,
                    "message": f"图片 URL 已作为视觉输入发送给模型：{file_name}。请直接根据图片内容回答。",
                },
                ensure_ascii=False,
            ),
            content=(image_part,),
        )

    # ---------- 本地路径分支 ----------

    def _handle_local(self, raw_path: str, image_native: bool) -> str | ToolModelResult:
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

    def _local_native(self, p: Path, mime_type: str) -> str | ToolModelResult:
        """视觉模型：返回可持久化的 ImageRef，provider 边界才展开 data URI。"""
        try:
            ref = self._media_store().put_file(
                p,
                mime_type=mime_type,
                source_kind="load_image",
            )
        except Exception as error:
            return json.dumps({"error": f"读取并固化图片失败：{error}"}, ensure_ascii=False)
        image_part = {"type": "image_ref", "image_ref": ref.to_dict()}
        return ToolModelResult(
            text=json.dumps(
                {
                    "status": "ok",
                    "routed_as": "image_ref",
                    "file": p.name,
                    "message": f"图片 {p.name} 已作为视觉输入发送给模型。请直接根据图片内容回答。",
                },
                ensure_ascii=False,
            ),
            content=(image_part,),
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

    @staticmethod
    def _media_store() -> MediaBlobStore:
        """读取当前 Agent 回合绑定的媒体存储，直接调用工具时使用工作目录。"""

        current = get_current_media_store()
        if current is not None:
            return current
        try:
            from tools.tools.bash_session import get_session

            workdir = Path(get_session().cwd)
        except Exception:
            workdir = Path.cwd()
        return MediaBlobStore.for_workdir(workdir)

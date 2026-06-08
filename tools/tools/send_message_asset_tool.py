"""通讯平台出站资源工具。

模型调用本工具表示“本轮回复想向 IM 平台发送一个资源”。工具本身不直接操作 QQ 或
微信连接，只负责把资源校验成结构化结果；真正发送由 PlatformEventRenderer 监听
ToolComplete 后交给当前平台适配器完成。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tool import Tool
from tools.toolParameter import ToolParameter


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
VALID_KINDS = {"sticker", "image", "file", "audio", "video"}


class SendMessageAssetTool(Tool):
    """请求通讯平台发送本地文件、图片或表情包。"""

    def __init__(
        self,
        *,
        project_root: Optional[Path] = None,
        sticker_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(
            name="send_message_asset",
            description=(
                "在通讯软件回复中发送本地资源文件。适用于发送表情包、图片、音频、视频或普通文件。"
                "发送表情包时优先传 sticker_name，工具会从表情包目录查找；发送任意文件时传 path。"
                "用户要求生成、下载、制作并发回的文件，应先保存到 /tmp/cb-agent-outputs/ "
                "或系统临时目录，再传 path 发送。不要把项目目录、服务器目录、配置目录里的"
                "现有本地文件复制到临时目录后发送，这属于绕过权限检查。"
                "本工具只应在 QQ/微信等通讯平台会话中使用，不要用纯文本假装已经发送文件。"
            ),
        )
        self.project_root = Path(project_root or Path.cwd()).resolve()
        raw_dir = sticker_dir or Path(os.getenv("CBAGENT_STICKER_DIR") or "assets/stickers")
        self.sticker_dir = raw_dir if raw_dir.is_absolute() else self.project_root / raw_dir

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="kind",
                type="string",
                required=False,
                default="file",
                description="资源类型：sticker/image/file/audio/video。发送表情包用 sticker。",
            ),
            ToolParameter(
                name="path",
                type="string",
                required=False,
                description=(
                    "要发送的本地文件路径。普通通讯用户只能发送系统临时目录里的新产物；"
                    "发送项目文件、配置文件、服务器文件等现有本地文件需要 root 用户权限。"
                    "工具会校验存在性、大小和可读性。"
                ),
            ),
            ToolParameter(
                name="sticker_name",
                type="string",
                required=False,
                description="表情包名称。可写文件名或不带扩展名的名称，将在表情包目录中递归查找。",
            ),
            ToolParameter(
                name="caption",
                type="string",
                required=False,
                description="发送资源前附带的一小段说明文字，可为空。",
            ),
            ToolParameter(
                name="reason",
                type="string",
                required=False,
                description="为什么要发送这个资源，供日志审计使用，不会直接展示给用户。",
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        kind = str(parameters.get("kind") or "file").strip().lower()
        if kind not in VALID_KINDS:
            return False
        path = parameters.get("path")
        sticker_name = parameters.get("sticker_name")
        if kind == "sticker":
            return bool(str(path or sticker_name or "").strip())
        return bool(str(path or "").strip())

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return _json_error("参数无效：kind 必须是 sticker/image/file/audio/video，并提供 path 或 sticker_name")

        kind = str(parameters.get("kind") or "file").strip().lower()
        caption = str(parameters.get("caption") or "").strip()
        reason = str(parameters.get("reason") or "").strip()

        try:
            path = self._resolve_asset_path(parameters, kind)
            file_bytes = self._read_limited_bytes(path)
            normalized_kind = self._normalize_kind_for_path(kind, path)
        except ValueError as exc:
            return _json_error(str(exc))
        except OSError as exc:
            return _json_error(f"读取资源失败：{exc}")

        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = {
            "queued": True,
            "kind": normalized_kind,
            "path": str(path),
            "file_name": path.name,
            "size": len(file_bytes),
            "content_hash": hashlib.md5(file_bytes).hexdigest(),
            "mime_type": mime_type,
            "caption": caption,
            "reason": reason,
            "sticker_dir": str(self.sticker_dir) if normalized_kind == "sticker" else "",
            "delivery_hint": "平台适配器会在当前通讯软件会话中发送该资源。",
        }
        return json.dumps(payload, ensure_ascii=False)

    def _resolve_asset_path(self, parameters: Dict[str, Any], kind: str) -> Path:
        raw_path = str(parameters.get("path") or "").strip().strip('"').strip("'")
        sticker_name = str(parameters.get("sticker_name") or "").strip()
        if kind == "sticker" and not raw_path:
            return self._find_sticker(sticker_name)
        if not raw_path:
            raise ValueError("缺少 path")
        path = Path(raw_path)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        if not path.exists():
            raise ValueError(f"文件不存在：{path}")
        if not path.is_file():
            raise ValueError(f"路径不是普通文件：{path}")
        return path

    def _find_sticker(self, sticker_name: str) -> Path:
        if not sticker_name:
            raise ValueError("缺少 sticker_name")
        root = self.sticker_dir.resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"表情包目录不存在：{root}")

        target = sticker_name.lower()
        candidates = [
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS
        ]
        for path in candidates:
            if path.name.lower() == target:
                return path.resolve()
        for path in candidates:
            if path.stem.lower() == target:
                return path.resolve()
        raise ValueError(f"未找到表情包：{sticker_name}")

    def _read_limited_bytes(self, path: Path) -> bytes:
        limit_mb = _float_env("CBAGENT_OUTBOUND_FILE_MAX_MB", 50.0)
        limit = max(1, int(limit_mb * 1024 * 1024))
        size = path.stat().st_size
        if size > limit:
            raise ValueError(f"文件 {path.name} 大小 {size} 字节，超过限制 {limit} 字节")
        return path.read_bytes()

    @staticmethod
    def _normalize_kind_for_path(kind: str, path: Path) -> str:
        ext = path.suffix.lower()
        if kind == "sticker":
            if ext not in IMAGE_EXTS:
                raise ValueError("表情包必须是图片文件")
            return "sticker"
        if kind == "image" and ext not in IMAGE_EXTS:
            raise ValueError("image 类型必须是图片文件")
        if kind == "audio" and ext not in AUDIO_EXTS:
            raise ValueError("audio 类型必须是音频文件")
        if kind == "video" and ext not in VIDEO_EXTS:
            raise ValueError("video 类型必须是视频文件")
        return kind


def _json_error(message: str) -> str:
    return json.dumps({"queued": False, "error": message}, ensure_ascii=False)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except Exception:
        return default


__all__ = ["SendMessageAssetTool"]

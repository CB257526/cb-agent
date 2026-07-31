"""内容寻址图片存储与确定性 provider 展开。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import mimetypes
import os
import urllib.request
import uuid
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Optional, Sequence

from core.media import ImageRef


MAX_MEDIA_BYTES = 20 * 1024 * 1024


def estimate_image_visual_tokens(
    *,
    width: Optional[int],
    height: Optional[int],
    byte_size: int,
) -> int:
    """按统一规则估算图片视觉 token，供附件、history 和窗口预算共用。"""

    if width and height:
        safe_width = max(1, int(width))
        safe_height = max(1, int(height))
        scale = min(1.0, 2048 / max(safe_width, safe_height))
        scaled_width = max(1, int(math.ceil(safe_width * scale)))
        scaled_height = max(1, int(math.ceil(safe_height * scale)))
        tiles = math.ceil(scaled_width / 512) * math.ceil(scaled_height / 512)
        return 85 + 170 * max(1, tiles)
    # 图片解码失败时不能因短占位符把视觉成本估成接近零。
    return max(512, min(8192, math.ceil(max(1, int(byte_size)) / 4096)))


def _data_uri_bytes(value: str) -> tuple[str, bytes]:
    """严格解析 base64 data URI，拒绝非图片正文和损坏编码。"""

    header, separator, encoded = str(value or "").partition(",")
    if not separator or not header.startswith("data:") or ";base64" not in header:
        raise ValueError("不是受支持的 base64 data URI")
    mime_type = header[5:].split(";", 1)[0].strip().lower()
    if not mime_type.startswith("image/"):
        raise ValueError("data URI 不是图片")
    try:
        return mime_type, base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("data URI 的 base64 正文损坏") from error


def _dimensions_from_bytes(data: bytes) -> tuple[Optional[int], Optional[int]]:
    """读取图片尺寸；伪图片仍允许落盘，但预算会使用字节回退。"""

    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
        return max(1, int(width)), max(1, int(height))
    except Exception:
        return None, None


def estimate_visual_tokens_in_payload(value: Any) -> int:
    """统计逻辑或 provider payload 中所有图片的视觉成本。"""

    if isinstance(value, list):
        return sum(estimate_visual_tokens_in_payload(item) for item in value)
    if not isinstance(value, dict):
        return 0
    if value.get("type") == "image_ref":
        try:
            ref = ImageRef.from_dict(value.get("image_ref") or {})
            return ref.visual_tokens or estimate_image_visual_tokens(
                width=ref.width,
                height=ref.height,
                byte_size=ref.byte_size,
            )
        except (TypeError, ValueError):
            return 0
    if value.get("type") == "image_url":
        image_url = value.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else None
        if isinstance(url, str) and url.startswith("data:"):
            try:
                _mime_type, data = _data_uri_bytes(url)
                width, height = _dimensions_from_bytes(data)
                return estimate_image_visual_tokens(
                    width=width,
                    height=height,
                    byte_size=len(data),
                )
            except ValueError:
                return 0
        # 旧远程图片无法在本地稳定读取尺寸，仍预留一个保守的最低视觉成本。
        if isinstance(url, str) and url:
            return 1024
    return sum(estimate_visual_tokens_in_payload(item) for item in value.values())


class MediaBlobStore:
    """按 SHA-256 保存图片正文，history 只引用 blob。"""

    def __init__(self, root: Path | str, *, max_bytes: int = MAX_MEDIA_BYTES) -> None:
        self.root = Path(root)
        self.max_bytes = max(1, int(max_bytes))

    @classmethod
    def for_session_store(cls, session_store: Any) -> "MediaBlobStore":
        """把所有会话共享的媒体放到 `.cbagent/media`，避免切会话后引用失效。"""

        root = Path(session_store.root).parent / "media"
        return cls(root)

    @classmethod
    def for_workdir(cls, workdir: Path | str) -> "MediaBlobStore":
        return cls(Path(workdir) / ".cbagent" / "media")

    def put_file(
        self,
        path: Path | str,
        *,
        mime_type: Optional[str] = None,
        source_kind: str = "local",
        detail: str = "auto",
    ) -> ImageRef:
        source = Path(path).expanduser().resolve()
        if source.stat().st_size > self.max_bytes:
            raise ValueError(f"图片超过媒体大小限制: {self.max_bytes} bytes")
        data = source.read_bytes()
        return self.put_bytes(
            data,
            mime_type=mime_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            file_name=source.name,
            source_kind=source_kind,
            detail=detail,
        )

    def put_url(
        self,
        url: str,
        *,
        source_kind: str = "url",
        detail: str = "auto",
    ) -> ImageRef:
        request = urllib.request.Request(
            str(url),
            headers={"User-Agent": "cb-agent-media/1"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            content_type = str(response.headers.get_content_type() or "image/*")
            data = response.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise ValueError(f"图片超过媒体大小限制: {self.max_bytes} bytes")
        name = str(url).split("?", 1)[0].rsplit("/", 1)[-1] or "remote-image"
        return self.put_bytes(
            data,
            mime_type=content_type,
            file_name=name,
            source_kind=source_kind,
            detail=detail,
        )

    def put_data_uri(
        self,
        value: str,
        *,
        file_name: Optional[str] = None,
        source_kind: str = "legacy_data_uri",
        detail: str = "auto",
    ) -> ImageRef:
        """把旧 history 中的 data URI 固化成可重放的 ImageRef。"""

        mime_type, data = _data_uri_bytes(value)
        return self.put_bytes(
            data,
            mime_type=mime_type,
            file_name=file_name,
            source_kind=source_kind,
            detail=detail,
        )

    def put_bytes(
        self,
        data: bytes,
        *,
        mime_type: str,
        file_name: Optional[str] = None,
        source_kind: str = "local",
        detail: str = "auto",
    ) -> ImageRef:
        if not isinstance(data, bytes):
            data = bytes(data)
        if not data:
            raise ValueError("图片内容为空")
        if len(data) > self.max_bytes:
            raise ValueError(f"图片超过媒体大小限制: {self.max_bytes} bytes")
        normalized_mime = str(mime_type or "").strip().lower()
        if not normalized_mime.startswith("image/"):
            raise ValueError(f"不支持的图片 MIME: {normalized_mime or '<empty>'}")
        digest = hashlib.sha256(data).hexdigest()
        blob_id = f"sha256:{digest}"
        blob_path = self._blob_path(digest)
        metadata_path = self._metadata_path(digest)
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if not blob_path.exists():
            # UUID 避免同一进程内并发摄取相同图片时争用同一个临时文件。
            temp = blob_path.with_name(
                f".{blob_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            temp.write_bytes(data)
            try:
                # replace 的结果始终是相同 SHA 内容；并发覆盖不会改变语义。
                temp.replace(blob_path)
            finally:
                temp.unlink(missing_ok=True)

        width, height = _dimensions_from_bytes(data)

        ref = ImageRef(
            schema_version=1,
            blob_id=blob_id,
            sha256=digest,
            mime_type=normalized_mime,
            byte_size=len(data),
            width=width,
            height=height,
            detail=str(detail or "auto"),
            canonicalization_version=1,
            file_name=file_name,
            source_kind=str(source_kind or "local"),
            visual_tokens=estimate_image_visual_tokens(
                width=width,
                height=height,
                byte_size=len(data),
            ),
        )
        if not metadata_path.exists():
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            temp = metadata_path.with_name(
                f".{metadata_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            temp.write_text(
                json.dumps(ref.to_dict(), ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            try:
                temp.replace(metadata_path)
            finally:
                temp.unlink(missing_ok=True)
        return ref

    def read(self, ref: ImageRef) -> bytes:
        path = self._blob_path(ref.sha256)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ValueError(f"图片 blob 不存在或不可读: {ref.blob_id}") from error
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError(f"图片 blob 校验失败: {ref.blob_id}")
        if len(data) != ref.byte_size:
            raise ValueError(f"图片 blob 长度不匹配: {ref.blob_id}")
        return data

    def validate_messages(self, messages: Sequence[Any]) -> int:
        """验证恢复 history 引用的全部唯一 blob，缺失或损坏时立即失败。"""

        refs: dict[str, ImageRef] = {}

        def collect(value: Any) -> None:
            if isinstance(value, list) or isinstance(value, tuple):
                for item in value:
                    collect(item)
                return
            if not isinstance(value, dict):
                return
            if value.get("type") == "image_ref":
                ref = ImageRef.from_dict(value.get("image_ref") or {})
                refs.setdefault(ref.blob_id, ref)
                return
            for item in value.values():
                collect(item)

        for message in messages:
            collect(message.model_dump(mode="json"))
        for ref in refs.values():
            self.read(ref)
        return len(refs)

    def to_data_uri(self, ref: ImageRef) -> str:
        """固定 MIME、base64 编码和换行策略，生成稳定传输表示。"""

        return f"data:{ref.mime_type};base64,{base64.b64encode(self.read(ref)).decode('ascii')}"

    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / "sha256" / digest[:2] / digest

    def _metadata_path(self, digest: str) -> Path:
        return self.root / "metadata" / "sha256" / digest[:2] / f"{digest}.json"


def migrate_legacy_data_uri_messages(
    messages: Sequence[Any],
    store: MediaBlobStore,
) -> tuple[list[Any], int]:
    """把已落 journal 的旧 data URI 消息投影为新一代 ImageRef history。"""

    migrated: list[Any] = []
    migrated_count = 0

    def replace_part(value: Any) -> Any:
        nonlocal migrated_count
        if isinstance(value, list):
            return [replace_part(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("type") == "image_url":
            image_url = value.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str) and url.startswith("data:"):
                detail = (
                    str(image_url.get("detail") or "auto")
                    if isinstance(image_url, dict)
                    else "auto"
                )
                ref = store.put_data_uri(url, detail=detail)
                migrated_count += 1
                return {"type": "image_ref", "image_ref": ref.to_dict()}
        return {key: replace_part(item) for key, item in value.items()}

    for message in messages:
        copy_message = message.model_copy(deep=True)
        copy_message.content = replace_part(copy_message.content)
        migrated.append(copy_message)
    return migrated, migrated_count


_current_media_store: ContextVar[Optional[MediaBlobStore]] = ContextVar(
    "cbagent_media_store",
    default=None,
)


def set_current_media_store(store: MediaBlobStore) -> Token[Optional[MediaBlobStore]]:
    """为当前 Agent 回合绑定媒体存储；工具线程会继承同一上下文。"""

    return _current_media_store.set(store)


def reset_current_media_store(token: Token[Optional[MediaBlobStore]]) -> None:
    _current_media_store.reset(token)


def get_current_media_store() -> Optional[MediaBlobStore]:
    return _current_media_store.get()


__all__ = [
    "MAX_MEDIA_BYTES",
    "MediaBlobStore",
    "estimate_image_visual_tokens",
    "estimate_visual_tokens_in_payload",
    "get_current_media_store",
    "migrate_legacy_data_uri_messages",
    "reset_current_media_store",
    "set_current_media_store",
]

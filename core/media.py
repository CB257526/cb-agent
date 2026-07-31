"""与 provider 无关的不可变图片引用。"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class ImageRef:
    """图片逻辑引用；消息历史只保存引用，不保存原始 base64。"""

    schema_version: int
    blob_id: str
    sha256: str
    mime_type: str
    byte_size: int
    width: Optional[int] = None
    height: Optional[int] = None
    detail: Optional[str] = "auto"
    canonicalization_version: int = 1
    file_name: Optional[str] = None
    source_kind: str = "local"
    visual_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        """生成稳定的逻辑 JSON，不包含图片正文。"""

        return {
            "schema_version": int(self.schema_version),
            "blob_id": str(self.blob_id),
            "sha256": str(self.sha256),
            "mime_type": str(self.mime_type),
            "byte_size": max(0, int(self.byte_size)),
            "width": self.width,
            "height": self.height,
            "detail": self.detail,
            "canonicalization_version": int(self.canonicalization_version),
            "file_name": self.file_name,
            "source_kind": str(self.source_kind),
            "visual_tokens": max(0, int(self.visual_tokens)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ImageRef":
        """严格恢复引用，拒绝缺少内容身份的伪引用。"""

        if not isinstance(value, Mapping):
            raise ValueError("image_ref 必须是对象")
        blob_id = str(value.get("blob_id") or "")
        digest = str(value.get("sha256") or "").lower()
        mime = str(value.get("mime_type") or "")
        if (
            len(digest) != 64
            or any(char not in string.hexdigits for char in digest)
            or blob_id != f"sha256:{digest}"
            or not mime.startswith("image/")
        ):
            raise ValueError("image_ref 缺少 blob_id、sha256 或 mime_type")
        return cls(
            schema_version=max(1, int(value.get("schema_version") or 1)),
            blob_id=blob_id,
            sha256=digest,
            mime_type=mime,
            byte_size=max(0, int(value.get("byte_size") or 0)),
            width=(int(value["width"]) if value.get("width") is not None else None),
            height=(int(value["height"]) if value.get("height") is not None else None),
            detail=(str(value.get("detail")) if value.get("detail") is not None else None),
            canonicalization_version=max(
                1, int(value.get("canonicalization_version") or 1)
            ),
            file_name=(str(value.get("file_name")) if value.get("file_name") else None),
            source_kind=str(value.get("source_kind") or "local"),
            visual_tokens=max(0, int(value.get("visual_tokens") or 0)),
        )


def image_ref_from_part(part: Any) -> Optional[ImageRef]:
    """从逻辑 content part 读取图片引用。"""

    if not isinstance(part, dict) or part.get("type") != "image_ref":
        return None
    value = part.get("image_ref")
    if isinstance(value, ImageRef):
        return value
    if isinstance(value, Mapping):
        return ImageRef.from_dict(value)
    return None


__all__ = ["ImageRef", "image_ref_from_part"]

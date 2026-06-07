"""多模态用户输入归一化。

这一层是“当前轮模型请求”和“跨轮上下文持久化”的安全边界：
- 当前轮请求可以在多模态模型上携带 image_url data URI；
- history/state/compact/transcript 只能保存文本摘要和元数据，绝不保存 base64。
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from constant.llm.constant_llm import ConstantLLM
from utils.multimodal import MultimodalProcessor


IMAGE_MIME_MAP = MultimodalProcessor.IMAGE_MIME_MAP
AUDIO_MIME_MAP = MultimodalProcessor.AUDIO_MIME_MAP
DEFAULT_ATTACHMENT_MAX_MB = 20


class MultimodalInputError(ValueError):
    """多模态输入无法处理时抛出，调用方应直接把 message 展示给用户。"""


@dataclass
class ProcessedAttachment:
    path: str
    modality: str
    mime_type: str
    file_name: str
    size: int
    content_hash: str
    source: str = "direct"
    text: str = ""
    routed_as: str = "text"


@dataclass
class ProcessedMultimodalPrompt:
    request_content: Union[str, List[Dict[str, Any]]]
    history_text: str
    attachments: List[ProcessedAttachment]

    def attachments_payload(self) -> List[Dict[str, Any]]:
        return [asdict(item) for item in self.attachments]


def model_supports_image(model: Optional[str]) -> bool:
    config = ConstantLLM.llm_dict.get(str(model or ""))
    return bool(config.get("image_ability")) if isinstance(config, dict) else False


def sanitize_multimodal_payload(value: Any) -> Any:
    """复制并脱敏可能包含二进制数据的多模态 payload。

    图片原生路由时，本轮 OpenAI message 会包含 ``data:image/...;base64,...``。
    这份内容只能进入模型请求，不能进入 token 估算、messages dump、日志或任何长期
    上下文。这里用递归复制的方式替换 data URI，调用方拿到的是安全副本，不会改动
    真正要发给模型的 ``messages``。
    """
    if isinstance(value, str):
        return _sanitize_data_uri(value)
    if isinstance(value, list):
        return [sanitize_multimodal_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_multimodal_payload(item) for key, item in value.items()}
    return value


def process_multimodal_prompt(
    *,
    text: str,
    attachments: Optional[Sequence[Dict[str, Any]]] = None,
    model: Optional[str] = None,
    cwd: Optional[Path] = None,
    processor: Optional[MultimodalProcessor] = None,
    history_text: Optional[str] = None,
) -> ProcessedMultimodalPrompt:
    """把用户文本和附件转换成“请求内容 + 跨轮摘要”。

    ``request_content`` 供本轮 OpenAI messages 使用；``history_text`` 供 history、
    transcript、compact 和 context_window_usage 使用。二者分开可以避免 data URI
    被长期保存或参与动态上下文估算。

    ``history_text`` 参数用于通讯软件这类入口：当前轮请求需要带上平台来源头部，
    方便模型知道消息来自 QQ/微信群；但长期会话恢复只应保留用户真正说的话。
    这里让调用方传入“干净落盘文本”，附件仍只处理一次，避免重复 OCR/ASR。
    """
    clean_text = str(text or "").strip()
    clean_history_text = str(history_text if history_text is not None else clean_text).strip()
    raw_attachments = list(attachments or [])
    if not clean_text and not raw_attachments:
        raise MultimodalInputError("请输入文本或至少添加一个附件。")

    if not raw_attachments:
        return ProcessedMultimodalPrompt(
            request_content=clean_text,
            history_text=clean_history_text,
            attachments=[],
        )

    workdir = Path(cwd) if cwd is not None else _default_cwd()
    mm_processor = processor or MultimodalProcessor()
    image_native = model_supports_image(model)
    request_parts: List[Dict[str, Any]] = []
    history_lines: List[str] = []
    processed: List[ProcessedAttachment] = []

    if clean_text:
        request_parts.append({"type": "text", "text": clean_text})
    if clean_history_text:
        history_lines.append(clean_history_text)

    attachment_notes: List[str] = []
    for idx, item in enumerate(raw_attachments, start=1):
        attachment = _process_one_attachment(
            item=item,
            index=idx,
            cwd=workdir,
            processor=mm_processor,
            image_native=image_native,
            request_parts=request_parts,
            attachment_notes=attachment_notes,
        )
        processed.append(attachment)
        history_lines.extend(_history_lines_for_attachment(idx, attachment))

    if attachment_notes:
        request_parts.insert(0, {
            "type": "text",
            "text": "\n".join(attachment_notes),
        })

    if not clean_text:
        request_parts.insert(0, {"type": "text", "text": "请根据以下附件回答用户问题。"})
    if not clean_history_text:
        history_lines.insert(0, "请根据以下附件回答用户问题。")

    return ProcessedMultimodalPrompt(
        request_content=request_parts,
        history_text="\n".join(line for line in history_lines if line).strip(),
        attachments=processed,
    )


def _process_one_attachment(
    *,
    item: Dict[str, Any],
    index: int,
    cwd: Path,
    processor: MultimodalProcessor,
    image_native: bool,
    request_parts: List[Dict[str, Any]],
    attachment_notes: List[str],
) -> ProcessedAttachment:
    if not isinstance(item, dict):
        raise MultimodalInputError(f"附件 #{index} 必须是对象。")

    path = _resolve_path(str(item.get("path") or ""), cwd)
    source = _normal_source(item.get("source"))
    modality, mime_type = _detect_modality(path, item.get("modality"))
    file_bytes = _read_limited_bytes(path)
    content_hash = hashlib.md5(file_bytes).hexdigest()
    base = ProcessedAttachment(
        path=str(path),
        modality=modality,
        mime_type=mime_type,
        file_name=path.name,
        size=len(file_bytes),
        content_hash=content_hash,
        source=source,
    )

    if modality == "image" and image_native:
        data_uri = "data:%s;base64,%s" % (
            mime_type,
            base64.b64encode(file_bytes).decode("utf-8"),
        )
        request_parts.append({"type": "image_url", "image_url": {"url": data_uri}})
        base.text = "图片已原生发送给支持视觉输入的模型。"
        base.routed_as = "image_url"
        attachment_notes.append(
            f"[附件 #{index}: image {path.name}] 图片将作为视觉输入发送给模型；"
            "跨轮历史只保留此摘要。"
        )
        return base

    if modality == "image":
        result = processor.process_image(str(path))
        text = str((result or {}).get("text") or "").strip()
        if not text:
            raise MultimodalInputError(
                f"图片附件 {path.name} OCR/视觉描述失败，请检查 OCR_API_KEY/OCR_BASE_URL 配置。"
            )
        request_parts.append({"type": "text", "text": f"[附件 #{index}: image {path.name}]\n{text}"})
        base.text = text
        base.routed_as = "ocr"
        return base

    result = processor.process_audio(str(path))
    text = str((result or {}).get("text") or "").strip()
    if not text:
        raise MultimodalInputError(
            f"音频附件 {path.name} ASR 转录失败，请检查 ASR_API_KEY/ASR_BASE_URL 配置。"
        )
    request_parts.append({"type": "text", "text": f"[附件 #{index}: audio {path.name}]\n{text}"})
    base.text = text
    base.routed_as = "asr"
    return base


def _resolve_path(raw_path: str, cwd: Path) -> Path:
    if not raw_path.strip():
        raise MultimodalInputError("附件缺少 path。")
    p = Path(raw_path.strip().strip('"'))
    if not p.is_absolute():
        p = cwd / p
    p = p.resolve()
    if not p.exists() or not p.is_file():
        raise MultimodalInputError(f"附件文件不存在或不是普通文件：{p}")
    return p


def _default_cwd() -> Path:
    """返回附件相对路径的默认基准目录。

    cb-agent 的文件读写、搜索和 bash 都共享 BashSession.cwd。用户执行过 ``cd`` 后，
    ``/attach ./image.png`` 也应当和 ``file_read(path="./image.png")`` 指向同一位置。
    单测或极早期初始化路径下如果 BashSession 不可用，则退回进程 cwd。
    """
    try:
        from tools.tools.bash_session import get_session
        return Path(get_session().cwd).expanduser().resolve()
    except Exception:
        return Path.cwd().resolve()


def _detect_modality(path: Path, requested: Any) -> tuple[str, str]:
    ext = path.suffix.lower()
    inferred: Optional[str] = None
    mime: Optional[str] = None
    if ext in IMAGE_MIME_MAP:
        inferred, mime = "image", IMAGE_MIME_MAP[ext]
    elif ext in AUDIO_MIME_MAP:
        inferred, mime = "audio", AUDIO_MIME_MAP[ext]

    wanted = str(requested or inferred or "").strip().lower()
    if wanted not in {"image", "audio"} or inferred is None:
        raise MultimodalInputError(f"不支持的附件格式：{path.suffix or path.name}")
    if wanted != inferred:
        raise MultimodalInputError(f"附件 {path.name} 的 modality={wanted} 与扩展名不匹配。")
    return inferred, str(mime)


def _read_limited_bytes(path: Path) -> bytes:
    limit_mb_raw = os.getenv("CBAGENT_ATTACHMENT_MAX_MB")
    try:
        limit_mb = float(limit_mb_raw) if limit_mb_raw else DEFAULT_ATTACHMENT_MAX_MB
    except ValueError:
        limit_mb = DEFAULT_ATTACHMENT_MAX_MB
    limit = max(1, int(limit_mb * 1024 * 1024))
    size = path.stat().st_size
    if size > limit:
        raise MultimodalInputError(
            f"附件 {path.name} 大小 {size} 字节，超过限制 {limit} 字节。"
        )
    return path.read_bytes()


def _normal_source(value: Any) -> str:
    source = str(value or "direct").strip().lower()
    return source if source in {"direct", "clipboard", "ocr", "asr"} else "direct"


def _sanitize_data_uri(value: str) -> str:
    """把 data URI 替换成短占位符，保留 MIME 与长度便于诊断。"""
    if not value.startswith("data:") or ";base64," not in value:
        return value
    mime = value[5:value.find(";base64,")] or "application/octet-stream"
    return f"[data-uri omitted: {mime}, chars={len(value)}]"


def _history_lines_for_attachment(index: int, item: ProcessedAttachment) -> List[str]:
    header = (
        f"[附件摘要 #{index}] modality={item.modality} file={item.file_name} "
        f"source={item.source} routed_as={item.routed_as} "
        f"size={item.size} hash={item.content_hash}"
    )
    if item.text:
        return [header, item.text]
    return [header]


__all__ = [
    "MultimodalInputError",
    "ProcessedAttachment",
    "ProcessedMultimodalPrompt",
    "process_multimodal_prompt",
    "model_supports_image",
    "sanitize_multimodal_payload",
]

"""多模态用户输入归一化。

这一层是“当前轮模型请求”和“跨轮上下文持久化”的安全边界：
- 当前轮请求可以在多模态模型上携带 image_url data URI；
- history/state/compact/transcript 只能保存文本摘要和元数据，绝不保存 base64。
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from constant.llm.constant_llm import ConstantLLM
from utils.multimodal import MultimodalProcessor


IMAGE_MIME_MAP = MultimodalProcessor.IMAGE_MIME_MAP
AUDIO_MIME_MAP = MultimodalProcessor.AUDIO_MIME_MAP
DEFAULT_ATTACHMENT_MAX_MB = 20
# 兼容旧 env 名：现表示「所有附件 preview 合计」字符预算，而非单文件 120K 整文注入。
DEFAULT_ATTACHMENT_TEXT_MAX_CHARS = 24_000
DEFAULT_ATTACHMENT_PREVIEW_MIN_CHARS = 2_000
DEFAULT_ATTACHMENT_PREVIEW_MAX_CHARS = 48_000
DEFAULT_ATTACHMENT_SOFT_LIMIT_RATIO = 0.08
TEXT_ATTACHMENT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".java",
    ".c",
    ".cpp",
    ".cs",
    ".go",
    ".rs",
    ".php",
    ".rb",
    ".sh",
    ".bat",
    ".ps1",
    ".sql",
    ".log",
}
DOCUMENT_ATTACHMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
    ".epub",
}


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
    text: str = ""  # 模型可见 preview 或 OCR/ASR 摘要；不是完整正文
    routed_as: str = "text"
    artifact_path: str = ""  # 完整 Markdown/转录 artifact；空表示未落盘
    full_chars: int = 0
    preview_chars: int = 0


@dataclass
class ProcessedMultimodalPrompt:
    request_content: Union[str, List[Dict[str, Any]]]
    history_text: str
    attachments: List[ProcessedAttachment]

    def attachments_payload(self) -> List[Dict[str, Any]]:
        return [asdict(item) for item in self.attachments]


def model_supports_image(model: Optional[str]) -> bool:
    """模型是否支持原生视觉输入。

    取值优先级:环境变量 IMAGE_ABILITY > llm_dict[model]["image_ability"] >
    默认 False。换服务商导致模型名对不上 llm_dict 时,用 .env 的 IMAGE_ABILITY
    兜底,避免多模态模型被误判为纯文本而强制走 OCR。
    """
    return ConstantLLM.resolve_image_ability(model, default=False)


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
    soft_limit_tokens: Optional[int] = None,
) -> ProcessedMultimodalPrompt:
    """把用户文本和附件转换成“请求内容 + 跨轮摘要”。

    ``request_content`` 供本轮 OpenAI messages 使用；``history_text`` 供 history、
    transcript、compact 和 context_window_usage 使用。二者分开可以避免 data URI
    被长期保存或参与动态上下文估算。

    文本/文档附件：完整 Markdown 写入
    ``.cbagent/attachments/<content_hash>/content.md``；请求与 history 只注入
    manifest + 聚合预算内的 preview，不再整文 120K 注入。
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

    aggregate_budget = _attachment_aggregate_budget_chars(
        soft_limit_tokens=soft_limit_tokens,
        user_text_chars=len(clean_text),
    )
    remaining_preview = aggregate_budget
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
            remaining_preview_budget=remaining_preview,
        )
        remaining_preview = max(0, remaining_preview - max(0, attachment.preview_chars))
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
    remaining_preview_budget: int,
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
        base.full_chars = 0
        base.preview_chars = 0
        attachment_notes.append(
            f"[附件 #{index}: image {path.name}] 图片将作为视觉输入发送给模型；"
            "跨轮历史只保留此摘要，base64 不会进入 history/transcript。"
        )
        return base

    if modality == "image":
        result = processor.process_image(str(path))
        text = str((result or {}).get("text") or "").strip()
        if not text:
            raise MultimodalInputError(
                f"图片附件 {path.name} OCR/视觉描述失败，请检查 OCR_API_KEY/OCR_BASE_URL 配置。"
            )
        artifact = _persist_text_artifact(cwd, content_hash, text, kind="ocr")
        preview = _clip_preview(text, remaining_preview_budget)
        block = _manifest_block(
            index=index,
            attachment=base,
            artifact_path=artifact,
            full_chars=len(text),
            preview=preview,
            modality_label="image/ocr",
        )
        request_parts.append({"type": "text", "text": block})
        base.text = preview
        base.routed_as = "ocr"
        base.artifact_path = artifact
        base.full_chars = len(text)
        base.preview_chars = len(preview)
        return base

    if modality == "audio":
        result = processor.process_audio(str(path))
        text = str((result or {}).get("text") or "").strip()
        if not text:
            raise MultimodalInputError(
                f"音频附件 {path.name} ASR 转录失败，请检查 ASR_API_KEY/ASR_BASE_URL 配置。"
            )
        artifact = _persist_text_artifact(cwd, content_hash, text, kind="asr")
        preview = _clip_preview(text, remaining_preview_budget)
        block = _manifest_block(
            index=index,
            attachment=base,
            artifact_path=artifact,
            full_chars=len(text),
            preview=preview,
            modality_label="audio/asr",
        )
        request_parts.append({"type": "text", "text": block})
        base.text = preview
        base.routed_as = "asr"
        base.artifact_path = artifact
        base.full_chars = len(text)
        base.preview_chars = len(preview)
        return base

    markdown = _convert_attachment_to_markdown(path, modality=modality)
    if not markdown.strip():
        raise MultimodalInputError(f"附件 {path.name} 转换为 Markdown 后没有可用文本。")
    artifact = _persist_text_artifact(cwd, content_hash, markdown, kind="markdown")
    preview = _clip_preview(markdown, remaining_preview_budget)
    block = _manifest_block(
        index=index,
        attachment=base,
        artifact_path=artifact,
        full_chars=len(markdown),
        preview=preview,
        modality_label=modality,
    )
    request_parts.append({"type": "text", "text": block})
    base.text = preview
    base.routed_as = "markdown"
    base.artifact_path = artifact
    base.full_chars = len(markdown)
    base.preview_chars = len(preview)
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
    elif ext in TEXT_ATTACHMENT_EXTENSIONS:
        inferred = "text"
        mime = mimetypes.guess_type(path.name)[0] or "text/plain"
    elif ext in DOCUMENT_ATTACHMENT_EXTENSIONS:
        inferred = "document"
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    wanted = str(requested or inferred or "").strip().lower()
    if wanted not in {"image", "audio", "text", "document"} or inferred is None:
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


def _convert_attachment_to_markdown(path: Path, *, modality: str) -> str:
    """Convert text/document attachments into Markdown using MarkItDown.

    The Python backend is launched from the project environment, so importing
    MarkItDown here uses the same runtime as the agent. Plain text files fall
    back to direct decoding if MarkItDown is unavailable or cannot parse them.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            from markitdown import MarkItDown

            result = MarkItDown().convert(str(path))
        text = (
            getattr(result, "text_content", None)
            or getattr(result, "markdown", None)
            or str(result)
        )
        return str(text or "").strip()
    except ImportError as exc:
        if modality == "text":
            return _read_text_attachment(path)
        raise MultimodalInputError(
            "MarkItDown 未安装，无法转换文档附件。请在启动 cb-agent 的 Python 环境中安装 markitdown。"
        ) from exc
    except Exception as exc:
        if modality == "text":
            fallback = _read_text_attachment(path)
            if fallback.strip():
                return fallback
        raise MultimodalInputError(f"附件 {path.name} 转换 Markdown 失败：{exc}") from exc


def _read_text_attachment(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def _attachment_aggregate_budget_chars(
    *,
    soft_limit_tokens: Optional[int],
    user_text_chars: int,
) -> int:
    """所有附件 preview 合计字符预算。

    优先用 soft_limit 的比例，再夹在 min/max 与 env 默认之间，并扣掉当前用户文字。
    """
    raw = os.getenv("CBAGENT_ATTACHMENT_TEXT_MAX_CHARS")
    try:
        env_default = int(raw) if raw else DEFAULT_ATTACHMENT_TEXT_MAX_CHARS
    except ValueError:
        env_default = DEFAULT_ATTACHMENT_TEXT_MAX_CHARS
    env_default = max(DEFAULT_ATTACHMENT_PREVIEW_MIN_CHARS, env_default)

    if soft_limit_tokens and soft_limit_tokens > 0:
        # 粗略 1 token ≈ 4 chars；附件预算 = soft_limit 的 8%。
        ratio_budget = int(soft_limit_tokens * 4 * DEFAULT_ATTACHMENT_SOFT_LIMIT_RATIO)
        budget = max(
            DEFAULT_ATTACHMENT_PREVIEW_MIN_CHARS,
            min(DEFAULT_ATTACHMENT_PREVIEW_MAX_CHARS, ratio_budget, env_default),
        )
    else:
        budget = min(DEFAULT_ATTACHMENT_PREVIEW_MAX_CHARS, env_default)

    # 用户正文先占用一部分预算，避免附件把当前输入顶爆。
    return max(DEFAULT_ATTACHMENT_PREVIEW_MIN_CHARS // 2, budget - max(0, user_text_chars))


def _persist_text_artifact(cwd: Path, content_hash: str, text: str, *, kind: str) -> str:
    """把完整转换文本写入 .cbagent/attachments/<hash>/content.md。"""
    root = Path(cwd).resolve() / ".cbagent" / "attachments" / content_hash
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / "content.md"
        path.write_text(text, encoding="utf-8", errors="replace")
        meta = root / "meta.txt"
        meta.write_text(f"kind={kind}\nchars={len(text)}\n", encoding="utf-8")
        return str(path.resolve())
    except OSError as error:
        raise MultimodalInputError(
            f"附件 artifact 落盘失败（hash={content_hash}）：{error}"
        ) from error


def _clip_preview(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    return text[:budget].rstrip()


def _manifest_block(
    *,
    index: int,
    attachment: ProcessedAttachment,
    artifact_path: str,
    full_chars: int,
    preview: str,
    modality_label: str,
) -> str:
    lines = [
        f"[附件 #{index}: {modality_label} {attachment.file_name}]",
        f"path={attachment.path}",
        f"hash={attachment.content_hash}",
        f"size_bytes={attachment.size}",
        f"full_chars={full_chars}",
        f"artifact={artifact_path}",
        f"preview_chars={len(preview)}",
        (
            f"续读: file_read(path={artifact_path!r}, head=100) "
            "或 start_line/end_line / start_char/end_char / start_byte/end_byte"
        ),
    ]
    if preview:
        lines.append("")
        lines.append(preview)
        if len(preview) < full_chars:
            lines.append("")
            lines.append(
                f"... [preview 已截断；完整内容见 artifact，共 {full_chars} 字符] ..."
            )
    else:
        lines.append("")
        lines.append(
            f"... [聚合 preview 预算已用尽；完整内容见 artifact，共 {full_chars} 字符] ..."
        )
    return "\n".join(lines)


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
    """history 只保留 manifest + 短 preview，不保存完整 Markdown 正文。"""
    header = (
        f"[附件摘要 #{index}] modality={item.modality} file={item.file_name} "
        f"source={item.source} routed_as={item.routed_as} "
        f"size={item.size} hash={item.content_hash}"
    )
    if item.artifact_path:
        header += f" artifact={item.artifact_path} full_chars={item.full_chars}"
    lines = [header]
    if item.text:
        # history 再限一次，避免 OCR/ASR 过长
        preview = item.text if len(item.text) <= 2000 else item.text[:2000] + "…"
        lines.append(preview)
    return lines


__all__ = [
    "MultimodalInputError",
    "ProcessedAttachment",
    "ProcessedMultimodalPrompt",
    "process_multimodal_prompt",
    "model_supports_image",
    "sanitize_multimodal_payload",
]

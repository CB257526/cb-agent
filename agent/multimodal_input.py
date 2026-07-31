"""多模态用户输入归一化。

这一层只生成模型真正接收的用户 content。调用方把该 content 原样追加到 canonical
history，因此当前轮和后续轮不存在两种表示。日志与 UI 可以使用脱敏副本，但不得
用脱敏内容反向覆盖模型历史。
"""

from __future__ import annotations

import base64
import hashlib
import math
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
DEFAULT_ATTACHMENT_MAX_COUNT = 16
DEFAULT_VISUAL_TOKEN_BUDGET = 8_000
DEFAULT_VISUAL_TOKEN_BUDGET_MAX = 16_000
DEFAULT_VISUAL_SOFT_LIMIT_RATIO = 0.10
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
    estimated_tokens: int = 0  # 原生视觉输入的保守 token 估算；文本 preview 仍由统一 tokenizer 统计


@dataclass
class ProcessedMultimodalPrompt:
    request_content: Union[str, List[Dict[str, Any]]]
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
    canonical history 会保存模型实际看见的 data URI，以维持下一请求的协议前缀。
    token 文本估算、messages dump 和日志使用本函数生成的递归安全副本；替换只发生
    在副本中，不会改动真正要发给模型或写入 history 的 ``messages``。
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
    soft_limit_tokens: Optional[int] = None,
    image_ability: Optional[bool] = None,
) -> ProcessedMultimodalPrompt:
    """把用户文本和附件转换成唯一的模型可见请求内容。

    ``request_content`` 会直接进入 canonical history。原生图片因此保留稳定 data
    URI；日志和 UI 只能通过 ``sanitize_multimodal_payload`` 生成脱敏副本。

    文本/文档附件：完整 Markdown 写入
    ``.cbagent/attachments/<content_hash>/content.md``；请求只注入 manifest 与聚合
    预算内的 preview，不再整文 120K 注入。
    """
    clean_text = str(text or "").strip()
    raw_attachments = list(attachments or [])
    if not clean_text and not raw_attachments:
        raise MultimodalInputError("请输入文本或至少添加一个附件。")

    if not raw_attachments:
        return ProcessedMultimodalPrompt(
            request_content=clean_text,
            attachments=[],
        )

    max_count_raw = os.getenv("CBAGENT_ATTACHMENT_MAX_COUNT")
    try:
        max_count = int(max_count_raw) if max_count_raw else DEFAULT_ATTACHMENT_MAX_COUNT
    except ValueError:
        max_count = DEFAULT_ATTACHMENT_MAX_COUNT
    if len(raw_attachments) > max(1, max_count):
        raise MultimodalInputError(
            f"附件数量 {len(raw_attachments)} 超过本轮上限 {max(1, max_count)}。"
        )

    workdir = Path(cwd) if cwd is not None else _default_cwd()
    mm_processor = processor or MultimodalProcessor()
    # Session 传入的 ActiveModelConfig 能区分同名模型的不同 provider；仅旧调用方
    # 缺少该值时才按 model id 回退全局默认。
    image_native = bool(image_ability) if image_ability is not None else model_supports_image(model)
    request_parts: List[Dict[str, Any]] = []
    processed: List[ProcessedAttachment] = []

    if clean_text:
        request_parts.append({"type": "text", "text": clean_text})

    aggregate_budget = _attachment_aggregate_budget_chars(
        soft_limit_tokens=soft_limit_tokens,
        user_text_chars=len(clean_text),
    )
    remaining_preview = aggregate_budget
    visual_budget = _visual_token_budget(soft_limit_tokens)
    visual_tokens_used = 0
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
        visual_tokens_used += max(0, attachment.estimated_tokens)
        if visual_tokens_used > visual_budget:
            raise MultimodalInputError(
                "原生图片聚合视觉预算超限："
                f"estimated={visual_tokens_used} tokens, budget={visual_budget} tokens。"
                "请减少图片数量、降低分辨率或分批发送。"
        )
        processed.append(attachment)

    if attachment_notes:
        request_parts.insert(0, {
            "type": "text",
            "text": "\n".join(attachment_notes),
        })

    if not clean_text:
        request_parts.insert(0, {"type": "text", "text": "请根据以下附件回答用户问题。"})

    return ProcessedMultimodalPrompt(
        request_content=request_parts,
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
        base.estimated_tokens = _estimate_native_image_tokens(path, len(file_bytes))
        attachment_notes.append(
            f"[附件 #{index}: image {path.name}] 图片将作为视觉输入发送给模型；"
            "后续请求继续使用 canonical history 中同一份图像内容。"
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
    """使用 MarkItDown 把文本或文档附件转换为 Markdown。

    Python 后端从项目环境启动，因此这里导入的 MarkItDown 与 Agent 使用同一运行
    环境。MarkItDown 不可用或无法解析普通文本时，回退为直接解码文件。
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


def _visual_token_budget(soft_limit_tokens: Optional[int]) -> int:
    """按当前模型 soft limit 计算所有原生图片共享的视觉预算。"""
    if soft_limit_tokens and soft_limit_tokens > 0:
        return max(
            512,
            min(
                DEFAULT_VISUAL_TOKEN_BUDGET_MAX,
                int(soft_limit_tokens * DEFAULT_VISUAL_SOFT_LIMIT_RATIO),
            ),
        )
    return DEFAULT_VISUAL_TOKEN_BUDGET


def _estimate_native_image_tokens(path: Path, size_bytes: int) -> int:
    """按图片尺寸估算视觉 token，无法读取尺寸时按文件字节数保守回退。"""
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
        width = max(1, int(width))
        height = max(1, int(height))
        scale = min(1.0, 2048 / max(width, height))
        scaled_width = max(1, int(math.ceil(width * scale)))
        scaled_height = max(1, int(math.ceil(height * scale)))
        tiles = math.ceil(scaled_width / 512) * math.ceil(scaled_height / 512)
        return 85 + 170 * max(1, tiles)
    except Exception:
        # 不同 provider 的视觉计费不同；尺寸不可得时宁可高估，避免 base64 被短占位符掩盖。
        return max(512, min(8_192, math.ceil(max(1, size_bytes) / 4096)))


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


__all__ = [
    "MultimodalInputError",
    "ProcessedAttachment",
    "ProcessedMultimodalPrompt",
    "process_multimodal_prompt",
    "model_supports_image",
    "sanitize_multimodal_payload",
]

"""QQ/NapCat 出站文件交付层。

NapCat 和 cb-agent 同机运行时，OneBot action 可以直接读取 cb-agent 传过去的
本地路径；但 NapCat 放进 Docker 后，容器文件系统和宿主机文件系统并不互通，
直接传宿主机路径会变成“容器内不存在的路径”。本模块把“本地文件”转换成
NapCat 可读取的引用，QQ 适配器只需要按候选引用依次尝试发送。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

from agent.qq.config import QQConfig

logger = logging.getLogger(__name__)

_TOKEN_PATH_PREFIX = "/cbagent-files/"


class FileDeliveryError(RuntimeError):
    """出站文件无法转换成 NapCat 可读取引用。"""


@dataclass(frozen=True)
class DeliveryCandidate:
    """一次可尝试的文件交付方式。

    ``ref`` 是最终交给 NapCat 的字符串。它可能是本地路径、容器内映射路径、HTTP
    URL 或 ``base64://`` 数据。适配器会按候选顺序调用 OneBot action，失败后再试
    下一个候选。
    """

    method: str
    ref: str
    source_path: str
    size: int
    note: str = ""


@dataclass(frozen=True)
class DeliveryPlan:
    """针对一个文件生成的候选交付计划。"""

    candidates: List[DeliveryCandidate]
    errors: List[str]


@dataclass
class _HttpFileEntry:
    path: Path
    file_name: str
    mime_type: str
    expires_at: float


class QQFileDeliveryManager:
    """把 cb-agent 本地文件转换成 NapCat 可读取的引用。

    支持模式：
    - ``path``：保持旧行为，直接把本机路径交给 NapCat。
    - ``mapped_path``：复制到宿主机共享目录，并把路径改写为容器内路径。
    - ``http``：启动只读临时 HTTP 文件服务，让 NapCat 通过 URL 下载。
    - ``base64``：小文件内联成 ``base64://``，避免路径共享。
    - ``auto``：按 mapped_path -> http -> base64 -> path 生成候选。
    """

    def __init__(self, config: QQConfig) -> None:
        self.config = config
        self._http_lock = threading.RLock()
        self._http_server: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._http_entries: Dict[str, _HttpFileEntry] = {}
        self._http_bound_port: Optional[int] = None

    def build_plan(self, path: str) -> DeliveryPlan:
        """为一个出站文件生成候选引用。

        这里会做文件存在性检查。复制共享目录、读取 base64 都可能较慢，调用方应放到
        线程池里执行，避免阻塞 asyncio 事件循环。
        """

        raw_path = str(path or "").strip()
        if is_external_file_reference(raw_path):
            return DeliveryPlan(
                candidates=[
                    DeliveryCandidate(
                        method="external",
                        ref=raw_path,
                        source_path=raw_path,
                        size=0,
                        note="资源已经是 URL/base64/data/file 引用",
                    )
                ],
                errors=[],
            )

        source = Path(str(path or "")).expanduser()
        if not source.is_absolute():
            source = Path.cwd() / source
        source = source.resolve()
        if not source.exists():
            raise FileDeliveryError(f"文件不存在：{source}")
        if not source.is_file():
            raise FileDeliveryError(f"路径不是普通文件：{source}")

        mode = self.config.file_delivery_mode
        methods = self._candidate_methods(mode)
        errors: List[str] = []
        candidates: List[DeliveryCandidate] = []
        for method in methods:
            try:
                candidate = self._build_candidate(method, source)
            except FileDeliveryError as exc:
                errors.append(f"{method}: {exc}")
                continue
            except OSError as exc:
                errors.append(f"{method}: 系统文件/网络错误：{exc}")
                continue
            candidates.append(candidate)
        return DeliveryPlan(candidates=candidates, errors=errors)

    def close(self) -> None:
        """关闭临时 HTTP 文件服务。

        进程退出时 daemon 线程也会结束；显式 close 主要用于测试或未来热重载配置。
        """

        with self._http_lock:
            server = self._http_server
            self._http_server = None
            self._http_thread = None
            self._http_entries.clear()
        if server is not None:
            server.shutdown()
            server.server_close()

    def _candidate_methods(self, mode: str) -> List[str]:
        clean = (mode or "path").strip().lower()
        if clean == "auto":
            return ["mapped_path", "http", "base64", "path"]
        if clean in {"path", "mapped_path", "http", "base64"}:
            return [clean]
        return ["path"]

    def _build_candidate(self, method: str, source: Path) -> DeliveryCandidate:
        if method == "path":
            size = source.stat().st_size
            return DeliveryCandidate("path", str(source), str(source), size, "直接传宿主机路径")
        if method == "mapped_path":
            return self._build_mapped_path_candidate(source)
        if method == "http":
            return self._build_http_candidate(source)
        if method == "base64":
            return self._build_base64_candidate(source)
        raise FileDeliveryError(f"未知交付方式：{method}")

    def _build_mapped_path_candidate(self, source: Path) -> DeliveryCandidate:
        host_root = _resolve_optional_path(self.config.file_host_prefix)
        napcat_root = (self.config.file_napcat_prefix or "").strip()
        if host_root is None:
            raise FileDeliveryError("未配置 QQ_FILE_HOST_PREFIX")
        if not napcat_root:
            raise FileDeliveryError("未配置 QQ_FILE_NAPCAT_PREFIX")

        host_root.mkdir(parents=True, exist_ok=True)
        digest = _file_sha256(source)[:16]
        target_name = f"{_safe_stem(source.stem)}-{digest}{source.suffix}"
        target = (host_root / target_name).resolve()

        # 复制到共享目录，而不是直接要求用户把所有可能发送的文件都预先放进共享卷。
        # Docker 场景只需要把 host_root 挂载到 napcat_root，NapCat 就能读到复制后的文件。
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)

        relative = target.relative_to(host_root)
        napcat_path = _join_remote_path(napcat_root, relative)
        return DeliveryCandidate(
            method="mapped_path",
            ref=napcat_path,
            source_path=str(source),
            size=target.stat().st_size,
            note=f"已复制到共享目录：{target}",
        )

    def _build_http_candidate(self, source: Path) -> DeliveryCandidate:
        public_base = self._public_http_base_url()
        token = secrets.token_urlsafe(24)
        file_name = source.name
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        expires_at = time.time() + max(30.0, float(self.config.file_http_ttl_seconds))
        with self._http_lock:
            self._ensure_http_server_locked()
            self._purge_expired_http_entries_locked()
            self._http_entries[token] = _HttpFileEntry(
                path=source,
                file_name=file_name,
                mime_type=mime_type,
                expires_at=expires_at,
            )
        url = f"{public_base}{_TOKEN_PATH_PREFIX}{token}/{quote(file_name)}"
        return DeliveryCandidate(
            method="http",
            ref=url,
            source_path=str(source),
            size=source.stat().st_size,
            note=f"HTTP URL 有效期约 {int(self.config.file_http_ttl_seconds)} 秒",
        )

    def _build_base64_candidate(self, source: Path) -> DeliveryCandidate:
        limit = max(1, int(float(self.config.file_base64_max_mb) * 1024 * 1024))
        size = source.stat().st_size
        if size > limit:
            raise FileDeliveryError(f"文件 {size} 字节超过 base64 上限 {limit} 字节")
        data = source.read_bytes()
        encoded = base64.b64encode(data).decode("ascii")
        return DeliveryCandidate(
            method="base64",
            ref=f"base64://{encoded}",
            source_path=str(source),
            size=size,
            note="小文件已内联为 base64",
        )

    def _public_http_base_url(self) -> str:
        configured = (self.config.file_http_public_base_url or "").strip().rstrip("/")
        if configured:
            parsed = urlparse(configured)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise FileDeliveryError("QQ_FILE_HTTP_PUBLIC_BASE_URL 必须是 http/https URL")
            if not parsed.port and int(self.config.file_http_port) <= 0:
                raise FileDeliveryError(
                    "QQ_FILE_HTTP_PUBLIC_BASE_URL 未包含端口时，必须显式配置 QQ_FILE_HTTP_PORT"
                )
            return configured

        host = (self.config.file_http_host or "127.0.0.1").strip()
        if host in {"0.0.0.0", "::", ""}:
            raise FileDeliveryError(
                "QQ_FILE_HTTP_PUBLIC_BASE_URL 未配置，且 QQ_FILE_HTTP_HOST 不是可访问地址"
            )
        self._ensure_http_server_for_public_port()
        port = self._http_bound_port or self.config.file_http_port
        return f"http://{host}:{port}"

    def _ensure_http_server_for_public_port(self) -> None:
        with self._http_lock:
            self._ensure_http_server_locked()

    def _ensure_http_server_locked(self) -> None:
        if self._http_server is not None:
            return

        manager = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server 固定方法名
                manager._serve_http_file(self)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 标准库签名
                logger.debug("QQ file HTTP server: " + format, *args)

        server = ThreadingHTTPServer(
            (self.config.file_http_host, self._http_listen_port()),
            Handler,
        )
        self._http_server = server
        self._http_bound_port = int(server.server_address[1])
        self._http_thread = threading.Thread(
            target=server.serve_forever,
            name="QQFileDeliveryHTTP",
            daemon=True,
        )
        self._http_thread.start()
        logger.info(
            "QQ file HTTP delivery server started on %s:%s",
            self.config.file_http_host,
            self._http_bound_port,
        )

    def _serve_http_file(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        if not parsed.path.startswith(_TOKEN_PATH_PREFIX):
            _send_http_error(handler, HTTPStatus.NOT_FOUND, "not found")
            return
        rest = parsed.path[len(_TOKEN_PATH_PREFIX):]
        token = unquote(rest.split("/", 1)[0])
        with self._http_lock:
            self._purge_expired_http_entries_locked()
            entry = self._http_entries.get(token)
        if entry is None:
            _send_http_error(handler, HTTPStatus.NOT_FOUND, "file token not found or expired")
            return
        if not entry.path.exists() or not entry.path.is_file():
            _send_http_error(handler, HTTPStatus.NOT_FOUND, "file not found")
            return

        try:
            size = entry.path.stat().st_size
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", entry.mime_type)
            handler.send_header("Content-Length", str(size))
            handler.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(entry.file_name)}",
            )
            handler.end_headers()
            with entry.path.open("rb") as fh:
                shutil.copyfileobj(fh, handler.wfile, length=64 * 1024)
        except BrokenPipeError:
            logger.debug("QQ file HTTP client disconnected: %s", entry.path)
        except Exception:
            logger.exception("QQ file HTTP serve failed: %s", entry.path)

    def _purge_expired_http_entries_locked(self) -> None:
        now = time.time()
        expired = [token for token, entry in self._http_entries.items() if entry.expires_at < now]
        for token in expired:
            self._http_entries.pop(token, None)

    def _http_listen_port(self) -> int:
        """返回 HTTP 文件服务监听端口。

        配了 ``QQ_FILE_HTTP_PUBLIC_BASE_URL`` 且 URL 中带端口时，默认使用这个端口
        监听，避免用户只配了公开 URL、却因为 ``QQ_FILE_HTTP_PORT=0`` 绑定到随机端口。
        反向代理场景仍可以显式设置 ``QQ_FILE_HTTP_PORT`` 为内部端口。
        """

        configured_port = int(self.config.file_http_port)
        if configured_port > 0:
            return configured_port
        public_base = (self.config.file_http_public_base_url or "").strip()
        if public_base:
            parsed = urlparse(public_base)
            if parsed.port:
                return int(parsed.port)
        return 0


def is_external_file_reference(value: str) -> bool:
    """判断字符串是否已经是 NapCat 可直接识别的资源引用。"""

    clean = str(value or "").strip().lower()
    return clean.startswith(("http://", "https://", "base64://", "data:", "file://"))


def looks_like_posix_absolute_path(value: str) -> bool:
    """识别 Docker/Linux 容器内的 POSIX 绝对路径。

    Windows 宿主机上如果直接 ``Path('/app/file')``，会被解析成当前盘符下的
    ``\\app\\file``，所以 OneBot 图片/语音消息段需要在转换前识别这类路径。
    """

    text = str(value or "").strip()
    return text.startswith("/") and not re.match(r"^/[A-Za-z]:", text)


def _resolve_optional_path(raw: str) -> Optional[Path]:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_stem(value: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z._-]+", "_", value.strip())
    return clean[:80] or "file"


def _join_remote_path(prefix: str, relative: Path) -> str:
    rel = "/".join(part for part in relative.parts if part not in {"", "."})
    if not rel:
        return prefix
    if prefix.endswith(("/", "\\")):
        return prefix + rel
    separator = "\\" if "\\" in prefix and not prefix.startswith("/") else "/"
    return prefix + separator + rel


def _send_http_error(handler: BaseHTTPRequestHandler, status: HTTPStatus, message: str) -> None:
    body = message.encode("utf-8", errors="replace")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


__all__ = [
    "DeliveryCandidate",
    "DeliveryPlan",
    "FileDeliveryError",
    "QQFileDeliveryManager",
    "is_external_file_reference",
    "looks_like_posix_absolute_path",
]

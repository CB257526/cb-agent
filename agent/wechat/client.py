"""微信 OC HTTP 客户端。"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urljoin

import requests

from agent.wechat.config import WeChatConfig

logger = logging.getLogger(__name__)


class WeChatOCClient:
    """封装个人微信 OC HTTP API。

    这里使用同步 requests，adapter 通过 ``asyncio.to_thread`` 调用，和项目现有 QQ
    附件下载路径一致，避免新增 aiohttp 运行时依赖。
    """

    def __init__(self, config: WeChatConfig) -> None:
        self.config = config
        self.token = config.token
        self.base_url = config.base_url
        self.cdn_base_url = config.cdn_base_url

    def update_auth(
        self,
        *,
        token: Optional[str] = None,
        base_url: str = "",
        cdn_base_url: str = "",
    ) -> None:
        if token is not None:
            self.token = token
        if base_url:
            self.base_url = base_url.rstrip("/")
        if cdn_base_url:
            self.cdn_base_url = cdn_base_url.rstrip("/")

    def request_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        token_required: bool = False,
        timeout_ms: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = urljoin(self.base_url.rstrip("/") + "/", endpoint.lstrip("/"))
        hdrs = self._headers(token_required=token_required)
        if headers:
            hdrs.update(headers)
        timeout = (timeout_ms or self.config.api_timeout_ms) / 1000
        try:
            response = requests.request(
                method.upper(),
                url,
                params=params,
                json=_drop_none(payload) if payload is not None else None,
                headers=hdrs,
                timeout=timeout,
            )
        except requests.Timeout:
            # getupdates / 二维码状态轮询本来就是长轮询，客户端超时不代表协议错误。
            # 调用方根据空响应继续下一轮即可，避免日志里反复出现正常 timeout 堆栈。
            if endpoint.endswith("getupdates"):
                return {"ret": 0, "msgs": []}
            if endpoint.endswith("get_qrcode_status"):
                return {"status": "wait"}
            raise
        text = response.text
        if response.status_code >= 400:
            raise RuntimeError(f"{method.upper()} {endpoint} failed: {response.status_code} {text[:500]}")
        if not text:
            return {}
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"{method.upper()} {endpoint} returned non-json: {text[:500]}") from exc

    def start_login(self) -> Dict[str, Any]:
        """发起扫码登录。

        openclaw-weixin 的实现会把本地已有 token 通过 ``local_token_list`` 上送。
        服务端据此可能直接返回 ``binded_redirect``，避免同一个微信号重复绑定。
        cb-agent 这里也保持同样的请求形状；没有历史 token 时列表为空。
        """

        return self.request_json(
            "POST",
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": self.config.bot_type},
            payload={"local_token_list": self._local_token_list()},
            timeout_ms=self.config.api_timeout_ms,
        )

    def poll_login(self, qrcode: str, verify_code: str = "") -> Dict[str, Any]:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        return self.request_json(
            "GET",
            "ilink/bot/get_qrcode_status",
            params=params,
            timeout_ms=self.config.long_poll_timeout_ms,
            headers={"iLink-App-ClientVersion": "1"},
        )

    def get_updates(self, sync_buf: str) -> Dict[str, Any]:
        return self.request_json(
            "POST",
            "ilink/bot/getupdates",
            payload={
                "get_updates_buf": sync_buf or "",
                "base_info": self._base_info(),
            },
            token_required=True,
            timeout_ms=self.config.long_poll_timeout_ms,
        )

    def send_message(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(body)
        payload["base_info"] = self._base_info()
        return self.request_json(
            "POST",
            "ilink/bot/sendmessage",
            payload=payload,
            token_required=True,
            timeout_ms=self.config.api_timeout_ms,
        )

    def get_config(self, *, user_id: str, context_token: str = "") -> Dict[str, Any]:
        return self.request_json(
            "POST",
            "ilink/bot/getconfig",
            payload={
                "ilink_user_id": user_id,
                "context_token": context_token or None,
                "base_info": self._base_info(),
            },
            token_required=True,
            timeout_ms=self.config.api_timeout_ms,
        )

    def send_typing(self, *, user_id: str, typing_ticket: str, cancel: bool = False) -> Dict[str, Any]:
        return self.request_json(
            "POST",
            "ilink/bot/sendtyping",
            payload={
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": 2 if cancel else 1,
                "base_info": self._base_info(),
            },
            token_required=True,
            timeout_ms=self.config.api_timeout_ms,
        )

    def prepare_media_item(
        self,
        *,
        to_user_id: str,
        file_path: str,
        upload_media_type: int,
        item_type: int,
        file_name: str = "",
    ) -> Dict[str, Any]:
        """上传本地文件到微信 CDN，并返回 sendmessage item。"""

        path = Path(file_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        raw_bytes = path.read_bytes()
        raw_size = len(raw_bytes)
        raw_md5 = hashlib.md5(raw_bytes).hexdigest()
        file_key = uuid.uuid4().hex
        aes_key_hex = os.urandom(16).hex()
        ciphertext_size = aes_padded_size(raw_size)

        upload_resp = self.request_json(
            "POST",
            "ilink/bot/getuploadurl",
            payload={
                "filekey": file_key,
                "media_type": upload_media_type,
                "to_user_id": to_user_id,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": ciphertext_size,
                "no_need_thumb": True,
                "aeskey": aes_key_hex,
                "base_info": self._base_info(),
            },
            token_required=True,
            timeout_ms=self.config.api_timeout_ms,
        )
        upload_param = str(upload_resp.get("upload_param") or "").strip()
        upload_full_url = str(upload_resp.get("upload_full_url") or "").strip()
        if not upload_param and not upload_full_url:
            raise RuntimeError(f"getuploadurl 未返回 upload_param/upload_full_url: {upload_resp}")

        encrypted_query_param = self.upload_to_cdn(
            data=raw_bytes,
            upload_full_url=upload_full_url,
            upload_param=upload_param,
            file_key=file_key,
            aes_key_hex=aes_key_hex,
        )
        aes_key_b64 = base64.b64encode(aes_key_hex.encode("utf-8")).decode("utf-8")
        media = {
            "encrypt_query_param": encrypted_query_param,
            "aes_key": aes_key_b64,
            "encrypt_type": 1,
        }
        if item_type == 2:
            return {"type": 2, "image_item": {"media": media, "mid_size": ciphertext_size}}
        if item_type == 5:
            return {"type": 5, "video_item": {"media": media, "video_size": ciphertext_size}}
        return {
            "type": 4,
            "file_item": {
                "media": media,
                "file_name": file_name or path.name,
                "len": str(raw_size),
            },
        }

    def upload_to_cdn(
        self,
        *,
        data: bytes,
        upload_full_url: str,
        upload_param: str,
        file_key: str,
        aes_key_hex: str,
    ) -> str:
        encrypted = encrypt_aes_ecb(data, aes_key_hex)
        if upload_full_url:
            url = upload_full_url
        else:
            url = (
                f"{self.cdn_base_url.rstrip('/')}/upload?"
                f"encrypted_query_param={quote(upload_param)}&filekey={quote(file_key)}"
            )
        response = requests.post(
            url,
            data=encrypted,
            headers={"Content-Type": "application/octet-stream"},
            timeout=self.config.api_timeout_ms / 1000,
        )
        detail = response.text
        if response.status_code != 200:
            raise RuntimeError(f"upload media to cdn failed: {response.status_code} {detail[:500]}")
        encrypted_param = response.headers.get("x-encrypted-param")
        if not encrypted_param:
            raise RuntimeError("upload media to cdn failed: missing x-encrypted-param")
        return encrypted_param

    def download_media(self, *, encrypted_query_param: str, aes_key_value: str = "", full_url: str = "") -> bytes:
        if full_url:
            url = full_url
        else:
            url = (
                f"{self.cdn_base_url.rstrip('/')}/download?"
                f"encrypted_query_param={quote(encrypted_query_param)}"
            )
        response = requests.get(url, timeout=self.config.api_timeout_ms / 1000)
        if response.status_code >= 400:
            raise RuntimeError(f"download media from cdn failed: {response.status_code} {response.text[:500]}")
        data = response.content
        if aes_key_value:
            return decrypt_aes_ecb(data, aes_key_value)
        return data

    def _headers(self, *, token_required: bool) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("utf-8"),
        }
        if token_required and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _local_token_list(self) -> list[str]:
        """返回扫码请求可携带的本地 token 列表。

        OC 服务端只需要最近的 bot_token 即可做“已绑定”判断。这里不额外维护历史
        token 池，避免把旧凭据长期扩散到更多文件；当前配置或状态里有 token 时才上送。
        """

        token = str(self.token or self.config.token or "").strip()
        return [token] if token else []

    @staticmethod
    def _base_info() -> Dict[str, str]:
        return {"channel_version": "cb-agent", "bot_agent": "cb-agent/0.1.0"}


def aes_padded_size(size: int) -> int:
    return size + (16 - (size % 16) or 16)


def encrypt_aes_ecb(data: bytes, aes_key_hex: str) -> bytes:
    try:
        from Crypto.Cipher import AES
    except Exception as exc:
        raise RuntimeError("微信媒体上传需要安装 pycryptodome：pip install pycryptodome") from exc
    key = bytes.fromhex(aes_key_hex)
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(_pkcs7_pad(data))


def decrypt_aes_ecb(data: bytes, aes_key_value: str) -> bytes:
    try:
        from Crypto.Cipher import AES
    except Exception as exc:
        raise RuntimeError("微信媒体下载需要安装 pycryptodome：pip install pycryptodome") from exc
    key = parse_media_aes_key(aes_key_value)
    cipher = AES.new(key, AES.MODE_ECB)
    return _pkcs7_unpad(cipher.decrypt(data))


def parse_media_aes_key(value: str) -> bytes:
    """兼容 raw 16 bytes base64、hex 字符串 base64、以及明文 hex 字符串。"""

    clean = str(value or "").strip()
    if not clean:
        raise ValueError("empty aes key")
    if len(clean) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in clean):
        return bytes.fromhex(clean)
    padded = clean + "=" * (-len(clean) % 4)
    decoded = base64.b64decode(padded)
    if len(decoded) == 16:
        return decoded
    text = decoded.decode("ascii", errors="ignore")
    if len(text) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return bytes.fromhex(text)
    raise ValueError("unsupported aes key format")


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    if pad_len == 0:
        pad_len = block_size
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len <= 0 or pad_len > block_size:
        return data
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return data
    return data[:-pad_len]


def _drop_none(value: Any) -> Any:
    """递归移除请求体中的 None 字段。

    openclaw-weixin 的 TypeScript 实现会把未设置字段序列化为 ``undefined``，
    最终不会出现在 JSON 中。Python 如果直接传 ``None`` 会变成 JSON null，
    个别 OC 接口会把 null 和缺省字段当作不同含义，因此统一在 HTTP 边界清理。
    """

    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value


__all__ = [
    "WeChatOCClient",
    "aes_padded_size",
    "decrypt_aes_ecb",
    "encrypt_aes_ecb",
    "parse_media_aes_key",
]

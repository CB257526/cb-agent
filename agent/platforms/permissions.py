"""通讯平台工具权限策略。

这一层专门处理“多人 IM 入口是谁在让 agent 调工具”的问题。TUI/CLI 是本机交互，
继续沿用原来的 Bash 权限弹窗和文件工具保护；QQ/NapCat 这类入口可能面对群友或
普通好友，因此必须在工具真正执行前做一层硬拦截，避免他们通过模型间接执行写文件、
回滚代码、发送项目文件等敏感操作。

微信 OC 接入比较特殊：openclaw-weixin 是在当前账号里创建一个私聊 bot，并不是把
一个独立机器人账号暴露给多人使用。当前实现把微信视为“账号持有人自用入口”，不再
按 root/普通用户分级拦截；工具自身的参数校验、Bash 权限机制和显式
``--dangerously-skip-permissions`` 语义仍保持原样。
"""

from __future__ import annotations

import json
import os
import tempfile
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set
from urllib.parse import urlparse

from agent.platforms.context import (
    get_current_platform_conversation,
    get_current_platform_sender,
)
from agent.platforms.messages import ConversationKey


# 明确只读的 MCP 操作。未展开的通用 mcp 工具只有这些 action 对普通通讯用户开放。
READ_ONLY_MCP_ACTIONS = {
    "list_tools",
    "list_resources",
    "read_resource",
    "list_prompts",
    "get_prompt",
}

# 这些命令虽然“只读”，但会把本地文件正文或代码片段带回通讯软件。QQ/NapCat 入口面向
# 多人远程用户，读取项目文件内容本身就属于敏感信息外发，必须只允许 root 用户触发。
LOCAL_FILE_DISCLOSURE_PREFIXES = {
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "nl",
    "tac",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "ack",
    "sed",
    "awk",
    "get-content",
    "gc",
    "type",
    "select-string",
    "sls",
    "git diff",
    "git show",
    "git blame",
}

# 当前项目默认配置里的查询型 MCP。它们主要读取网络信息，不修改本地项目或远端状态，
# 普通通讯用户可以使用。涉及账号/浏览器状态的 MCP 放在敏感前缀里。
DEFAULT_PUBLIC_MCP_PREFIXES = {
    "amap-maps_",
    "fetch_",
    "tavily_",
}

DEFAULT_SENSITIVE_MCP_PREFIXES = {
    "github_",
    "playwright_",
}


@dataclass(frozen=True)
class PermissionDecision:
    """一次通讯平台工具权限判断结果。"""

    allowed: bool
    sensitive: bool
    reason: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


def check_platform_tool_permission(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    conversation: Optional[ConversationKey] = None,
    sender_id: Optional[str] = None,
) -> PermissionDecision:
    """判断当前通讯平台用户是否允许执行这次工具调用。

    ``conversation`` / ``sender_id`` 默认来自 ContextVar，通讯平台适配器在每次 inbound
    运行前都会绑定它们；后续新增平台只要也设置这两个上下文，就能复用本策略。
    没有通讯平台上下文时认为是本地 CLI/TUI，不额外拦截。
    """

    conversation = conversation if conversation is not None else get_current_platform_conversation()
    sender_id = sender_id if sender_id is not None else get_current_platform_sender()
    if conversation is None:
        return PermissionDecision(allowed=True, sensitive=False)

    # 微信 OC bot 是当前账号里的私聊入口，真实使用者就是账号持有人；它不像 QQ/NapCat
    # 群聊那样需要区分 root 和普通群友。这里仅跳过“通讯平台 root 门禁”，不改变工具
    # 自身的校验、Bash 权限弹窗或 --dangerously-skip-permissions 的显式语义。
    if conversation.platform.strip().lower() == "wechat":
        return PermissionDecision(allowed=True, sensitive=False)

    sensitive_reason = sensitive_tool_reason(tool_name, arguments)
    if not sensitive_reason:
        return PermissionDecision(allowed=True, sensitive=False)

    if is_platform_root_user(conversation.platform, sender_id):
        return PermissionDecision(allowed=True, sensitive=True, reason=sensitive_reason)

    if sender_id:
        reason = (
            f"通讯平台用户 {sender_id} 不是 root 用户，不能执行敏感操作："
            f"{sensitive_reason}"
        )
    else:
        reason = f"通讯平台消息缺少发送者身份，不能执行敏感操作：{sensitive_reason}"
    return PermissionDecision(allowed=False, sensitive=True, reason=reason)


def sensitive_tool_reason(tool_name: str, arguments: Dict[str, Any]) -> str:
    """返回工具调用的敏感原因；空字符串表示普通通讯用户也可执行。

    判断规则偏保守：本地读文件、搜索、todo、表情包名称发送等常见聊天能力放行；凡是
    会修改磁盘/记忆/知识库/权限，或可能外发任意本地文件的能力，都要求多人 IM 平台
    的 root 用户。微信 OC 自用入口会在 ``check_platform_tool_permission`` 提前放行。
    """

    name = (tool_name or "").strip()
    args = arguments or {}

    if name == "file_read":
        return "file_read 会读取并外发本地文件内容"

    if name in {"file_write", "file_edit"}:
        if _is_tool_path_under_safe_output_root(args.get("path"), mode="bash_cwd"):
            return ""
        return f"{name} 会修改本地文件"

    if name == "grep":
        mode = str(args.get("output_mode") or "files_with_matches").strip().lower()
        if mode == "content":
            return "grep(output_mode=content) 会外发本地文件匹配内容"
        return ""

    if name == "run_skill_script":
        return "run_skill_script 会执行本地 skill 脚本"

    if name == "bash":
        return _sensitive_bash_reason(args)

    if name == "bash_task":
        action = str(args.get("action") or "").strip().lower()
        if action in {"output", "wait"}:
            return f"bash_task(action={action}) 可能读取后台任务输出"
        if action == "kill":
            return "bash_task(action=kill) 会终止后台任务"
        return ""

    if name == "bash_permission":
        action = str(args.get("action") or "").strip().lower()
        if action in {"grant", "revoke"}:
            return f"bash_permission(action={action}) 会修改命令授权规则"
        return ""

    if name == "send_message_asset":
        return _sensitive_asset_reason(args)

    if name == "qqtool":
        return _sensitive_qqtool_reason(args)

    if name == "wechattool":
        return _sensitive_wechattool_reason(args)

    if name == "memory":
        action = str(args.get("action") or "").strip().lower()
        if action not in {"search", "summary", "stats", "list"}:
            return f"memory(action={action or '<空>'}) 会修改长期记忆"
        return ""

    if name == "rag":
        action = str(args.get("action") or "").strip().lower()
        if action not in {"ask", "search", "search_images", "search_audio", "stats"}:
            return f"rag(action={action or '<空>'}) 会修改知识库"
        return ""

    if _looks_like_mcp_tool(name):
        return _sensitive_mcp_reason(name, args)

    return ""


def is_platform_root_user(platform: str, sender_id: Optional[str]) -> bool:
    """检查某个平台用户是否在 root 名单中。

    通用 ``IM_ROOT_USERS`` 适合 QQ/后续 Telegram 等多人平台共用；``QQ_ROOT_USERS``
    用于 QQ 单独配置。只要任一名单命中就视为 root。未配置 root 时，多人平台敏感工具
    默认拒绝，这比“忘配就全放行”安全。
    """

    sender = str(sender_id or "").strip()
    if not sender:
        return False
    root_users = set(_csv_set(os.getenv("IM_ROOT_USERS")))
    if platform.strip().lower() == "qq":
        root_users.update(_csv_set(os.getenv("QQ_ROOT_USERS")))
    return sender in root_users


def permission_denied_payload(
    tool_name: str,
    arguments: Dict[str, Any],
    decision: PermissionDecision,
) -> str:
    """生成给模型看的结构化拒绝结果。

    工具没有真正执行，但仍要按 tool calling 协议回灌一条 tool 消息；结构化 JSON 能让
    模型稳定识别这是权限拒绝，而不是普通工具异常。
    """

    payload = {
        "permission_denied": True,
        "error": decision.reason or "通讯平台用户无权执行该敏感工具",
        "tool": tool_name,
        "sensitive": decision.sensitive,
        "hint": "请让 .env 中为当前多人通讯平台配置的 root 用户重新发起该操作。",
    }
    if tool_name == "bash":
        payload["command"] = str((arguments or {}).get("command") or "")[:500]
    return json.dumps(payload, ensure_ascii=False)


def _sensitive_bash_reason(arguments: Dict[str, Any]) -> str:
    command = str(arguments.get("command") or "").strip()
    if not command:
        return "bash 空命令无法判断安全性"
    try:
        from tools.tools.bash_permission import is_read_only
        from tools.tools.bash_permission import extract_prefix
        from tools.tools.bash_security import parse_pipeline

        segments = parse_pipeline(command)
        for argv in segments:
            prefix = extract_prefix(argv).lower()
            if prefix in LOCAL_FILE_DISCLOSURE_PREFIXES:
                return f"bash 命令 {prefix} 会读取并外发本地文件内容"
        if segments and all(is_read_only(argv) for argv in segments):
            return ""
        if _is_safe_temp_download_command(segments):
            return ""
    except Exception:
        # 解析失败时按敏感处理，避免复杂 shell 语法绕过。
        return "bash 命令解析失败，无法确认是只读操作"
    return "bash 命令不是只读白名单操作"


def _sensitive_asset_reason(arguments: Dict[str, Any]) -> str:
    kind = str(arguments.get("kind") or "file").strip().lower()
    path = str(arguments.get("path") or "").strip()
    if path:
        if _is_tool_path_under_safe_output_root(path, mode="process_cwd"):
            return ""
        return "send_message_asset(path=...) 可能外发任意本地文件"
    if kind in {"file", "image", "audio", "video"}:
        return f"send_message_asset(kind={kind}) 会发送本地资源文件"
    # sticker_name 只会从表情包目录查找，保留给普通聊天使用。
    return ""


def _sensitive_qqtool_reason(arguments: Dict[str, Any]) -> str:
    """判断 qqtool 子功能是否需要通讯平台 root 权限。"""

    funname = str(arguments.get("funname") or "").strip()
    raw_args = arguments.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    try:
        from tools.tools.qq.registry import get_qq_function_spec
    except Exception:
        return "qqtool 子功能注册表加载失败，无法确认安全性"

    spec = get_qq_function_spec(funname)
    if spec is None:
        return f"qqtool(funname={funname or '<空>'}) 是未知 QQ 操作"
    if spec.root_only:
        return f"qqtool(funname={spec.funname}) 属于 root-only QQ 操作"

    if spec.current_conversation_only:
        current = get_current_platform_conversation()
        if current is None:
            return ""
        mismatch = _qqtool_current_conversation_mismatch(spec.funname, args, current)
        if mismatch:
            return mismatch

    if spec.file_param:
        file_path = args.get(spec.file_param)
        if not file_path:
            return f"qqtool(funname={spec.funname}) 缺少文件路径，无法确认安全性"
        if _is_external_resource_ref(file_path):
            return ""
        if _is_tool_path_under_safe_output_root(file_path, mode="process_cwd"):
            return ""
        if _is_sticker_dir_path(file_path):
            return ""
        return f"qqtool(funname={spec.funname}) 可能外发任意本地文件"

    return ""


def _sensitive_wechattool_reason(arguments: Dict[str, Any]) -> str:
    """微信 OC 是当前账号自用入口，平台权限层不再给 wechattool 分级。

    QQ/NapCat 暴露给群友和好友，需要 root/普通用户门禁；openclaw-weixin 的微信 OC
    bot 只存在于当前账号私聊里，使用者就是账号持有人。这里恒定返回空字符串，具体
    参数合法性和网络/API 错误交给 wechattool 与 adapter 自己处理。
    """
    return ""


def _qqtool_current_conversation_mismatch(
    funname: str,
    args: Dict[str, Any],
    conversation: ConversationKey,
) -> str:
    """普通用户只能让 qqtool 操作触发本轮的当前会话。"""

    kind = str(conversation.kind).lower()
    conv_id = str(conversation.id)
    group_id = str(args.get("group_id") or "").strip()
    user_id = str(args.get("user_id") or "").strip()

    if funname in {
        "send_group_msg",
        "upload_group_file",
        "upload_image_to_qun_album",
        "get_group_info",
        "get_group_info_ex",
        "get_group_member_list",
        "get_group_member_info",
        "send_group_sign",
        "get_group_signed_list",
        "get_qun_album_list",
        "get_group_album_media_list",
        "get_group_at_all_remain",
        "set_group_todo",
        "complete_group_todo",
        "cancel_group_todo",
    }:
        if kind != "group" or group_id != conv_id:
            return f"qqtool(funname={funname}) 试图操作非当前群聊"

    if funname in {"send_private_msg", "upload_private_file"}:
        if kind != "private" or user_id != conv_id:
            return f"qqtool(funname={funname}) 试图操作非当前私聊"

    if funname in {"send_like", "send_poke"}:
        # 私聊只能戳/赞当前好友；群聊里 user_id 是群成员，允许对当前群内用户操作。
        if kind == "private" and user_id != conv_id:
            return f"qqtool(funname={funname}) 试图操作非当前好友"
        if kind == "group" and group_id != conv_id:
            return f"qqtool(funname={funname}) 试图操作非当前群聊"

    return ""


def _is_external_resource_ref(raw: Any) -> bool:
    text = str(raw or "").strip().lower()
    # file:// 仍然是本地文件语义，普通通讯用户不能用它绕过任意本地文件外发检查。
    return text.startswith(("http://", "https://", "base64://", "data:"))


def _is_sticker_dir_path(raw: Any) -> bool:
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return False
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved = path.resolve(strict=False)
        sticker_root = Path(os.getenv("CBAGENT_STICKER_DIR") or "assets/stickers").expanduser()
        if not sticker_root.is_absolute():
            sticker_root = Path.cwd() / sticker_root
        return _is_relative_to(resolved, sticker_root.resolve(strict=False))
    except OSError:
        return False


def _is_safe_temp_download_command(segments: list[list[str]]) -> bool:
    """只放行“从 http(s) 下载到临时目录”的窄 bash 场景。

    普通通讯用户常见需求是“下载一张图/生成一个 PDF 后发回来”。我们不整体放开
    bash 写操作，只允许 curl/wget 明确把网络资源写入系统临时目录；本地复制、
    file:// URL、管道到 shell、未指定输出路径等都继续走敏感工具拒绝。
    """

    if len(segments) != 1:
        return False
    argv = segments[0]
    if not argv:
        return False
    from tools.tools.bash_permission import extract_prefix

    prefix = extract_prefix(argv).lower()
    if prefix == "curl":
        parsed = _parse_curl_download(argv[1:])
    elif prefix == "wget":
        parsed = _parse_wget_download(argv[1:])
    else:
        return False
    if parsed is None:
        return False
    url, output_path = parsed
    if not _is_http_url(url):
        return False
    return _is_tool_path_under_safe_output_root(output_path, mode="bash_cwd")


def _parse_curl_download(args: list[str]) -> Optional[tuple[str, str]]:
    """解析安全子集: curl [少量无害选项] -o OUT URL。"""

    safe_flags = {
        "-s",
        "-S",
        "-sS",
        "-f",
        "--fail",
        "--compressed",
    }
    valued_flags = {
        "--retry",
        "--connect-timeout",
        "--max-time",
        "-A",
        "--user-agent",
    }
    output = ""
    urls: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in safe_flags:
            i += 1
            continue
        if token in valued_flags:
            i += 2
            continue
        if token in {"-o", "--output"}:
            if i + 1 >= len(args):
                return None
            output = args[i + 1]
            i += 2
            continue
        if token.startswith("--output="):
            output = token.split("=", 1)[1]
            i += 1
            continue
        if token.startswith("-o") and len(token) > 2:
            output = token[2:]
            i += 1
            continue
        if token.startswith("-"):
            return None
        urls.append(token)
        i += 1
    if len(urls) != 1 or not output:
        return None
    return urls[0], output


def _parse_wget_download(args: list[str]) -> Optional[tuple[str, str]]:
    """解析安全子集: wget [少量无害选项] -O OUT URL。"""

    safe_flags = {
        "-q",
        "--quiet",
        "--show-progress",
        "--no-verbose",
        "--https-only",
    }
    valued_flags = {
        "--timeout",
        "--tries",
        "--user-agent",
    }
    output = ""
    urls: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in safe_flags:
            i += 1
            continue
        if token in valued_flags:
            i += 2
            continue
        if token in {"-O", "--output-document"}:
            if i + 1 >= len(args):
                return None
            output = args[i + 1]
            i += 2
            continue
        if token.startswith("--output-document="):
            output = token.split("=", 1)[1]
            i += 1
            continue
        if token.startswith("-O") and len(token) > 2:
            output = token[2:]
            i += 1
            continue
        if token.startswith("-"):
            return None
        urls.append(token)
        i += 1
    if len(urls) != 1 or not output:
        return None
    return urls[0], output


def _is_http_url(raw: str) -> bool:
    parsed = urlparse(str(raw or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


def _is_tool_path_under_safe_output_root(raw_path: Any, *, mode: str) -> bool:
    """判断工具路径最终真实位置是否位于系统临时目录。

    ``Path.resolve`` 会展开已存在的软链接父目录，因此 ``/tmp/link -> 项目目录`` 这类
    绕过不会被误判为安全。新文件只要父目录在临时目录内，也允许创建。
    """

    path_text = str(raw_path or "").strip().strip('"').strip("'")
    if not path_text:
        return False
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        if mode == "bash_cwd":
            try:
                from tools.tools.bash_session import get_session

                path = Path(get_session().cwd) / path
            except Exception:
                path = Path.cwd() / path
        else:
            path = Path.cwd() / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    return any(_is_relative_to(resolved, root) for root in _safe_output_roots())


def _safe_output_roots() -> Set[Path]:
    roots = {Path(tempfile.gettempdir()).resolve()}
    if os.name != "nt":
        roots.add(Path("/tmp").resolve())
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sensitive_mcp_reason(tool_name: str, arguments: Dict[str, Any]) -> str:
    if _has_prefix(tool_name, _public_mcp_prefixes()):
        return ""
    if _has_prefix(tool_name, _sensitive_mcp_prefixes()):
        return f"{tool_name} 是可能修改远端状态或执行网页动作的 MCP 工具"

    action = str(arguments.get("action") or "").strip().lower()
    if action in READ_ONLY_MCP_ACTIONS:
        return ""
    if tool_name == "mcp" and action:
        return f"mcp(action={action}) 可能调用外部服务写操作"
    return f"{tool_name} 是 MCP/外部工具，无法确认只读性"


def _looks_like_mcp_tool(tool_name: str) -> bool:
    if tool_name == "mcp":
        return True
    prefixes = _public_mcp_prefixes() | _sensitive_mcp_prefixes()
    return _has_prefix(tool_name, prefixes)


def _public_mcp_prefixes() -> Set[str]:
    prefixes = set(DEFAULT_PUBLIC_MCP_PREFIXES)
    prefixes.update(_csv_set(os.getenv("CBAGENT_MCP_PUBLIC_PREFIXES")))
    return prefixes


def _sensitive_mcp_prefixes() -> Set[str]:
    prefixes = set(DEFAULT_SENSITIVE_MCP_PREFIXES)
    prefixes.update(_csv_set(os.getenv("CBAGENT_MCP_SENSITIVE_PREFIXES")))
    return prefixes


def _has_prefix(value: str, prefixes: Iterable[str]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes if prefix)


def _csv_set(raw: Optional[str]) -> Set[str]:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


__all__ = [
    "PermissionDecision",
    "check_platform_tool_permission",
    "is_platform_root_user",
    "permission_denied_payload",
    "sensitive_tool_reason",
]

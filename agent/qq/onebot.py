"""OneBot V11 消息解析与发送消息段转换。

本模块是 QQ/NapCat 适配器的协议翻译层。它只做纯数据转换，不涉及网络请求。
OneBot V11 规范定义了三种 post_type：message（消息）、notice（通知）、request（请求）。
这里只处理 message 事件，因为只有用户发消息时才需要触发 Agent 回复。
"""

# 允许在类型标注中使用未完全导入的类名（python 3.10+ 特性）
from __future__ import annotations

import html      # 用于反转义 CQ 码中的 HTML 实体（如 &amp; &lt; &gt;）
import re        # 正则匹配 CQ 码和清理纯文本中的 CQ 标记
from pathlib import Path                # 处理本地文件路径 → file:// URI 转换
from typing import Any, Dict, List, Optional, Tuple  # 类型标注
from urllib.parse import quote, urlparse  # quote: URL 编码路径字符；urlparse: 从 URL 中提取文件名

# 平台无关的消息数据结构
from agent.platforms.messages import ConversationKey, InboundAttachment, InboundMessage, OutboundSegment
# QQ 适配器配置（白名单、群聊唤醒模式、前缀等）
from agent.qq.config import QQConfig
# 判断路径是否为外部引用（base64:// / http:// 等）或 POSIX 绝对路径（Docker 内使用）
from agent.qq.file_delivery import is_external_file_reference, looks_like_posix_absolute_path

# ─── 常量 ────────────────────────────────────────────────────────────────

# 预编译 CQ 码正则。CQ 码格式：[CQ:类型名,key1=val1,key2=val2,...]
# group(1) = 类型名（如 image、at、record），group(2) = 参数部分（含前导逗号）
_CQ_CODE_RE = re.compile(r"\[CQ:([A-Za-z0-9_]+)((?:,[^\]]*)?)\]")


# ═══════════════════════════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════════════════════════

def parse_onebot_message_event(
    event: Dict[str, Any],     # OneBot V11 的原始 JSON 事件字典
    config: QQConfig,          # QQ 适配器配置（白名单、唤醒模式等）
    *,
    require_wakeup: bool = True,  # 群聊是否需要 @机器人 或前缀唤醒；私聊忽略此参数
) -> Optional[InboundMessage]:    # 返回 None 表示不需触发 Agent
    """把 OneBot V11 message 事件转换为平台无关入站消息。

    返回 None 表示该事件不需要触发 Agent，例如群聊未唤醒、白名单不匹配、非消息事件。
    这个函数保持纯函数风格，不调用任何 NapCat action（网络请求留给 adapter 补全）。
    """

    # ── 第1步：过滤非消息事件 ──
    # OneBot 事件有 message / notice / request 三种，这里只处理 message
    if event.get("post_type") != "message":
        return None

    # 消息类型必须是 private（私聊）或 group（群聊），其他如 discuss（讨论组）暂不处理
    message_type = str(event.get("message_type") or "")
    if message_type not in {"private", "group"}:
        return None

    # ── 第2步：白名单过滤 ──
    # 从事件中提取发送者 QQ 号和群号（群聊才有 group_id）
    user_id = str(event.get("user_id") or "")
    group_id = str(event.get("group_id") or "")

    # 如果配置了用户白名单且当前用户不在列表内，跳过
    if config.allowed_users and user_id not in config.allowed_users:
        return None
    # 群聊场景还检查群白名单
    if message_type == "group" and config.allowed_groups and group_id not in config.allowed_groups:
        return None

    # ── 第3步：解析消息段 ──
    # OneBot 消息体有两种格式：数组段（list[dict]）或 CQ 码字符串（str）
    # _parse_message_segments 统一处理，返回：文本、附件列表、是否被@、引用消息ID
    # self_id 是机器人自己的 QQ 号，用于判断是否被 @
    text, attachments, mentioned, reply_to = _parse_message_segments(
        event.get("message"),
        str(event.get("self_id") or ""),
    )
    # 清理文本中残留的 CQ 码（如图片、表情等非文本段），只保留纯文本
    text = _strip_cq_codes(text).strip()

    # ── 第4步：群聊唤醒检查 ──
    # 三种模式：all（所有群消息都触发）/ mention（需 @ 或前缀）/ prefix（仅前缀）
    if message_type == "group" and require_wakeup:
        text = _apply_group_wakeup(text=text, mentioned=mentioned, config=config)
        # 返回 None 表示未被唤醒，这条消息不触发 Agent
        if text is None:
            return None

    # ── 第5步：提取发送者信息 ──
    # sender 字段包含群名片（card）和昵称（nickname）
    # 群聊优先用群名片，私聊用昵称，都没有则回退到 QQ 号
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}

    # ── 第6步：构造会话标识 ──
    # ConversationKey 是平台无关的会话 ID
    # 群聊：platform=qq, kind=group, id=群号
    # 私聊：platform=qq, kind=private, id=发送者 QQ 号
    conversation = ConversationKey(
        platform="qq",
        kind="group" if message_type == "group" else "private",
        id=group_id if message_type == "group" else user_id,
    )

    # ── 第7步：组装 InboundMessage ──
    return InboundMessage(
        conversation=conversation,                     # 会话标识
        sender_id=user_id,                             # 发送者 QQ 号
        sender_name=str(sender.get("card") or sender.get("nickname") or user_id),  # 群名片 > 昵称 > QQ号
        text=text,                                     # 纯文本消息内容
        raw=event,                                     # 保留原始事件，供后续补全使用
        attachments=attachments,                       # 图片/文件/语音等附件
        message_id=str(event.get("message_id") or "") or None,  # OneBot 消息 ID
        reply_to_message_id=reply_to,                  # 引用回复的消息 ID（不是当前消息的ID）
    )


def parse_onebot_event(
    event: Dict[str, Any],
    config: QQConfig,
    *,
    require_wakeup: bool = True,
) -> Optional[InboundMessage]:
    """统一解析 OneBot V11 入站事件。

    只有 ``message`` 事件表示用户真的发了一句话给机器人，才允许触发 Agent。
    ``notice`` / ``request`` 是平台状态或管理事件，例如群文件上传、戳一戳、输入
    状态、好友申请等；它们没有普通对话上下文，默认静默，避免机器人自己发文件后被
    群文件上传回声再次触发。
    """

    # 按 post_type 分发：message → 进入消息解析；notice/request → 返回 None（静默忽略）
    post_type = str(event.get("post_type") or "")
    if post_type == "message":
        return parse_onebot_message_event(event, config, require_wakeup=require_wakeup)
    # notice（群文件上传、戳一戳等）和 request（好友/加群申请）不触发 Agent
    return None


def outbound_segment_to_onebot(segment: OutboundSegment) -> List[Dict[str, Any]]:
    """把平台无关出站段转换为 OneBot V11 消息段。

    普通文件在 NapCat 中也可以走消息段 file；如果实际平台不支持，适配器会根据
    action 响应再降级文本提示。
    """

    # 每个 OutboundSegment 有一个 kind 字段，标识消息类型
    kind = segment.kind

    # 文本类消息：text（普通文本）、status（状态提示）、todo（待办列表）、question（询问选项）
    # 都发为 OneBot text 段。segment.text 为空时不发，避免空白消息。
    if kind in {"text", "status", "todo", "question"}:
        return [{"type": "text", "data": {"text": segment.text}}] if segment.text else []

    # 图片/贴纸：发为 OneBot image 段，file 参数是本地文件的 file:// URI
    if kind in {"image", "sticker"}:
        return [{"type": "image", "data": {"file": _local_file_uri(segment.path)}}]

    # 音频：发为 OneBot record 段
    if kind == "audio":
        return [{"type": "record", "data": {"file": _local_file_uri(segment.path)}}]

    # 视频：发为 OneBot video 段
    if kind == "video":
        return [{"type": "video", "data": {"file": _local_file_uri(segment.path)}}]

    # 文件：发为 OneBot file 段，额外附带文件名
    if kind == "file":
        return [{"type": "file", "data": {"file": _local_file_uri(segment.path), "name": segment.file_name}}]

    # 未知类型降级为文本提示，避免 agent 发不出任何消息
    return [{"type": "text", "data": {"text": segment.text or f"[不支持的消息段: {kind}]"}}]


# ═══════════════════════════════════════════════════════════════════════════
# 内部函数：消息段解析
# ═══════════════════════════════════════════════════════════════════════════

def _parse_message_segments(message: Any, self_id: str) -> Tuple[str, List[InboundAttachment], bool, Optional[str]]:
    """解析 OneBot 消息段。

    返回值最后一个 ``reply_to`` 是引用消息 ID。这里不直接调用 ``get_msg``，因为解析器
    保持纯函数，真正需要访问 NapCat action 的补全工作交给 adapter 做。

    返回元组：
      - text:        纯文本拼接（包括 @提及 的占位文本）
      - attachments: 附件列表（图片/语音/文件等）
      - mentioned:   机器人是否被 @
      - reply_to:    引用消息的 ID（用户回复了哪条消息）
    """

    # ── 分支1：字符串格式消息 ──
    # 某些 OneBot 实现把消息发成 "[CQ:image,file=xxx.jpg]" 这样的纯文本
    if isinstance(message, str):
        return _parse_cq_string_message(message, self_id)

    # ── 分支2：既不是字符串也不是数组 ──
    # 防御性处理：把未知类型转成字符串
    if not isinstance(message, list):
        return str(message or ""), [], False, None

    # ── 分支3：数组段格式消息（标准 OneBot V11 格式）──
    # 每个元素是一个 {"type": "...", "data": {...}} 的 dict
    texts: List[str] = []                     # 收集所有文本片段，最后 join
    attachments: List[InboundAttachment] = []  # 收集所有附件
    mentioned = False                          # 标记：机器人是否被 @
    reply_to: Optional[str] = None             # 引用回复的消息 ID

    for seg in message:
        # 跳过非 dict 的元素（数据容错）
        if not isinstance(seg, dict):
            continue

        # 每个段有 type（类型）和 data（参数）两个字段
        seg_type = str(seg.get("type") or "")
        # data 必须为 dict，否则置空避免后续访问出错
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}

        # --- text：纯文本段 ---
        if seg_type == "text":
            texts.append(str(data.get("text") or ""))

        # --- at：@提及 ---
        elif seg_type == "at":
            qq = str(data.get("qq") or "")     # 被 @ 的 QQ 号
            if self_id and qq == self_id:       # 判断被 @ 的是不是机器人自己
                mentioned = True
            texts.append(f"@{qq} ")             # 在文本中保留 @某人 的占位

        # --- image：图片段 ---
        elif seg_type == "image":
            url = str(data.get("url") or "")
            # 从 URL 路径提取文件名，没有则用 "image"
            file_name = str(data.get("file") or Path(urlparse(url).path).name or "image")
            attachments.append(InboundAttachment(
                modality="image",                                                       # 附件类型
                url=url or None,                                                        # 图片 URL（可能为空，后续由 adapter 补全）
                file_name=file_name,                                                    # 文件名
                description=f"QQ 图片 {file_name}" + (f" URL={url}" if url else ""),   # 人类可读描述
                metadata=dict(data),                                                    # 保留原始 data 元信息
            ))

        # --- record / audio：语音段 ---
        elif seg_type in {"record", "audio"}:
            file_name = str(data.get("file") or "audio")
            attachments.append(InboundAttachment(
                modality="audio",
                url=str(data.get("url") or "") or None,
                file_name=file_name,
                description=f"QQ 音频 {file_name}",
                metadata=dict(data),
            ))

        # --- file：文件段 ---
        elif seg_type == "file":
            # 文件名可能有多个来源：name > file_name > file > 默认 "file"
            file_name = str(data.get("name") or data.get("file_name") or data.get("file") or "file")
            # file_id 用于后续通过 NapCat action 获取下载 URL
            file_id = str(data.get("file_id") or data.get("id") or "") or None
            url = str(data.get("url") or "") or None
            attachments.append(InboundAttachment(
                modality="file",
                url=url,
                file_id=file_id,                             # NapCat 文件 ID，用于 action 查询
                file_name=file_name,
                description=f"QQ 文件 {file_name}" + (f" URL={url}" if url else ""),
                metadata=dict(data),
            ))

        # --- reply：引用回复段 ---
        elif seg_type == "reply":
            # 提取被引用的消息 ID。不能覆盖已有值（一条消息只能引用一条）
            reply_to = str(data.get("id") or data.get("message_id") or "") or reply_to
            if reply_to:
                texts.append(f"[引用消息 {reply_to}] ")      # 在文本中标记引用

    # join 所有文本片段并去除首尾空白
    return "".join(texts).strip(), attachments, mentioned, reply_to


# ═══════════════════════════════════════════════════════════════════════════
# 内部函数：CQ 码字符串解析
# ═══════════════════════════════════════════════════════════════════════════

def _parse_cq_string_message(message: str, self_id: str) -> Tuple[str, List[InboundAttachment], bool, Optional[str]]:
    """解析 OneBot 字符串消息格式。

    NapCat/OneBot 可以把消息发成数组段，也可以发成 ``[CQ:image,...]`` 字符串。
    后者如果只做正则清理，会丢掉图片、语音等附件；这里把常见 CQ 码恢复成和数组段
    相同的 InboundAttachment，保证不同 message_format 下后端行为一致。

    返回元组与 _parse_message_segments 完全一致。
    """

    texts: List[str] = []                     # 收集 CQ 码之间的普通文本
    attachments: List[InboundAttachment] = []  # 收集从 CQ 码提取的附件
    mentioned = False                          # 机器人是否被 @
    reply_to: Optional[str] = None             # 引用消息 ID
    last = 0                                   # 上一个 CQ 码结束位置，用于截取中间的纯文本

    # 用预编译的正则扫描整个字符串，每找到一个 CQ 码就处理一次
    for match in _CQ_CODE_RE.finditer(message):
        # ── 提取 CQ 码前面的纯文本 ──
        # match.start() 是当前 CQ 码的起始位置
        # last 是上一个 CQ 码的结束位置
        # 中间这段就是普通文本，需要保留
        if match.start() > last:
            texts.append(_unescape_cq_value(message[last:match.start()]))

        # 获取 CQ 码类型，统一小写（IMAGE → image）
        seg_type = match.group(1).lower()
        # 解析参数部分：去掉前导逗号后按 key=value 拆分
        data = _parse_cq_params(match.group(2))

        # --- at 码：[CQ:at,qq=123456] ---
        if seg_type == "at":
            qq = str(data.get("qq") or "")
            if self_id and qq == self_id:
                mentioned = True
            texts.append(f"@{qq} ")

        # --- image 码：[CQ:image,file=xxx.jpg,url=...] ---
        elif seg_type == "image":
            url = str(data.get("url") or "")
            file_name = str(data.get("file") or Path(urlparse(url).path).name or "image")
            attachments.append(InboundAttachment(
                modality="image",
                url=url or None,
                file_name=file_name,
                description=f"QQ 图片 {file_name}" + (f" URL={url}" if url else ""),
                metadata=dict(data),
            ))

        # --- record / audio 码：[CQ:record,file=xxx.amr] ---
        elif seg_type in {"record", "audio"}:
            url = str(data.get("url") or "")
            file_name = str(data.get("file") or Path(urlparse(url).path).name or "audio")
            attachments.append(InboundAttachment(
                modality="audio",
                url=url or None,
                file_name=file_name,
                description=f"QQ 音频 {file_name}",
                metadata=dict(data),
            ))

        # --- file 码：[CQ:file,name=xxx.pdf] ---
        elif seg_type == "file":
            url = str(data.get("url") or "")
            file_name = str(data.get("name") or data.get("file_name") or data.get("file") or Path(urlparse(url).path).name or "file")
            file_id = str(data.get("file_id") or data.get("id") or "") or None
            attachments.append(InboundAttachment(
                modality="file",
                url=url or None,
                file_id=file_id,
                file_name=file_name,
                description=f"QQ 文件 {file_name}" + (f" URL={url}" if url else ""),
                metadata=dict(data),
            ))

        # --- reply 码：[CQ:reply,id=123456] ---
        elif seg_type == "reply":
            reply_to = str(data.get("id") or data.get("message_id") or "") or reply_to
            if reply_to:
                texts.append(f"[引用消息 {reply_to}] ")

        # 更新扫描位置到当前 CQ 码之后
        last = match.end()

    # ── 处理最后一个 CQ 码之后的剩余文本 ──
    if last < len(message):
        texts.append(_unescape_cq_value(message[last:]))

    return "".join(texts).strip(), attachments, mentioned, reply_to


# ═══════════════════════════════════════════════════════════════════════════
# 内部函数：CQ 码辅助工具
# ═══════════════════════════════════════════════════════════════════════════

def _parse_cq_params(raw: str) -> Dict[str, str]:
    """解析 CQ 码参数。

    CQ 参数里的逗号、方括号、& 会被转义成 HTML 实体；先按未转义逗号切分，再统一
    html.unescape，可以覆盖 OneBot V11 的常见转义写法。

    例如: raw = ",file=hello.jpg,url=http://x.com?a=1&amp;b=2"
    返回: {"file": "hello.jpg", "url": "http://x.com?a=1&b=2"}
    """

    result: Dict[str, str] = {}

    # 去除前导逗号。CQ 码格式为 [CQ:type,key1=val1]，所以参数部分以逗号开头
    value = raw[1:] if raw.startswith(",") else raw
    # 空参数直接返回空字典
    if not value:
        return result

    # 按逗号切分，每个 item 是 "key=value" 或裸字符串
    for item in value.split(","):
        # 跳过不含 = 的项（格式错误或无需处理的参数）
        if "=" not in item:
            continue
        # 只按第一个 = 切分，因为 value 本身可能包含 =
        key, val = item.split("=", 1)
        key = key.strip()
        if key:
            # 反转义 HTML 实体：&amp; → &, &lt; → <, &#44; → , 等
            result[key] = _unescape_cq_value(val)

    return result


def _unescape_cq_value(value: str) -> str:
    """反转义 CQ 参数值中的 HTML 实体。

    OneBot V11 规范要求 CQ 码中的特殊字符（,&[]）使用 HTML 实体编码，
    这里用标准库 html.unescape 还原。
    """
    return html.unescape(value)


# ═══════════════════════════════════════════════════════════════════════════
# 内部函数：群聊唤醒控制
# ═══════════════════════════════════════════════════════════════════════════

def _apply_group_wakeup(*, text: str, mentioned: bool, config: QQConfig) -> Optional[str]:
    """群聊唤醒检查。

    群聊消息不会每条都触发 Agent，需要根据配置决定是否唤醒：
      - all:    所有群消息都触发
      - mention: 需要 @机器人 或消息以唤醒前缀开头才触发
      - prefix:  仅消息以唤醒前缀开头才触发

    返回 None 表示不唤醒，返回字符串是去掉唤醒标记后的纯文本。

    注意：三个参数都使用 keyword-only（*）防止调用顺序错误。
    """

    mode = config.group_mode        # 唤醒模式：all / mention / prefix
    prefix = config.wake_prefix     # 唤醒前缀，如 "/bot"
    clean = text.strip()            # 去除首尾空白后的纯文本

    # ── 模式1：all —— 所有群消息都触发，直接返回原文本 ──
    if mode == "all":
        return clean

    # ── 模式2：mention —— 需要 @ 或前缀 ──
    if mode == "mention":
        # 已经通过 @ 方式提到机器人，去掉文本中的 @xxx 占位后返回
        if mentioned:
            return _strip_at_tokens(clean)
        # 消息以唤醒前缀开头（如 "/bot 帮我查天气"），去掉前缀后返回
        if prefix and clean.startswith(prefix):
            return clean[len(prefix):].strip()
        # 既没 @ 也没前缀 → 不唤醒
        return None

    # ── 模式3：prefix —— 仅前缀唤醒 ──
    if mode == "prefix":
        if prefix and clean.startswith(prefix):
            return clean[len(prefix):].strip()
        return None

    # 未知模式或空配置 → 默认不唤醒（安全策略）
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 内部函数：文本清理与路径处理
# ═══════════════════════════════════════════════════════════════════════════

def _strip_cq_codes(text: str) -> str:
    """移除文本中所有 CQ 码。

    在 extract 附件后，文本里不应再残留 [CQ:image,...] 之类的标记，
    用这个函数统一清理，保证传给 LLM 的文本干净。
    """
    return re.sub(r"\[CQ:[^\]]+\]", "", text)


def _contains_cq_at_self(text: str, self_id: str) -> bool:
    """判断 CQ 码字符串消息中是否包含 @机器人自己的 at 码。

    这是字符串格式消息专用的检测函数，仅在旧格式兼容时使用。
    """
    if not self_id:
        return False
    # 同时检查 [CQ:at 标记和 qq=self_id 参数
    return f"qq={self_id}" in text and "[CQ:at" in text


def _strip_at_tokens(text: str) -> str:
    """移除文本中的 @QQ号 占位符。

    群聊中用户 @机器人后，文本中会残留 "@123456 " 这样的标记，
    需要清理掉再传给 LLM，避免干扰语义理解。
    """
    return re.sub(r"@\d+\s*", "", text).strip()


def _local_file_uri(path: str) -> str:
    """把本地文件路径转换为 NapCat 可读的 file:// URI。

    三种情况：
      1. 已经是外部引用（base64:// / http:// / https://）→ 原样返回
      2. POSIX 绝对路径（Docker 容器内 /app/outbound/xxx.png）→ 原样返回
      3. Windows 本地路径 → 转成 file:///C:/xxx 格式的 URI

    NapCat 的 image/record/video/file 段要求 file 字段为 URI 或 Docker 内路径。
    """
    # 外部引用（http:// / base64://）或 Docker 内的 POSIX 路径直接返回
    if is_external_file_reference(path) or looks_like_posix_absolute_path(path):
        return str(path)

    # 展开 ~ 并转为绝对路径
    p = Path(path).expanduser().resolve()
    try:
        # 尝试转为标准 file:// URI（Windows 上为 file:///C:/...）
        return p.as_uri()
    except ValueError:
        # as_uri() 在某些情况下可能失败（路径含非法字符等），
        # 手动拼接 file:// 前缀，并将反斜杠换为正斜杠
        return "file://" + quote(str(p).replace("\\", "/"))


# 模块公开接口列表，同时也用作文档提示
__all__ = ["parse_onebot_event", "parse_onebot_message_event", "outbound_segment_to_onebot"]

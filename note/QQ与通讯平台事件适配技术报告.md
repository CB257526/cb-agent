# QQ 与通讯平台事件适配技术报告

## 背景

本次更新把 cb-agent 从“本地 CLI/TUI 对话”扩展到“通讯软件对话”。首个落地平台是 QQ/NapCat/OneBot V11，但实现时没有把 QQ 逻辑写死到 AgentSession 内部，而是新增平台无关的消息层，为后续微信等通讯软件预留同一套入口和出口。

核心目标有三个：

- QQ 用户能通过 NapCat 反向 WebSocket 与 agent 对话；
- TUI 专用事件在 QQ 中有合理降级，不报错、不刷屏；
- agent 可以显式发送表情包、图片和本地文件。

## 架构

新增 `agent.platforms` 作为通讯平台抽象层：

- `ConversationKey`：统一标识 `platform/kind/id`，例如 `qq/group/123456`；
- `InboundMessage`：平台入站消息，包含文本、发送者、附件摘要；
- `OutboundMessage` / `OutboundSegment`：平台出站消息，支持 `text/image/audio/video/file/sticker/question/todo/status`；
- `PlatformEventRenderer`：订阅 EventBus，把 agent 内部事件转换成通讯软件可发送消息。

QQ 侧新增 `agent.qq`：

- `config.py` 从 `.env` 读取 `QQ_*`、`IM_*` 配置；
- `onebot.py` 解析 OneBot V11 message 事件，并把出站段转成 OneBot 消息段；
- `adapter.py` 启动反向 WebSocket，维护 action echo、按会话队列、入站附件下载和发送降级。

`run_agent.py --transport qq` 仍复用 AgentRunner 装配 LLM、工具、MCP、Skill 和 EventBus，只是关闭 CLI renderer，改由 QQ 适配器消费事件。和 CLI/TUI 不同的是，QQ 模式不会让所有消息共用同一个 AgentSession，而是通过 `session_factory` 按 `ConversationKey` 获取独立会话。

## 通讯会话隔离

QQ 群聊、QQ 好友私聊、未来微信好友和微信群都统一使用 `ConversationKey(platform, kind, id)` 表示会话边界。例如：

- QQ 群聊：`qq:group:123456`
- QQ 私聊：`qq:private:10001`
- 未来微信好友：`wechat:private:wxid_xxx`
- 未来微信群：`wechat:group:roomid_xxx`

`AgentRunner.get_or_create_platform_session()` 保留了旧名字，但当前语义是“每条消息创建一个短生命周期 `AgentSession`”。它不会重新加载工具系统，而是复用同一个进程里的 LLM、ToolRegistry、ToolExecutor、MCP 和 EventBus。

私聊会按好友 ID 拆出持久化目录：

```text
.cbagent/platform_sessions/<platform>/private_<id>/sessions/
```

这意味着不同好友之间的以下数据互不共享：

- history；
- rolling state；
- transcript；
- compact 快照；
- messages 日志文件；

群聊默认不挂 `LocalSessionStore`，所以不写 history/state/transcript/compact。每条群消息仍会获得临时 `AgentSession`，但处理完成后对象释放，避免群消息过多时把本地磁盘和后续上下文撑大。

为了让群聊回复仍能理解“上面/刚才/这个”等指代，QQ 适配器会在群消息真正触发 agent 后，额外调用 NapCat `get_group_msg_history` 拉取最近群消息，并把整理后的摘要放入 `InboundMessage.transient_context`。这段内容会出现在当前轮 `prompt_text()` 中，但不会进入 `persistent_text()`，因此不会写入 history/state/transcript/compact。

相关配置：

```env
QQ_GROUP_CONTEXT_MESSAGES=50
QQ_GROUP_CONTEXT_MAX_CHARS=8000
```

`QQ_GROUP_CONTEXT_MESSAGES=0` 可以关闭这次额外 action。注入文本会明确标注为“最近群聊消息背景”，系统提示词也会提醒模型：这只是理解上下文的背景，不是本轮用户指令，真正需要执行的是当前 `sender_id` 对应用户的新消息。

QQ 适配器内部使用 `_conversation_queues[conversation.stable_id]` 维护轻量队列。同一个群聊或好友内部按消息到达顺序串行处理，避免两条消息并发写同一份私聊 history/state；不同 `ConversationKey` 可以并发执行。

EventBus 事件本身没有携带会话 ID。为了解决并发回传路由，平台层新增 `agent.platforms.context`，在每轮 `chat_async()` 前用 `ContextVar` 绑定当前 `ConversationKey`。`PlatformEventRenderer` 渲染 `Done`、`TodoListUpdated`、`AskUserQuestion`、`send_message_asset` 等事件时，优先读取这个上下文，从而把最终回答、todo 更新、编号问题和文件资源发回正确的 QQ 会话。

`TodoTool` 也是全局工具实例，因此额外按 `ConversationKey.stable_id` 分配独立 `TodoStore`。普通 CLI/TUI 没有平台上下文时仍使用默认 store，避免影响原有 TUI 行为。

这套隔离不是 QQ 专用。后续接微信时，微信适配器只要把微信入站事件转成带有 `ConversationKey(platform="wechat", kind=..., id=...)` 的 `InboundMessage`，就能复用独立 AgentSession、按会话串行队列、事件回传和 todo 隔离。

## 事件适配

TUI 可以渲染结构化卡片，但 QQ 只能接收普通消息和文件。`PlatformEventRenderer` 按事件类型做降级：

| EventBus 事件 | QQ 行为 |
|---|---|
| `Done` | 发送最终回答 |
| `AskUserQuestion` | 发送编号选项，记录 `question_id`、选项映射和发起用户；群聊只接受发起人的编号回复 |
| `AskUserQuestionAnswered` | 默认发送“已选择”短确认 |
| `TodoListUpdated` | 发送简洁任务列表 |
| `Error` / `Cancelled` | 发送短状态提示 |
| `BackgroundNotification` | 发送后台任务完成提示 |
| `ToolStart` | 默认发送短提示：普通工具为 `（调用工具:工具名 参数）`，bash 为 `（执行命令:命令）`；参数会脱敏和截断 |
| `ToolComplete` | 默认静默，`IM_EVENT_VERBOSITY=full` 时发送耗时等调试摘要；`send_message_asset` 完成时仍会触发资源发送 |
| `MCPStatus` / `BuddyUpdated` | 默认静默，避免运行状态污染聊天 |

当 QQ 会话有待回答问题时，下一条消息会优先尝试解释为选项回复。群聊中此路径不要求再次 @ 机器人，避免用户回复 `1` 后工具仍卡住。支持：

- `1`
- `1,3`
- `其他: 自定义内容`
- `取消`

## 表情包与文件发送

新增 `send_message_asset` 工具，仅在通讯平台模式注册。它不直接知道 QQ 连接，而是返回结构化 JSON：

- `queued`
- `kind`
- `path`
- `file_name`
- `size`
- `content_hash`
- `caption`
- `delivery_hint`

事件渲染器监听该工具的 `ToolComplete`，再把资源段交给当前平台适配器发送。这样以后微信适配器可以复用同一个工具结果。

表情包目录默认是 `assets/stickers`，可用 `CBAGENT_STICKER_DIR` 覆盖。用户选择允许任意路径发送文件，因此工具允许本地任意普通文件，但保留以下保护：

- 文件必须存在；
- 路径必须是普通文件；
- 大小不得超过 `CBAGENT_OUTBOUND_FILE_MAX_MB`；
- 记录 hash、大小、文件名用于审计；
- history/compact 不保存二进制。

QQ 中 `sticker/image` 转成 OneBot `image` 段，普通文件优先走 `upload_group_file` / `upload_private_file`。如果发送失败，会降级为文件路径提示，并把交付失败原因发给用户，方便判断是 NapCat action 不支持、文件不存在，还是 Docker 路径不可见。

## 出站文件交付层

NapCat 和 cb-agent 同机运行时，旧实现直接把本地路径传给 OneBot action 就能工作。但 NapCat 放进 Docker 后，容器文件系统看不到宿主机路径，例如宿主机的 `C:\Users\...\run_agent.py` 或 `/home/cb/cb-agent/run_agent.py` 在容器里通常不存在。为了解决这个问题，本次新增 `agent/qq/file_delivery.py`，把“本地文件”先转换成 NapCat 可读取的引用，再交给 QQ 适配器发送。

交付模式由 `QQ_FILE_DELIVERY_MODE` 控制：

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `path` | 保持旧行为，直接传 cb-agent 本机路径 | NapCat 与 cb-agent 同机运行，或容器内外路径完全一致 |
| `mapped_path` | 复制到宿主机共享目录，再改写成 NapCat 容器内路径 | Docker 部署推荐方案 |
| `http` | cb-agent 启动只读临时 HTTP 文件服务，NapCat 拉取 URL | 不方便挂共享卷、跨机器或容器网络可互通 |
| `base64` | 小文件内联为 `base64://...` | 小图片、表情包兜底；不适合大文件 |
| `auto` | 按 `mapped_path -> http -> base64 -> path` 生成候选并依次尝试 | 想自动兜底，但仍建议明确配置生产模式 |

`mapped_path` 模式需要两段路径：

```env
QQ_FILE_DELIVERY_MODE=mapped_path
QQ_FILE_HOST_PREFIX=/opt/cb-agent/outbound
QQ_FILE_NAPCAT_PREFIX=/app/cb-agent-outbound
```

对应 Docker 挂载：

```bash
docker run ... -v /opt/cb-agent/outbound:/app/cb-agent-outbound:ro ...
```

发送时，cb-agent 会把源文件复制到 `QQ_FILE_HOST_PREFIX`，生成带内容 hash 的文件名，避免同名文件覆盖；然后把这个路径改写成 `QQ_FILE_NAPCAT_PREFIX` 下的容器路径传给 NapCat。这样模型仍然只需要调用 `send_message_asset(path=...)`，不用知道 Docker 内部路径。

`http` 模式会启动一个只读临时文件服务：

```env
QQ_FILE_DELIVERY_MODE=http
QQ_FILE_HTTP_HOST=0.0.0.0
QQ_FILE_HTTP_PORT=6200
QQ_FILE_HTTP_PUBLIC_BASE_URL=http://宿主机内网IP:6200
QQ_FILE_HTTP_TTL_SECONDS=300
```

生成的 URL 带随机 token，只暴露当前要发送的文件，并在 TTL 后失效。`QQ_FILE_HTTP_PUBLIC_BASE_URL` 必须是 NapCat 容器实际能访问到的地址；Docker Desktop 可尝试 `host.docker.internal`，Linux Docker 通常直接填宿主机内网 IP 更可靠。大文件不要使用 `base64`，否则会撑大 WebSocket 消息、日志和内存，所以默认 `QQ_FILE_BASE64_MAX_MB=3`。

交付层对 `file/image/sticker/audio/video` 都生效。图片、表情、语音、视频不再盲目转成宿主机 `file://`，如果交付层返回的是 HTTP URL、`base64://` 或容器内 `/app/...` 路径，OneBot 消息段会保留原始引用。

## 入站附件

QQ/NapCat 图片和音频事件通常给 URL。适配器会尝试下载到：

```text
.cbagent/platform_attachments/qq/
```

下载成功后复用现有多模态输入层；下载失败时保留 URL 和附件说明，让模型知道用户发过附件，但不会假装已经完成视觉/语音理解。

长期上下文仍遵守多模态输入策略：只保存附件摘要，不保存 base64、data URI 或二进制。

## 使用方式

`.env` 示例：

```env
QQ_ENABLE=1
QQ_HOST=127.0.0.1
QQ_PORT=6199
QQ_ACCESS_TOKEN=
QQ_GROUP_MODE=mention
QQ_WAKE_PREFIX=/agent
CBAGENT_STICKER_DIR=./assets/stickers
CBAGENT_OUTBOUND_FILE_MAX_MB=50
QQ_FILE_DELIVERY_MODE=path
IM_EVENT_VERBOSITY=normal
```

启动：

```powershell
..\venv\python.exe run_agent.py --transport qq
```

NapCat 反向 WebSocket 地址：

```text
ws://127.0.0.1:6199/onebot/v11/ws
```

## 限制与后续

当前实现已经按 `ConversationKey` 隔离 AgentSession。同一 QQ 群聊或好友私聊内会排队串行处理，不再因为上一条未完成而直接拒绝；不同群聊和不同好友可以并发运行。私聊会落盘恢复上下文，群聊默认只使用临时内存上下文。

文件发送能力依赖 NapCat 对 OneBot action 的实际支持。普通文件优先使用上传 API，失败会降级文本提示；图片和表情包优先走 OneBot 图片段。Docker 场景下优先使用 `mapped_path` 或 `http`，不要依赖默认 `path` 模式读取宿主机路径。

微信接入时不需要重写 AgentSession 隔离、事件渲染器或 `send_message_asset` 工具，只需要新增微信平台适配器，把微信事件转成 `InboundMessage`，把 `OutboundMessage` 翻译为微信发送 API。真正需要额外处理的是微信侧鉴权、消息回调协议、文件上传/下载 API 和群聊唤醒策略。

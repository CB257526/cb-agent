# QQTool 平台专用工具技术报告

## 背景

此前通讯平台侧的资源发送主要依赖 `send_message_asset`，它适合“把模型产物发送回当前会话”这一类窄场景，但 QQ/NapCat 提供的能力远不止发送文件。用户希望模型可以主动执行戳一戳、群打卡、获取群成员、上传群相册、查询登录号信息、发送群/私聊消息等操作，同时又不希望这些平台能力污染 CLI/TUI 的工具列表。

本次新增 `qqtool`，把 QQ 平台主动操作集中到一个平台专用聚合工具中。最终回答、思考内容、工具过程提示、`ask_user_question` 编号问题等仍走现有事件渲染器自动发送，避免模型必须自己负责“普通回复怎么发出去”。

## 注册边界

`run_agent.py` 的原生工具注册改为按 transport 注入平台工具：

- 普通 CLI：不注册 `qqtool`。
- TUI / JSON-RPC：不注册 `qqtool`。
- `run_agent.py --transport qq`：注册 `QQTool()`。
- 未来微信接入时应注册自己的 `wechattool`，不复用 QQ action。

这样模型在本地开发场景不会看到 QQ 专用能力，也不会因为没有 NapCat 连接而误调用平台 action。

## 工具接口

`qqtool` 只暴露两个参数：

```json
{
  "funname": "send_poke",
  "args": {
    "group_id": "10001",
    "user_id": "20001"
  }
}
```

`funname` 是 cb-agent 稳定暴露给模型的功能名，真实 NapCat action 集中维护在 `tools/tools/qq/registry.py`。这样后续 NapCat Apifox 文档更新或 action 名调整时，只需要改注册表，不需要改工具描述、权限层和 prompt。

## 参数容错与当前会话默认值

实际 QQ 测试里发现，模型虽然能看到 `qqtool` 的 schema，但仍可能把 `args` 对象二次 JSON 编码成字符串，例如：

```json
{"funname":"send_group_msg","args":"{\"group_id\":123,\"message\":\"hello\"}"}
```

这会导致工具循环反复试错。因此本次在 `QQTool.run()` 和 `validate_parameters()` 入口增加 `_coerce_args()`：如果 `args` 是 JSON 字符串且解析后是对象，就自动转回 dict，并在返回 metadata 中标记 `args_auto_parsed=true`。工具描述和 README 仍强调正确格式应为对象：

```json
{"funname":"send_group_msg","args":{"group_id":123,"message":"hello"}}
```

权限层也必须做同样的解析。因为 `ToolExecutor` 会先调用 `agent/platforms/permissions.py` 再执行工具，如果权限层仍把字符串化 `args` 当成非法或空对象，工具入口就没有机会容错。现在 `_sensitive_qqtool_reason()` 通过 `_coerce_object()` 解析字符串化参数，保证权限判断和真实执行使用同一套语义。

另一个高频试错来自当前群聊/私聊目标 ID。模型在当前群里常会调用 `send_group_msg` 但漏写 `group_id`。权限层和执行层现在都会读取 `ConversationKey`，在普通用户只能操作当前会话的前提下自动补齐当前 `group_id/user_id`。执行层的补齐发生在必填校验之前，避免先报“缺少 group_id”再让模型进入探索循环。

## 图片消息段与文件交付

此前模型想“直接发图片到聊天框”时，容易退而求其次调用 `upload_group_file`，导致图片作为群文件出现。现在 `send_private_msg` / `send_group_msg` 支持 OneBot 图片、语音、视频、文件消息段里的本地路径：

```json
{
  "funname": "send_group_msg",
  "args": {
    "group_id": 123,
    "message": [
      {"type": "image", "data": {"file": "/tmp/cb-agent-outputs/a.png"}}
    ]
  }
}
```

执行层会把 `data.file` 交给 `prepare_file_reference()`，复用 `mapped_path/http/base64/path` 文件交付策略。这样 NapCat 在 Docker 里运行时，也能把宿主机临时产物转换成容器可读路径或 HTTP/base64 引用。

如果模型使用 OneBot CQ 字符串，例如 `[CQ:image,file=/tmp/cb-agent-outputs/a.png]`，执行层也会提取 `file=` 并走同一套交付转换。新实现仍推荐消息段数组，因为结构化参数更不容易被逗号、空格和转义规则影响。

安全边界同步放在权限层：普通 QQ 用户可以发送 `http(s)`、`base64://`、`data:`、表情包目录资源，以及系统临时目录里的新产物；不能通过 image/file/record/video 消息段把项目源码、配置、日志、密钥或任意本地文件外发。`file://` 不被视为外部资源，也不能绕过本地文件检查。

首批覆盖能力包括：

- 消息互动：`send_private_msg`、`send_group_msg`、`send_poke`。
- 文件媒体：`upload_private_file`、`upload_group_file`、`upload_image_to_qun_album`。
- 群信息：`get_group_list`、`get_group_info`、`get_group_info_ex`、`get_group_member_list`、`get_group_member_info`。
- 群扩展：`send_group_sign`、`get_group_signed_list`、`get_qun_album_list`、`get_group_album_media_list`、`get_group_at_all_remain`、群待办相关 action。
- 好友与账号：`get_login_info`、`send_like`、`get_friend_list`、`get_friends_with_category`、`get_recent_contact`、`get_online_clients`。
- 历史消息：`get_group_msg_history`、`get_friend_msg_history`、`get_forward_msg`。
- 资料与 Ark：`set_self_longnick`、`set_qq_profile`、`_set_model_show`、`ArkShareGroup`、`ArkSharePeer`、`get_mini_app_ark`。
- 调试兜底：`raw_action`，仅 root 用户可用。

## Action Bridge

QQ 适配器运行在 asyncio WebSocket 事件循环中，而工具调用通常发生在 agent 工具线程里。为避免 `qqtool` 重复实现 WebSocket echo、超时和连接状态管理，新增 `agent/qq/action_bridge.py`：

- NapCat WebSocket 连接成功后，adapter 注册自己的 `call_action` 协程。
- `qqtool` 在线程中通过 `asyncio.run_coroutine_threadsafe()` 把 action 投递回 adapter 的事件循环。
- 连接断开时通过 token 注销，避免旧连接断开误清掉新连接。
- 未连接 NapCat 时返回明确错误：`NapCat websocket is not connected`。

adapter 仍然是唯一真正理解 OneBot WebSocket 协议的模块，`qqtool` 只负责参数构造和结果压缩。

## 文件交付

Docker 部署 NapCat 时，cb-agent 宿主机路径通常不能被容器直接读取。本次 `qqtool` 文件类 funname 复用现有 `QQFileDeliveryManager`：

- `path`：直接传 cb-agent 本机路径，适合同机或路径一致部署。
- `mapped_path`：先复制到 `QQ_FILE_HOST_PREFIX`，再改写成 `QQ_FILE_NAPCAT_PREFIX` 容器路径，推荐 Docker 部署。
- `http`：启动只读临时 HTTP 文件服务，给 NapCat 一个带随机 token、会过期的 URL。
- `base64`：小文件内联成 `base64://`，不适合大文件。
- `auto`：按 `mapped_path -> http -> base64 -> path` 生成候选。

`qqtool` 优先通过 bridge 调 adapter 内部 action `__cbagent_prepare_resource_reference__`，这样能复用正在运行的 adapter 配置和 HTTP 服务。离线单测或没有连接时，再退回到本地 `QQFileDeliveryManager(QQConfig.from_env())`。

## 权限策略

`agent/platforms/permissions.py` 新增 `qqtool` 敏感判断：

- 未知 `funname` 默认敏感。
- 注册表标记 `root_only=True` 的功能默认只有 `QQ_ROOT_USERS` / `IM_ROOT_USERS` 可用。
- 普通用户只能操作当前通讯会话：
  - 当前群聊只能调用当前 `group_id` 的群 action。
  - 当前私聊只能调用当前 `user_id` 的私聊 action。
  - 群聊 `send_poke` 必须带当前 `group_id`，否则无法证明没有跨群操作，会被拒绝。
- 文件类 funname 只允许普通用户发送 `http(s)`、`base64://`、`data:` 这类非本地资源引用、系统临时产物目录、表情包目录；`file://` 和任意本地路径一样需要 root。
- `raw_action` 始终 root-only，因为它能绕过注册表调用任意 NapCat action。

这层门禁只对通讯平台触发的工具调用生效，不影响本地 CLI/TUI 原有权限体验。

## 与事件系统的边界

`qqtool` 是“模型主动操作 QQ”的入口，不承担普通聊天投递：

- 最终回答仍由 `PlatformEventRenderer` 监听 `Done` 后发送。
- 思考内容仍由 `IM_SHOW_REASONING` 控制。
- 工具开始/结束过程消息仍由 `IM_EVENT_VERBOSITY` 和 `IM_GROUP_TOOL_MESSAGES` 控制。
- `ask_user_question` 仍会渲染成编号问题，并由平台层处理用户回复。
- `send_message_asset` 的模型入口不再默认注册，但底层工具和事件兼容逻辑保留，避免旧 transcript 或测试路径断裂。

这个边界很重要：如果让模型自己负责发送最终回答，普通聊天可靠性会下降，也容易重复发送或漏发。

## 上下文与结果压缩

`qqtool` 返回 JSON 字符串，包含 `ok`、`funname`、`action`、`duration_ms`、`summary`、`data` 等字段。执行层会压缩列表和长字符串：

- 好友列表、群成员列表、历史消息等最多保留 `result_limit` 条。
- 超长字符串截断到约 1200 字符。
- `base64://` 参数在工具结果里折叠成长度摘要，避免把二进制写入 history/compact。

这样既能让模型知道 action 是否成功，也不会把 QQ 社交图谱或大文件内容一次性塞进上下文。

## 验证

已补测试：

- `test/test_qqtool.py`：
  - schema 校验。
  - 未连接 NapCat 时返回明确错误。
  - action payload 通过 bridge 投递。
  - `mapped_path` 文件交付会复制共享目录并改写容器路径。
  - adapter 内部资源引用 action 可用。
  - 手动 registry 注册 `qqtool` 时不会带入 `send_message_asset`。
- `test/test_executor.py`：
  - 普通用户可发送当前群聊的外部资源和临时产物。
  - 普通用户发送任意本地文件会被拒绝，`file://` 也不能绕过本地文件外发检查。
  - root-only funname、跨会话 action 会被拒绝。
  - root 用户可执行敏感 funname。
  - 群聊 `send_poke` 缺少当前 `group_id` 会被拒绝。

验证命令：

```powershell
python -m unittest discover -s test -p "test_qqtool.py"
python -m unittest discover -s test -p "test_executor.py"
python -m unittest discover -s test -p "test_qq_adapter.py"
python -m py_compile run_agent.py agent\qq\action_bridge.py agent\qq\adapter.py agent\platforms\permissions.py tools\tools\qqtool.py tools\tools\qq\registry.py tools\tools\qq\functions.py tools\tools\qq\media.py tools\tools\qq\__init__.py
```

## 后续

后续如果继续对齐 NapCat Apifox 文档，可以只在 `tools/tools/qq/registry.py` 增加新的 `QQFunctionSpec`，再按风险设置 `root_only`、`current_conversation_only` 和 `file_param`。如果接入微信，建议复用平台消息层、权限层和事件渲染器，但单独新增 `wechattool` 与微信 action registry。

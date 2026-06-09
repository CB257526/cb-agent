# 微信 OC 接入技术报告

## 背景

cb-agent 已经通过 `agent.platforms` 建立了通讯平台抽象。QQ/NapCat 是独立机器人账号，会暴露给好友和群聊，所以必须做 root 用户、白名单、群聊唤醒和敏感工具门禁。微信 OC 的实际形态不同：openclaw-weixin 会在当前微信账号里创建一个 `direct` 私聊 bot，它不是独立账号，也不是面向群友开放的机器人。

因此微信接入采用“当前账号持有人自用入口”的模型：

- 只处理私聊消息。
- 上游如果出现 `group_id`，直接忽略，避免误触发。
- 不提供微信群聊唤醒、用户白名单或 root 用户分级配置。
- 通讯平台权限层对微信上下文直接放行，QQ 的敏感权限策略保持不变。

实现仍复用 QQ 已验证过的通讯平台底座：`ConversationKey`、`InboundMessage`、`OutboundMessage`、`PlatformEventRenderer`、会话隔离、编号问答和事件自动回传。

## 模块结构

新增模块集中在 `agent/wechat/`：

- `config.py`：读取微信 OC API、CDN、登录态文件、附件目录和 action 超时配置。这里不再读取群聊唤醒、白名单或 root 用户变量。
- `client.py`：封装微信 OC HTTP API，包括二维码登录、状态轮询、`getupdates` 长轮询、`sendmessage`、`getuploadurl`、输入状态和 CDN 上传下载。
- `oc_types.py`：解析 `getupdates` 消息 item，构造文本和媒体 `sendmessage` 请求体；该层明确忽略带 `group_id` 的消息。
- `media.py`：处理入站媒体落盘，把图片等附件保存到本地后交给多模态输入层。
- `action_bridge.py`：为 `wechattool` 提供跨线程 action 投递，避免工具层直接持有 adapter。
- `adapter.py`：维护登录态、长轮询循环、会话队列、事件渲染器和出站消息发送。

平台主动工具集中在 `tools/tools/wechat/` 与 `tools/tools/wechattool.py`。它和 `qqtool` 使用相同的“一个入口 + 子功能注册表”结构，但权限模型不同：微信工具默认按自用入口放行。

## 启动流程

`run_agent.py --transport wechat` 会创建 `AgentRunner(communication_platform="wechat")`，因此只有微信 transport 会注册 `wechattool`。CLI、TUI、QQ 模式不会看到该工具。

adapter 启动流程：

1. 读取 `.env` 与 `WECHAT_STATE_FILE`。
2. 如果已有 token，直接恢复登录态。
3. 如果没有 token，调用 `get_bot_qrcode`，在终端打印二维码或二维码链接。
4. 轮询 `get_qrcode_status`，确认后保存 `token`、`account_id`、`sync_buf` 和最近会话的 `context_token`。
5. 注册 `global_wechat_action_bridge`。
6. 循环调用 `getupdates`，把私聊消息转成 `InboundMessage` 并启动 agent run。

状态文件默认是：

```text
.cbagent/wechat/state.json
```

该文件等同于微信登录凭据，不应提交或公开。

## 会话隔离

微信私聊使用：

```python
ConversationKey("wechat", "private", wxid)
```

私聊会复用现有平台 session 目录并落盘：

```text
.cbagent/platform_sessions/wechat/private_<wxid>/sessions/
```

虽然当前 openclaw-weixin 的使用者只有账号持有人，但仍保留按 wxid 隔离的目录结构，方便后续兼容多个联系人或多个 direct 会话。每个会话内部通过轻量队列串行处理消息，LLM、MCP、工具注册表和 EventBus 仍在进程内共享，不会为每条消息重复加载。

微信模式不创建群聊 session。上游如果下发 `group_id`，`parse_wechat_message()` 会返回 `None`，adapter 不会触发 agent。

## 事件渲染

微信模式不让模型负责普通回复投递，仍由 `PlatformEventRenderer` 自动发送关键事件：

- `Done`：发送最终回答。
- `ReasoningDelta`：`IM_SHOW_REASONING=1` 时分段发送 `【思考】`。
- `ToolStart`：按 `IM_EVENT_VERBOSITY` 发送工具开始提示。
- `ToolComplete` 权限拒绝或异常：发送短提示。
- `AskUserQuestion`：渲染成编号问题，用户回复 `1`、`1,3`、`其他: ...` 或 `取消`。
- `TodoListUpdated`：渲染成简洁 todo。
- `Error` / `Cancelled`：发送短状态消息。

这保证微信、QQ 和未来其他 IM 平台共享同一套事件降级逻辑，TUI/CLI 不受影响。

## wechattool

`wechattool` 是模型主动执行微信操作的入口，只暴露：

```json
{
  "funname": "send_image",
  "args": {
    "path": "/tmp/cb-agent-outputs/demo.png"
  }
}
```

首批功能：

- `send_text`：发送额外文本。
- `send_image`：发送图片或表情包。
- `send_file`：发送普通文件。
- `send_typing`：发送或取消输入状态。
- `get_status`：查看 transport 运行状态。
- `get_login_info`：查看登录账号信息。

最终回答、思考内容、工具过程提示和编号问答仍由事件系统自动发送，不需要模型再调用 `wechattool(send_text)` 补发。模型只有在需要额外主动操作时才使用 `wechattool`。

`WECHAT_ACTION_TIMEOUT_SECONDS` 控制工具线程等待 adapter action 的最长时间；adapter 内部 HTTP 请求仍分别受 `WECHAT_API_TIMEOUT_MS` 和 `WECHAT_LONG_POLL_TIMEOUT_MS` 控制。

## 媒体发送

微信媒体发送不复用 QQ/NapCat 的 Docker 路径共享层。QQ/NapCat 发送文件时需要 NapCat 能读取 cb-agent 的路径，因此才有 `path/mapped_path/http/base64/auto`。微信 OC 发送媒体时由 cb-agent 自己读取本地文件，调用微信 CDN 上传，然后把返回的 media 引用放进 `sendmessage`。

这意味着：

- NapCat Docker 共享目录问题不影响微信。
- cb-agent 所在机器必须能访问微信 OC API 与 CDN。
- 用户让 agent 生成、下载或制作要发回的文件时，建议放在 `/tmp/cb-agent-outputs/` 或系统临时目录，便于审计和清理。
- 微信模式不做通讯平台 root 分级，但 Bash 工具自身的确认机制和 `--dangerously-skip-permissions` 语义仍保持原样。

媒体上传使用 AES-ECB 加密，因此新增依赖 `pycryptodome`。扫码二维码终端展示使用 `qrcode`；如果运行环境不能渲染二维码，会退回打印原始二维码链接。

## 入站媒体

入站图片、文件、视频、语音会先解析为 `InboundAttachment`。图片会下载到：

```text
.cbagent/platform_attachments/wechat/
```

下载成功后，图片会作为本地路径交给现有多模态输入层；如果主模型支持图片，就原生发送 `image_url`，否则走 OCR/视觉描述。微信语音常见 SILK 编码，目前会保存为临时文件并在 prompt 中提示路径，暂不直接交给 ASR 附件管线，避免 `.silk` 导致整轮失败。

## 权限策略

微信 OC 与 QQ 的权限策略已经分开：

- QQ/NapCat：独立机器人账号，可能面对普通好友或群友，因此继续检查 `QQ_ROOT_USERS` / `IM_ROOT_USERS`。
- 微信 OC：当前账号里的私聊 bot，真实使用者就是账号持有人，因此 `check_platform_tool_permission()` 在 `conversation.platform == "wechat"` 时直接放行。

这层放行只表示“不再套用通讯平台 root 门禁”。工具自身的参数校验、文件存在性校验、媒体上传错误处理、Bash 工具确认机制和 `--dangerously-skip-permissions` 仍按原来的规则工作。TUI/CLI 没有通讯平台上下文，也不会受到微信策略影响。

## 配置项

核心配置：

```env
WECHAT_ENABLE=1
WECHAT_BASE_URL=https://ilinkai.weixin.qq.com
WECHAT_CDN_BASE_URL=https://novac2c.cdn.weixin.qq.com/c2c
WECHAT_STATE_FILE=.cbagent/wechat/state.json
CBAGENT_PLATFORM_ATTACHMENT_DIR_WECHAT=.cbagent/platform_attachments/wechat
WECHAT_ACTION_TIMEOUT_SECONDS=30
```

`WECHAT_TOKEN` 与 `WECHAT_ACCOUNT_ID` 可以手动配置，也可以让扫码登录自动写入 state 文件。微信模式不需要配置 `WECHAT_ROOT_USERS`、`WECHAT_ALLOWED_USERS`、`WECHAT_ALLOWED_GROUPS`、`WECHAT_GROUP_MODE` 或 `WECHAT_WAKE_PREFIX`。

## 已知限制

- 当前不支持微信群聊、公众号、企业微信、Gewechat。
- SILK 语音暂不直接转 WAV/ASR。
- 微信媒体发送需要可访问微信 CDN；网络受限的服务器需要额外代理或网关。
- `wechattool` 首版只覆盖最小主动操作集合，后续可按实际 OC API 能力扩展联系人、历史消息或更多资料接口。

## 验证

测试覆盖：

- `WeChatConfig.from_env()` 默认值和环境变量覆盖。
- 私聊消息解析、带 `group_id` 消息忽略、图片附件解析。
- `sendmessage` 文本请求体结构，空文本时不发送 JSON null 可选字段。
- 媒体发送请求保持单 item；caption 由 adapter 先发文本，再单独发送媒体 item。
- 扫码登录按 openclaw-weixin 的协议形状使用 POST，并携带 `local_token_list`。
- 扫码轮询支持 `verify_code`、`scaned_but_redirect` 重定向后继续确认登录。
- `wechattool` schema、未连接错误、bridge action 投递和当前会话参数补全。
- adapter 状态文件落盘/恢复。
- adapter 出站文本发送。
- 通讯平台权限层在微信上下文下不再套用 root 门禁。

推荐验证命令：

```bash
python -m unittest discover -s test -p "test_wechat*.py"
python -m unittest discover -s test -p "test_platform*.py"
python -m unittest discover -s test -p "test_executor.py"
python -m py_compile run_agent.py agent/wechat/*.py tools/tools/wechattool.py tools/tools/wechat/*.py
```

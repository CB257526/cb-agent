# cb-agent

> 一个可拆解、可审计、带工具循环的 LLM Agent 框架。
> 默认走轻量 Markdown 记忆；需要旧向量记忆/RAG 时再显式启用 full 模式。

```
用户输入
  └─> ContextBuilder(GSSC)
        ├─ system / session state / Markdown memory / history
        ├─ full memory + RAG（可选）
        └─ tools + MCP + skills
  └─> LLM stream think
  └─> Function Calling 工具循环
  └─> final answer + 工作记录 + 本地会话持久化
```

## 功能概览

| 能力 | 说明 |
|---|---|
| 多轮 Function Calling | 支持流式内容、reasoning delta、tool_calls 分片累积与 tool result 回灌 |
| ContextBuilder | GSSC：Gather → Select → Structure → Compress，按优先级和 token 预算组织上下文 |
| 轻量 Markdown 记忆 | 默认启用，使用 `~/.cbagent/memory/` 与项目 `.cbagent/memory/`，不依赖 embedding / 向量库 |
| full 记忆/RAG | 旧 `MemoryTool` / `RAGTool` 完整保留，通过 `--memory-system full` 启用 |
| 跨轮工作上下文 | 每轮工具轨迹压成 `【工作记录】`，写入 `.cbagent/sessions/`，重启后可恢复 |
| 多会话隔离 | 本地 session 可创建、切换、清理；不同会话 history/state/transcript 隔离 |
| 上下文压缩 | TUI 支持 `/compact`；后端也会在上下文接近模型窗口 80% 时自动 compact |
| 多模态输入 | 用户消息支持 `text + attachments[]`，图片可原生发给多模态基模，纯文本基模自动走 OCR，音频统一 ASR 成文本 |
| TUI | Ink/React 终端界面，支持工具卡片、会话切换、Context 占用指标、日志面板 |
| QQ / NapCat | `--transport qq` 接入 OneBot V11 反向 WebSocket，支持文本、表情包/文件发送、编号问答、todo 事件降级，并按群聊/好友隔离 AgentSession |
| 微信 OC | `--transport wechat` 接入个人微信 OC HTTP 长轮询，支持扫码登录、私聊持久化、媒体上传、事件自动发送和平台专用 `wechattool` |
| MCP | 读取 `mcp.json`，通过 stdio 启动 MCP server，并展开成可调用工具 |
| Skills | Markdown + YAML frontmatter 描述能力，按需加载指令和脚本 |
| Bash 权限 | Bash 工具支持权限语义，TUI 模式下权限确认走 UI 通道 |
| Buddy 宠物 | 可选虚拟宠物系统，支持孵化、摸摸、静音、TUI 输入框旁展示和本地气泡反应 |

## Quickstart

### 0. 准备环境

必需：

- Python `>=3.10`
- 一个 OpenAI-compatible LLM 服务，至少提供 `LLM_MODEL_ID`、`LLM_API_KEY`、`LLM_BASE_URL`

如果要使用 TUI：

- Node.js `>=20`
- npm
- 剪贴板图片粘贴可选依赖：Windows 使用 PowerShell/.NET Clipboard；macOS 推荐安装 `pngpaste`；Linux 推荐安装 `wl-paste` 或 `xclip`

如果要使用 MCP：

- 能运行 `npx`
- `mcp.json` 中相关服务需要的环境变量，例如 `AMAP_MAPS_API_KEY`

如果要使用 QQ / NapCat：

- 已登录并启用 OneBot V11 反向 WebSocket 的 NapCat
- Python 依赖中的 `websockets`

如果要使用微信 OC：

- 能访问个人微信 OC HTTP API（默认 `https://ilinkai.weixin.qq.com`）
- Python 依赖中的 `requests`、`pycryptodome` 和 `qrcode`
- 首次启动需要在终端扫码登录；token 会保存到 `.cbagent/wechat/state.json`

### 1. 克隆并进入项目

```bash
git clone <your-fork-url> cb-agent
cd cb-agent
```

如果你当前就在本仓库，路径通常类似：

```powershell
cd C:\Users\cb135\Desktop\cbAgent\cb-agent
```

### 2. 创建 Python 虚拟环境

Windows PowerShell：

```powershell
python -m venv ..\venv
..\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

macOS / Linux：

检查你的python版本
```bash
python --version
sudo apt update
#假如是python3.10
sudo apt install python3.10-venv -y
```

```bash
python3 -m venv ../venv
source ../venv/bin/activate
python -m pip install --upgrade pip
```

> TUI 会优先查找 `../venv/python.exe`、`../venv/Scripts/python.exe` 或 `../venv/bin/python`，所以推荐把虚拟环境放在项目父目录的 `venv`。

### 3. 安装依赖

默认轻量安装：

```bash
pip install -e .
```

或使用 requirements：

```bash
pip install -r requirements.txt
```

完整安装，启用旧向量记忆/RAG、多模态 RAG、PDF 等可选依赖。Web 搜索只需要核心依赖里的 `requests` 和对应 API Key：

```bash
pip install -e ".[full]"
```

或：

```bash
pip install -r requirements-full.txt
```

两种安装模式的区别：

| 模式 | 安装命令 | 默认记忆 | 是否注册 `memory` / `rag` | 适合场景 |
|---|---|---|---|---|
| light | `pip install -e .` | Markdown 文件 | 否 | 先跑起来、低依赖、无需向量库 |
| full | `pip install -e ".[full]"` | 旧向量记忆/RAG | 是 | 需要 embedding、RAG、多模态、向量/图存储 |

### 4. 配置 `.env`

复制模板：

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

macOS / Linux：

```bash
cp .env.example .env
```

至少填写：

```env
LLM_MODEL_ID=deepseek-v4-flash
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://your-provider.example/v1
LLM_TIMEOUT=60
```

日志等级也可以在 `.env` 里控制：

```env
# basic | detail | full
CBAGENT_LOG_LEVEL=basic
CBAGENT_LOG_DIR=.cbagent/logs
```

- `basic`: 记录启动、会话、工具、网关、错误等关键生命周期日志。
- `detail`: 增加每轮 think、工具调度、RPC 分发和 messages 摘要日志。
- `full`: 开启 DEBUG，并把发送给 LLM 的完整 messages 写入 `messages-*.log`。

运行日志默认写到 `.cbagent/logs/cb-agent-<timestamp>.log`；TUI 仍会把 Python stderr 镜像到 `~/.cb-agent/logs/gateway-<timestamp>.log`。

Buddy 宠物默认关闭。需要使用时在 `.env` 里开启：

```env
FEATURE_BUDDY=1
```

开启后重启 CLI 或 TUI，再使用 `/buddy hatch` 孵化。Buddy 状态会持久化到 `~/.cbagent/buddy.json`，同一台机器上的 CLI 和 TUI 会共享同一只 Buddy。

注意：`LLM_MODEL_ID` 必须在 [constant/llm/constant_llm.py](constant/llm/constant_llm.py) 的 `ConstantLLM.llm_dict` 里登记。新增模型时要补：

- `is_tool`
- `is_reasoning`
- `json_output`
- `max_tokens`
- `image_ability`

可选环境变量：

| 变量 | 用途 |
|---|---|
| `AMAP_MAPS_API_KEY` | `mcp.json` 里的高德 MCP server |
| `TAVILY_API_KEY` | `my_advanced_search` 的 Tavily 搜索源 |
| `SERPAPI_API_KEY` | `my_advanced_search` 的 SerpApi 搜索源 |
| `FEATURE_BUDDY` | 设为 `1` / `true` / `on` 后启用 Buddy 宠物系统 |
| `CBAGENT_DANGEROUSLY_SKIP_PERMISSIONS` | 设为 `1` 后等价于 `--dangerously-skip-permissions`，BashTool 将跳过权限确认和高危命令拦截 |
| `CBAGENT_ATTACHMENT_MAX_MB` | 单个多模态附件大小上限，默认 `20` MB |
| `OCR_API_KEY` / `OCR_BASE_URL` / `OCR_MODEL_NAME` | 纯文本基模处理图片附件时使用的 OCR/视觉描述模型 |
| `ASR_API_KEY` / `ASR_BASE_URL` / `ASR_MODEL_NAME` | 音频附件转写为文本时使用的 ASR 模型 |
| `QQ_ENABLE` | 是否启用 QQ transport；设为 `1` 后，`python run_agent.py --transport qq` 会监听 NapCat 反向 WebSocket |
| `QQ_HOST` | cb-agent 监听地址；本机 NapCat 用 `127.0.0.1`，跨机器/Docker 可用 `0.0.0.0` |
| `QQ_PORT` | cb-agent 监听端口；NapCat 反向 WebSocket URL 的端口必须一致 |
| `QQ_ACCESS_TOKEN` | 可选访问令牌；填写后 NapCat 侧也要配置同一个 token |
| `QQ_GROUP_MODE` | 群聊唤醒策略：`mention`、`prefix` 或 `all`；私聊不受影响 |
| `QQ_WAKE_PREFIX` | 群聊文本唤醒前缀，例如 `/agent 帮我查一下` |
| `QQ_ALLOWED_GROUPS` | QQ 群白名单，逗号分隔群号；为空表示不限制群 |
| `QQ_ALLOWED_USERS` | QQ 用户白名单，逗号分隔 QQ 号；为空表示不限制用户 |
| `QQ_ROOT_USERS` | QQ root 用户，逗号分隔 QQ 号；只有这些用户能在 QQ 中触发敏感工具，未配置时通讯平台敏感工具默认拒绝 |
| `IM_ROOT_USERS` | 通用多人通讯平台 root 用户；QQ 会同时检查它和 `QQ_ROOT_USERS`，微信 OC 是当前账号私聊 bot，不检查该配置 |
| `CBAGENT_MCP_PUBLIC_PREFIXES` / `CBAGENT_MCP_SENSITIVE_PREFIXES` | 自定义 MCP 展开工具名前缀的权限分类；默认 `fetch_`、`tavily_`、`amap-maps_` 普通可用，`github_`、`playwright_` 需要 root |
| `QQ_ACTION_TIMEOUT_SECONDS` | OneBot action 超时时间，影响发送消息和上传文件 |
| `IM_EVENT_VERBOSITY` | 通讯软件事件输出等级：`normal` 会发关键事件和工具开始提示，`full` 额外同步工具完成、round/token 等调试摘要 |
| `IM_GROUP_TOOL_MESSAGES` | 群聊是否显示工具过程消息，默认 `1`；设为 `0` 后群聊不再发送“调用工具/执行命令/工具完成”等过程提示，私聊不受影响 |
| `IM_SHOW_REASONING` | 是否把思考模型的 `reasoning_content` 同步到 QQ/微信，默认 `0` 关闭 |
| `IM_REASONING_CHUNK_CHARS` | `IM_SHOW_REASONING=1` 时每段“思考”消息的字符数，默认 `1200` |
| `IM_REASONING_MAX_CHARS` | `IM_SHOW_REASONING=1` 时每轮最多展示的思考字符数，默认 `8000`，`0` 表示不限制 |
| `IM_CONFIRM_QUESTION_ANSWER` | 通讯平台编号回答后是否发送“已选择”确认，默认 `1` |
| `CBAGENT_STICKER_DIR` | 表情包目录，默认 `./assets/stickers`；QQ/微信模式下可通过平台专用工具上传这里的图片作为 sticker/image |
| `CBAGENT_OUTBOUND_FILE_MAX_MB` | agent 发送本地文件到通讯软件的大小上限，默认 `50` MB |
| `QQ_FILE_DELIVERY_MODE` | QQ/NapCat 出站文件交付方式：`path` 兼容旧行为，`mapped_path` 适合 Docker 共享卷，`http` 让 NapCat 拉临时 URL，`base64` 只适合小文件，`auto` 会按顺序尝试 |
| `QQ_FILE_HOST_PREFIX` / `QQ_FILE_NAPCAT_PREFIX` | `mapped_path` 模式使用；前者是宿主机共享目录，后者是同一目录在 NapCat 容器内的路径 |
| `QQ_FILE_HTTP_HOST` / `QQ_FILE_HTTP_PORT` / `QQ_FILE_HTTP_PUBLIC_BASE_URL` | `http` 模式使用；cb-agent 启动只读临时文件服务，公开 URL 必须是 NapCat 容器能访问到的地址 |
| `QQ_FILE_HTTP_TTL_SECONDS` | HTTP 临时文件 URL 有效期，默认 `300` 秒 |
| `QQ_FILE_BASE64_MAX_MB` | `base64` 模式或 `auto` 兜底允许内联的最大文件大小，默认 `3` MB |
| `CBAGENT_PLATFORM_ATTACHMENT_DIR` | QQ 图片/音频入站 URL 下载目录，下载成功后交给多模态输入层处理 |
| `WECHAT_ENABLE` | 是否启用微信 OC transport；设为 `1` 后，`python run_agent.py --transport wechat` 会启动扫码登录和 HTTP 长轮询 |
| `WECHAT_BASE_URL` | 微信 OC API 地址，默认 `https://ilinkai.weixin.qq.com` |
| `WECHAT_CDN_BASE_URL` | 微信媒体 CDN 地址；图片、视频和文件会先上传 CDN，再通过 `sendmessage` 发出 |
| `WECHAT_TOKEN` / `WECHAT_ACCOUNT_ID` | 微信登录凭据；可留空扫码登录，成功后会写入 `WECHAT_STATE_FILE` |
| `WECHAT_BOT_TYPE` | 微信 OC 扫码登录使用的 bot_type，默认 `3` |
| `WECHAT_API_TIMEOUT_MS` / `WECHAT_LONG_POLL_TIMEOUT_MS` | 普通 API 与 `getupdates` 长轮询超时，单位毫秒 |
| `WECHAT_STATE_FILE` | 微信 token、account_id、sync_buf 和 context token 的本地状态文件，默认 `.cbagent/wechat/state.json` |
| `CBAGENT_PLATFORM_ATTACHMENT_DIR_WECHAT` | 微信图片/文件/语音入站媒体下载目录，默认 `.cbagent/platform_attachments/wechat` |
| `WECHAT_ACTION_TIMEOUT_SECONDS` | `wechattool` 调用微信 adapter action 的超时时间 |
| `VECTOR_STORE_TYPE` / `QDRANT_URL` / `QDRANT_API_KEY` | full RAG/Memory 的向量存储 |
| `GRAPH_STORE_TYPE` / `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` | full 语义记忆图存储 |
| `EMBED_MODEL_TYPE` / `EMBED_MODEL_NAME` / `EMBED_API_KEY` | full embedding 配置 |

`.env` 已被 `.gitignore` 忽略，不会进仓库。

#### 自定义系统提示词

长期稳定的系统提示词集中在 [constant/system_prompt.py](constant/system_prompt.py)。如果你想调整 agent 的基础行为、回答风格或角色扮演风格，优先改这个文件里的 `ConstantSystemPrompt`，不用去 `context/` 或 `agent/` 里翻拼接代码。

常用入口：

```python
class ConstantSystemPrompt:
    USER_COSPLAY_PROMPT = "你是一位耐心、严格、偏工程审查风格的资深架构师。"
```

也兼容 `USER_COSERPLAY_PROMPT` 这个拼写。为空时不会注入。这里建议只写长期稳定的风格偏好；当前时间、cwd、工具列表、MCP instructions、Buddy 状态、CLAUDE.md/记忆内容、通讯平台会话信息等运行时动态内容仍由上下文系统自动拼接，避免后续做高缓存命中优化时把动态内容混进静态前缀。

### 5. 启动 CLI

默认启动，使用轻量 Markdown 记忆：

```bash
python run_agent.py
```

等价于：

```bash
python run_agent.py --memory-system light
```

常用启动参数：

```bash
python run_agent.py --memory-system off
python run_agent.py --memory-system full
python run_agent.py --no-mcp
python run_agent.py --no-ctx
```

危险权限模式：

```bash
python run_agent.py --dangerously-skip-permissions
```

开启后 BashTool 会跳过权限确认、非只读检查和高危命令拦截，agent 可以直接执行任意 shell 命令。该模式适合你在完全受信任的本地环境里临时加速操作，不建议在公网服务、群聊 QQ、共享服务器或不可信模型/提示词场景开启。工具结果中的 `permission.dangerously_skipped=true` 会标记这次放行来自危险模式。

CLI 里建议先跑：

```text
/tools
/skills
帮我看一下这个项目有哪些核心模块
```

退出：

```text
/quit
```

### 6. 启动 TUI

第一次进入 TUI 目录安装依赖：

```bash
cd ui-tui
npm install
```

启动：

```bash
npm start
```

TUI 会自动 spawn：

```bash
python run_agent.py --transport jsonrpc --memory-system light
```

如果 TUI 找不到 Python，可以指定：

Windows PowerShell：

```powershell
$env:CB_AGENT_PYTHON="C:\Users\cb135\Desktop\cbAgent\venv\Scripts\python.exe"
npm start
```

macOS / Linux：

```bash
CB_AGENT_PYTHON=/path/to/cbAgent/venv/bin/python npm start
```

如果没有显式配置 `CB_AGENT_PYTHON`，TUI 会先找项目父目录的 `../venv/bin/python`；仍找不到时，Linux/macOS 会兜底调用系统 `python3`。

TUI 需要开启危险权限模式时，用环境变量透传给 Python 后端：

```bash
CBAGENT_DANGEROUSLY_SKIP_PERMISSIONS=1 npm start
```

TUI 快捷键：

| 快捷键 / 命令 | 说明 |
|---|---|
| `Enter` | 发送输入 |
| `/` | 打开 slash command picker |
| `/tools` | 列出后端注册工具 |
| `/sessions` | 打开本地会话切换面板 |
| `/new` | 新建并切换到空白会话 |
| `/switch <id>` | 切换到指定 session |
| `/compact` | 手动压缩当前会话上下文 |
| `/buddy` | 查看、孵化或互动 Buddy 宠物 |
| `/attach <path>` | 添加本地图片或音频附件到下一轮消息 |
| `/paste-image` | 从系统剪贴板读取图片并加入附件队列，终端不传 `Ctrl-V` 时优先用它 |
| `/attachments` | 查看待发送附件队列 |
| `/detach <index\|all>` | 移除一个或全部待发送附件 |
| `/clear` | 清空前端显示并清空后端当前会话历史 |
| `/log` 或 `Ctrl-O` | 切换后端日志面板 |
| `Ctrl-V` | 尝试读取剪贴板图片；部分终端会拦截该快捷键，此时改用 `/paste-image` |
| `Ctrl-L` | 清当前屏幕显示，保留后端 history |
| `Ctrl-C` | busy 时取消当前回答；空闲时退出 |

TUI 底部状态栏会显示：

- 当前模型
- 当前 session
- `Context used/max percent`，也就是 state + history 对上下文窗口的占用
- OpenAI usage 累计
- 工具循环 round

### 7. 启动 QQ / NapCat

QQ 接入使用 NapCat 的 OneBot V11 反向 WebSocket。cb-agent 负责启动 WebSocket 服务，NapCat 主动连接进来。

`.env` 示例。最小可用配置只需要把 `QQ_ENABLE` 改成 `1`；其它字段按你的 NapCat 部署方式调整：

```env
# 开启 QQ transport。不开启时 --transport qq 会直接退出，不监听 WebSocket。
QQ_ENABLE=1

# cb-agent 监听地址和端口。NapCat 反向 WebSocket 要连到 ws://127.0.0.1:6199/onebot/v11/ws。
# 如果 NapCat 不在同一台机器，把 QQ_HOST 改为 0.0.0.0，并建议设置 QQ_ACCESS_TOKEN。
QQ_HOST=127.0.0.1
QQ_PORT=6199

# 可选访问令牌。为空表示不校验；填写后 NapCat 侧也要填写相同 token。
QQ_ACCESS_TOKEN=

# 群聊唤醒策略:
# mention = 群聊 @机器人 或使用 QQ_WAKE_PREFIX 才响应；
# prefix  = 只响应 QQ_WAKE_PREFIX；
# all     = 群里每条消息都响应，容易刷屏。
QQ_GROUP_MODE=mention
QQ_WAKE_PREFIX=/agent

# 白名单，逗号分隔。为空表示不限制。
QQ_ALLOWED_GROUPS=
QQ_ALLOWED_USERS=

# root 用户，逗号分隔。不是“谁能聊天”，而是“谁能执行敏感工具”。
# 未配置时，QQ 等多人通讯平台触发的敏感工具默认拒绝；TUI/CLI/微信 OC 不受影响。
QQ_ROOT_USERS=
IM_ROOT_USERS=

# MCP 展开工具名前缀权限分类。默认 fetch_/tavily_/amap-maps_ 普通可用，
# github_/playwright_ 需要 root；自定义 MCP server 可在这里补前缀。
CBAGENT_MCP_PUBLIC_PREFIXES=
CBAGENT_MCP_SENSITIVE_PREFIXES=

# OneBot action 超时时间，单位秒。发送消息和上传文件都会用到。
QQ_ACTION_TIMEOUT_SECONDS=30

# 通讯软件事件输出等级:
# normal = 发送最终回答、编号问题、todo、错误、后台提示、文件资源和工具开始提示；
#          工具开始提示格式为“（调用工具:工具名 参数）”，bash 为“（执行命令:命令）”。
# full   = 额外发送工具完成、round、token、MCP/Buddy 状态等调试摘要。
IM_EVENT_VERBOSITY=normal

# 群聊是否显示工具过程消息。0 表示群聊不发“调用工具/执行命令/工具完成”等过程提示；
# 私聊、最终回答、敏感工具拒绝提示、文件/表情包实际发送不受影响。
IM_GROUP_TOOL_MESSAGES=1

# 是否把思考模型的 reasoning_content 发到 QQ/微信。
# 默认关闭；开启后会发送“【思考】”状态消息，适合私聊调试，不建议在大群常开。
IM_SHOW_REASONING=0
IM_REASONING_CHUNK_CHARS=1200
IM_REASONING_MAX_CHARS=8000

# 用户回复编号后是否发“已选择: xxx”确认。0 表示静默确认。
IM_CONFIRM_QUESTION_ANSWER=1

# 表情包目录。QQ/微信模式下模型可用平台专用工具上传这里的图片作为 sticker/image。
# 旧 send_message_asset 入口保留给事件兼容，但默认不再注册给模型。
CBAGENT_STICKER_DIR=./assets/stickers

# agent 允许发送到通讯软件的本地文件大小上限，单位 MB。
CBAGENT_OUTBOUND_FILE_MAX_MB=50

# QQ/NapCat 出站文件交付方式。
# path 保持旧行为；Docker 推荐 mapped_path 或 http。
QQ_FILE_DELIVERY_MODE=path

# mapped_path 示例:
# Docker 挂载 -v /opt/cb-agent/outbound:/app/cb-agent-outbound:ro 后配置：
# QQ_FILE_HOST_PREFIX=/opt/cb-agent/outbound
# QQ_FILE_NAPCAT_PREFIX=/app/cb-agent-outbound
QQ_FILE_HOST_PREFIX=
QQ_FILE_NAPCAT_PREFIX=

# http 示例:
# QQ_FILE_HTTP_HOST=0.0.0.0
# QQ_FILE_HTTP_PORT=6200
# QQ_FILE_HTTP_PUBLIC_BASE_URL=http://宿主机内网IP:6200
QQ_FILE_HTTP_HOST=127.0.0.1
QQ_FILE_HTTP_PORT=0
QQ_FILE_HTTP_PUBLIC_BASE_URL=
QQ_FILE_HTTP_TTL_SECONDS=300
QQ_FILE_BASE64_MAX_MB=3

# QQ 图片/音频 URL 下载目录。下载成功后只把本地路径交给多模态输入层。
CBAGENT_PLATFORM_ATTACHMENT_DIR=.cbagent/platform_attachments/qq
```

启动 cb-agent：

Windows PowerShell：

```powershell
..\venv\python.exe run_agent.py --transport qq
```

Linux / macOS：

```bash
../venv/bin/python run_agent.py --transport qq
```

NapCat 需要先安装并登录 QQ。去 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载最新版本，按 NapCat 自己的说明启动后进入 WebUI。

Linux 服务器上也可以用 NapCat 安装脚本快速创建 Docker 部署：

```bash
# napcat Linux 一键配置。把 your_qq 改成实际 QQ 号。
curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh && sudo bash napcat.sh --docker y --qq your_qq --mode ws --proxy 1 --confirm
```

如果 agent 需要给 QQ 发送本地文件，建议删除安装脚本创建的旧 NapCat 容器，只保留镜像，然后手动重新创建容器并加上共享目录挂载。下面示例把宿主机 `/root/CBAGENT/cb-agent/outbound` 挂载到容器内 `/app/cb-agent-outbound`，对应 `QQ_FILE_DELIVERY_MODE=mapped_path`：

```bash
docker run -d \
  -e NAPCAT_GID=$(id -g) \
  -e NAPCAT_UID=$(id -u) \
  -e ACCOUNT=your_qq \
  -e WS_ENABLE=true \
  -p 3001:3001 \
  -p 6099:6099 \
  -v ./QQ:/app/.config/QQ \
  -v ./config:/app/napcat/config \
  -v ./plugins:/app/napcat/plugins \
  -v /root/CBAGENT/cb-agent/outbound:/app/cb-agent-outbound:ro \
  --name napcat \
  --restart=always \
  mlikiowa/napcat-docker:latest
```

cb-agent 侧配套填写：

```env
QQ_FILE_DELIVERY_MODE=mapped_path
QQ_FILE_HOST_PREFIX=/root/CBAGENT/cb-agent/outbound
QQ_FILE_NAPCAT_PREFIX=/app/cb-agent-outbound
```

在 NapCat WebUI 中打开“网络配置”，选择 **WebSocket 客户端**，让 NapCat 主动连接 cb-agent 提供的反向 WebSocket 服务。本地运行 cb-agent 和 NapCat 时，主机地址可以直接使用 `localhost` 或 `127.0.0.1`，地址填写为：

```text
ws://127.0.0.1:6199/ws
```

也可以写成：

```text
ws://localhost:6199/ws
```

注意:
```text
当napcat部署在docker时,则在webui界面上应该填使用宿主机内网 IP地址比如ws://172.17.0.1:6299/ws
```
```bash
# Linux/macOS 获取内网 IP 的示例命令
ip addr show | grep -E "inet " | grep -v 127.0.0.1
```


本地配置示例：

![NapCat WebSocket 客户端配置示例 1](img/napcat配置1.png)

![NapCat WebSocket 客户端配置示例 2](img/napcat配置2.png)

如果设置了 `QQ_ACCESS_TOKEN`，NapCat 侧也要填写相同 token。适配器支持 `Authorization: Bearer <token>`、`Authorization: <token>`，以及 URL 查询参数 `access_token=<token>`。

群聊默认 `QQ_GROUP_MODE=mention`，只有 @机器人 或使用 `QQ_WAKE_PREFIX` 前缀时才会响应。私聊默认直接响应。`QQ_ALLOWED_GROUPS` 和 `QQ_ALLOWED_USERS` 为空时不做白名单限制；填写后分别按群号和 QQ 号过滤。

QQ 模式还会做一层敏感工具门禁。`QQ_ALLOWED_USERS` 只决定谁能触发 agent 聊天，`QQ_ROOT_USERS` / `IM_ROOT_USERS` 决定谁能执行敏感工具。敏感范围包括读取或外发本地文件内容、写项目/服务器文件、非只读 bash、git 回滚/提交/推送、`bash_permission` 授权变更、memory/rag 写操作、`run_skill_script`、发送项目/服务器本地文件、敏感 MCP 工具等。未配置 root 用户时，QQ 触发的敏感工具会在执行前直接拒绝；本地 TUI/CLI 继续使用原来的权限机制。微信 OC 是当前账号里的私聊 bot，不走这套 root/普通用户门禁。

普通 QQ 用户有一个受限例外：如果用户要求生成、下载或制作新文件并发回，agent 应把新产物放到系统临时目录，Linux 通常是 `/tmp/cb-agent-outputs/`，再通过 `qqtool` 上传或发送。不要把项目源码、配置、日志、密钥、服务器隐私文件复制或移动到 `/tmp` 后发送，这属于绕过权限检查。普通用户用 bash 下载文件时也只允许 `curl/wget` 从公网 `http(s)` URL 明确写入临时目录；localhost、内网地址、`file://`、管道脚本和本地复制仍会被拒绝。

通讯软件模式会把 TUI 风格事件降级成 QQ/微信可读消息：

| Agent 事件 | 通讯软件中的表现 |
|---|---|
| `Done` | 发送最终回答 |
| `ask_user_question` | 渲染为编号问题，用户回复 `1`、`1,3`、`其他: ...` 或 `取消`；群聊中只有发起请求的用户可以确认 |
| `todo` | 渲染为简洁任务列表 |
| `Error` / `Cancelled` | 发送简短状态提示 |
| 工具开始 | 默认发送短提示：普通工具为 `（调用工具:工具名 参数）`，bash 为 `（执行命令:命令）`；参数会脱敏和截断；群聊可用 `IM_GROUP_TOOL_MESSAGES=0` 关闭 |
| 敏感工具被拒绝 | 发送 `（已拒绝敏感工具调用:...）`，真实工具不会执行 |
| 思考流 | 默认不发；`IM_SHOW_REASONING=1` 时把模型 `reasoning_content` 分段发送为 `【思考】` 状态消息 |
| 工具结束 | 默认不发；`IM_EVENT_VERBOSITY=full` 时发送耗时等调试摘要；群聊可用 `IM_GROUP_TOOL_MESSAGES=0` 关闭 |

表情包放在：

```text
assets/stickers/
```

QQ 模式会额外注册 `qqtool` 工具，CLI/TUI 不会注入它。模型需要主动执行 QQ 操作时，通过 `qqtool(funname,args)` 调用 NapCat action，例如 `send_poke`、`send_group_msg`、`upload_group_file`、`upload_private_file`、`upload_image_to_qun_album`、`get_group_member_list`、`get_login_info` 等。最终回答、思考内容、工具过程提示和 `ask_user_question` 编号问题仍由事件系统自动发送，不需要模型自己再调用 `qqtool` 补发。

`send_message_asset` 的模型入口已不再默认注册；底层资源校验和事件兼容逻辑仍保留，避免旧 transcript 或测试路径失效。普通用户只能通过 `qqtool` 操作当前会话，发送表情包目录里的图片，或发送系统临时目录里的新产物；发送项目/服务器现有文件、跨会话发消息、读取历史消息、设置资料、Ark 分享、`raw_action` 等需要 root 用户权限。文件类 funname 会复用 Docker/HTTP/base64/path 文件交付层，history/compact 只记录文件摘要，不保存二进制。QQ 图片/音频入站 URL 会尽量下载到 `.cbagent/platform_attachments/qq/`，再复用多模态附件流程；下载失败时会把 URL 作为文本提示交给模型。

文件发送现在支持多种交付模式，默认 `QQ_FILE_DELIVERY_MODE=path` 保持旧行为：直接把 cb-agent 本机路径交给 NapCat。这个模式最适合同机运行，或者 NapCat 容器内外路径完全一致的部署。

NapCat 在 Docker 中时，推荐使用 `mapped_path`。cb-agent 会先把要发送的文件复制到共享目录，再把路径改写成容器内路径交给 NapCat：

```bash
docker run ... -v /opt/cb-agent/outbound:/app/cb-agent-outbound:ro ...
```

```env
QQ_FILE_DELIVERY_MODE=mapped_path
QQ_FILE_HOST_PREFIX=/opt/cb-agent/outbound
QQ_FILE_NAPCAT_PREFIX=/app/cb-agent-outbound
```

如果不方便挂载共享卷，可以使用 `http`。cb-agent 会启动一个只读临时文件服务，给 NapCat 一个带随机 token、会过期的下载 URL：

```env
QQ_FILE_DELIVERY_MODE=http
QQ_FILE_HTTP_HOST=0.0.0.0
QQ_FILE_HTTP_PORT=6200
QQ_FILE_HTTP_PUBLIC_BASE_URL=http://宿主机内网IP:6200
QQ_FILE_HTTP_TTL_SECONDS=300
```

Docker Desktop 有时可用 `http://host.docker.internal:6200`；Linux Docker 默认不一定支持这个域名，通常直接填宿主机内网 IP 更稳。`base64` 只建议给小图片/表情包兜底，大文件会撑爆 WebSocket、日志和内存。`auto` 会按 `mapped_path -> http -> base64 -> path` 的顺序生成候选并依次尝试。

QQ 模式会按通讯会话隔离 `AgentSession`。每条 QQ 消息都会创建短生命周期 session 对象，工具系统、LLM、MCP 和 EventBus 仍在进程内共享，不会反复加载。私聊会根据 `ConversationKey(platform, kind, id)` 挂载独立本地会话目录，处理结束后追加落盘：

```text
.cbagent/platform_sessions/qq/private_<QQ号>/sessions/
```

群聊默认不持久化 history/state/transcript/compact，避免群消息过多导致本地上下文无限增长。同一个群聊或好友内部使用按会话队列顺序处理消息；不同群聊、不同好友之间可以并发处理。事件回传会通过通讯平台上下文路由到对应会话。这个隔离层不影响 TUI：TUI 仍通过 `--transport jsonrpc` 使用普通 `.cbagent/sessions/`。

### 8. 启动微信 OC

微信接入使用个人微信 OC HTTP 协议。cb-agent 主动扫码登录、长轮询 `getupdates` 收消息，并通过 `sendmessage` 与微信 CDN 上传发送文本、图片和文件。

`.env` 最小配置：

```env
WECHAT_ENABLE=1
WECHAT_BASE_URL=https://ilinkai.weixin.qq.com
WECHAT_CDN_BASE_URL=https://novac2c.cdn.weixin.qq.com/c2c
WECHAT_STATE_FILE=.cbagent/wechat/state.json
CBAGENT_PLATFORM_ATTACHMENT_DIR_WECHAT=.cbagent/platform_attachments/wechat
```

启动：

Windows PowerShell：

```powershell
..\venv\python.exe run_agent.py --transport wechat
```

Linux / macOS：

```bash
../venv/bin/python run_agent.py --transport wechat
```

第一次启动时，如果 `.env` 没有 `WECHAT_TOKEN`，终端会打印登录二维码。用手机微信扫码确认后，adapter 会把 `token`、`account_id`、`sync_buf` 和最近会话的 `context_token` 写到：

```text
.cbagent/wechat/state.json
```

后续重启会优先从这个状态文件恢复登录。这个文件等同于登录凭据，已经位于 `.cbagent` 私有运行目录内，不要提交或公开。

微信模式复用 QQ 已经搭好的通讯平台层：

| 能力 | 微信模式行为 |
|---|---|
| 最终回答 | 由 `PlatformEventRenderer` 自动发送到当前微信会话 |
| 思考内容 | `IM_SHOW_REASONING=1` 时自动分段发送 `【思考】` |
| 工具过程 | 私聊默认发送工具开始提示 |
| `ask_user_question` | 渲染成编号问题，用户回复 `1`、`1,3`、`其他: ...` 或 `取消` |
| todo | 渲染成简洁任务列表 |
| 会话隔离 | 私聊按 `wechat/private_<wxid>` 持久化；带 `group_id` 的上游消息会被忽略 |
| 权限模型 | 当前微信账号自用入口，不做管理员/普通用户分级，平台权限层直接放行 |

私聊持久化路径形如：

```text
.cbagent/platform_sessions/wechat/private_<wxid>/sessions/
```

openclaw-weixin 的 OC bot 是当前微信账号里的 `direct` 私聊 bot，不是独立机器人账号，也不是群聊机器人。cb-agent 因此只处理私聊消息；如果上游接口未来下发 `group_id`，adapter 会忽略该消息，避免在微信群里误触发。

微信模式会额外注册 `wechattool`，CLI/TUI/QQ 模式不会注入它。模型需要主动发送额外内容时，可以调用：

```text
wechattool(funname="send_text", args={"text": "一条额外消息"})
wechattool(funname="send_image", args={"path": "/tmp/cb-agent-outputs/demo.png"})
wechattool(funname="send_file", args={"path": "/tmp/cb-agent-outputs/report.pdf"})
wechattool(funname="send_typing", args={})
wechattool(funname="get_status", args={})
```

最终回答、思考内容、工具过程提示、编号问答这些事件仍由事件系统自动发送，不需要模型自己调用 `wechattool(send_text)` 补发。`get_login_info` 在微信模式下也可用，因为微信 OC 是当前账号自用入口。

微信媒体发送和 QQ/NapCat 不同：它不把本机路径交给另一个进程读取，而是 cb-agent 直接 `getuploadurl -> CDN upload -> sendmessage`。因此 Docker 路径共享问题只影响 QQ/NapCat，不影响微信。用户让 agent 生成、下载或制作要发回的文件时，仍建议把新产物放在 `/tmp/cb-agent-outputs/` 或系统临时目录，再用 `wechattool` 发送，便于审计和清理。

入站微信图片会下载到 `CBAGENT_PLATFORM_ATTACHMENT_DIR_WECHAT`，再交给多模态输入层处理。微信语音常见 SILK 编码，当前会先保存为临时文件并在 prompt 中提示路径；现有 ASR 附件管线暂不直接处理 `.silk`，后续可以接 SILK 转 WAV 后再转写。

### 9. 多模态输入

cb-agent 的用户消息协议现在是 `text + attachments[]`。附件只在当前轮请求中参与模型推理；跨轮 history、transcript、compact 和 `context_window_usage` 只保存文本摘要和附件元数据，不保存图片/音频二进制，也不保存 data URI。

CLI 和 TUI 都支持从本地文件添加附件：

```text
/attach C:\Users\cb135\Desktop\shot.png
/attachments
/detach 1
/detach all
```

TUI 额外支持从系统剪贴板读取图片：

```text
/paste-image
```

`Ctrl-V` 也会尝试读取剪贴板图片，但很多终端会自己拦截粘贴快捷键，导致 TUI 收不到按键事件。遇到“Ctrl-V 没反应”时直接使用 `/paste-image`。

剪贴板图片不会直接通过 JSON-RPC 传二进制。TUI 会先把图片保存到：

```text
~/.cb-agent/attachments/clipboard-<timestamp>.png
```

然后只把路径随 `prompt.submit` 发送给后端。后端会重新校验路径、格式、大小和 MIME。

路由规则：

| 附件 | 当前主模型能力 | 后端处理 |
|---|---|---|
| 图片 | `ConstantLLM.llm_dict[model]["image_ability"] == True` | 当前轮转为 `image_url` 发给主模型 |
| 图片 | `image_ability == False` | 调用 `utils.multimodal.MultimodalProcessor.process_image()`，把 OCR/视觉描述文本发给主模型 |
| 音频 | 任意主模型 | 调用 `process_audio()` 做 ASR，把转写文本发给主模型 |

支持的图片格式：`png`、`jpg`、`jpeg`、`webp`、`gif`、`bmp`、`tiff`、`tif`。

支持的音频格式：`mp3`、`wav`、`m4a`、`aac`、`flac`、`ogg`、`wma`。

### 10. 验证安装是否正常

Python 侧：

```bash
python run_agent.py --help
python agent_run_basic.py --help
python test/test_context_builder.py
python test/test_session_renderer.py
python test/test_transport.py
```

Windows 控制台如果出现 `✓` 编码问题，使用：

```powershell
$env:PYTHONIOENCODING="utf-8"
python test/test_context_builder.py
```

TUI 侧：

```bash
cd ui-tui
npm test
npm run build
```

Linux 部署前也可以运行只读自检脚本，提前暴露 Python、Node/npx、MCP、`rg`、剪贴板、QQ/NapCat 配置缺口：

```bash
python3 scripts/check_linux_deploy.py
```

## Linux 部署

核心 CLI/Agent 在 Linux 上可以直接运行；部署时主要要处理解释器路径、Node/npx、MCP、剪贴板桌面工具和 NapCat 文件路径这些外部依赖。

### 推荐部署步骤

```bash
cd /opt/cbAgent/cb-agent
python3 -m venv ../venv
source ../venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少填好 `LLM_MODEL_ID`、`LLM_API_KEY`、`LLM_BASE_URL`。如果要用 TUI，建议显式固定 Python 路径：

```bash
export CB_AGENT_PYTHON=/opt/cbAgent/venv/bin/python
```

启动 CLI：

```bash
../venv/bin/python run_agent.py
```

启动 TUI：

```bash
cd ui-tui
npm install
CB_AGENT_PYTHON=/opt/cbAgent/venv/bin/python npm start
```

启动 QQ / NapCat：

```bash
QQ_ENABLE=1 ../venv/bin/python run_agent.py --transport qq
```

NapCat 在同一台机器时，反向 WebSocket 客户端地址填：

```text
ws://127.0.0.1:6199/onebot/v11/ws
```

跨机器或 Docker 部署时，把 `QQ_HOST=0.0.0.0`，并强烈建议配置 `QQ_ACCESS_TOKEN`。如果 agent 需要发送本地文件或表情包，请配置 `QQ_FILE_DELIVERY_MODE`：同机/同路径可继续用默认 `path`，Docker 推荐 `mapped_path` 共享卷，不方便挂卷时用 `http` 临时 URL。

启动微信 OC：

```bash
WECHAT_ENABLE=1 ../venv/bin/python run_agent.py --transport wechat
```

第一次启动会在终端打印二维码，扫码后登录态写入 `.cbagent/wechat/state.json`。微信媒体发送走 CDN 上传，不需要像 NapCat Docker 那样配置共享目录；但服务器仍需能访问 `WECHAT_BASE_URL` 和 `WECHAT_CDN_BASE_URL`。

### MCP 和 Playwright

当前 [mcp.json](mcp.json) 里有多个 server 通过 `npx` 启动，所以 Linux 上需要安装 Node.js/npm，并保证 `npx` 在 PATH 中。Playwright MCP 首次使用浏览器能力时，通常还需要：

```bash
npx playwright install --with-deps
```

如果服务器暂时不需要 MCP，可以先用：

```bash
../venv/bin/python run_agent.py --no-mcp
```

### 剪贴板图片粘贴

TUI 的 `/paste-image` 和 `Ctrl-V` 图片粘贴依赖桌面剪贴板：

| 环境 | 依赖 | 说明 |
|---|---|---|
| Wayland | `wl-paste`（来自 `wl-clipboard`） | 有桌面会话时可用 |
| X11 | `xclip` | 有桌面会话时可用 |
| 纯 SSH / headless | 无稳定系统剪贴板 | 使用 `/attach <path>` |

服务器上最稳的多模态输入方式是先把图片或音频放到后端可读路径，再在 TUI/CLI 里输入 `/attach <path>`。

### systemd 示例

下面示例只演示 QQ transport 常驻。微信常驻时把 `ExecStart` 里的 `--transport qq` 改成 `--transport wechat`，并在 `.env` 中设置 `WECHAT_ENABLE=1`。请把路径、用户和环境变量文件改成你的实际部署值：

```ini
[Unit]
Description=cb-agent QQ transport
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cbagent
WorkingDirectory=/opt/cbAgent/cb-agent
EnvironmentFile=/opt/cbAgent/cb-agent/.env
ExecStart=/opt/cbAgent/venv/bin/python run_agent.py --transport qq
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 记忆系统

### light：默认 Markdown 记忆

light 模式不会 import / register 旧 `MemoryTool`、`RAGTool`，因此不需要 embedding、向量库或 RAG env。

目录：

| 级别 | 路径 | 用途 |
|---|---|---|
| 用户全局 | `~/.cbagent/memory/` | 长期偏好、跨项目事实 |
| 当前项目 | `.cbagent/memory/` | 当前仓库的约定、进展、参考 |

每个目录都有 `MEMORY.md` 索引，具体记忆写在同目录其它 `.md` 文件。记忆文件建议：

```markdown
---
name: 用户偏好
description: 用户希望回答使用中文并附验证命令
type: user
scope: global
---

用户偏好：回答使用中文，必要时说明验证命令。
```

light 模式不新增记忆工具。用户要求“记住/保存偏好/保存项目事实”时，agent 会通过现有 `file_read` / `file_write` 修改 Markdown 记忆文件。

### full：旧向量记忆与 RAG

完整安装后可启用：

```bash
python run_agent.py --memory-system full
```

full 模式会注册：

| 工具 | 说明 |
|---|---|
| `memory` | Episodic / Semantic / Working 三层记忆 |
| `rag` | 文档、多模态 RAG 检索 |

相关文档：

- [memory/rag/RAG_GUIDE.md](memory/rag/RAG_GUIDE.md)
- [memory/storage/VECTOR_STORE_GUIDE.md](memory/storage/VECTOR_STORE_GUIDE.md)
- [memory/storage/GRAPH_STORE_GUIDE.md](memory/storage/GRAPH_STORE_GUIDE.md)

### off：关闭长期记忆

```bash
python run_agent.py --memory-system off
```

off 模式不使用 Markdown 记忆，也不注册旧 `memory` / `rag`，但仍保留本地 session/history/state。

## 会话、工作记录与 compact

本地会话保存在：

```text
.cbagent/sessions/
```

主要文件：

| 文件 | 说明 |
|---|---|
| `index.json` | 当前 active session 指针 |
| `transcript.jsonl` | 每轮 user、final answer、工作记录、压缩 trace 审计 |
| `state.json` | rolling summary、已读文件、已改文件、最近命令、待办等 |
| `compact.json` | 最近一次 compact 快照 |
| `compactions.jsonl` | compact 审计事件 |

跨轮工具记录不会保存完整 file content 或完整 bash stdout。工具结果会压缩成 `【工作记录】`，下一轮作为普通 assistant history 进入上下文。

### 多会话

CLI 支持：

| 命令 | 说明 |
|---|---|
| `/sessions` | 列出本项目本地会话 |
| `/new` | 新建并切换到空白会话 |
| `/switch <session_id>` | 切换到指定会话 |
| `/clear` | 删除当前 active session 文件并清空内存 history |

TUI 支持 `/sessions` 面板、`/new`、`/switch <id>`。

## Buddy 宠物

Buddy 是一个可选的本地虚拟宠物功能。它不会写入会话 history，也不会参与工具轨迹；它只作为 CLI/TUI 的附属状态存在。TUI 模式下，Buddy 会显示在输入框旁边，`pet` 时出现短暂爱心动画，有本地模板反应时会显示气泡。

### 开启

在 `.env` 中设置：

```env
FEATURE_BUDDY=1
```

然后重启 CLI 或 TUI。未开启时执行 `/buddy` 会提示设置 `FEATURE_BUDDY=1`。

### 常用命令

CLI 和 TUI 都支持：

| 命令 | 说明 |
|---|---|
| `/buddy` 或 `/buddy status` | 查看当前 Buddy；还没孵化时提示先 hatch |
| `/buddy hatch` | 第一次孵化 Buddy；已有 Buddy 时不会覆盖 |
| `/buddy rehatch` | 重新孵化，替换当前 Buddy |
| `/buddy pet` | 摸摸 Buddy，触发爱心动画和一条本地反应 |
| `/buddy mute` 或 `/buddy off` | 静音并隐藏 Buddy |
| `/buddy unmute` 或 `/buddy on` | 取消静音，重新显示 Buddy |

状态文件默认在：

```text
~/.cbagent/buddy.json
```

如果要重新开始，可以用 `/buddy rehatch`。一般不建议手动编辑 `buddy.json`，除非是在调试持久化格式。

### 手动 compact

TUI 支持：

```text
/compact
```

它会把当前 `history + state` 压成 `【上下文压缩】`，保留 transcript 审计，不重绘旧屏幕，只追加一条系统提示。

### 自动 compact

模型上下文窗口在 [constant/llm/constant_llm.py](constant/llm/constant_llm.py) 中维护。后端使用模型 `max_tokens` 的 80% 作为安全上下文预算：

```python
CONTEXT_USAGE_RATIO = 0.8
```

当工具循环或下一轮请求接近该阈值时，后端会自动 compact，TUI 会追加类似“已自动压缩上下文”的系统提示。

## CLI 命令

`python run_agent.py` 的 REPL 支持：

| 命令 | 说明 |
|---|---|
| `/help` | 打印帮助 |
| `/tools` | 列出所有已注册工具 |
| `/buddy` | 查看、孵化或互动 Buddy 宠物 |
| `/attach PATH` | 添加本地图片或音频附件到下一轮消息 |
| `/attachments` | 查看待发送附件队列 |
| `/detach N\|all` | 移除一个或全部待发送附件 |
| `/skills` | 列出所有 Skill |
| `/history` | 查看当前会话 history |
| `/sessions` | 列出本地会话 |
| `/new` | 新建会话 |
| `/switch ID` | 切换会话 |
| `/clear` | 清空并删除当前 active session |
| `/ctx on\|off` | 开关 ContextBuilder |
| `/msg on\|off` | 开关每轮 messages dump |
| `/quit` | 退出 |

## 工具系统

默认 light 模式常见工具：

| 工具 | 说明 |
|---|---|
| `file_read` | 读取文件 |
| `file_edit` | 精确替换文件片段，配合 read-before-write 保护 |
| `file_write` | 写文件，配合 read-before-write 保护 |
| `bash` | 执行 shell 命令 |
| `bash_task` | 后台任务管理 |
| `bash_permission` | Bash 权限相关控制 |
| `todo` | 任务拆解与状态管理 |
| `skill` | 加载 Skill 指令 |
| `run_skill_script` | 执行 Skill 附带脚本 |
| `ask_user_question` | 工具循环中向用户提问 |
| `qqtool` | 仅 QQ transport 注册，封装 NapCat/OneBot action；用于发消息、戳一戳、上传文件/相册、查群/好友信息等 |
| `wechattool` | 仅微信 transport 注册，封装微信 OC action；用于额外发文本、图片、文件、输入状态和查看运行状态 |
| `my_advanced_search` | Tavily / SerpApi 搜索源可用时执行 Web 搜索 |
| MCP 子工具 | 从 `mcp.json` 中展开 |

full 模式额外注册：

- `memory`
- `rag`

自定义工具参考 [tools/TOOL_SYSTEM_DESIGN.md](tools/TOOL_SYSTEM_DESIGN.md)。

## MCP

项目根目录的 [mcp.json](mcp.json) 当前包含：

| server | 说明 |
|---|---|
| `amap-maps` | 高德地图 MCP，需要 `AMAP_MAPS_API_KEY` |
| `playwright` | Playwright MCP，通过 `npx` 启动；Linux 首次使用浏览器能力时可能需要 `npx playwright install --with-deps` |
| `tavily` | Tavily MCP，需要 `TAVILY_API_KEY` |
| `github` | GitHub HTTP MCP，需要 `GITHUB_PAT`；如果不需要可从 `mcp.json` 中移除或启动时加 `--no-mcp` |

启动时 `run_agent.py` 会读取 `mcp.json`，把每个 MCP server 的子工具展开注册。跳过 MCP 可用：

```bash
python run_agent.py --no-mcp
```

## Skills

Skill 是 “Prompt as Capability”：用 Markdown + YAML frontmatter 写能力说明，必要时引用脚本。

常用命令：

```text
/skills
```

相关文档：

- [skills/SKILLS_GUIDE.md](skills/SKILLS_GUIDE.md)

## 项目结构

```text
cb-agent/
├── agent/                 AgentSession、LLM 客户端、事件、transport gateway
│   ├── platforms/         通讯平台通用消息结构与事件渲染器
│   ├── qq/                NapCat / OneBot V11 适配器
│   └── wechat/            个人微信 OC HTTP 长轮询适配器
├── context/               ContextBuilder 与 Markdown memory provider
├── core/                  Message 等核心结构
├── memory/                full 模式旧记忆/RAG/存储
├── tools/                 原生工具、MCP 工具包装、ToolRegistry
├── skills/                Skill 加载与执行
├── ui-tui/                Ink/React TUI
├── constant/llm/          模型能力和上下文窗口配置
├── note/                  技术报告
├── mcp.json               MCP server 配置
├── run_agent.py           主入口：CLI / JSON-RPC gateway
├── agent_run_basic.py     简化 CLI 入口
├── requirements.txt       light 依赖
├── requirements-full.txt  full 依赖
└── pyproject.toml
```

## 设计要点

### ContextBuilder：GSSC

Gather 顺序：

1. system instructions
2. 本地 session state
3. Markdown memory state / related
4. full memory state / related
5. full RAG
6. history
7. additional packets

之后按相关性、新近性、MMR、多级优先级和 token 预算筛选。结构化 prompt 固定为：

- `[Role & Policies]`
- `[Task]`
- `[State]`
- `[Evidence]`
- `[Context]`
- `[Output]`

详细见 [context/README.md](context/README.md)。

### 单轮 messages 与跨轮 history 分离

单轮工具循环里的 OpenAI `messages` 可以包含：

- `assistant.tool_calls`
- `role=tool`
- `tool_call_id`
- 完整 tool result

这些协议字段不会跨轮直接恢复。跨轮只保存普通 user/assistant 文本、`【工作记录】` 和 `【上下文压缩】`，避免 tool_call_id 污染下一轮协议。

### TUI transport

TUI 通过 stdio JSON-RPC 与 Python 后端通信：

- stdout：NDJSON 事件/响应
- stdin：RPC 请求
- stderr：后端日志，写到 `~/.cb-agent/logs/gateway-<timestamp>.log`

详细见 [note/Stage5a stdio JSON-RPC 网关技术报告.md](<note/Stage5a stdio JSON-RPC 网关技术报告.md>)。

### 通讯平台 transport

QQ/NapCat 与微信 OC 都通过 `agent.platforms` 的平台无关消息层适配 EventBus 事件。`agent.qq` 负责翻译 OneBot V11 action，`agent.wechat` 负责翻译微信 OC HTTP API、扫码登录、长轮询和 CDN 媒体上传。这个分层让 `InboundMessage`、`OutboundMessage`、`PlatformEventRenderer`、`ConversationKey` 会话隔离、QQ 敏感权限和编号问答都能跨平台复用。

## 开发与测试

Python：

```bash
python -m py_compile agent_run_basic.py run_agent.py context/builder.py context/markdown_memory.py
python test/test_context_builder.py
python test/test_session_renderer.py
python test/test_transport.py
python -m unittest discover -s test -p "test_buddy*.py"
python -m unittest discover -s test -p "test_multimodal_input.py"
python -m unittest discover -s test -p "test_platform*.py"
python -m unittest discover -s test -p "test_qq*.py"
python -m unittest discover -s test -p "test_wechat*.py"
```

TUI：

```bash
cd ui-tui
npm test
npm test -- commands.test.ts transport.test.ts buddySprite.test.ts buddyCard.test.ts
npm run build
```

其它专项测试：

```bash
python test/test_bash_tool.py
python test/test_work_context.py
python test/test_memory_operations.py
python test/test_rag_operations.py
```

`test_memory_operations.py` 和 `test_rag_operations.py` 属于 full 能力测试，可能需要 full 依赖和相应 env。

## 技术报告

`note/` 下记录了主要演进：

- [跨轮工作上下文技术报告.md](<note/跨轮工作上下文技术报告.md>)
- [多会话隔离与TUI切换技术报告.md](<note/多会话隔离与TUI切换技术报告.md>)
- [项目级会话隔离实现思路.md](<note/项目级会话隔离实现思路.md>)
- [Compact上下文压缩命令技术报告.md](<note/Compact上下文压缩命令技术报告.md>)
- [轻量Markdown记忆系统技术报告.md](<note/轻量Markdown记忆系统技术报告.md>)
- [Stage5a stdio JSON-RPC 网关技术报告.md](<note/Stage5a stdio JSON-RPC 网关技术报告.md>)
- [Bash 权限弹窗走 UI 通道技术报告.md](<note/Bash 权限弹窗走 UI 通道技术报告.md>)
- [Bash 工具 UI 输出预览字段技术报告.md](<note/Bash 工具 UI 输出预览字段技术报告.md>)
- [本地搜索与导航工具技术报告.md](<note/本地搜索与导航工具技术报告.md>)
- [多模态输入与上下文管理技术报告.md](<note/多模态输入与上下文管理技术报告.md>)
- [QQ与通讯平台事件适配技术报告.md](<note/QQ与通讯平台事件适配技术报告.md>)
- [微信OC接入技术报告.md](<note/微信OC接入技术报告.md>)

## 常见问题

### 启动时报 `LLM_MODEL_ID` 找不到

检查 `.env` 中的 `LLM_MODEL_ID` 是否在 [constant/llm/constant_llm.py](constant/llm/constant_llm.py) 登记。

### TUI 启动后 Python 后端退出

查看日志：

```text
~/.cb-agent/logs/gateway-<timestamp>.log
```

也可以指定 Python：

```bash
CB_AGENT_PYTHON=/path/to/python npm start
```

### `/tools` 里没有 `memory` 和 `rag`

这是默认 light 模式的预期行为。要使用旧向量记忆/RAG：

```bash
pip install -e ".[full]"
python run_agent.py --memory-system full
```

### Web 搜索不可用

`my_advanced_search` 使用核心依赖里的 `requests` 直连 Tavily 或 SerpApi，不需要额外 SDK。配置任意一个 API Key 后重启即可：

```env
TAVILY_API_KEY=...
SERPAPI_API_KEY=...
```

对应 Python 包在 full 依赖中。

### full RAG/Memory 依赖太重

使用默认 light 模式即可。Markdown 记忆不需要 embedding、Qdrant、Neo4j 或本地模型。

## License

MIT

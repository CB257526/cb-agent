# cb-agent ✨

<p align="center">
  <b>让你的 LLM 长出双手双脚，真的"做到"点什么 (｀・ω・´)</b><br>
  <i>一个会写代码、能调工具、能上 QQ 微信的 LLM Agent 框架</i>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/status-%E2%9C%A8%20%E7%94%9F%E4%BA%A7%E5%8F%AF%E7%94%A8-brightgreen" alt="Status">
</p>

<p align="center">
  🧠 <b>零向量依赖即开即用</b> · 🚀 <b>Section/Boundary 静态缓存命中拉满</b><br>
  📱 <b>QQ / 微信 / TUI / CLI 全平台制霸</b> · 🧩 <b>MCP / Skills / Hooks / Plan Mode 随便扩展</b>
</p>

---

## 📖 这里有什么？

> cb-agent 是一个**工具箱塞满、四肢发达、头脑也不简单的 coding Agent** ✨
> 不只让大模型"能说会道"，更让它**真的动手干活** ( `･ω･)ﾉ

✨ 默认走轻量 Markdown 记忆，**零向量库、零 embedding、零外部服务**就能跑起来。
✨ 想要更猛的？设置 `CBAGENT_ENABLE_FULL_MEMORY=1` 解锁完整向量记忆与 RAG。
✨ 动手前先做计划？切 **Plan Mode** 先写方案，批准了再撸袖子干 (`･ω･)ﾉ

看看它能帮你做什么……(｀・ω・´) ⬇️

---

## 🚀 一分钟！就能跑起来！

<details open>
<summary><b>Windows (PowerShell)</b> 🪟</summary>

```powershell
# 1. 拿到项目 (∩´∀｀)∩
git clone <your-fork-url> cb-agent
cd cb-agent

# 2. 搓个虚拟环境
python -m venv ..\venv
..\venv\Scripts\Activate.ps1

# 3. 装依赖 ✨
pip install -r requirements.txt; pip install -e .

# 4. 配 .env
Copy-Item .env.example .env

# 需要提前检查bun是否安装
bun --version
#没有安装bun，先安装bun https://bun.sh/
powershell -c "irm bun.sh/install.ps1 | iex"

# 5. 启动！！ヽ(✧∀✧)ﾉ
cd .\ui-otui\
bun install
bun start
```
</details>

<details>
<summary><b>macOS / Linux</b> 🍎🐧</summary>

```bash
# 1. 拿到项目 (∩´∀｀)∩
git clone <your-fork-url> cb-agent
cd cb-agent

# 2. 搓个虚拟环境
python3 -m venv ../venv
source ../venv/bin/activate

# 3. 装依赖 ✨
pip install -r requirements.txt && pip install -e .

# 4. 配 .env
cp .env.example .env

# 需要提前检查bun是否安装
bun --version
#没有安装bun，先安装bun https://bun.sh/
curl -fsSL https://bun.sh/install | bash

# 5. 启动！！ヽ(✧∀✧)ﾉ
cd ./ui-otui/
bun install
bun start
```
</details>

> 详细的安装姿势请看 [📚 详细安装姿势](docs/详细安装姿势.md) 喵~

---

## 💫 功能亮点

| 能力 | 说明 |
|---|---|
| 🔄 多轮 Function Calling | 流式 tool_calls 分片累积、tool result 回灌 |
| ⚡ 高缓存命中上下文 | System message 纯静态化，动态内容塞独立 user 消息；Section 级 LRU 缓存（100 条），跨轮复用 provider KV cache，**快就一个字** |
| 🎣 Hooks 生命周期钩子 | `SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `PreCompact` / `Stop`，双向可阻断，想怎么拦截就怎么拦截 |
| 📋 Plan Mode 协作模式 | 「先计划后执行」—— plan 阶段只暴露只读工具，批准后自动切 execute；双层防护（prompt 层 + 服务端硬拒绝），剁手都改不了东西 |
| 📝 三层 Markdown 记忆 | 全局/项目/短期，默认启用，**零向量依赖** |
| 🧠 旧向量记忆/RAG | 可选 `--memory-system full`，Episodic/Semantic/Working 三层全开 |
| 🔀 多会话隔离 | 新建 / 切换 / 清理，不同会话 history/state 互不打扰 |
| 🎨 多模态输入 | 图片直接发给多模态模型 / 纯文本模型自动 OCR；音频直接 ASR |
| 🖥️ TUI / OTUI | Ink/React 版 + OpenTUI+Solid.js 重构版，**颜值在线** |
| 💬 QQ / NapCat | OneBot V11 反向 WebSocket，群聊私聊隔离，敏感权限门禁 (｀・ω・´) |
| 💬 微信 OC | HTTP 长轮询，扫码登录嗖嗖快，CDN 媒体上传 |
| 🔌 MCP | 读 `mcp.json` 起 MCP server，工具自动展开 |
| 📜 Skills | Markdown 声明式工作流 + 附带脚本，**想加啥加啥** |

---

## 🖼️ 长这样！

<p align="center">
  <img src="img/opentui界面.png" width="720" alt="OTUI 界面"><br>
  <em>✨ OTUI — 三栏布局，左侧对话、右侧 Sidebar、底部状态栏</em>
</p>

<br>

<p align="center">
  <img src="img/真实素材2-qq1.jpg" width="320" alt="QQ 群聊">
  <img src="img/真实素材2-微信1.jpg" width="320" alt="微信私聊">
  <br>
  <em>💬 QQ 群聊 &nbsp;&nbsp;&nbsp; 📱 微信私聊</em>
</p>

<br>

<p align="center">
  <!-- 项目宣传视频 -->
  <a href="https://v.douyin.com/Io2Ic8s2QQA/" target="_blank">
  <br>
  <em>🎬 项目宣传视频 — 一分半钟带你了解 cb-agent 能做什么！</em>
</p>

---

## 📚 文档索引 (づ｡◕‿‿◕｡)づ

| 文档 | 里面有什么 |
|---|---|
| [📖 项目介绍](docs/项目介绍.md) | 项目定位、架构图、完整能力表、平台展示图片 |
| [📦 部署与配置](docs/部署与配置.md) | 环境准备、安装（light/full）、.env 配置、CLI/TUI/OTUI/QQ/微信/Linux 启动 |
| [🔧 功能详解](docs/功能详解.md) | 记忆系统（light/full/off）、会话 & compact、工具系统、MCP、Skills、CLI 命令 |
| [🧑‍💻 开发指南](docs/开发指南.md) | 项目结构、五大子系统、缓存命中设计、Hooks、Plan Mode、测试命令、技术报告、FAQ |

---

## 📜 License

MIT

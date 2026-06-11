# BongoCat 兼容轻量桌宠系统技术报告

## 背景

旧版 Buddy 是 CLI/TUI 内部的 ASCII/状态组件，无法直接使用社区已有的 BongoCat 桌宠素材，也不适合作为未来桌面端的原生桌宠基础。早期方案曾尝试将 BongoCat/Tauri 项目整体接入，但依赖链过重，需要 Rust、Node、Tauri 构建环境，并且对 cb-agent 的启动、分发和调试成本都不友好。

本次改造改为保留 BongoCat 的素材组织与交互思路，用 cb-agent 内置的轻量 Python sidecar 实现桌面浮窗、Live2D 渲染和输入事件转发。这样用户可以继续导入 BongoCat 风格的 Live2D 素材目录，同时不再需要构建外部 Tauri 程序。

## 目标

- 删除旧 Buddy 入口，统一改为 `/pet` 桌宠命令。
- 支持直接导入 BongoCat 风格 Live2D 素材目录，典型结构包含 `*.model3.json`、`.moc3`、textures、physics、display info、`resources/cover.png` 和键盘/手柄叠图资源。
- 支持 spritesheet 包，为后续 AI 生成桌宠素材保留轻量格式。
- 桌宠运行时由独立 Python 进程承载，cb-agent 主进程只负责安装、选择、启动、停止和状态同步。
- 鼠标、键盘、滚轮缩放等桌面交互由 runtime 直接监听或由 WebView DOM 回传，不依赖 TUI 命令持续调整。
- 桌宠库支持命令安装，也支持用户直接把素材目录放入 `~/.cbagent/pets/` 后在 `/pet launch` 时被识别。

## 架构

### 包管理层

`agent/pet.py` 负责宠物包识别、安装、选择和 runtime 控制。包识别规则如下：

- 目录内存在 `pet.json` 且 `renderer` 为 `spritesheet` 时，按 spritesheet 包处理。
- 目录内存在 `*.model3.json` 时，按 BongoCat-compatible Live2D 包处理。
- Live2D 包会检查 `Moc`、`Textures` 和 `resources/cover.png`，并根据 `resources/right-keys` 粗略推断 `standard`、`keyboard` 或 `gamepad` 模式。

安装命令 `/pet install <folder>` 会将素材目录复制到 cb-agent 的宠物库，并写入必要的本地元信息。用户也可以跳过命令，直接把素材目录放入 `~/.cbagent/pets/<name>/`；后续 `/pet list`、`/pet select`、`/pet launch` 会按同一套检测逻辑读取。

### 运行时层

`agent/pet_runtime.py` 是独立 sidecar：

- 使用 `pywebview` 创建透明、置顶、无边框窗口。
- 使用 Qt WebEngine 承载本地 Live2D WebGL 页面。
- 用内置 HTTP 服务器向 WebView 提供素材文件，避免直接 file URL 的跨域和路径编码问题。
- 使用 `pynput` 监听全局鼠标与键盘事件。
- 使用 stdio JSON-RPC 接收主进程的 `load`、`show`、`hide`、`set_state`、`shutdown` 等控制消息。
- 桌宠尺寸写入 `~/.cbagent/pet/state.json`，重启后保留。

### 渲染层

`agent/pet_web_src/renderer.ts` 负责浏览器侧渲染逻辑，构建产物放在 `agent/pet_web/`：

- Live2D 由 `pixi.js` 和 `pixi-live2d-display` 加载。
- 模型尺寸优先使用 Live2D 内部 local bounds，避免读取被缩放或被动作污染后的 `model.width`。
- 鼠标、按键状态被映射到 BongoCat 常见参数，例如鼠标方向、左右键按下、键盘键位和 agent 活动状态。
- `wheel` 事件会通过 `window.pywebview.api.petWheel(deltaY)` 回传 Python runtime，实现鼠标停在桌宠窗口上时直接滚轮缩放。

## 关键行为

### Live2D 兼容

运行时不要求素材来自 cb-agent 专用格式，只要目录符合 BongoCat 常见 Live2D 模型结构即可加载。`*.model3.json` 内的 `FileReferences.Moc`、`Textures`、`Physics`、`DisplayInfo`、`Motions`、`Expressions` 会按 Cubism 包的相对路径解析。

### 透明背景

早期尝试直接修改 Qt widget 透明属性会导致崩溃，且纯透明背景在部分 WebEngine/Windows 组合中容易出现白底。本次实现采用黑色窗口背景配合 WebGL 透明画布和 Windows layered color key，避免把 Live2D 自身的桌面、键盘或背景参数强行透明化。

默认情况下 runtime 不会自动隐藏模型部件，也不会自动把疑似桌面/键盘参数改成半透明。只有素材 manifest 显式声明对应字段时才会执行，避免把 BongoCat 素材里的键盘、桌面或装饰层误处理成透明。

### 输入事件

全局鼠标移动、点击和键盘输入由 `pynput` 监听，并转发给 WebView 中的 Live2D 参数。滚轮缩放有两条路径：

- 鼠标在桌宠窗口内时，由 DOM `wheel` 事件回传 runtime，日志会出现 `dom wheel resized`。
- 全局监听作为兜底，命中窗口区域时会记录 `pynput wheel resized`。

如果桌宠窗口只显示在任务栏但屏幕不可见，runtime 默认使用固定初始位置 `(80, 80)`，并在加载前先显示窗口，避免隐藏窗口时 WebGL 初始化被阻塞。

### 进程生命周期

cb-agent 主进程通过 `PetRuntimeController` 启动 sidecar，并等待日志中的 `pet runtime: ready`。如果 runtime 启动失败，会提示缺失依赖或日志路径。手动关闭或异常退出后，控制层会在状态查询时回收进程句柄，避免主进程继续认为桌宠正在运行。

## CLI/TUI 命令

`/pet` 取代旧 `/buddy`，主要子命令包括：

- `/pet status`：查看 runtime、可见性、当前活动和当前宠物。
- `/pet list`：列出已安装或直接放入库目录的宠物。
- `/pet install <folder>`：从外部素材目录安装到本地宠物库。
- `/pet select <id>`：选择当前宠物。
- `/pet launch`：启动 runtime 并加载当前宠物。
- `/pet show` / `/pet hide`：显示或隐藏桌宠窗口。
- `/pet quit`：关闭 runtime。
- `/pet uninstall <id>`：从本地宠物库移除宠物。

## 依赖

桌宠不再需要 Rust、Node 或 Tauri。使用项目 Python 环境安装 `requirements.txt` 即可。运行时依赖主要包括：

- `pywebview`
- `PySide6`
- `PySide6-QtWebEngine`
- `qtpy`
- `pynput`
- `json5`

TUI 默认会优先使用项目父目录的 `../venv`，因此建议按 README 建立并安装依赖后再启动 TUI。

## 测试

本次改造新增了 `test/test_pet.py`，覆盖：

- BongoCat Live2D 包识别与缺失资源诊断。
- spritesheet 包识别。
- 直接放入宠物库目录后的发现逻辑。
- runtime 状态、选择、安装、卸载命令行为。
- 默认不自动透明化或隐藏 Live2D 模型部件。
- 鼠标停留在桌宠窗口区域时滚轮缩放状态更新。

同时删除了旧 Buddy 的单元测试和 TUI Buddy 组件测试，并更新 transport/session/system prompt 相关测试，确保旧 Buddy 入口不再注入。

## 后续方向

- 补充更多 BongoCat 社区素材的兼容样例，覆盖键盘、手柄、标准模式差异。
- 将 spritesheet renderer 打磨为和 Live2D 同级的稳定格式，便于 AI skill 直接生成可导入的桌宠包。
- 为未来桌面端复用同一套包检测、runtime 协议和渲染页面，避免 CLI/TUI 与桌面端各自实现一套桌宠系统。

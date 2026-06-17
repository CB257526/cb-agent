# Agent Hooks 机制技术报告

## 概述

为 cb-agent 引入对齐 Claude Code 的 hooks 机制，让用户可在 agent 生命周期关键节点
自动执行外部命令，命令可以**阻止工具执行**、**改写工具输入**或**向模型注入额外上下文**。

解决的问题：
- agent 行为缺少用户可配置的拦截/扩展点，定制只能改源码
- 已有 EventBus 是单向广播（不收返回值、不影响主流程），无法承载「拦截/改写」语义
- 工具执行前只有内置的平台权限检查，没有项目级的可配置策略层

## 核心设计决策

### 决策一：独立 HookManager，不复用 EventBus

| | EventBus | HookManager |
|---|---|---|
| 方向 | 单向（广播给前端） | 双向（收集决策影响主流程） |
| 返回值 | 无（emit 不收返回） | 有（HookOutcome 决策对象） |
| 异常处理 | 吞掉、隔离 | 区分阻塞/非阻塞 |
| 阻塞性 | 禁止阻塞 | 同步阻塞等命令执行完 |
| 用途 | 通知前端渲染 | 拦截/改写 agent 行为 |

EventBus 的设计契约明确写着「事件是只读快照，订阅者不要回写」「订阅者绝不能阻塞 emit」，
与 hooks 需要的双向可阻断语义根本冲突。因此新建独立 HookManager。

**协作方式**：「控制流走 HookManager，可见性走 EventBus」——HookManager 触发 hook 时
反过来通过 EventBus emit `HookStarted` / `HookCompleted`，让 CLI/TUI 能看到 hook 在跑、
是否拦截。

### 决策二：配置放 `.cbagent/hooks.json`

与现有 `.cbagent/permissions.json` 平级，单文件单职责。第一版只读项目级。

### 决策三：第一版只支持 `type: "command"`

shell 默认跟随系统，可在 handler 里显式指定 `shell: "powershell"`。

### 决策四：6 个有真实触发点的事件

用户原列了 8 个，但探索确认项目**无「Agent 调用子 Agent」的递归机制**（只有 Bash 工具的
`is_subagent` cwd 隔离），故 `SubagentStart` / `SubagentStop` 第一版不做，待引入子 agent
工具后再加。实现的 6 个：

| 事件 | 触发时机 | matcher 字段 | 能力 |
|---|---|---|---|
| SessionStart | 本会话首个 Prompt（一次） | source（startup） | 注入上下文 |
| UserPromptSubmit | 每次提交 Prompt | 无 matcher | 拦截 / 注入上下文 |
| PreToolUse | 工具执行前 | 工具名 | 阻止工具 / 改写输入 |
| PostToolUse | 工具成功完成后 | 工具名 | 注入上下文 |
| PreCompact | 上下文即将压缩前 | trigger（auto/manual） | 仅通知 |
| Stop | 整轮回答收尾前 | 无 matcher | 仅通知 |

## 新增文件

### agent/hooks/ 子系统

| 文件 | 职责 |
|------|------|
| `matcher.py` | matcher 匹配函数，三段式规则（全匹配/精确竖线列表/正则） |
| `config.py` | hooks.json 加载 + 数据结构（HookHandler/HookGroup），全程容错 |
| `manager.py` | HookManager 核心：触发、执行 command、合并决策 |
| `__init__.py` | 导出 HookManager / HookOutcome / load_hooks_config / matches 等 |
| `HOOKS_GUIDE.md` | 中文使用文档（配置示例、事件表、退出码语义） |

核心数据结构：

```python
@dataclass(frozen=True)
class HookHandler:        # 单个处理器（第一版仅 command）
    type: str = "command"
    command: str = ""
    timeout: float = 60.0
    shell: Optional[str] = None    # None=跟随系统；"bash"/"powershell"

@dataclass(frozen=True)
class HookGroup:           # 一个 matcher 组
    matcher: str = "*"
    handlers: List[HookHandler] = field(default_factory=list)

HooksConfig = Dict[str, List[HookGroup]]   # 事件名 -> [HookGroup]

@dataclass
class HookOutcome:         # 一组 hook 执行后的合并决策
    blocked: bool = False
    block_reason: str = ""
    updated_input: Optional[Dict] = None
    additional_context: str = ""
    stop: bool = False
```

matcher 三段式规则：

| 写法 | 含义 |
|---|---|
| `"*"` / `""` / 省略 | 全匹配 |
| 仅 `[A-Za-z0-9_\|]` | 精确串，或 `\|` 分隔列表（如 `bash\|file_edit`） |
| 含其它字符 | 当作正则（re.search，如 `mcp__.*`） |

### test/test_hooks.py

19 个测试（unittest 风格，因 venv 未装 pytest），覆盖：
- matcher 四类规则（全匹配/精确/竖线列表/正则/非法正则不崩）
- config 加载容错（缺失文件/坏 JSON/非对象根/不支持的事件名与 handler 类型）
- HookManager.fire 决策合并（exit 0/2/其它、stdout JSON 的 deny/updatedInput/
  additionalContext/continue=false、matcher 未命中跳过、超时非阻塞）

## 修改文件

### agent/events.py

新增 `HookStarted` / `HookCompleted` 两个 dataclass，加进 `Event` Union 和 `__all__`。

### agent/executor.py

1. `__init__` 新增 `hook_manager=None` 参数
2. `_run_one()` 平台权限检查通过后、emit ToolStart 前：触发 **PreToolUse**。
   blocked 时复用平台权限同款「回灌结构化拒绝消息」模式（新增 `_hook_blocked_payload`），
   updated_input 非空则改写 args
3. `_run_one()` 工具执行成功后：触发 **PostToolUse**。additional_context 通过
   新增 `_append_hook_context` 追加进 result 的 `_hook_context` 字段（零协议改动）

### agent/session.py

1. `__init__` 新增 `hook_manager=None` 参数 + `_session_start_fired` 去重标志
2. `_chat_impl()` 开头：触发 **SessionStart**（仅首个 Prompt）和 **UserPromptSubmit**。
   UserPromptSubmit blocked 直接返回拒绝原因不进 LLM；两者的 additional_context
   追加进 system_instructions 注入模型
3. `_auto_compact_history()` 真正压缩前：触发 **PreCompact**（reason 归一化为 auto/manual）
4. `_chat_impl()` emit Done 前：触发 **Stop**（通知类）

### run_agent.py

`AgentRunner.__init__` 在 ToolExecutor 之前装配 HookManager（读 `.cbagent/hooks.json`），
透传给 ToolExecutor 和 `_create_agent_session` 里的 AgentSession。

### agent/renderers/cli.py

订阅 `HookStarted` / `HookCompleted`，渲染「hook 运行中 / 拦截了该操作 / 注入了上下文」。

## hook 命令的输入输出协议

### 输入（stdin JSON，对齐 Claude Code）

```json
{
  "session_id": "...",
  "cwd": "c:\\Users\\cb135\\Desktop\\cbAgent\\cb-agent",
  "hook_event_name": "PreToolUse",
  "tool_name": "bash",
  "tool_input": { "command": "rm -rf /tmp/x" }
}
```

### 输出：exit code

| exit code | 行为 |
|---|---|
| 0 | 成功；有 stdout JSON 则按其决策处理 |
| 2 | 阻塞错误；stderr 作为原因反馈模型并阻止操作 |
| 其它非零 | 非阻塞错误；记 warning 后继续 |

### 输出：stdout JSON

解析 `hookSpecificOutput.permissionDecision`（deny→阻止）、`updatedInput`（改写输入）、
`additionalContext`（注入上下文），以及顶层 `decision: block`、`continue: false`。

## 实现中发现并修复的两个 Windows 真实 bug

端到端真实子进程测试（非 mock）暴露出两个仅在 Windows 真实执行时才出现的 bug，
若不修，所有中文 hook 在 Windows 上都会失效：

### bug 1：GBK 解码崩溃

`subprocess.run(text=True)` 在 Windows 默认用 GBK 解码子进程输出，hook 输出 UTF-8
中文时抛 `UnicodeDecodeError`，整个读取线程崩溃。

修复：`_run_command` 显式 `encoding="utf-8", errors="replace"`。与仓库顶部
「Windows 控制台 UTF-8 reconfig」是同一类问题。

### bug 2：WSL bash 抢占 Git Bash

Python `subprocess` 解析 `bash` 时，PATH 里 `C:\Windows\System32\bash.exe`（WSL 启动器）
排在 Git Bash 之前。未装 WSL 发行版时它失败并输出 UTF-16 提示，污染 hook 结果。
（注意：这与交互终端用的 Git Bash 不是同一个 bash。）

修复：`manager.py` 新增 `_find_git_bash()`，Windows 下从 git 安装位置推断 +
候选路径列表显式定位 Git Bash，进程内缓存为 `_WIN_BASH`；`_resolve_shell` 在 Windows
上把 bash/跟随系统都解析到该真实路径。

## 验证

| 验证项 | 结果 |
|---|---|
| test/test_hooks.py（19 个单测） | 全过 |
| 集成测试（PreToolUse 拦截 + PostToolUse 注入 + 事件 emit） | 通过 |
| 真子进程 E2E（真实 hooks.json + hook 脚本 + 中文输出） | 通过 |
| test/test_executor.py（45 个回归） | 全过 |
| file_edit / bash / ask_user_question（共 100 个回归） | 全过 |
| run_agent 导入 + 签名校验 | 通过 |

## 向后兼容

- ToolExecutor / AgentSession 新增的 `hook_manager` 参数默认 None
- 无 `.cbagent/hooks.json` 时 HookManager.enabled=False，所有触发点 has_event 短路返回，
  agent 行为与引入该功能前完全一致，零回归、零额外开销

## 后续扩展

- SubagentStart / SubagentStop（待项目引入子 agent / Task 工具）
- handler 类型：python（进程内回调）、http、prompt
- 用户级 `~/.cbagent/hooks.json` 分层合并
- `if` 条件（权限规则语法过滤）、`async` 异步 hook
- PreCompact 的 exit 2 阻止压缩、Stop hook 的 stop_hook_active 防循环

# cb-agent Hooks 使用指南

Hooks 让你在 agent 生命周期的关键节点自动执行外部命令，命令可以**阻止工具执行**、
**改写工具输入**或**向模型注入额外上下文**。设计上对齐 Claude Code 的 hooks。

## 与 EventBus 的区别

cb-agent 已有 EventBus（`agent/event_bus.py`），但它是**单向广播**——只把事件推给前端
渲染，不收返回值、不影响主流程。Hooks 需要的是**双向、可阻断**的能力，因此由独立的
`HookManager`（`agent/hooks/`）负责。两者分工：

- **控制流走 HookManager**：收集 hook 的决策，影响工具/会话行为
- **可见性走 EventBus**：hook 运行时 HookManager 反过来 emit `HookStarted` /
  `HookCompleted`，让 CLI/TUI 能看到 hook 在跑、是否拦截

无 `hooks.json` 配置时，hooks 完全关闭，agent 行为与未引入该功能时一致。

## 配置文件

配置位于项目级 `.cbagent/hooks.json`（与 `.cbagent/permissions.json` 平级）。结构：

```json
{
  "hooks": {
    "<事件名>": [
      {
        "matcher": "bash|file_edit",
        "hooks": [
          { "type": "command", "command": "python .cbagent/hooks/check.py", "timeout": 30 }
        ]
      }
    ]
  }
}
```

三层结构：

1. **事件名** → 见下方「支持的事件」
2. **matcher 组** → `matcher` 决定哪些调用触发；`hooks` 是要跑的 handler 列表
3. **handler** → 第一版只支持 `type: "command"`

## 支持的事件

| 事件 | 触发时机 | matcher 匹配字段 | 能力 |
|---|---|---|---|
| `SessionStart` | 本会话首个 Prompt 提交时（一次） | `source`（固定 `startup`） | 注入上下文 |
| `UserPromptSubmit` | 每次提交 Prompt | 无 matcher（总触发） | 拦截输入 / 注入上下文 |
| `PreToolUse` | 工具执行前 | 工具名（如 `bash`） | 阻止工具 / 改写输入 |
| `PostToolUse` | 工具成功完成后 | 工具名 | 注入上下文 |
| `PreCompact` | 上下文即将压缩前 | `trigger`（`auto` / `manual`） | 仅通知（导出/保存） |
| `Stop` | 整轮回答收尾前 | 无 matcher（总触发） | 仅通知（生成报告/清理） |

> `SubagentStart` / `SubagentStop` 已用于 `agent` / `agent_task` 子 Agent 流程。所有 hook stdin
> 都会带上 `agent_scope`、`subagent_id`、`subagent_type`、`parent_session_id`、`task_id`
> 和 `run_in_background`，子 Agent 内部生命周期与工具 hooks 使用 `agent_scope: "subagent"`。

## matcher 写法

| 写法 | 含义 | 例子 |
|---|---|---|
| `"*"` / `""` / 省略 | 全匹配 | 每次该事件都触发 |
| 仅字母数字下划线竖线 | 精确串，或 `\|` 分隔列表 | `bash`、`file_edit\|file_write` |
| 含其它字符 | 当作正则（`re.search`） | `mcp__.*`、`^Notebook` |

## handler 字段（command 类型）

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 是 | 目前只支持 `"command"` |
| `command` | 是 | 要执行的 shell 命令 |
| `timeout` | 否 | 超时秒数，默认 60。超时按非阻塞错误处理 |
| `shell` | 否 | `null`=跟随系统（Windows 走 Git Bash，POSIX 走 sh）；`"bash"` / `"powershell"` 显式指定 |

## hook 命令的输入与输出

### 输入（stdin）

hook 命令通过 **stdin** 收到一段 JSON，含通用字段加事件相关字段：

```json
{
  "session_id": "...",
  "cwd": "c:\\Users\\cb135\\Desktop\\cbAgent\\cb-agent",
  "hook_event_name": "PreToolUse",
  "tool_name": "bash",
  "tool_input": { "command": "rm -rf /tmp/x" }
}
```

各事件额外字段：

- `PreToolUse`：`tool_name`、`tool_input`
- `PostToolUse`：`tool_name`、`tool_input`、`tool_response`
- `UserPromptSubmit`：`prompt`
- `SessionStart`：`source`
- `PreCompact`：`trigger`、`reason`
- `Stop`：`last_assistant_message`

### 输出：exit code

| exit code | 行为 |
|---|---|
| `0` | 成功。若有 stdout JSON 则按其决策处理 |
| `2` | 阻塞错误。stderr 作为原因反馈给模型，并阻止该操作（PreToolUse 拦截工具） |
| 其它非零 | 非阻塞错误。记 warning 后继续，不影响主流程 |

### 输出：stdout JSON（更精细的控制）

stdout 是合法 JSON 时，按以下字段解析（对齐 Claude Code）：

```json
{
  "continue": true,
  "decision": "block",
  "reason": "...",
  "hookSpecificOutput": {
    "permissionDecision": "deny",
    "permissionDecisionReason": "...",
    "updatedInput": { "command": "ls -la" },
    "additionalContext": "项目使用 4 空格缩进"
  }
}
```

- `hookSpecificOutput.permissionDecision == "deny"` → 阻止工具（PreToolUse）
- `hookSpecificOutput.updatedInput` → 改写工具输入（PreToolUse）
- `hookSpecificOutput.additionalContext` → 注入给模型的额外上下文（PostToolUse / UserPromptSubmit / SessionStart）
- 顶层 `decision == "block"` → 阻止
- 顶层 `continue == false` → 让整个 chat 收尾

## 例子

### 例 1：拦截危险的 rm 命令

`.cbagent/hooks.json`：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          { "type": "command", "command": "python .cbagent/hooks/block_rm.py", "timeout": 10 }
        ]
      }
    ]
  }
}
```

`.cbagent/hooks/block_rm.py`：

```python
import json
import sys

data = json.load(sys.stdin)
command = (data.get("tool_input") or {}).get("command", "")

if "rm -rf" in command:
    print(json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "rm -rf 被本地 hook 拦截",
        }
    }, ensure_ascii=False))
# 否则 exit 0 无输出 = 放行
```

### 例 2：写文件后自动格式化（通知类）

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "file_edit|file_write",
        "hooks": [
          { "type": "command", "command": "ruff format ." }
        ]
      }
    ]
  }
}
```

### 例 3：每次提交 Prompt 注入项目约定

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "echo '{\"hookSpecificOutput\":{\"additionalContext\":\"本项目注释用中文，不用 emoji\"}}'" }
        ]
      }
    ]
  }
}
```

## 注意事项

- **配置容错**：`hooks.json` 缺失、JSON 损坏、字段类型不符都不会让 agent 崩溃，
  只会记 warning 并退化为「该部分不生效」。
- **不支持的事件名/handler 类型**：加载期记 warning 并跳过（避免拼写错误静默失效）。
- **并发**：`PreToolUse` / `PostToolUse` 可能在工具并发执行的 worker 线程里触发，
  command 走独立子进程，线程安全。但 hook 会计入工具执行耗时，务必设合理 `timeout`。
- **第一版限制**：PreCompact / Stop 只做通知用途，不支持阻止压缩 / 阻止收尾；
  仅支持 `command` 类型 handler；只读项目级配置，无用户级合并。这些都在后续扩展计划内。

# Subagent 子系统

`subagent/` 只维护角色定义、权限和任务生命周期；LLM/工具装配入口仍在
`tools/tools/subagent_tool.py`，主程序只负责依赖注入。

## 内置角色

每个内置角色必须放在 `subagent/list/` 的独立 Python 文件中，并导出一个
`SubagentDefinition`。随后在 `subagent/list/__init__.py` 的
`BUILTIN_SUBAGENTS` 中注册。角色文件负责：

- 中文系统提示词和职责边界；
- 模型可见工具名单；
- 最大工具轮次；
- Bash 模式、工作区写入和派生权限。

首版内置 `general`、`explore`、`reviewer`、`worker`，全部禁止继续派生子代理。

## 自定义角色

用户级定义放在 `~/.cbagent/agents/*.md`，项目级定义放在
`.cbagent/agents/*.md`。项目定义覆盖同名用户定义，用户定义覆盖内置定义。

```markdown
---
name: api-reviewer
description: API 兼容性审查
tools: [file_read, glob, grep, ls, bash]
max_turns: 20
permissions:
  bash_mode: read_only
  workspace_write: false
  external_paths: false
---

你负责检查 API 兼容性、错误码和测试覆盖，不修改文件。
```

`bash_mode` 只能是 `deny`、`read_only` 或 `inherit`。省略 `tools` 时使用
`file_read/glob/grep/ls` 最小只读集合。未知角色会直接报错，不会回退到通用角色。

角色权限在工具执行器层再次校验。内置角色不能访问工作区外路径、其它会话的
`.cbagent` 运行数据、真实 `.env`/凭据文件或 `.git` 内部文件；项目级
`.cbagent/agents` 和 `.cbagent/skills` 保持可访问，便于扩展角色与技能。
Worker 的 Bash 仍继承父会话 allowlist，但不允许启动后台/脱离进程。每个任务使用
独立工作目录上下文和工具结果目录，主 Agent 的 `cd`、图片缓冲与大输出不会串到
其它子代理。

这些限制属于工具执行层的能力边界，不是操作系统沙箱。若要运行来源不可信的脚本
并抵御脚本内部主动绕过路径检查，仍应把 Worker 放进容器或系统级沙箱。

## 工具扩展

子代理不会直接共享主 Agent 的有状态工具实例。自定义 `Tool` 若包含锁、客户端或
可变缓存，应实现 `clone_for_subagent(event_bus=...)` 并返回独立实例；没有专用接口
时框架会尝试深拷贝，再尝试无参重建。裸函数工具默认不进入子代理，只有显式设置
`subagent_thread_safe = True` 后才允许克隆。

## 任务状态

任务快照位于 `.cbagent/subagents/<task_id>.json`，事件日志位于对应的
`*.events.jsonl`，最终文本位于 `*.result.txt`。运行状态包括：

`queued -> running/waiting_tool -> completed/failed/cancelled`

进程重启时遗留的运行中任务会标记为 `orphaned`，不会自动重放可能带副作用的操作。
主 Agent 在每次模型调用前自动获取增量进度，也可用 `agent_task inspect` 按游标查询。
主会话取消只关闭使用同一取消令牌的模型流；后台任务使用独立令牌，不会因父回合
结束而被误取消。应用退出、`/clear` 或 `agent_task cancel` 会进入统一取消流程。

# list_tools 动态工具发现 — 技术报告

## 背景

之前系统提示词 `_build_system_instructions()` 调用 `registry.get_tools_description()` 将所有工具描述（30+ 条，每条几十字）内联到 system prompt 中。随着 MCP 工具增多，这部分占用越来越大，且模型每轮都要背负这些文本，不管本轮是否用到。

## 方案

新增 `list_tools` 工具，让模型按需动态获取工具列表，替代 system prompt 中的静态内联。

### 工具设计

- **文件**：`tools/tools/list_tools_tool.py`
- **名称**：`list_tools`
- **参数**：无
- **实现**：调用 `tools.toolRegistry.global_registry.get_tools_description()` 返回所有已注册工具的描述
- **使用全局单例**：`global_registry` 是 `toolRegistry.py:226` 定义的全局唯一实例，确保与实际注册的工具完全一致

### 系统提示词变更

**之前**：
```
你是 cb-agent 的智能助手。下面列出当前可用的能力，按需调用：
- todo: 任务管理工具...
- bash: 执行 Shell 命令...
- file_read: 读取文本文件...
... (30+ 条工具描述，每轮 system prompt 都带)
```

**之后**：
```
你是 cb-agent 的智能助手。
当前系统注册了 33 个工具。开始任务前，请先调用 list_tools 获取完整工具列表和功能描述——不要猜测工具是否存在。
遇到复杂问题时请务必调用 todo 工具分解任务。
```

### 注册位置

`ListToolsTool` 注册在 `TodoTool` 之后、其他工具之前（`run_agent.py:317`），确保模型第一轮 tool_calls 中最先看到它。

## 效果

| 指标 | 之前 | 之后 |
|------|------|------|
| system prompt 中工具描述占用 | ~2500 tokens（30+ 工具） | ~50 tokens（1 行指引） |
| 工具发现方式 | 被动（每轮必带） | 主动（模型按需调用 list_tools） |
| 新增工具的成本 | 所有轮次都增加 | 仅 list_tools 返回结果增加 |

## 文件改动

- `tools/tools/list_tools_tool.py`：新增，ListToolsTool 类
- `run_agent.py`：+2 行（导入 + 注册）
- `agent/session.py`：`_build_system_instructions()` 替换工具清单为简短指引

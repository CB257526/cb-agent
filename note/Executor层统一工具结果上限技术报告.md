# Executor 层统一工具结果上限技术报告

## 概述

在 ToolExecutor 层新增统一的工具结果字符上限机制，解决以下问题：
- 各工具各自截断标准不统一（bash 100k、file_read 100KB 等），无全局兜底
- MCP 工具、第三方 skill 脚本等新工具可能完全没有截断逻辑
- 一轮并行调用多个工具时，总字符量可能撑爆上下文窗口
- 模型读取持久化文件时可能进入"截断→读取→再截断"的无限循环

## 架构设计

### 两层截断模型

```
工具层（各工具 run() 内部）      Executor 层（统一兜底）
┌─────────────────────────┐    ┌──────────────────────────────┐
│ bash: stdout 100k chars │    │ 单条上限: 50,000 字符         │
│ file_read: 100KB        │ →  │ 批量上限: 200,000 字符（总和） │
│ MCP: 无截断             │    │ 超限持久化 + preview 替换      │
│ skill: 无截断           │    │ 防循环: 读持久化文件免二次存盘  │
└─────────────────────────┘    └──────────────────────────────┘
```

### 数据流

```
工具 run() 返回原始 result
        │
        ▼
_run_one() → cap_single_result()
        │       ├─ ≤ 50k: 原样返回
        │       ├─ > 50k + 工具已自行持久化(output_file): 复用路径，构建 preview
        │       ├─ > 50k + file_read 读持久化文件: inline 硬截断，不存盘
        │       └─ > 50k + 正常: 存盘 + preview 替换
        ▼
execute() → cap_batch_results()
                ├─ 总量 ≤ 200k: 不动
                └─ 总量 > 200k: 从最长的开始逐条持久化
```

## 新增文件

### agent/result_cap.py

核心模块，包含：

| 函数 | 职责 |
|------|------|
| `cap_single_result()` | 单条结果上限检查，返回 (result, persisted) |
| `cap_batch_results()` | 批量总量上限检查，原地修改 results 列表 |
| `_is_reading_persisted_result()` | 防循环判断：是否在读取持久化文件 |
| `_extract_existing_persist_path()` | 检测工具是否已自行持久化（如 bash output_file） |
| `_persist_full_result()` | 将完整结果写磁盘 |
| `_build_truncated_payload()` | 构建截断后的 JSON 替换内容 |
| `default_persist_dir()` | 默认持久化目录路径 |

常量：

```python
MAX_SINGLE_RESULT_CHARS = 50_000       # 单条上限
MAX_BATCH_RESULT_CHARS = 200_000       # 批量总量上限
PREVIEW_HEAD_CHARS = 2000              # preview 头部字符
PREVIEW_TAIL_CHARS = 500               # preview 尾部字符
PERSIST_DIR_NAME = "tool_results"      # 持久化目录名
```

### test/test_result_cap.py

10 个测试用例覆盖：
1. 未超限不截断
2. 超限触发持久化 + preview 替换
3. 防循环：file_read 读持久化文件只做 inline 截断
4. 非 file_read 工具不触发防循环
5. 工具已自行持久化（output_file）时复用路径
6. 持久化失败退化为 inline 截断
7. 批量未超限不动
8. 批量超限从最长开始截断
9. 批量中已截断的 payload 不被二次处理
10. `_extract_existing_persist_path` 对非 JSON 返回 None

## 修改文件

### agent/executor.py

改动点：
1. 新增 `import`：`from agent.result_cap import cap_batch_results, cap_single_result`
2. `__init__` 新增 `persist_dir` 参数，默认 `.cbagent/tool_results/`
3. `_run_one()` 末尾：工具执行成功（`is_error=False`）时调用 `cap_single_result`
4. `execute()` 返回前：调用 `cap_batch_results` 做批量总量检查

## 持久化格式

文件路径：`.cbagent/tool_results/<call_id>.txt`

截断后消息体示例：
```json
{
  "truncated": true,
  "tool_name": "bash",
  "total_chars": 82000,
  "total_lines": 1560,
  "preview_head": "前 2000 字符的原始内容...",
  "preview_tail": "...末尾 500 字符",
  "persisted_path": ".cbagent/tool_results/call_abc123.txt",
  "hint": "完整内容已持久化（82000 字符 / 1560 行）。请用 file_read(path=\".cbagent/tool_results/call_abc123.txt\", start_line=X, end_line=Y) 按需分段读取，不要一次性全量读取。"
}
```

## 防循环机制

| 场景 | 行为 |
|------|------|
| 普通工具超限 | 持久化 + preview 替换 |
| file_read 读 `tool_results/` 下的文件 | 只做 inline 硬截断到 50k，不存盘 |
| 工具返回 JSON 含 `output_file` 字段 | 不重复存盘，复用已有路径构建 preview |

这确保：
- 模型读取持久化文件的结果不会再被存到另一个文件里
- bash 等已有落盘逻辑的工具不产生冗余文件
- 循环链条最多两步就断了（原始调用→持久化→file_read 分段读→inline 截断结束）

## 与现有压缩机制的关系

| 机制 | 触发时机 | 作用范围 | 关系 |
|------|----------|----------|------|
| executor result_cap | 工具返回时立即 | 单条/单批 | 最先生效 |
| `_maybe_compress_tool_loop_messages` | 每轮结束，80% 窗口时 | 当前 messages 中所有 tool content | 在 cap 之后，做进一步摘要 |
| `cached_microcompact` | 跨轮积累 | 旧 tool result 按条数淘汰 | 处理历史消息 |
| auto_compact | 85% 窗口时 | 整个 history | 最后的兜底压缩 |

四层各管各的生命周期阶段，互不冲突。

## 向后兼容

- `ToolExecutor.__init__` 新增的 `persist_dir` 参数有默认值，现有调用方无需改动
- 工具层截断逻辑保持不变，executor cap 只是兜底
- 如果所有工具返回都 < 50k 且总量 < 200k，cap 逻辑零开销（只是长度比较）

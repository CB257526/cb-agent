# Executor 层统一工具结果上限技术报告

## 概述

在 ToolExecutor 层提供统一的工具结果 token/字节上限机制，解决以下问题：
- 各工具各自截断标准不统一（bash 100k、file_read 100KB 等），无全局兜底
- MCP 工具、第三方 skill 脚本等新工具可能完全没有截断逻辑
- 一轮并行调用多个工具时，总 token 量可能撑爆上下文窗口
- 模型读取持久化文件时可能进入"截断→读取→再截断"的无限循环

## 架构设计

### 三层截断模型

```
原始采集层                  工具语义层                  Executor 模型可见层
┌────────────────────┐    ┌────────────────────┐    ┌────────────────────────────┐
│ 防止进程内存失控     │    │ file_read: 32 KiB  │    │ 单条: 10K tokens / 40K bytes │
│ bash 超限落盘        │ →  │ bash: preview+文件  │ →  │ 批量: 40K tokens / 160K bytes│
│ 不等于模型可见额度   │    │ 支持分页和范围读取   │    │ 超限持久化 + 稳定 preview     │
└────────────────────┘    └────────────────────┘    └────────────────────────────┘
```

### 数据流

```
工具 run() 返回原始 result
        │
        ▼
_run_one() → cap_single_result()
        │       ├─ ≤ 10K tokens 且 ≤ 40K bytes: 原样返回
        │       ├─ 超限 + 工具已自行持久化(output_file): 复用路径，构建 preview
        │       ├─ 超限 + file_read: 保留 JSON 元数据，只缩短 content
        │       └─ 超限 + 其它工具: 存盘 + preview 替换
        ▼
execute() → cap_batch_results()
                ├─ 总量 ≤ 40K tokens 且 ≤ 160K bytes: 不动
                └─ 超限: 从 token 最大的结果开始逐条缩短
```

## 新增文件

### agent/result_cap.py

核心模块，包含：

| 函数 | 职责 |
|------|------|
| `cap_single_result()` | 单条结果上限检查，返回 (result, persisted) |
| `cap_batch_results()` | 批量总量上限检查，原地修改 results 列表 |
| `_truncate_file_read_payload()` | 保持 file_read JSON 与分页信息，只缩短 content |
| `_extract_existing_persist_path()` | 检测工具是否已自行持久化（如 bash output_file） |
| `_persist_full_result()` | 将完整结果写磁盘 |
| `_build_truncated_payload()` | 构建截断后的 JSON 替换内容 |
| `default_persist_dir()` | 默认持久化目录路径 |

常量：

```python
MAX_SINGLE_RESULT_TOKENS = 10_000      # Codex 风格单条 token 上限
MAX_SINGLE_RESULT_BYTES = 40_000       # 单条 UTF-8 字节硬兜底
MAX_BATCH_RESULT_TOKENS = 40_000       # cb-agent 额外批量保护
MAX_BATCH_RESULT_BYTES = 160_000       # 批量 UTF-8 字节硬兜底
PREVIEW_HEAD_CHARS = 2000              # preview 头部字符
PREVIEW_TAIL_CHARS = 500               # preview 尾部字符
PERSIST_DIR_NAME = "tool_results"      # 持久化目录名
```

### test/test_result_cap.py

测试覆盖：
1. 未超限不截断
2. 超限触发持久化 + preview 替换
3. file_read 超限时保留路径与分页信息，只缩短 content
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
3. `_run_one()` 末尾：成功和异常结果统一调用 `cap_single_result`
4. `execute()` 返回前：调用 `cap_batch_results` 做批量总量检查；此时结果尚未追加到模型 messages，不会改写旧前缀

## 持久化格式

文件路径：`.cbagent/tool_results/<call_id>.txt`

截断后消息体示例：
```json
{
  "truncated": true,
  "tool_name": "bash",
  "total_chars": 82000,
  "total_tokens": 20500,
  "total_bytes": 82000,
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
| file_read 结果超限 | 保留原路径、范围和 JSON 结构，只缩短 content，不存盘 |
| 工具返回 JSON 含 `output_file` 字段 | 不重复存盘，复用已有路径构建 preview |

这确保：
- 任意 file_read 结果都不会被复制到另一个持久化文件里
- bash 等已有落盘逻辑的工具不产生冗余文件
- 循环链条最多两步就断了（原始调用→持久化→file_read 分段读→inline 截断结束）

## 与现有压缩机制的关系

| 机制 | 触发时机 | 作用范围 | 关系 |
|------|----------|----------|------|
| executor result_cap | 工具返回时立即 | 单条/单批 | 最先生效 |
| preflight compact | 首次模型调用前请求超预算 | active history | 通过正式摘要边界释放空间 |
| post-turn/manual compact | 回合结束达到阈值或用户触发 | 整个 history | 通过正式摘要边界释放空间 |
| blocking threshold | 工具循环请求接近完整窗口 | 当前请求 | 停止发送并提示 compact/clear |

本地 microcompact 已移除。工具循环保持 append-only，不再替换已发送的旧 tool result；这样入口硬截断后的消息前缀可以持续复用 provider prefix cache。

Codex 当前实现使用每条工具结果 10K token policy，没有同轮批量预算。cb-agent 的批量保护是额外兜底，因为当前还没有 Codex 风格的 mid-turn compact。它在 executor 返回前完成，此时这些结果从未发送给模型，因此不会破坏已缓存前缀。

## 向后兼容

- `ToolExecutor.__init__` 新增的 `persist_dir` 参数有默认值，现有调用方无需改动
- 工具层截断逻辑保持不变，executor cap 只是兜底
- 如果单条和批量结果都在 token/字节预算内，cap 不改写任何结果

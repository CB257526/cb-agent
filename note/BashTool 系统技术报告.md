# BashTool 系统技术报告

> 更新于 2026-07-23。模块仍在 `tools/tools/bash_*.py`；本文重点写清**当前输出截断与落盘语义**，并保留架构索引。旧文中“仅 >1MB 才落盘”“100K–1MB 区间永久丢失”等描述已作废。

## 1. 工具面

| 工具 | 职责 |
|------|------|
| `bash` | 前台/后台执行单条命令 |
| `bash_task` | list / output / wait / kill 后台任务 |
| `bash_permission` | allowlist grant/revoke/list/check |

支撑模块：`bash_session`（cwd）、`bash_security` / `bash_permission`、`bash_shell`、`bash_output`、`bash_background`、`bash_classify`、`bash_semantics`、`bash_prompt`。

## 2. 输出：先保存、后预览

实现：`tools/tools/bash_output.py` 的 `process_output`。

### 2.1 阈值

| 项 | 值 | 含义 |
|----|-----|------|
| `MAX_STDOUT_CHARS` | 100_000 | 模型可见 stdout 预览上限（字符） |
| `MAX_STDERR_CHARS` | 20_000 | 模型可见 stderr 预览上限 |
| `HARD_CAP_BYTES` | 64 MiB | 无法保证完整保存；标记 `hard_limit_exceeded` |
| `PERSIST_THRESHOLD_BYTES` | 1 MiB | **仅兼容旧常量名**；现逻辑是“截断即落盘”，不再要求超过 1MB |

### 2.2 不变量

1. **只要** stdout 或 stderr 触发模型可见截断，就先分别尝试落盘**完整原文**。
2. 返回字段：
   - `stdout_file` / `stderr_file`：完整文件绝对路径  
   - `output_file`：stdout 的兼容别名（供 result_cap / 旧调用复用）  
   - `output_truncated`、`stdout_chars` / `bytes` / `lines` 等 stats  
   - `hard_limit_exceeded`、`persist_error`
3. preview 中带 `full_file:` 与 `file_read(...)` 续读示例。
4. **持久化失败** → 明确 `persist_error`，不得伪装成“可恢复的静默截断成功”。
5. executor `result_cap` 若发现已有 `stdout_file`/`output_file`，**不**再把已截断 JSON 二次当全文存盘。

### 2.3 路径布局

```text
.cbagent/bash_outputs/<task_id>.stdout.log
.cbagent/bash_outputs/<task_id>.stderr.log
```

后台任务：合并日志仍由 `bash_background` 写入 `output_file`；与前台“先截断再丢”无关。

### 2.4 后续（未在本阶段强制）

边读边写 spool（ring buffer + 原子 rename）可进一步降低 `communicate()` 全量内存；接口应保持 `ProcessedOutput` 字段兼容。

## 3. 与 file_read 的配合

截断后模型用：

```text
file_read(path=<stdout_file>, head=100)
file_read(path=<stdout_file>, start_line=..., end_line=...)
file_read(path=<stdout_file>, start_byte=..., end_byte=...)
```

file_read 为流式分页（见代码 `tools/tools/file_read_tool.py`），不要求整文件 `read_text`。

## 4. 权限与安全（摘要）

- 致命命令拦截、只读分类、allowlist + UI 弹窗（`bash_permission`）。
- cwd 通过 shell marker 跨调用持久化（`bash_session`）。
- PowerShell 包装与编码问题在 `bash_shell` / prompt 中说明：避免用户自行 `>` 重定向出 UTF-16。

## 5. 测试

- `test/test_bash_tool.py::TestOutput`：小输出不落盘、100K+ 落盘、stderr 分离、双流路径不碰撞。
- `test/test_result_cap.py`：复用已有 `output_file`。

## 6. 关键代码

- `tools/tools/bash_output.py`  
- `tools/tools/bash_tool.py`  
- `tools/tools/bash_prompt.py`  
- `agent/result_cap.py`  

# 文件修改 Diff 可视化展示 — 技术报告

## 背景

agent 用 `file_write` 工具修改文件时，当前的 TUI 工具卡片只显示行数统计（+N/-N）和操作消息，用户无法直观看到具体改了什么内容。需要在 TUI 中展示文件修改的 diff，类似 Claude Code 的差异显示效果。

## 实现方案

数据流完全复用现有 `ToolComplete.result` JSON 通道，无需新增事件类型或协议字段：

```
FileWriteTool.run() → JSON {..., diff: "unified diff text"}
  → ToolComplete.result → EventBus → JSON-RPC → App.tsx
  → ChatItem.toolResult → ToolBlock.tsx（解析 + 可视化渲染）
```

### 后端：`tools/tools/file_write_tool.py`

1. **导入 `difflib`** 标准库
2. **新增 `_generate_unified_diff()` 函数**：利用 stale check 时已读取的 `old_content` 和即将写入的 `content`，调用 `difflib.unified_diff()` 生成 unified diff 文本
   - 新建文件时 `old=None` → `old_lines=[]`，`fromfile="/dev/null"`，diff 全部是 `+` 行
   - `keepends=True` 保留输入行尾，`lineterm="\n"` 确保每行正确分隔
   - 无变更时 `unified_diff` 返回空迭代器 → `diff_text` 为空字符串
   - 超过 80 行自动截断，返回 `truncated=True`
3. **修改 `run()` 返回值**：在 JSON 中附加 `diff`、`diff_truncated`、`diff_lines_total`、`diff_lines_shown` 字段
   - `diff_text` 为空时不附加，避免无效传输

### 前端：`ui-tui/src/components/ToolBlock.tsx`

#### 扩展 `extractDisplay()`
- 增加 `message` 字段回退：FileWriteTool 的 OUT 区现在显示"已更新 xxx（+1/-1 行）"而非原始 JSON

#### diff 解析管道
1. **`extractDiff()`**：从 `toolResult` JSON 解析 diff 数据
2. **`buildDiffBlocks()`**：将原始 unified diff 文本转为可视化块列表
   - 过滤 `---`、`+++`、`@@` 技术头行（用户不需要看这些）
   - 将相邻的 `-` 和 `+` 行配对为 `modify` 块
   - 孤立的 `-`/`+` 行分别归为 `removal`/`addition` 块
   - 上下文行合并为 `context` 块
3. **`computeLineDiff()`**：对 modify 块中的每对行做词级差异定位
   - 找最长公共前缀 + 最长公共后缀，中间即为变更区域
   - 变更比例 > 60% 时放弃词级高亮，整行替换渲染

#### DIFF 区域渲染
- **上下文行**：dim 灰色，无背景
- **删除行**：红字 + 暗红底 (`#3d1f28`)
- **新增行**：绿字 + 暗绿底 (`#1a3a2a`)
- **修改行**：红/绿底，未变更部分 dim，变更部分 bold + 更亮底色高亮
- **截断提示**：`... [diff 已截断，显示 80/523 行]`

## 截断策略（双层防护）

| 层 | 位置 | 限制 | 作用 |
|---|---|---|---|
| 后端 | `_generate_unified_diff` | 80 行 | 防止 diff 撑大 JSON-RPC 事件流 |
| 前端 | `buildDiffBlocks` 切片 | 40 个块 | 安全兜底 |

## 边界情况处理

| 情况 | 处理 |
|---|---|
| 新建文件 | `old=None` → diff 全 `+` 行 |
| 无实际变更 | `diff_text` 为空 → 不附加 diff 字段，不渲染 DIFF 区 |
| 文件清空 | 旧有内容新为空 → diff 全 `-` 行 |
| 非 file_write 工具 | `extractDiff()` 返回 `null`，无 DIFF 区 |
| 超大文件 | 后端截断 80 行 + 前端通知 |
| 超长单行 | 终端自动换行，不做处理 |

## 文件改动

- `tools/tools/file_write_tool.py`：+94 行（导入 difflib、新增 `_generate_unified_diff()`、修改 `run()` 返回值）
- `ui-tui/src/components/ToolBlock.tsx`：+278 行（扩展 `extractDisplay()`、新增 diff 解析+可视化渲染）

# Bash 工具 UI 输出预览字段技术报告

## 背景

TUI 前端展开 bash 工具调用块时，OUT 区会把 `BashTool.run()` 的返回值整段当字符串渲染。
而 BashTool 返回的是 JSON 字符串，例如：

```json
{"stdout": "", "stderr": "Traceback (most recent call last):\n  File \"<string>\", ...",
 "exit_code": 1, "cwd": "C:\\Users\\cb135\\Desktop\\cbAgent\\cb-agent",
 "interrupted": false, "timeout": false, "is_error": true, "semantic": null,
 "background": false, "classification": {"kind": "normal"}, "warnings": [],
 "output_truncated": false, "output_file": null,
 "permission": {"decision": "allow", "reason": "本次允许", ...}}
```

这导致用户看到：

- `\n` 是字面量，多行 stdout 全挤一行
- `\\` 路径转义被原样吐出
- PowerShell 表格输出彻底糊掉
- 字段噪声盖过真正想看的 stdout/stderr

后端 CLI 渲染器其实早就有 [agent/renderers/cli.py:146 _render_bash_output](../agent/renderers/cli.py#L146) 解决这事，TUI 没复用。

## 方案选型

考虑过三个：

| 方案 | 改动 | LLM 上下文 | 扩展性 |
|---|---|---|---|
| A. ToolComplete 加 `display_result` 字段 | 6 个文件 | 干净（display 不进上下文）| 高（每个工具自定义） |
| **B. BashTool JSON 里加 `__display__` 键** | **2 个文件** | **多花 ~50 token** | **中** |
| C. 前端 try-parse JSON 提取 stdout | 1 个文件 | 干净 | 低（每加结构化工具都要前端写） |

最终选 **B**：改动最小、行为可立即验证、`__display__` 的数据量本身就是 stdout 的子集，token 浪费在可接受范围内。如果将来其他工具也要这套，可以平移到 ToolBlock 通用识别（前端逻辑已经按通用方式写）。

## 实现

### 改动文件

- [tools/tools/bash_tool.py](../tools/tools/bash_tool.py) — 加 `_build_bash_display()` 工厂 + 在 6 个 return 出口注入 `__display__`
- [ui-tui/src/components/ToolBlock.tsx](../ui-tui/src/components/ToolBlock.tsx) — `extractDisplay()` 识别 `__display__` 并优先渲染
- [test/test_bash_tool.py](../test/test_bash_tool.py) — `TestBashDisplay` 共 8 个用例

### 关键代码

**`_build_bash_display()`** — 单源生成预览文本，无 ANSI 颜色（前端自己着色）：

```python
def _build_bash_display(*, stdout="", stderr="", exit_code=0, is_error=False,
                       interrupted=False, timeout=False, background=False,
                       background_task_id=None, error_override=None) -> str:
    if error_override:
        return f"✗ {error_override}"
    if background:
        return f"⟳ 后台运行中 (task {background_task_id or '?'})"
    if timeout:
        return "⏱ 命令超时"
    if interrupted:
        return "✗ 命令被中断"

    parts = []
    if is_error:
        parts.append(f"✗ exit {exit_code}")
        if stderr: parts.append(_clip(stderr, 800))
        if stdout: parts.append(_clip(stdout, 800))
    else:
        if stdout: parts.append(_clip(stdout, 800))
        if stderr: parts.append(_clip(stderr, 400))

    return "\n".join(parts) if parts else "Done."
```

优先级语义：
1. `error_override`（参数验证失败 / fatal 拒绝 / 权限拒绝）→ 单行 `✗ <reason>`
2. `background` → 单行 `⟳ 后台运行中 (task <id>)`
3. `timeout` / `interrupted` → 单行标记
4. `is_error=True` → 首行 `✗ exit N`，下面 stderr 优先（用户确认的方案）+ stdout
5. 正常 → stdout，可选追加 stderr
6. 全空 → `Done.`

**6 个注入点**：参数验证失败、fatal 拒绝、权限拒绝、主成功路径、后台启动失败、后台启动成功。

**前端识别** ([ui-tui/src/components/ToolBlock.tsx](../ui-tui/src/components/ToolBlock.tsx)):

```ts
function extractDisplay(s: string): string | null {
  const t = s.trimStart();
  if (!t.startsWith("{")) return null;
  try {
    const obj = JSON.parse(s);
    if (obj && typeof obj === "object" && typeof obj.__display__ === "string") {
      return obj.__display__;
    }
  } catch { return null; }
  return null;
}
```

写成通用形式，不绑死 BashTool；其他工具想用同样模式，往 JSON 加 `__display__` 即可。

### 折叠/展开行为

- 折叠态保持原样（紧凑单行）
- 展开态 OUT 区：有 `__display__` → 渲染预览；没有 → fallback 原 result

### 字段长度

| 区域 | 上限 | 来源 |
|---|---|---|
| stdout（正常）| 800 字符 | 用户确认 |
| stdout（错误）| 800 字符 | 同上 |
| stderr（错误）| 800 字符 | 错误时 stderr 是主信号 |
| stderr（正常）| 400 字符 | 正常时多半是 deprecation 提示 |
| 截断尾巴 | `... [+N chars]` | 与 ToolBlock 现有 truncate 风格一致 |

## 测试

新增 `TestBashDisplay`：

| 用例 | 检查 |
|---|---|
| `test_normal_stdout_only` | stdout 直出 |
| `test_empty_returns_done` | 空回 `Done.` |
| `test_error_prefixes_exit_code` | 错误首行有 `✗ exit N` |
| `test_error_override_short_circuits` | override 短路所有其他逻辑 |
| `test_background_uses_task_id` | 后台分支带 task id |
| `test_timeout_priority` | timeout 优先于 stdout |
| `test_stdout_clipped_at_800` | 大输出截断 + 尾标 |
| `test_run_success_includes_display` | 真实 `BashTool.run()` 路径 JSON 里有 `__display__` 且不含 `"stdout"` 字面 |

```
Ran 78 tests in 5.687s
OK
```

`ui-tui` 端 `tsc --noEmit` 干净。

## 兼容性

- LLM 看到的字段：`stdout` / `stderr` / `exit_code` / `is_error` / 等照旧；多了一个 `__display__`，对 LLM 决策无影响（多花 ~50 token / 调用）
- 老前端代码/外部消费者：JSON 里多个无害字段，行为不变
- 前端 `extractDisplay` 失败（非 JSON / 无字段）→ 自动 fallback 到原渲染，对其他工具无影响

## 后续可改

- 其他高频结构化工具（todo / search / file_read）若也想要同样的视觉清爽，往返回 JSON 加 `__display__` 即可，前端不用动
- 如果将来想让 LLM 上下文绝对干净（不带 `__display__`），可平移到 ToolComplete 事件层面（方案 A 的形态），代价是再改 events.py + executor.py + App.tsx

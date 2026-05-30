"""BashTool 模型提示词

参考 Claude Code prompt.ts 的设计。

这段文本会在每轮对话的系统指令中注入，告诉 LLM：
- 什么场景用 Bash、什么场景优先用本项目的专用工具
- 当前平台提示（Windows/Unix 命令差异）
- 超时和后台执行的用法
- 多命令并行/串行策略
- Git 安全协议
- sleep 限制
"""

from tools.tools.bash_shell import get_platform_hint

BASH_SYSTEM_PROMPT = """# Bash 工具使用规范

## 平台提示

{platform_hint}

## 何时使用

Bash 工具用于执行 Shell 命令。但在以下场景，优先使用本项目提供的专用工具（体验更好，输出更结构化）：

- 任务管理：使用 todo 工具，不要用 Bash 操作文本文件跟踪任务
- 网络搜索：使用 my_advanced_search 工具，不要用 curl
- 代码搜索：如果需要搜索项目文件内容，直接使用本项目的 grep/glob 能力
- 记忆存取：使用 memory 工具

## 基本规则

- 创建新文件或目录前，先用 Bash 确认父目录存在
- 包含空格的路径用双引号包裹
- 尽量使用绝对路径并保持当前工作目录，避免频繁 `cd`
- 可选超时参数（毫秒）：默认 120000（2 分钟），最大 600000（10 分钟）

## 多命令执行

- 多个**独立**的命令：在一次回复中发起多次 Bash 工具调用，ToolRegistry 会并行执行
- 多个**依赖**的命令（后者需要前者的结果）：用 `&&` 串接在一次调用中
- `;` 仅用于需要串行但不关心前面命令失败与否的场景
- 不要用换行分隔多个命令（带引号的字符串内换行除外）

## 用户确认（弹窗）

bash 工具默认是**严格模式**：除了只读命令（pwd / ls / dir / cat / head / tail / grep / find / git status|log|diff、docker ps、kubectl get 等）之外，**所有命令第一次都会弹窗让用户确认**。这是预期行为，不是 bug。

弹窗时用户可选：
- 允许这一次：本次放行，下次同命令仍要再弹
- 总是允许 "<前缀>" 在此目录：写到项目级 allowlist，同前缀同 cwd 直接放行
- 总是允许 "<前缀>" 在所有目录：写到全局 allowlist
- 拒绝：本次返回 `[权限拒绝]`

实践建议：
- 不要试图组合命令绕过弹窗（例如 `ls && python x.py`），任一段非只读都会弹
- 看到 `permission_unavailable: true` 说明 stdin 不是 tty（无法弹窗），命令被自动拒绝；告知用户在交互终端下重试
- 如果用户已经为同前缀加过 allowlist，后续相同命令静默通过，不会再有弹窗记录

## 确认 allowlist 是否真的写入了

bash 返回 JSON 里有 `permission` 字段，结构：
```
{"decision": "allow"|"deny",
 "reason": "命中 allowlist" | "本次允许" | "已加入项目级 allowlist" | ...,
 "matched_rule": {prefix, scope, cwd, added_at} 或 null,
 "permission_unavailable": false}
```
判断规则：
- `matched_rule` 非空 → 命令通过 allowlist 放行（用户之前选过 [2]/[3] 加进了规则）
- `matched_rule` 为空 + `reason` 含"本次允许" → **没**写入 allowlist，下次还会弹
- `matched_rule` 为空 + `reason` 含"只读" → 命令本身就在只读白名单里
- 不要凭"命令成功了"就向用户宣称"已加入 allowlist"，要看 matched_rule 是否真有规则

## 用户授权（bash_permission 工具）

当用户用自然语言授权（"以后 X 不要再问我"、"授权 npm install"、"撤销 python 授权"），不要等弹窗，直接调 `bash_permission` 工具：
- `bash_permission(action="grant", prefix="python", scope="cwd")` —— 在当前目录授权 python
- `bash_permission(action="grant", prefix="npm install", scope="global")` —— 全局授权 npm install
- `bash_permission(action="revoke", prefix="python", scope="cwd")` —— 撤销当前目录的 python 授权
- `bash_permission(action="list")` —— 列所有规则
- `bash_permission(action="check", prefix="python")` —— 在执行前自检某条规则是否已生效

prefix 的写法：
- 单 token 命令（python / mkdir / mv）写命令名
- 多动词命令（git push / npm install / docker build）写两段，与弹窗显示的前缀保持一致

注意：高危前缀（rm / Remove-Item / curl / wget / sudo / iex 等）禁止通过本工具写入，只能走弹窗。这是为了防止意外把破坏性命令加入 allowlist。

## 后台执行

长时间命令（npm install、docker build、pip install 等）使用 `run_in_background: true`。返回的 `background_task_id` 是后续操作句柄。

完成通知：当后台任务结束后，下一轮对话开头你会收到一条 `[后台任务完成通知]`，里面有 task_id / status / exit / output 路径。看到这条通知后，**主动**用 `bash_task(action=output, task_id=...)` 拉一下结果，不要等用户问。

主动查询：在通知出现前，需要中途看进度可以调 `bash_task(action=list)` 看所有任务，或 `bash_task(action=output, task_id=...)` 拉当前已写入的输出。`bash_task(action=wait, task_id=..., timeout=...)` 是阻塞等结束。`bash_task(action=kill, task_id=...)` 杀进程。

## 大输出处理

不要自己用 `>`、`Out-File`、`Tee-Object` 重定向输出到文件。让命令直接往 stdout 打，bash 工具会自动处理：

- 输出 ≤100KB：直接在 `stdout` 字段返回
- 输出 >1MB：完整原文落盘到 `output_file` 路径，`stdout` 字段只保留首尾片段并标记 `output_truncated=true`；后续用 `file_read(path=<output_file>, tail=N)` 或 `head=N`、`start_line/end_line` 拉感兴趣的部分
- 后台任务：完整输出始终写在 `output_file`（task 返回里），用 `bash_task(action=output, ...)` 拿尾部，需要全文用 `file_read`

特别是 PowerShell：用 `>` 默认会写 UTF-16-LE BOM 文件，再读会乱码。永远让 bash 工具替你管落盘。

## Git 安全协议

- 永远不要修改 git config
- 不要执行破坏性 git 命令（push --force、reset --hard、checkout .、restore .、clean -f、branch -D），除非用户明确要求
- 不要跳过 hooks（--no-verify、--no-gpg-sign），除非用户明确要求
- 永远不要 force push 到 main/master
- 优先创建新 commit 而非 amend；pre-commit hook 失败意味着提交没有发生，不要用 --amend 继续
- 暂存文件时用具体文件名而非 `git add -A` 或 `git add .`，防止意外纳入密钥/大文件
- 只有用户明确说"提交"时才提交

## 创建 commit

1. 并行运行：`git status`、`git diff`（看 staged + unstaged）、`git log`（看最近提交风格）
2. 分析改动并拟定 1-2 句的提交信息（侧重 WHY 而非 WHAT）
3. 并行运行：`git add <具体文件>`、`git commit -m "..."`、`git status`

## 创建 PR

1. 检查分支状态：`git status`、`git diff`、检查 remote 是否最新、`git log` + `git diff [base]...HEAD`
2. 分析所有被纳入的 commits（不只最新一个），拟定 PR 标题（<70 字符）和正文
3. 必要时创建新分支，推送到 remote，用 `gh pr create` 创建 PR

## 避免不必要的 sleep

- 不要 sleep 在可以直接执行的命令之间
- 长时间运行的命令用 `run_in_background`，不要 poll
- 失败的命令不要放在 sleep 循环里重试 — 先诊断根因
- 如需 sleep，保持 1-5 秒，避免阻塞用户"""


def get_bash_prompt() -> str:
    """返回注入给模型的 Bash 工具使用规范，含平台提示。"""
    hint = get_platform_hint()
    if hint:
        hint = "> " + hint
    return BASH_SYSTEM_PROMPT.format(platform_hint=hint or "")

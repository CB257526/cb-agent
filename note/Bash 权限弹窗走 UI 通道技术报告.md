# Bash 权限弹窗走 UI 通道技术报告

## 背景

之前在 TUI 模式下让 agent 执行非白名单命令（如 `python3 -c "print('hello')"`）时，bash 工具会直接返回：

```
{"stderr": "[权限拒绝] 无可用终端确认权限",
 "permission_unavailable": true, ...}
```

模型看到只能放弃或绕路（写脚本文件再执行）。原因：

1. `BashTool.run` 调用 `PermissionGate.prompt_user`
2. `prompt_user` 检查 `sys.stdin.isatty()`，TUI 模式下 stdin 已被 ink 前端接管，返回 False
3. 于是直接返回 DENY + permission_unavailable=True

而真实的 Claude Code 行为是：弹一个带选项的对话框（Yes / Yes don't ask again / No），让用户即时决定。

cb-agent 项目里其实已经有这套机制，就是 `AskUserQuestionTool`：emit `AskUserQuestion` 事件 → 前端弹框 → 用户答完 → `QuestionRegistry.wait_for_answer` 解阻塞。问题是这套逻辑写死在 ask 工具内部，bash 权限想用不能复用。

## 方案

把"问用户一道选择题、阻塞等回答"的能力抽成独立的 `QuestionChannel`，让所有需要询问的工具都能用同一条 UI 路径。

### 文件结构

新增：

- [agent/question_channel.py](../agent/question_channel.py) — `QuestionChannel.ask(question, options, ...)` 同步阻塞返回 `{"answer", "cancelled", "other_text"}`

修改：

- [tools/tools/bash_permission.py](../tools/tools/bash_permission.py) — `PermissionGate` 加可选 `question_channel`，`prompt_user` 三级降级：channel → stdin TTY → permission_unavailable
- [tools/tools/ask_user_question_tool.py](../tools/tools/ask_user_question_tool.py) — 重构走 QuestionChannel，去掉重复代码 ~50 行
- [tools/tools/bash_tool.py](../tools/tools/bash_tool.py) — `__init__` 加可选 `question_channel` 参数（绑到 gate 上）
- [run_agent.py](../run_agent.py) — session 构造完后把 channel 注入全局 PermissionGate 单例

### 关键代码

**QuestionChannel** ([agent/question_channel.py](../agent/question_channel.py)):

```python
class QuestionChannel:
    def __init__(self, registry: QuestionRegistry, bus: EventBus) -> None:
        self._registry = registry
        self._bus = bus

    def ask(self, question, options, multi_select=False, recommended_index=None):
        qid = self._registry.new_question_id()
        self._registry.register(qid)
        self._bus.emit(AskUserQuestion(question_id=qid, ...))
        cancel_token = get_current_cancel_token()
        cancel_event = cancel_token.event if cancel_token is not None else None
        try:
            slot = self._registry.wait_for_answer(qid, cancel_event=cancel_event)
        finally:
            self._registry.discard(qid)
        # ... 拼回 dict 返回
```

阻塞跨线程：工具线程进 `wait_for_answer()` 睡，UI 线程收到用户点击后 emit `AskUserQuestionAnswered` → `QuestionRegistry` 唤醒。`cancel_event` 让 Ctrl+C 能立刻退出等待。

**PermissionGate.prompt_user 三级降级** ([tools/tools/bash_permission.py:391](../tools/tools/bash_permission.py#L391)):

```python
def prompt_user(self, command, prefix, reason, cwd):
    # 1) UI 通道
    if self.question_channel is not None:
        try:
            return self._prompt_via_channel(command, prefix, reason, cwd)
        except Exception:
            # 通道炸了不能让 bash 跟着挂，降级
            ...
    # 2) stdin TTY
    if sys.stdin and sys.stdin.isatty():
        return self._prompt_via_stdin(command, prefix, reason, cwd)
    # 3) 都不可用
    return GateResult(Decision.DENY, reason="无可用终端确认权限",
                      permission_unavailable=True)
```

四个选项 label 设计上要能回译为四档决策：

```python
opt_once   = "允许这一次"
opt_cwd    = f'总是允许 "{prefix}" 在此目录'
opt_global = f'总是允许 "{prefix}" 在所有目录'
opt_deny   = "拒绝"
```

UI 端拿到 label 直接显示；用户点完返回的 `answer` 字段还是这个 label，后端字符串 == 比对就回到了原来的 1/2/3/4 路径。

**注入点** ([run_agent.py:191](../run_agent.py#L191)):

```python
# 5b. AskUserQuestionTool 注册（已存在）
# 5c. 给全局 PermissionGate 单例装上 channel
get_permission_gate().question_channel = QuestionChannel(
    self.session.question_registry, self.event_bus,
)
```

为什么改单例而不是 BashTool 构造参数？因为 `_register_native_tools` 跑在 session 构造之前，BashTool 实例化时还没 question_registry。改 gate 单例比把 BashTool 注册搬到后面更轻。BashTool 也保留了构造参数（`question_channel: Optional[Any] = None`）方便测试单独绑。

## 三级降级的取舍

为什么不直接 channel 没注入就 deny？

- **CLI 模式**：用户从终端跑 `run_agent.py`，stdin 是 TTY，没接 UI。这种情况走 `_prompt_via_stdin` 打印 1/2/3/4 让用户键盘选，跟原来一样。
- **TUI 模式**（VSCode 插件 / ink 前端）：stdin 被前端独占，channel 注入了，走 UI 弹框。
- **gateway 模式**（headless）：没 channel 也没 TTY，permission_unavailable 让模型知道没法问。

第三种返回 unavailable 是有意的：模型看到这个标记会用别的方式（比如 ask_user_question 工具本身、或者拆解任务）而不是认定"命令本身被禁"。

## 测试

新增 `TestPermissionPromptChannel` 类，6 个用例覆盖 channel 路径：

- `test_no_channel_no_tty_returns_unavailable` — 无 channel + 非 TTY → permission_unavailable=True（旧兜底行为）
- `test_channel_allow_once` — 选"允许这一次" → ALLOW，不写 store
- `test_channel_grant_cwd_writes_rule` — 选"总是允许在此目录" → ALLOW + 写 store + 下次 evaluate 同 cwd 直接 ALLOW
- `test_channel_deny_returns_deny` — 选"拒绝" → DENY，permission_unavailable=False（区别于无 channel 路径）
- `test_channel_cancelled_returns_deny` — 用户关弹窗 → DENY
- `test_channel_exception_falls_back` — channel.ask 抛异常 → 不让 bash 挂掉，降级到 stdin / unavailable

测试用 FakeChannel（duck-typing，不依赖真实 EventBus）。

stdin TTY 检测在 unittest 模式下默认是 True，所以 `test_no_channel_no_tty` 用 `unittest.mock.patch("sys.stdin")` 强制 isatty=False。

跑 `../venv/python.exe -m unittest discover -s test`：

```
Ran 199 tests in 6.072s
OK
```

包括：
- TestPermission（已有）+ TestPermissionPromptChannel（新加 6 个）= 24 个
- test_ask_user_question 11 个（重构后没破）
- 其他 164 个保持绿

## 边角

1. **ide_diagnostics**：FakeChannel 的未用参数 pylance 一直报"未存取"提示，hint 级别不影响功能；用 `*_a, **_kw` 还会被报，无视。
2. **PermissionGate 单例 + channel 状态**：channel 是注入到单例上的可变字段，多 session 场景如果换 channel 要小心；当前架构一个进程一个 session，OK。
3. **未做**：模型一侧的 prompt 还没改。等以后观察 `permission_unavailable=true` 是不是还会在 TUI 模式下出现，应该不再有了——因为现在永远走 channel。

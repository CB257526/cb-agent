# cb-agent AgentSession / CLIRenderer 拆分技术报告（Stage 3）

> 写于 cb-agent v0.2，对应 commit 待签 → 本报告随该 commit 一起进库。
> 同期相关历史：`08bfd06`（EventBus）→ `5e88093`（cb_agents 流式接 EventBus）→ `61fbb51`（TodoStore 加锁）→ `db2fd3b`（ToolExecutor + CancelToken）。
> 本文是给"想看懂为什么这样拆"的人写的，不是 API 文档。
> 关键源代码：[agent/session.py](../agent/session.py)、[agent/renderers/cli.py](../agent/renderers/cli.py)、[run_agent.py](../run_agent.py)。

---

## 0. 这份报告解决什么问题

到 Stage 2 结束（commit `db2fd3b`），cb-agent 的"运行时"长成了这样：

```
┌────────────────────────────────────────────────┐
│ AgentRunner（run_agent.py，760 行单文件）         │
│                                                │
│  ├─ __init__        装配 LLM/Registry/Executor  │
│  ├─ _register_*     注册原生工具 + MCP            │
│  ├─ _section/_info  启动期 print                 │
│  ├─ _render_*       todo 面板/bash 面板/Thought  │
│  │                  渲染（写死 print）            │
│  ├─ _chat_once      构 messages → tool_loop     │
│  ├─ _tool_loop      think → 工具循环（写死 print）│
│  ├─ _handle_command 斜杠命令                     │
│  └─ run             REPL 主循环                  │
└────────────────────────────────────────────────┘
```

这套结构跑 demo 没问题，但接下来要的几件事它都做不了：

1. **Stage 4：异步 + Ctrl-C 中断**——主流程是同步阻塞的 input/think/print，没法在中断时安全停下。
2. **Stage 5：Textual TUI**——所有"现在发生了什么"全是直接 print 到 stdout，TUI 拿不到结构化的"模型在思考""工具开始执行""收到一个 chunk"这些信号。
3. **未来：FastAPI / SSE / 多前端**——前端切换意味着把所有 print 重写一遍。
4. **测试**：`_tool_loop` 内嵌 print，没法在不污染 stdout 的前提下断言"应该跑了 2 轮、调了 1 个工具"。

Stage 3 解决这一坨：把"做事"和"显示做了什么"彻底分开。做事的逻辑去 [agent/session.py](../agent/session.py)，显示的逻辑去 [agent/renderers/cli.py](../agent/renderers/cli.py)，[run_agent.py](../run_agent.py) 退化成"装配两边 + REPL 输入循环 + 斜杠命令"。中间通过 Stage 1 的 EventBus 解耦。

---

## 1. 顶层设计

### 1.1 拆分后的拓扑

```
                    ┌──────────────────────────┐
                    │    run_agent.py          │
                    │    AgentRunner           │
                    │  ─ 装配阶段 print          │
                    │  ─ /xxx 斜杠命令           │
                    │  ─ REPL: input → chat     │
                    │  ─ messages dump 钩子      │
                    └────────┬─────────┬───────┘
                             │ chat()  │ attach()
                             ▼         ▼
                     ┌────────────────────────┐
                     │   AgentSession         │     ┌─────────────────┐
                     │   纯逻辑、零 print       │ ──► │   EventBus      │
                     │  ─ 构 messages          │     │ (Stage 1)       │
                     │  ─ _tool_loop           │     └────┬────────────┘
                     │  ─ history 维护          │          │ subscribe
                     │  ─ background 通知       │          ▼
                     └─┬──────┬──────┬─────┬──┘     ┌──────────────┐
                       │      │      │     │        │ CLIRenderer   │
                       ▼      ▼      ▼     ▼        │ ─ ANSI 颜色    │
                   ContextBld LLM Registry Executor │ ─ 流式 stdout  │
                              │              │     │ ─ 面板渲染      │
                              ▼              ▼     │ ─ thread-safe │
                          (Stage 2)      (Stage 2) └──────────────┘
```

### 1.2 模块职责一句话

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `AgentSession` | 单次 chat 的全部逻辑：构 messages、跑 _tool_loop、落 history。所有"现在发生了什么"通过 EventBus.emit 派发 | 不 print、不读 stdin、不知道是谁在订阅它的事件 |
| `CLIRenderer` | 订阅 EventBus 各事件 → ANSI 渲染 → stdout/stderr | 不读历史、不知道 session、不构 messages |
| `AgentRunner`（run_agent.py） | 启动期装配（注入依赖）、REPL 输入循环、斜杠命令、messages dump 调试钩子 | 不做运行时输出（除了启动期的 _section/_info） |

每一条都是单向依赖。Session 不知道 Renderer 存在，Renderer 不知道 Session 长啥样——它们只通过 EventBus 的事件契约对话。这就是为什么 Stage 5 换 Textual 时 Session 一行不用动。

---

## 2. 关键决策

### 2.1 为什么是 EventBus 而不是回调函数 / asyncio.Queue / 直接传 renderer

考虑过的方案：

| 方案 | 淘汰原因 |
|---|---|
| `Session(on_text=cb, on_tool=cb, ...)` 一堆回调参数 | 每加一个事件类型都要改 Session 的构造签名；多前端时一个事件要分发给多个回调还得手写广播 |
| `asyncio.Queue` 让 Session 往里 put | 把 Session 绑死在 asyncio 上；现在 cb_agents 的 think 是同步流式（OpenAI SDK 同步迭代器），强行异步化要重写一遍 |
| 直接 `Session(renderer=cli)` 注入 | Session 知道了 renderer 的存在，违背单向依赖；多前端时变成 `renderers=[cli, tui, ws]` 又退化成手写广播 |
| **EventBus（选这个）** | 订阅/发布完全解耦；多前端只是多 attach 几个 renderer；现有 Stage 1 已经有了，零额外成本 |

EventBus 的代价是事件类型变成了一种"协议"——增删字段需要在 events.py 里改并跨模块同步。但这些事件已经在 Stage 1 就定型了（12 种），Stage 3 没新加事件，只是开始消费它们。

### 2.2 为什么 dump_messages 不走事件流

第一版我打算把 messages snapshot 放进 `RoundStart` 事件的字段里。马上发现两个问题：

1. **`messages` 是局部变量**，要进事件里得做 deep copy（messages 列表后面会被 _tool_loop 自己 append 工具消息），否则订阅者拿到的是会变化的引用，渲染时机不对就打错内容。
2. **dump 是面向开发者的调试通道**，不是产品级展示——它打整段 JSON、量很大、TUI/Web 用不上。把它塞进事件流相当于给所有 renderer 都强加了一个 N×8000 token 的负担。

最后选了一个更小的接口：`AgentSession.__init__(messages_snapshot_hook=callable)`，每轮 think 前调一次。这个钩子只给 CLI 这种"原型期需要看原始上下文"的前端用，TUI/Web 不传就好——它跟事件流是平行的两条通道。

### 2.3 为什么 Renderer 是 `attach()/detach()` 而不是构造时自动订阅

考虑过的方案：

```python
class CLIRenderer:
    def __init__(self, bus):
        bus.subscribe(self._on_text, TextDelta)
        bus.subscribe(self._on_tool_start, ToolStart)
        ...
```

省了一行 `attach()`，但有个隐患：单测里要"创建一个 renderer 但不订阅"以验证它对单个事件的渲染（直接调 `renderer._on_round_start(e)` 然后 capsys），如果构造时强行订阅，测试拿不干净的 stdout。

另一个原因：**幂等性**。`attach()` 实现里第一行就是 `self.detach()`——多次 attach 不会订阅多次。如果放在 `__init__` 里，要么没法重复 attach，要么得在 attach 里检测"是否已订阅"，复杂度反而上去了。

### 2.4 为什么 reasoning 不实时打而是累积到 RoundEnd 才打

CLI 的视觉次序是：
```
[round 1] 调用模型 ...
assistant > 这是最终回答         ← TextDelta 流式打的
▸ Thought for 5.6s              ← RoundEnd 时打的
  灰字逐行的思考过程
```

为什么 thought 不在前面？因为 OpenAI 协议下 reasoning_content 跟 content 是**两个并行的 chunk 流**——客户端无法预知一个 chunk 来时，"是 reasoning 先来还是 content 先来"。实测下来 deepseek-reasoner 等模型经常 reasoning 还没完时 content 就开始来了。

如果实时打 reasoning，会变成：
```
▸ Thought
  灰字思考...
assistant > 最终回...
  这里突然又来一段灰字思考！
答继续...
```

折叠起来在 RoundEnd 一次性渲染就避开这个交错问题。代价是 thought 永远显示在 answer 之后——**视觉次序违反时间次序**。但这是 hermes / Claude Code 桌面端等流式产品的通用做法（先看到答案，再看"模型为什么这么答"），用户已经习惯。

为了让"Thought for Xs"的 X 是真实耗时，CLIRenderer 在 `_on_round_start` 里记 `time.perf_counter()`，到 `_on_round_end` 算差值。这个时间是模型从开始 think 到结束 think 的总时长，不只是 reasoning 部分——但对用户来说"等了多久"才是有意义的，区分 reasoning vs content 的内部时序没必要暴露。

### 2.5 为什么 `[并发]` 标签是 renderer 算的而不是 ToolStart 事件带的字段

ToolStart 事件目前只有 `call_id / name / arguments / round_idx`，没有"是不是并发批次"这个字段。renderer 自己根据连续到达的 ToolStart 的 `threading.get_ident()` 判断——同一线程是串行，多线程是并行。

为啥不在 ToolExecutor.execute 里直接把 "is_parallel: bool" 塞进事件？两个理由：

1. **事件契约保持窄**——is_parallel 是个展示语义，不是 agent 状态语义。同样信息（多个工具同时在跑）TUI 更好的呈现可能是"并排显示 N 个进度条"而不是文本标签，事件不该绑死渲染选择。
2. **看 thread_id 也很容易**——ToolExecutor 并发分支用 ThreadPoolExecutor 启线程，串行分支在主线程跑。renderer 数 distinct thread_id 就够了，不必新增字段。

### 2.6 为什么 Done 事件由 Session 发，但 history 也由 Session 维护

最后的 final_answer 通过 TextDelta 流式发了一遍——CLI 已经打出来了。Done 事件的 `final_answer` 字段冗余吗？看场景：

- CLI：拿不拿都行（TextDelta 已经显示完了），但拿到能放进 history。
- 程序化调用（FastAPI）：必须靠 Done.final_answer 拿到完整字符串去返回 HTTP body。
- Textual TUI：可能要把对话块标记成"已完成"，需要 Done 事件触发 UI 状态切换。

所以 Done.final_answer 不是冗余，是**给"非流式消费者"的便利**——它跟 TextDelta 是两个抽象层级。

至于 history 维护，AgentSession 自己存 `self.history: List[Message]`，原因是：history 跟 LLM/Registry/Executor 是同级的会话状态，前端不该关心它的内部结构。`/history` 命令通过 `session.history` 访问，但这是只读访问。

### 2.7 为什么 CLIRenderer 内部加了 `self._lock`

EventBus.emit 在订阅者侧拿快照后释放锁，再串行调订阅者——单订阅者不会被并发调用同一个事件。但**不同事件类型的订阅者会并发**：worker 线程里 ToolExecutor emit 一个 ToolStart，主线程同时 cb_agents emit 一个 TextDelta，CLIRenderer 的 `_on_tool_start` 和 `_on_text` 会同时跑。

`print()` 在 Python 里**不是原子**的——`print(prefix, end="")` 后跟 `print(content)` 之间，另一个线程的 `print(other_line)` 可以插进来，把 `assistant > 这是回答\n` 变成 `assistant > 这是工→ 调用工具 file_read(...)\n答\n`。

`self._lock` 包住所有 print 块，把"打一行"原子化。锁是 renderer 自己的，不影响 EventBus 的吞吐。

---

## 3. 跟原版 print 行为的差异

理论上 Stage 3 是"行为不变的纯重构"——但实际上有几处微妙变化：

| 行为 | 原版 | Stage 3 | 理由 |
|---|---|---|---|
| 工具调用时序 | `print 调用 → execute → print 结果` | `emit ToolStart → execute → emit ToolComplete` | ToolExecutor 内部已经在 emit；renderer 看事件，不看代码顺序。**对单个工具是等价的**，但并发批次下 ToolStart 全部先打，再陆续打 ToolComplete（之前是阻塞顺序） |
| `[并发]` 标签 | 整批同标签 | 第二个及之后才显示 | 第一个 ToolStart 时只看到 1 个 thread_id，distinct count = 1；第二个起才能判定 |
| 工具失败渲染 | 单行 `[ERROR] 工具 X 抛异常` | `← 错误: ...` 红字 + JSON 字段 | 原版直接在 except 里造字符串；Stage 3 是 ToolComplete.is_error=True，result 已被序列化成 JSON |
| Thought 时长 | 仅 think 调用本身的耗时 | 整轮 RoundStart→RoundEnd 的耗时 | 包含工具循环的轮次切换开销，毫秒级，不显著 |
| 后台任务通知 | 注入 user_query 前缀 | 注入 user_query 前缀 + emit BackgroundNotification 事件 | 原版只塞 prompt；现在 renderer 还能蓝字提示用户"刚刚有任务完成了" |

这些差异都是事件化后的自然结果，没有功能回归。

---

## 4. 测试策略

[test/test_session_renderer.py](../test/test_session_renderer.py) 30 个测试，分三组：

### 4.1 AgentSession（11 个）

用 `FakeLLM` 替代真 OpenAI：构造时给一组预设结果，每次 `think` 按顺序返回；可选地 emit 流式事件以模拟 reasoning/content。`MagicMock` 替代 ToolRegistry。验：

- 单轮 chat 无工具 → 返回 answer，emit RoundStart/RoundEnd/Done
- 双轮 chat 含工具 → 第二轮 messages 里有 tool 消息
- history 追加（user + assistant）/ history 在空 answer 下只追加 user / clear_history
- messages_snapshot_hook 每轮调一次 / hook 异常被吞
- MAX_TOOL_ROUNDS 超限 emit Error / 不支持 FC 的模型分支 / 模型返回非预期结构

### 4.2 渲染辅助函数（8 个）

`_short_args` / `_render_thought` / `_render_todo_panel` / `_render_bash_output` 都是纯函数，输入 → 输出，不依赖 stdout。invalid JSON / wrong shape 返回 None 由调用方兜底。

### 4.3 CLIRenderer（11 个）

构造一个独立 EventBus + renderer，`with redirect_stdout(io.StringIO())` 包住 emit 验输出包含期望子串。验：

- RoundStart → 打 `[round N]`
- TextDelta → `assistant > xxx`，连续 delta 累积
- ReasoningDelta → 不实时打；RoundEnd 后打 Thought 块 + `for Xs`
- ToolStart → `→ 调用工具`，单线程不打 `[并发]`
- ToolComplete → todo 名称下走面板，bash 走 silent Done
- Error/Cancelled → 红/黄字
- BackgroundNotification → 蓝字
- TokenUsage 默认隐藏，`show_token_usage=True` 时显示
- attach 幂等 / detach 清空订阅（通过 `bus.subscriber_count` 验）

一个有趣的细节：`test_messages_snapshot_hook_exception_swallowed` 故意让 hook 抛异常，确认 chat 仍能完成。日志层会打一条 `messages_snapshot_hook 抛异常，已吞`，单测断言不看 stderr，这是正常的。

---

## 5. 跟未来阶段的接口契约

### 5.1 Stage 4（异步 + Ctrl-C）会动什么

- **AgentSession.chat 改成 async**：里面的 `executor.execute` 已经是同步阻塞；要么 wrap 成 `loop.run_in_executor`，要么把 Executor 也异步化。
- **REPL 改成 prompt_toolkit / asyncio.Queue**：input 不能阻塞主 loop。
- **CancelToken 接进 chat()**：Ctrl-C → `token.cancel()` → cb_agents.think 检测到 token.event 后中断 → emit Cancelled → renderer 打 `✗ 已取消`。

CLIRenderer 不动；事件协议不动。

### 5.2 Stage 5（Textual TUI）会做什么

- 写一个 `TextualRenderer`，订阅同样的事件，但写到 `RichLog` widget 而不是 stdout。
- AgentRunner 改成可选 renderer：`--ui textual` 启 TextualRenderer，默认还是 CLIRenderer。
- AgentSession 一行不动。
- messages dump 钩子改成往 TUI 的"调试面板"塞，而不是 print。

### 5.3 Stage 6+（FastAPI / SSE）

- `WebRenderer` 把事件序列化成 SSE 帧。
- `AgentSession.chat` 在 HTTP handler 里跑，每个请求一个 EventBus + WebRenderer 实例。
- history 持久化到 DB（session 加 storage 注入）。

---

## 6. 已知遗留问题 & 后续要管的

1. **think 暂未拿 cancel_event**：cb_agents 已经支持，但 AgentSession 还没把 token 透下去。Stage 4 的活，留作下个 commit。
2. **Renderer 跟 Session 之间没有"chat 边界"事件**：Done 是单条 chat 的边界，但 RoundStart/RoundEnd 是工具循环内的边界。如果未来要在 chat 起止做 UI 状态切换（"模型在响应中" → "可以输入"），可能需要加 ChatStart/ChatEnd 事件。Stage 4 视情况补。
3. **messages_snapshot_hook 是面向 CLI 的"逃生舱"**：如果 Stage 5 也想看原始 messages，得想个更通用的"调试通道"，而不是给每个前端各开一个 hook。当前的妥协是：原型期 dump 调试只有 CLI 用得到，TUI/Web 不需要。
4. **ANSI 颜色检测在 Windows 上有 fallback**：先尝试 `SetConsoleMode` 启 VT，失败用 `colorama`，再失败禁用颜色。Stage 5 用 Textual 之后这套就退役了——Textual 自己管渲染。

---

## 7. 一句话总结

Stage 3 把单文件 760 行的 AgentRunner 拆成 logic（AgentSession 280 行）+ display（CLIRenderer 380 行）+ wiring（run_agent 370 行），用 EventBus 做单向解耦。148 个单测验证语义不变，行为差异都是事件化的自然产物。下一步可以无痛接异步、TUI、Web。

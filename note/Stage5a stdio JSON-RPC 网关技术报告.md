# cb-agent stdio JSON-RPC 网关（Stage 5a）

> 写于 cb-agent v0.4，对应 commit 待签。
> 关键源代码：[agent/transport/jsonrpc.py](../agent/transport/jsonrpc.py)、
> [agent/transport/gateway.py](../agent/transport/gateway.py)、[run_agent.py](../run_agent.py)。
> 协议参考 Hermes Agent（开源），但裁掉了多 session、长 RPC 线程池、TeeTransport 等用不上的复杂度。
> Stage 5b（独立 UI 客户端）不在本仓库，跟 agent 物理隔离。

---

## 0. 这一阶段的目标

到 Stage 4 结束，cb-agent 内部接口齐全：EventBus（事件订阅）+ chat_async（异步入口）+
CancelToken（中断）+ MarkdownAccumulator（流式安全切分）。但所有这些都"圈"在 Python 进程里——
唯一的消费者是同进程的 CLIRenderer。

要把 UI 做出 Claude Code CLI 那种水平（分区、可折叠工具块、底部固定输入框、状态栏），有两条路：

| 路 | 代价 |
|---|---|
| **A. 在 Python 进程里直接做 TUI**（Textual / Rich / curses） | 仓库里多 ~500 行跟 agent 无关的 UI 代码，污染学习焦点 |
| **B. 把事件流通过 stdio 暴露出去，UI 是独立子进程**（参考 Hermes） | Python 这边只多 transport 层（~250 行），UI 用什么语言、长什么样跟 agent 仓库无关 |

选 B。本阶段（5a）只做 Python 这一侧的 transport 层；UI 客户端（5b）放到独立目录或独立仓库，
跟 cb-agent 物理隔离。

---

## 1. 协议选型：JSON-RPC 2.0 over NDJSON

### 1.1 为什么是 JSON-RPC

- **标准协议**：[jsonrpc.org/specification](https://www.jsonrpc.org/specification) 上有完整定义，
  错误码、响应格式、notification 概念都现成；UI 客户端不论用什么语言，找一个 JSON-RPC 库 5
  分钟就能对上
- **双向语义清楚**：同一个 channel 上既有"事件推送"（无 id 的 notification），又有"请求-响应"
  （带 id 的 request/response），UI 端不用自己设计区分机制
- **Hermes 也用这个**：未来想换 Hermes 那条 Node Ink TUI，协议层零改动

### 1.2 为什么是 NDJSON 而不是 LSP 那种 Content-Length 分帧

LSP（Language Server Protocol）用 `Content-Length: N\r\n\r\n` + body 的方式分帧，主要为了
支持非 UTF-8 安全的字节序列。但我们：

- 全 UTF-8（agent 事件内容都是文本）
- 行内不会出现裸 `\n`（`json.dumps` 默认会把 `\n` escape 成 `\\n`）

所以 NDJSON 完全够：**一行一个 JSON object，`\n` 分帧**。客户端写 `for line in stdin: json.parse(line)`
就完事，比 LSP 简单一个数量级。Hermes 也是这套。

### 1.3 事件 type 直接复用 cb-agent 现有 events.py 的 type 字段

写 transport 时纠结过：要不要把 cb-agent 的事件名（`text_delta` / `tool_start` / `tool_complete`）
重映射到 Hermes 那套（`message.delta` / `tool.start` / `tool.complete`）？

最后**没映射**。理由：

- 事件名是给 UI 实现用的契约，跟 Hermes 不需要二进制兼容
- 现有 events.py 的 type 字段（`@dataclass` 里 `type: str = field(default="text_delta", init=False)`）
  序列化时直接当 type，零额外代码
- 将来真要兼容 Hermes UI，写一个 5 行的事件名映射函数就够了，不动 events.py

---

## 2. 实现：两个文件 + 一个 argparse 分支

### 2.1 jsonrpc.py：协议层（~135 行）

只有三件事：

```python
make_event_message(event)            # dataclass 事件 → JSON-RPC notification
make_response(rpc_id, result|error)  # 标准 JSON-RPC response
class StdioTransport:
    write(msg) -> bool               # 锁保护写 stdout，BrokenPipe 时返回 False
    read_loop() -> Iterator[dict]    # 阻塞迭代 stdin，遇到非 JSON 行回写 -32700
```

`StdioTransport.write` 是**全局唯一的 stdout 写入点**——所有事件、所有响应、所有错误都走这里。
所以锁就加在这个地方（`threading.Lock`），cb-agent 任何线程任何时候 emit 事件都不会出现"半行
JSON 被另一行截断"。

### 2.2 gateway.py：业务层（~270 行）

把 EventBus + AgentSession 跟 stdio 接起来。线程关系：

```
+----------------------+        +---------------------+        +----------+
| asyncio loop (main)  | <----- | stdin reader thread | <----- | UI stdin |
|  - chat_async        |  call  |  - read_loop()      |  read  |          |
|  - schedule via      |  via   |  - dispatch RPC     |        |          |
|    run_coroutine_    |  fut   |                     |        |          |
|    threadsafe        |        |                     |        |          |
+----------------------+        +---------------------+        +----------+
        |
        | bus.emit (在工具线程 / chat 线程 / 主线程都可能)
        v
+----------------------+        +-----------+
| StdioTransport.write | -----> | UI stdout |
|  (lock 保护)         |        |           |
+----------------------+        +-----------+
```

四个 RPC method：

| method | 同步/异步 | 行为 |
|---|---|---|
| `prompt.submit` | **立即响应 + 异步执行** | 立刻 ack `{status:"accepted"}`，把 `chat_async` 投到 asyncio loop。chat 期间 EventBus 自然 emit 事件流到 UI，结束时 emit `done` 事件 |
| `session.cancel` | 同步 | 直接调 `session.current_cancel_token.cancel()`（threading.Event.set 是线程安全的） |
| `session.quit` | 同步 | 写 `{bye:true}` 响应，set asyncio.Event 让 serve_forever 退出 |
| `session.clear_history` | 同步 | 调 session.clear_history() |

未知 method → `-32601`，参数无效 → `-32602`，session 正忙 → `-32001`，内部错误 → `-32603`。

### 2.3 关键决策

#### a) 为什么 prompt.submit 要立即 ack 而不是等 chat 完成再 ack

想过让 ack 跟着 chat 完成一起回，那样 UI 端 await 一个 promise 就拿到答案了，看上去优雅。但：

- **chat 可能跑几十秒**：期间 RPC 等于挂着；UI 端这个连接就堵死了，连 `session.cancel` 都发不进来
  （reader 线程在等响应？不对——但 UI 端如果照"一个 RPC 一个 response"的常规写法走，会把自己锁住）
- **答案本来就在事件流里**：`done` 事件带 `final_answer`，UI 拿事件就够了，不需要 RPC response 再带一遍

所以 ack 只表达**"我接受了你的请求"**，跟 HTTP `202 Accepted` 一个意思。chat 期间 UI 自由发
cancel，结束看 done 事件。

#### b) 为什么 session 模式做 busy 拒绝而不是排队

cb-agent 是单 session 的玩具 agent，理论上一个 user 一次只 chat 一句。如果 UI 在 chat 没结束
时又 submit 新 prompt，这通常是 UI bug 或用户误操作——直接拒（`-32001 session busy`）让 UI
自己处理（弹个 toast 之类），比让 Python 这边搞个队列简单，且不会出现"事件流跟不上、UI 看到第二
条 chat 的 round_start 时第一条还没收完"这种乱序问题。

#### c) 为什么要把 sys.stdout 切到 sys.stderr

这是从 Hermes 抄的，**踩过这个坑后再写就不会忘**：

stdout 已经被 JSON 协议占用。agent 内部任何 `print(...)`、`traceback.print_exc(...)`、
第三方库的诊断输出，都会**直接写一行非 JSON 进 stdout 污染协议**。UI 端 json.parse 一遇到
`Initialized MCP client...` 就崩。

切到 stderr 之后：

- agent 启动期日志、调试 print、Python 的 deprecation warning 都走 stderr
- UI 端可以选择性地读 stderr 显示在"日志面板"里，或者直接丢弃

run_agent.py 里 `--transport jsonrpc` 分支启动顺序：

```python
real_stdout = sys.stdout       # 先把真 stdout 抢救出来
sys.stdout = sys.stderr        # 再切，AgentRunner 启动期 print 自动走 stderr
runner = AgentRunner(...)      # 这里面所有 _section / _info / print 都打到 stderr
gw = Gateway(
    transport=StdioTransport(stdin=sys.stdin, stdout=real_stdout),  # gateway 用真 stdout
    redirect_stdout_to_stderr=False,  # 已经在外层切过了，gateway 不要再切
)
```

#### d) 为什么 Gateway.serve_forever 用 asyncio.Event 而不是 future.result()

最初版本是：reader 线程读 EOF → `loop.call_soon_threadsafe(loop.stop)`。能跑，但会丢一些
"loop.stop 后还在排队的"事件——比如 session 收尾正在 emit Done，reader 已经 EOF 了，loop 一
stop done 事件就没机会写出去。

改成 `asyncio.Event`：reader EOF 时 `set` 这个 event，主协程 `await stop_event.wait()` 醒来后
loop 自然结束所有 pending 任务再退出。chat 收尾的事件能完整地刷进 stdout。

---

## 3. 一个时序"小坑"（写文档警告）

冒烟测试时发现：如果 UI 启动后**立刻**发 quit，stdout 上看到的顺序可能是：

```
{"jsonrpc":"2.0","id":"q1","result":{"bye":true}}        ← quit 响应先
{"jsonrpc":"2.0","method":"event","params":{"type":"gateway_ready",...}}  ← ready 事件后
```

原因：`Gateway._serve_async` 启动 reader thread 后才 emit `gateway_ready`；如果 stdin
已经有 quit 等着，reader thread 会先于 emit ready 那行处理掉 quit。

**这不是 bug，是 JSON-RPC 协议本来就不保证顺序**——id 关联响应，事件 type 区分类型。但 UI 实现
要注意：

- ❌ 不要写"等 ready 之后再发 RPC"这种依赖
- ✅ 启动后想发什么就发，看 type/id 各自处理

如果 UI 真需要等 agent 准备好（比如显示 model 名再渲染界面），订阅 `gateway_ready` 事件就行。

---

## 4. 测试策略

[test/test_transport.py](../test/test_transport.py) 14 个测试，三组：

### 4.1 序列化（4 个）

- 事件 dataclass → JSON-RPC notification 格式正确
- ToolStart 的 arguments dict 完整保留
- response 成功 / 失败两种 envelope

### 4.2 StdioTransport（5 个）

- write 加换行、flush
- read_loop 一行一条，空行跳过
- 非 JSON 行回写 `-32700`，合法行不丢
- 非对象行回写 `-32600`
- **并发 write 不交错**：两个线程各跑 50 次，所有输出行都必须是合法 JSON

### 4.3 Gateway 端到端（5 个）

用 FakeLLM + 真实 EventBus + 真实 AgentSession + 自定义可阻塞的 _PipeStdin 跑：

- prompt.submit → 收到 ready / accept / text_delta / done 事件
- 未知 method → `-32601`
- 空 text → `-32602`
- session.quit → `{bye:true}` 响应、loop 退出
- session.clear_history → `{cleared:true}` 响应

### 4.4 全量回归

177 个测试全绿（13 + 12 + 9 + 20 + 30 + 15 + 14 + 64）。

---

## 5. 给 Stage 5b（UI 客户端）的接口契约

这一节是写给将来的 UI 实现看的——不论用 Node Ink、Python textual、Go bubbletea 还是 Rust
ratatui，只要遵守下面这份契约就能接上。

### 5.1 启动

UI 是**主进程**。Python agent 是**子进程**：

```bash
# 伪代码（具体看 UI 用什么语言）
proc = spawn("python", ["run_agent.py", "--transport", "jsonrpc", "--no-mcp", "--no-ctx"])
# 或者：proc = spawn("path/to/cb-agent.exe", ["--transport", "jsonrpc"])
```

UI 拿到 `proc.stdin`（写 RPC）和 `proc.stdout`（读事件 + 响应）。`proc.stderr` 是 agent 的日志，
可选订阅。

### 5.2 入站事件 schema

UI 收到的所有 stdout 行都是合法 JSON，分两种：

**事件（无 id）**：

```json
{"jsonrpc": "2.0", "method": "event", "params": {"type": "<event_type>", ...其他字段}}
```

**响应（有 id）**：

```json
{"jsonrpc": "2.0", "id": "<rpc_id>", "result": {...}}
{"jsonrpc": "2.0", "id": "<rpc_id>", "error": {"code": <int>, "message": "..."}}
```

事件 type 全集（按发生顺序大致）：

| type | 字段 | 何时发 |
|---|---|---|
| `gateway_ready` | model | gateway 启动后 |
| `round_start` | round_idx, max_rounds | 每轮工具循环开始 |
| `text_delta` | delta, accumulated, round_idx | 模型流式输出文本 |
| `reasoning_delta` | delta, accumulated, round_idx | 模型思考过程（DeepSeek 等） |
| `tool_call_planned` | call_id, name, arguments_json, round_idx | 模型决定要调什么工具 |
| `tool_start` | call_id, name, arguments(dict), round_idx | 工具开始执行 |
| `tool_complete` | call_id, name, result, duration_seconds, is_error, round_idx | 工具完成 |
| `round_end` | round_idx, has_tool_calls, final | 每轮工具循环结束 |
| `token_usage` | prompt_tokens, completion_tokens, total_tokens, round_idx | 流式响应结束时 |
| `cancelled` | where, round_idx | 用户中断 |
| `error` | where, message, exception_type, round_idx | 出错（非致命） |
| `done` | final_answer, rounds_used, cancelled | 整个 chat 结束 |
| `background_notification` | task_id, status, exit_code, output_path | drain 出来的后台任务 |

### 5.3 出站 RPC

| method | params | 响应 result |
|---|---|---|
| `prompt.submit` | `{text: string}` | `{status: "accepted"}` 或 `error.code=-32001` (busy) |
| `session.cancel` | `{}` | `{cancelled: bool}` |
| `session.clear_history` | `{}` | `{cleared: true}` |
| `session.quit` | `{}` | `{bye: true}` 然后 stdin 关闭 → agent 退出 |

### 5.4 流式 Markdown 渲染建议

`text_delta.accumulated` 是当前轮**累积全文**，UI 想要的话可以：

- 简单做法：只用 `delta` 追加显示
- 进阶做法：用 `accumulated` 全量重渲染——但要做"安全切分"（不在 ``` 代码块内闭合时不切），
  否则代码块边界会闪。Stage 4 的 MarkdownAccumulator 已经被删了，**这部分逻辑由 UI 实现**

### 5.5 Cancel / Quit 协议

Ctrl-C 的处理：UI 端绑定按键 → 看当前是否 chat 中（看到过 `round_start` 但还没看到 `done`）：

- chat 中：发 `session.cancel` RPC
- 空闲：发 `session.quit` RPC，等 `{bye:true}` 响应 + EOF，然后 UI 自己退

### 5.6 错误处理

- agent 子进程崩了 → UI 看到 stdout EOF，可以读 stderr 显示崩溃信息
- UI 关了 → agent 看到 stdin EOF，gateway 的 reader_loop 退出，serve_forever 返回，进程退出
- RPC 错误码 → UI 显示用户友好提示，不要让用户看到原始 -32601

---

## 6. 已知遗留 & 后续

- **没做请求 schema 校验**：UI 发任何乱七八糟的 params 都只在 dispatch handler 里手工 `if isinstance`。
  如果以后 method 多了（slash 命令、文件上传等），考虑引入 pydantic 或 jsonschema 自动校验
- **gateway_ready 事件不在 events.py 里**：它是 gateway 自己发的"协议层信号"，不是 agent 业务事件。
  目前裸写在 gateway.py 里，将来如果还有更多协议层事件（gateway_shutdown 等）考虑独立成
  `transport_events.py`
- **没有 heartbeat**：UI 长时间没收到事件不知道是 chat 在跑还是 agent 卡死。后续可以考虑每 N 秒
  发一次空的 `heartbeat` 事件，或让 UI 自己 30 秒发个 `session.ping` RPC
- **CLI 启动期日志还在 stderr**：用户在 jsonrpc 模式下能看到一堆 `[初始化 cb-agent]` 之类的输出。
  UI 客户端可以选择性吞掉 / 显示在面板里 / 完全忽略

---

## 7. 一句话总结

Stage 5a 给 cb-agent 加了一层 ~250 行的 stdio JSON-RPC 网关：agent 内部一行不动，事件流通过
NDJSON 暴露出去，未来任何语言任何形态的 UI 客户端都能挂上来，agent 仓库永远只有 agent 代码。
14 个新单测 + 全量 177 个回归全绿。Stage 5b（UI 客户端实现）独立项目独立 commit，跟本仓库零耦合。

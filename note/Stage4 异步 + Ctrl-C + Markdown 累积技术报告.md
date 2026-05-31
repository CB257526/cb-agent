# cb-agent 异步入口 + Ctrl-C 中断 + Markdown 累积器（Stage 4）

> 写于 cb-agent v0.3，对应 commit 待签 → 本报告随该 commit 一起进库。
> 同期相关历史：`08bfd06`（EventBus）→ `5e88093` / `61fbb51` / `db2fd3b`（Stage 1+2）
> → `62677a3`（Stage 3 拆 Session/Renderer）→ 本次。
> 关键源代码：[agent/session.py](../agent/session.py)、[agent/executor.py](../agent/executor.py)、
> [agent/renderers/markdown.py](../agent/renderers/markdown.py)、[run_agent.py](../run_agent.py)。

---

## 0. 这份报告解决什么问题

到 Stage 3 结束（commit `62677a3`），cb-agent 的"运行时"形状是：

```
input("you > ") ──► AgentSession.chat(user_input)  # 同步阻塞
                       │
                       ▼
                   _tool_loop ──► think (流式同步迭代)
                       │           │
                       │           └─ event_bus.emit(TextDelta...)
                       ▼
                   executor.execute (串行 / 线程池并发)
```

跑 demo 没问题，但对用户场景有三个"半成品"：

1. **Ctrl-C 体验**：用户按 Ctrl-C 想中断当前回答，整个进程直接挂掉（`KeyboardInterrupt` 从
   stream 的 `for chunk in response` 抛出，穿透 chat / REPL / 主线程）。要重新跑一次得重新
   加载 MCP / 嵌入模型 / skills，10 秒起步。
2. **没有异步入口**：Stage 5 要做 Textual TUI、Stage 6+ 要做 FastAPI/SSE，两者都是 asyncio
   原生世界。`session.chat()` 是阻塞 IO，前端要么忍受界面卡死，要么各自实现 `to_thread` 包装。
3. **Markdown 流式渲染**：CLI 直接 `print(chunk, end="")` 字符流没问题。但 Textual 的
   `RichLog` / Rich 的 `Markdown` 渲染器是按段落或代码块组装，把"半个 fence"喂进去会闪烁、
   错位。需要一个"安全切分点"组件把流式 chunk 切成稳定可渲染的前缀 + 待稳定缓冲。

Stage 4 解决这三件事，并且做完之后 Stage 5（Textual）就只剩"写 widget"——核心机制全在。

---

## 1. CancelToken 端到端集成

### 1.1 既有基础

Stage 2（commit `db2fd3b`）已经引入了 [agent/cancel.py](../agent/cancel.py) 的 `CancelToken`
（包 `threading.Event`）和 ContextVar（`_current_token`），还有 cb_agents 流式循环里每个 chunk
边界看 `cancel_event.is_set()`。但当时只有 LLM 流式这一段被串起来了——session 和 executor 都
没消费 token。Stage 4 把链路补完。

### 1.2 链路全貌

```
REPL(SIGINT handler)
    └─ token.cancel()                          # 主线程 signal 内
       │
       ▼
 ┌─────────────────────────────────────────────┐
 │  AgentSession._tool_loop                    │
 │   ① 进新一轮前 token.is_cancelled() ?       │ → emit Cancelled+RoundEnd 收尾
 │   ② think(cancel_event=token.event) ──┐    │
 │   ③ think 返回后 token.is_cancelled() ?│    │ → 不再发新一轮工具
 │   ④ executor.execute(cancel_token=token)    │
 └─────────────────────────────────────────────┘
                                          ▼
                          ┌──────────────────────────┐
                          │ ToolExecutor.execute     │
                          │  ⓐ submit 前看一眼 → 全占位 │
                          │  ⓑ 串行：每个工具前看 → 占位 │
                          │  ⓒ 并发：已 submit 的 join  │
                          └──────────────────────────┘
                                          ▼
                          ┌──────────────────────────┐
                          │ cb_agents._think         │
                          │  每个 chunk 边界看 → break  │
                          │  emit Cancelled(llm_stream)│
                          └──────────────────────────┘
```

### 1.3 关键决策

#### a) 为什么 token 不影响"已 submit 的工具"

`concurrent.futures.ThreadPoolExecutor` 不支持线程级中断——Python 没有 thread.interrupt() 这种
机制（GIL 下没有可移植的强行中断方法）。我有三个选项：

| 方案 | 评价 |
|---|---|
| 强行 `thread.kill` / 抛异步异常 | 不可移植；Windows 下 ctypes hack 不可靠；可能让 bash/MCP 子进程变孤儿 |
| 把工具改 async，用 task.cancel() | bash/MCP/向量库 SDK 都是同步 API；改不动 |
| **已 submit 的让它跑完，未跑的回占位**（选这个） | 接受"工具粒度"中断不可能，承诺更小但更稳 |

工具粒度的硬中断需要工具自己实现（bash 已经有 timeout、MCP 有自己的 cancel 协议）。Session 层
能做的只是**不发起新工具调用**——这跟用户感知一致：按 Ctrl-C 后正在跑的那个 search 跑完，但
不会再往下连续调三个 file_read。

#### b) 为什么 cancel 时还要给"已取消"工具回占位 message

OpenAI 协议要求每个 `tool_calls[i].id` 必须有对应的 `role: tool, tool_call_id: <id>` 消息回灌，
不然下一轮 `messages` 直接 400。Cancel 后 chat 不会有"下一轮"，但**返回的 ToolCallResult 会被
回灌进 messages**——如果不回灌，两个问题：

1. `messages` 状态破坏，clear_history 之前如果这个 chat 留在 history 里，下次 chat 拼出去就 400
2. 模型如果在某个时机恢复（比如未来加 retry），看到的不是"我让它做 X，被取消了"，而是"我让它
   做 X，没有结果"——前者更准确

占位 result 形如 `{"cancelled": true, "reason": "user requested cancel"}`，is_error=True，duration=0。
模型完全有能力理解这个语义。

#### c) 为什么 `is_cancelled` 要用 `()`

写 session 时我把它当 property 用了 `if token.is_cancelled:`——bound method 永远 truthy，结果
**第一轮就直接退出**，30 个测试一起红。修完后立的规矩：cancel.py 现有约定就是方法不是属性
（test_executor.py 早就有 `token.is_cancelled()` 了），不再改 cancel.py 加 @property，免得动一处
全工程跟着改。**写 review checklist：每出现 `token.is_cancelled` 看下后面有没有 `()`。**

---

## 2. asyncio 入口：chat_async + asyncio REPL

### 2.1 chat_async 的实现

```python
async def chat_async(self, user_query, cancel_token=None):
    return await asyncio.to_thread(self.chat, user_query, cancel_token)
```

就这么一行。考虑过的"更原生"方案：

| 方案 | 淘汰原因 |
|---|---|
| 把 chat 整体改 async，think 改 async iteration | OpenAI Python SDK 流式只有 `client.chat.completions.create(stream=True)` 同步迭代器；要 async 得换 `AsyncOpenAI`，等于把 cb_agents 重写一遍，且 deepseek/智谱/百炼的 SDK 兼容性各不相同 |
| 用 `loop.run_in_executor` | 跟 `asyncio.to_thread` 等价，后者是 3.9+ 的语法糖，更短 |
| 整个跑独立线程，异步只暴露 queue | 复杂度高，没有收益——Textual / FastAPI 反正都能 await 一个 to_thread coroutine |

`asyncio.to_thread` 的限制写在 chat_async 的 docstring 里：**外部 `task.cancel()` 不会中止
chat 线程**。要中断必须调 `cancel_token.cancel()`。这是接受 ThreadPoolExecutor 局限的代价。

### 2.2 REPL 改造

旧版：

```python
def run(self):
    while True:
        user_input = input("you > ")
        self.session.chat(user_input)
```

`KeyboardInterrupt` 从 `chat` 里抛出来，穿透到外层退出进程。

新版（[run_agent.py:264](../run_agent.py#L264) 起）：

```python
def run(self):
    asyncio.run(self._run_async())

async def _run_async(self):
    while True:
        user_input = await asyncio.to_thread(input, "you > ")
        ...
        await self._run_chat(user_input)

async def _run_chat(self, user_input):
    token = CancelToken()
    prev = signal.getsignal(SIGINT)
    signal.signal(SIGINT, lambda *_: token.cancel())
    try:
        await self.session.chat_async(user_input, cancel_token=token)
    finally:
        signal.signal(SIGINT, prev)
```

注意几个细节：

- **input() 也要 to_thread**：直接在 async 函数里调 input 会把整个 loop 阻塞，async 化就没意义了
  （signal handler 也跑不起来——signal 在 Python 里是"等到下一个 bytecode"才递交，loop 阻塞
  时根本递交不到）
- **SIGINT handler 只在 chat 期间安装**：空闲时（input 阻塞）保留默认行为（KeyboardInterrupt
  → 退出 REPL → "再见"），跟用户预期一致："对话中按 Ctrl-C = 中断本次"，"输入态按 Ctrl-C = 退出"
- **finally 里 restore handler**：保证下次 input 阻塞时 Ctrl-C 还能退出。如果不 restore，下次
  按 Ctrl-C 会调一个空 token，没人响应，看起来"假死"

### 2.3 为什么不用 `loop.add_signal_handler`

Windows ProactorEventLoop 不支持 `add_signal_handler`，会抛 `NotImplementedError`。`signal.signal`
是跨平台兜底，唯一代价是 handler 在主线程运行（不在 loop thread）——但我们 handler 只调
`token.cancel()`（线程安全的 `Event.set`），没问题。

---

## 3. MarkdownAccumulator

### 3.1 问题

CLI 模式下 cb_agents 的 TextDelta 直接 `print(chunk, end="")` 没问题：终端是字符流，半个 fence
也只是字符显示，最终视觉跟拼起来的整段一致。

但 Stage 5 Textual / Stage 6 Web 都用 Markdown 渲染器，喂半段会出问题：

- `'```python\nprint(1'` 这时 fence 没闭合 → 渲染器把 ```python\nprint(1 整个当普通段落
- `'# 标'` 标题没换行 → 渲染成空标题或半个标题
- `'**加粗'` 没闭合 → 整个剩余渲染加粗
- 表格没收齐就 reflow

这些渲染会随后续 chunk 来了再"改写"，视觉上闪烁；某些渲染器（Textual RichLog）甚至直接挂了。

### 3.2 设计

```python
acc = MarkdownAccumulator()
for chunk in stream:
    new_stable = acc.feed(chunk)  # 返回这次新增的"稳定前缀"
    if new_stable:
        widget.write_markdown(new_stable)  # 渲染（永不变化）
final = acc.flush()  # chat 结束，剩余 pending 一次性当稳定
widget.write_markdown(final)
```

切分规则（保守，倾向于晚切）：
- 末尾在某个未闭合 fence (```) 内 → 整段 pending，不切
- 否则切到**最后一个 `\n\n`**（段落分界）；没有就一个字符不返回

### 3.3 关键决策

#### a) 为什么是段落级而不是 token 级 / 行级

- token 级：每来一个 token 喂渲染器，Textual 会 reflow 整个 widget，性能差且闪
- 行级：单行内的加粗 / 代码 / 链接没闭合时仍然会"渲染→改写"
- **段落级**：Markdown 的最小完整渲染单位就是段落（block element）。一个段落一旦完成，渲染
  结果就稳定了——这是 Markdown 规范本身保证的，不是经验

#### b) 为什么 fence 检测只看"独立行 ``` 计数"

Markdown 规范本身就这样定义 fence：必须独占一行（前面只能是空白）。所以 `count(三反引号 in 独立行) % 2 == 1`
就是"还在 fence 内"。`text.split('\n')` 后逐行 `lstrip().startswith('```')` 计数即可。

不处理：
- 嵌套 fence：Markdown 规范不支持，行内 ``` 也不会被当 fence
- 行内代码：`` `x` `` 不参与 fence，没切分影响

#### c) 为什么 CLI 暂时不切

CLIRenderer 已经有 `print(chunk, end="")` 的旧路径，视觉是对的。如果接 MarkdownAccumulator：
- 用户看到的不再是字符流，而是"等一段时间整段冒出来"——比旧版差
- ANSI 颜色处理（thought 灰字、用户 you > 高亮）跟段落渲染冲突
- 没有真实 Markdown 渲染器，切了也只是 print 段落

所以 Stage 4 这边 CLIRenderer 不动；MarkdownAccumulator 是给 Stage 5 准备的"组件库"。Stage 5
的 Textual TUI 会订阅同样的 TextDelta，但走 Accumulator → RichLog 写入。

---

## 4. 测试策略

[test/test_stage4.py](../test/test_stage4.py) 共 15 个 case，分三块：

### 4.1 AgentSession + Cancel（3 个）

- `test_cancel_before_chat_returns_immediately`：传一个已 cancel 的 token 进 chat → think 一次
  不调，Done.cancelled=True
- `test_cancel_mid_stream_no_more_rounds`：FakeLLM 在 think 内部 set cancel_event；本轮虽然
  返回了 tool_calls，第 2 轮也不再调 think
- `test_cancel_during_chat_via_current_token`：用一个独立线程通过 `session.current_cancel_token`
  调 cancel——验证"REPL 主线程 SIGINT handler"这条路径在并发下安全

### 4.2 ToolExecutor + Cancel（3 个）

- `test_serial_cancel_skips_remaining`：第一个工具跑完后 cancel → 第二个变占位
- `test_pre_submit_cancel_all_placeholders`：execute 入口已 cancel → runner 一次不调，全部占位
- `test_parallel_completes_normally_when_not_cancelled`：保护原有并发路径不被 token 参数破坏

### 4.3 chat_async（2 个）

- `test_chat_async_basic`：asyncio.run 跑通基本回答
- `test_chat_async_cancel_via_token`：await 期间外部线程 cancel → 协程正常返回不抛

### 4.4 MarkdownAccumulator（7 个）

- 空 feed / 无段落分界不吐 / 段落分界吐前一段
- 未闭合 fence 整段 pending
- 闭合 fence 后段落正常切
- flush 直出 + flush 后 feed 直出
- reset 清状态

### 4.5 全量回归

```
13 EventBus + 12 cb_agents + 9 Todo + 20 Executor + 30 Session/Renderer
+ 15 Stage4 + 64 Bash = 163 测试
```

修了一个 Bug 引入的 14 个红 case：`token.is_cancelled` 写漏 `()`，session 第一轮直接退出。
修复后全绿。

---

## 5. 跟未来阶段的接口契约

### 5.1 Stage 5（Textual TUI）会怎么用

```python
# 主 widget
async def on_user_input(self, msg):
    self._cancel_token = CancelToken()
    self._chat_task = asyncio.create_task(
        session.chat_async(msg, cancel_token=self._cancel_token)
    )

async def on_key_ctrl_c(self):
    if self._cancel_token:
        self._cancel_token.cancel()  # chat_task 自然收尾

# Markdown 渲染
def on_text_delta(self, event: TextDelta):
    new_stable = self.md_acc.feed(event.delta)
    if new_stable:
        self.rich_log.write(Markdown(new_stable))
```

Session/Renderer/Accumulator 都不用动。

### 5.2 Stage 6+（FastAPI / SSE）

```python
@app.post("/chat/stream")
async def stream(req):
    queue = asyncio.Queue()
    bus.subscribe(lambda e: queue.put_nowait(e), TextDelta)
    asyncio.create_task(session.chat_async(req.text, cancel_token=token))
    async def gen():
        while True:
            e = await queue.get()
            yield f"data: {json.dumps(asdict(e))}\n\n"
    return StreamingResponse(gen())
```

cancel 通过另一个 endpoint POST 触发 `token.cancel()`。

---

## 6. 已知遗留 & 后续要管的

1. **chat_async 多 chat 并发未保护**：当前 session 的 `current_cancel_token` 是单例字段，同一个
   session 同时跑两个 chat_async 会互相覆盖。Stage 5 单 TUI 不会出现，但如果 Stage 6 多用户共享
   一个 session 会出问题——届时改为每个 chat 自己持有 token、不放 self
2. **MarkdownAccumulator 不处理"\r\n"换行**：Windows 流式假设 LF 行末。如果未来对接的 LLM 返回
   CRLF（暂未见过）需要预处理
3. **SIGINT handler 没法在子线程装**：当前 REPL run 跑在主线程没问题。如果未来 run() 要被嵌入
   到一个非主线程的容器里（不太可能），signal.signal 会抛 ValueError，try/except 里已经兜底
   退化到"无 Ctrl-C 中断"
4. **think 的 reasoning_content 没接 cancel**：当前 cancel 的是 content/tool_calls 的 chunk
   循环。如果 reasoning 阶段卡很久，cancel 要等到下一段 reasoning chunk 才生效。实测 deepseek-reasoner
   reasoning chunk 也是 1-2 秒一段，可接受

---

## 7. 一句话总结

Stage 4 把 Stage 2 留下的 CancelToken 端到端串通（session/executor 各检查点全补齐），加了一个
`asyncio.to_thread` 包的 chat_async 让前端能 await，加了一个段落级 MarkdownAccumulator 给
Stage 5 准备。15 个新单测 + 全量 163 个回归全绿。下一步 Stage 5 写 Textual UI 时这边一行不用动。

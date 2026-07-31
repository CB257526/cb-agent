# Canonical History 与 Compact 重构技术报告

> 更新于 2026-07-30。本文只描述 `refactor/canonical-history` 分支已经落地的实现，代码事实源为 `core/conversation_history.py`、`agent/history_journal.py`、`agent/legacy_history_migrator.py`、`agent/compaction.py` 与 `agent/session.py`。

## 1. 重构结论

本次重构删除了“长期 history + 每轮可变 messages + transcript/active-turn/compact 多份恢复状态”并存的旧链路。现在只维护一份模型可见历史：

```text
ConversationHistory
        │
        ├── 普通运行：只能 append
        ├── 正式 compact：generation + 1 后整体 replace
        └── 持久化：history.jsonl
```

每次请求发送给 provider 的 `messages` 只是一个短生命周期快照：

```text
稳定 system 外壳 + canonical history
```

请求组装阶段不再按长度、消息数或角色过滤旧历史，也不再修剪孤儿 tool、替换图片内容、规范化旧 reasoning/tool arguments，或从回合末尾反向提取消息写入另一份 history。

## 2. 核心不变量

1. `ConversationHistory` 是内存中的唯一模型可见消息序列。
2. 模型已经看见的内容必须立即进入 canonical history；普通运行只能在尾部追加。
3. system 与 tools schema 是稳定请求外壳，不写入 history。
4. provider 临时 `messages` 不是状态，请求结束后即可丢弃。
5. `assistant.tool_calls` 必须先写 journal，再执行任何工具。
6. 并行工具结果可以并行产生，但必须按模型声明顺序一次追加到 history。
7. 普通限流重试和未产生正文的传输重试复用同一个请求快照。
8. 只有明确的 provider overflow 且正式 compact 成功后，才允许重建同一轮请求。
9. 当前活动工具回合不能被 mid-turn compact 摘要或裁断。
10. compact 摘要失败、replacement 超预算或 journal 写入失败时，内存 history 与 generation 均不得变化。

`ConversationHistory` 为每条消息补 `item_id`、`turn_id` 和模型协议字段摘要。provider 请求生成前会重新校验摘要，因此旧消息被意外原地改写时会明确失败，而不是悄悄发送一个前缀不同的新请求。

## 3. 一次正常用户回合

### 3.1 回合开始

会话开始时 history 可以为空。用户提交下一条输入后，系统先构造一个待提交批次：

```text
[发生变化的 world-state section]
[本轮 RAG / 显式 Skill / hook 等 turn evidence]
[user 原始模型输入]
```

这个批次先完整写入 `history.jsonl`，成功后再推进内存 history。此后第一次 provider 请求直接使用：

```text
system + history
```

若请求前估算已超过 soft limit，系统先对尚未追加本轮输入的旧 history 执行 preflight compact；成功后重新计算最新 section 和用户批次，再一次性追加。这样新用户消息不会在 compact 前后重复提交。

### 3.2 工具循环

每轮工具循环遵循下面的顺序：

```text
history -> 生成请求快照 -> provider
                           |
                           v
                  assistant.tool_calls
                           |
                    先写 journal/history
                           |
                         执行工具
                           |
              每个终态先写非模型可见 checkpoint
                           |
             按 tool_calls 声明顺序批量追加 tool
                           |
                      下一轮 provider
```

工具 checkpoint 只用于进程崩溃恢复，不会提前进入模型上下文。正常工具批次完成后，正式 `tool` 消息进入 history，对应 checkpoint 即失效。即使 provider 未来复用了相同 call id，也不会错误套用旧工具结果。

用户回合第一次请求生成的 system 与 tools schema 会冻结为本回合请求外壳。普通工具循环中的追加、限流/传输重试和 mid-turn compact 都复用这份快照；注册表、MCP、权限或 Skill 状态的变化只能在下一用户回合形成新的明确边界。

`load_image` 产生的模型可见桥接消息、运行通知、Plan 状态变化和明确加载的 Skill 都在产生后追加到同一 history，不存在“本轮临时可见、下一轮消失”的第二条消息链路。

### 3.3 正常回答、失败与取消

- 最终 assistant 原文立即追加，Plan UI 只过滤展示，不改写历史原文。
- provider 最终失败时保留已经提交的用户输入，并追加 `turn_failed` 事实消息。
- 工具调用已写入但进程退出时，恢复层按原调用顺序补齐 checkpoint 结果；没有 checkpoint 的调用补 `unknown`，禁止自动重放。
- 用户取消后，只有在工具终态和 canonical history 提交完成后才向前端发送会话级取消事件。

## 4. World state 基线

World state 基线不是另一份对话，也不是隐藏 system prompt。它只是“模型最近一次已经看见的长期现场值”的结构化索引，用来决定下一轮需要追加哪些变化。

例如上一轮模型已经看见：

```text
cwd=/repo-a
language=Chinese
plan=执行阶段
```

下一轮只有 cwd 变成 `/repo-b`，history 只追加 cwd 的变化，而不重写旧消息。读取失败使用三态语义：

- `present`：新值存在，和基线不同时追加变化。
- `absent`：确认值已经不存在，追加删除语义。
- `error`：本次读取失败，沿用旧基线，禁止误判为删除。

RAG/knowledge、显式 Skill、hook 追加内容属于 `turn_evidence`：它们是某次采样实际使用过的证据，因此进入 history 并保留缓存锚点；但它们不代表下一轮仍成立的环境，所以不写入 world-state baseline。

## 5. Compact 触发边界

模型窗口使用动态边界：

```text
hard_limit = full_window - max_output_tokens
margin     = clamp(full_window * 2%, 2K, 16K)
soft_limit = hard_limit - margin
```

触发路径包括：

| 场景 | 行为 |
|---|---|
| 新用户消息 preflight 超过 soft limit | 先 compact 旧 history，再重新追加本轮输入 |
| 工具循环请求达到 soft limit | 在完整协议批次边界执行 mid-turn compact |
| provider 明确返回上下文 overflow | compact 成功后重建请求并只重试一次 |
| 一轮结束后的基线达到 soft limit | 执行 post-turn compact，为下一轮释放空间 |
| 用户执行 `/compact` | 与自动路径共用同一个正式 compact 实现 |
| 大窗口模型切换到小窗口模型 | 用旧模型摘要，按目标模型窗口校验 replacement 后再切换 |

如果目标小模型配置由具体 `ModelChoice` 提供，降档判断和 replacement 校验使用该 choice 的完整窗口参数，不按同名 model id 猜测。只有旧模型明确返回类型化 `LLMInvalidRequestError` 时，才允许临时切到目标模型重试摘要；失败会回滚模型。未知 provider 错误不会触发猜测式 compact 或重试。

## 6. Compact 分区

### 6.1 动态原始回合保留预算

最近完整旧回合的目标预算不再写死为 64K：

```text
retained_target = clamp(soft_limit * 10%, 16K, 128K)
```

实际预算还要扣除稳定 system、tools schema、最新 world state、活动回合和摘要预留。选择时从最新旧回合向前倒序装入，任何 user/assistant/tool 协议单元都不能切半。若最新完整旧回合自身已经超预算，就只保留其摘要，不制造一个看似完整的半截回合。

### 6.2 普通 compact

普通 compact 把当前 history 分为：

```text
要淘汰的旧前缀 | 预算内最近完整回合
```

摘要模型只总结左侧真正会被淘汰的旧前缀。安装后的新 generation 为：

```text
最近完整回合 + handoff summary
```

普通 compact 后 world-state baseline 重置为空。下一条用户输入会追加一份最新完整 world-state 快照，避免依赖摘要猜测 cwd、Plan、instructions 等当前事实。

### 6.3 Mid-turn compact

mid-turn 使用当前 `turn_id` 找到活动回合起点：

```text
要淘汰的旧前缀 | 最近完整旧回合 | 当前活动回合
```

活动回合从最初 section/RAG/user 一直保留到最新 assistant/tool 边界，绝不进入摘要请求。compact 边界会重新读取当下不依赖用户查询的 world-state sections，并移除活动回合中已经被新完整快照取代的旧 `context_update`；RAG、Skill、user、assistant/tool 原文继续保留。replacement 顺序固定为：

```text
最近完整旧回合
最新完整 world-state 快照
当前活动回合原文
handoff summary
```

将 summary 放在末尾是为了让下一次采样直接收到“从这里继续”的交接指令。若同一活动回合再次 compact，前一次 summary 位于活动切片中，仍会原样保留，不会让已经影响过后续采样的信息突然消失。

如果“活动回合 + 最新 world state + summary”本身已超过目标 soft limit，系统明确失败并保留原 history；不会摘要当前回合，也不会丢最旧工具结果来勉强发送。

## 7. 本地摘要实现

摘要请求采用 Codex 风格的结构化消息输入：

```text
[可选稳定 system]
[要淘汰的原始协议消息]
[user: CONTEXT CHECKPOINT COMPACTION 交接指令]
```

它不会先把历史拼成一段自定义 `summary_source` 文本，不提供 tools schema，也不使用规则摘要兜底。模型必须返回纯 Markdown 文本；若返回结构化 tool calls、文本化工具协议或空摘要，则拒绝安装。

最终摘要包装为 `role=user`、`metadata.kind=context_compaction` 的 handoff 消息。摘要内容要求覆盖当前进度、关键决定、约束、剩余工作和继续任务所需的重要引用。

## 8. 超大摘要输入

整份待摘要前缀能放入 hard limit 时只发一次请求。超窗时启用有界 hierarchical map/reduce：

1. 按完整用户回合和工具协议块拆成不可再分的单元。
2. 贪心打包成不超过摘要模型 hard limit 的 chunk。
3. 每个 chunk 生成局部 handoff。
4. 对局部 handoff 继续 reduce，直到得到唯一最终摘要。

默认硬上限为：

```text
max_chunks = 8
max_summary_requests = 12
max_total_prompt_tokens = 4 * hard_limit
max_total_completion_tokens = min(64K, 4 * max_output_tokens)
```

每条 source 消息必须至少进入一次真正发出的摘要请求，`covered_message_count` 必须等于 `source_message_count`。命中任一预算、单个不可分协议段超窗或 provider 请求失败时抛出 `CompactionError`，禁止安装局部摘要或静默丢弃旧消息。

## 9. History journal v4

`history.jsonl` 是会话恢复的唯一事实源，事件类型为：

| 类型 | 模型可见 | 作用 |
|---|---:|---|
| `append` | 是 | 普通用户、assistant、tool、上下文证据追加 |
| `tool_checkpoint` | 否 | 保存单个工具终态，供崩溃恢复 |
| `replace` | 是 | 正式 compact 安装新 generation |
| `migration` | 是 | 旧会话一次性迁移到 v4 |

每个事件包含连续 `event_seq`、`generation`、完整消息、校验和与时间。写入顺序始终是“journal 成功后再改内存”。`replace` 只有 generation 严格递增时才能安装。

恢复只接受事件序号连续、generation 匹配且 checksum 正确的记录。若进程留下半条 JSON，下一次写入会先补物理换行，避免新事件与损坏尾段粘连；缺口序号可以由恢复后的新事件安全复用。真正的恢复异常会阻止会话启动，不会悄悄把 history 置空。

旧 generation 的 append/replace 事件仍保留在 journal 中，形成完整审计记录；运行时只回放最新有效 generation。

## 10. 旧会话迁移

只有目标会话不存在 `history.jsonl` 时，迁移器才读取一次：

```text
compact.json
transcript.jsonl
active_turn.jsonl
pending_user.json
```

v3 使用 `transcript_cursor_seq`；v2 的 `transcript_offset` 固定按非空物理行序号解释。迁移会去重同一 `turn_id` 的重复记录，恢复 active-turn 工具终态，丢弃旧窗口裁剪遗留的孤儿 tool，并为有父无结果的调用补 `unknown`。迁移成功后写一条 `migration` 事件，后续启动不再读取或更新旧文件。

## 11. 前缀缓存语义

未发生 compact 时，连续请求满足：

```text
request N+1 = request N 的完整结构化前缀 + 新追加消息
```

限流/传输重试复用同一个内存快照，不重新序列化动态 section。RAG、Skill、hook 和运行通知一旦被模型看见就保留在历史原位置。工具结果只在首次进入模型前执行结果上限保护，进入 history 后不再二次截断或改写。

正式 compact 必然是一次明确的缓存边界。目标不是承诺 compact 不破坏缓存，而是保证只有 replacement generation 变化时发生一次前缀重建，之后重新恢复严格 append-only。

当前多模态消息为保护前缀，仍会把模型实际看见的 image data URL 保存在 canonical history/journal。完整的 `ImageRef/MediaBlobStore` 去 base64 持久化属于独立重构，不应在本次代码中重新引入“下一轮换成 filepath/占位符”的旧行为。

## 12. 删除的旧链路

本次生产代码已经删除或退出正常运行路径的内容包括：

- 长期可变的 `messages` 数组和回合末尾反向提交。
- `history_window`、`max_messages` 与请求组装阶段的历史尾裁。
- `commit_offset`、`turn_prefix_messages`、`request_only_inflight`。
- `_pending_context_update_text`、`_pending_world_state`。
- `_build_chat_messages`、`_sliced_history_dicts`。
- transcript/active-turn/pending/compact offset 的正常读写 API。
- 请求发送前静默删除孤儿 tool 的公开生产接口。
- `summary_source`、规则摘要兜底和“未摘要直接丢最旧消息”。

`agent/work_context.py` 现在只负责 `state.json`、`usage.json`、token 校准与工具轨迹索引，不再拥有模型历史。

生产代码行数对比（相对本分支起点）：现有生产文件删除 3239 行、增加 1266 行；新增 `history_journal.py`、`legacy_history_migrator.py` 与 `conversation_history.py` 共 951 行，整体净减少 1022 行。若排除只在旧会话首次打开时运行的 412 行迁移器，正常运行时代码净减少 1434 行。

## 13. 验证结果

本分支当前验证结果：

```text
核心 session/context/compact/transport/skill/subagent 聚焦回归：189 passed
新增 journal、协议清理与请求外壳冻结回归均已包含
OTUI：23 passed
完整 Python 套件：640 passed, 9 failed, 5 errors
py_compile：通过
git diff --check：通过
```

完整套件剩余项不在本次修改文件中：3 个 `cb_agents` 流式取消事件断言、1 个 Windows 路径断言、3 个 sandbox 禁止绑定端口的 MCP/QQ 用例、2 个缺 embedding 服务/依赖的 memory 用例，以及 5 个错误声明 `deps` fixture 的 RAG 用例。

## 14. 禁止重新引入的行为

- 请求构造时按长度、消息数或角色裁剪 canonical history。
- 为了“修好协议”在正常发送前静默删除 tool 消息。
- provider 看见消息后再规范化字段、替换图片表示或缩短工具结果。
- compact 失败后仍替换 history、推进 generation 或更新 world-state baseline。
- mid-turn compact 摘要当前活动回合。
- hierarchical compact 超预算后退化为规则摘要或丢最旧消息。
- 恢复失败后用空 history 继续会话。
- 同时恢复 v4 journal 与旧 transcript/compact，再依赖 offset 去重。

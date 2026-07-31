# 唯一对话历史与 Canonical History 重构技术报告

## 1. 报告范围

本文记录 `refactor/canonical-history` 分支完成的上下文维护重构，覆盖：

- 唯一模型对话历史的内存结构；
- `history.jsonl` v4 追加式 journal；
- 用户回合、工具循环、运行通知、RAG、Skill、Plan 和多模态消息的进入顺序；
- preflight、mid-turn、post-turn、手动 compact 和模型降档；
- 旧 `transcript.jsonl`、`active_turn.jsonl`、`pending_user.json`、`compact.json` 的一次性迁移；
- 会话创建、切换、清理和进程崩溃后的恢复；
- 删除的旧链路、代码规模变化、测试结果和保留边界。

本报告描述已经落地的代码，不是待实施计划。压缩算法的细节另见
`note/Compact上下文压缩命令技术报告.md`。

## 2. 重构前的问题

旧实现同时维护多种含义接近但生命周期不同的数据：

```text
self.history
临时 messages
transcript.jsonl
active_turn.jsonl
pending_user.json
compact.json / replacement_history
state.json 中的恢复辅助字段
```

请求前要从这些数据反向拼装模型消息，回合结束后又要从临时 `messages` 中提取增量
写回 history。compact、恢复、图片和工具中断分别有自己的偏移量与兼容规则。由此产生
以下结构性风险：

1. 模型已经看过的消息可能在下一次请求中被换一种表示。
2. 临时请求有内容，但 durable history 没有，下一轮或重启后事实消失。
3. history、transcript、active turn 对同一消息的提交边界不一致。
4. compact 后要同时修复 history、offset、active turn 和 world-state baseline。
5. 请求组装器可能为了“适配窗口”静默裁剪、过滤或重排旧消息。
6. reasoning、tool arguments、tool results、RAG、图片等协议字段可能被二次序列化。
7. 为旧链路服务的兼容状态不断扩张，主流程很难证明 append-only。

这些问题的共同根因不是某一个截断常量，而是“模型实际看到的消息”和“会话认为自己
保存的历史”不是同一个事实源。

## 3. 目标不变量

重构后的核心约束如下：

1. `ConversationHistory` 是所有随对话增长的模型可见消息的唯一内存事实源。
2. 普通运行只能追加；只有正式 compact 可以替换整代 history。
3. provider 请求由“本用户回合冻结的 system/tools 外壳 + 全量 history”临时派生。
4. 请求组装阶段禁止裁剪、过滤、补写、重排或规范化旧 history。
5. 任何下一次请求会看到的动态消息，必须在请求前先写入 canonical journal。
6. 任何 `assistant.tool_calls` 都必须有同一协议块内的终态 `tool` 消息。
7. journal 先提交，内存后推进；journal 写入失败时内存 history 不得变化。
8. 没有 compact、模型切换或显式配置变化时，前一请求必须是后一请求的结构化前缀。
9. compact 是明确的缓存重置边界；边界之后重新建立 append-only 前缀。
10. 恢复不能追求“语义差不多”，必须恢复相同 role、content 类型、tool call ID、
    arguments、reasoning 和多模态表示。

## 4. 最终架构

### 4.1 请求外壳与唯一 history

最终请求结构是：

```text
本用户回合冻结的 system message
+ ConversationHistory.provider_messages()
+ 本用户回合冻结的 tools schema
```

这里需要区分“唯一对话历史”和“完整请求”：

- system 是稳定请求外壳，不随工具循环增长；
- tools schema 是 API 顶层参数，不是 chat message；
- canonical history 保存所有会随对话推进而追加的消息；
- provider 请求是一次性深拷贝快照，不是第二个长期可变消息数组。

system 和 tools 在用户回合开始时冻结。后台 MCP 注册、权限、Skill 索引即使在工具循环
中变化，也不能中途改写本回合请求前缀；变化只能在下一用户回合形成明确边界。

### 4.2 `ConversationHistory`

`core/conversation_history.py` 提供：

- `prepare_batch()`：深拷贝消息，分配 `item_id`、`turn_id` 和协议摘要；
- `append_prepared()`：追加已经成功写 journal 的消息；
- `provider_messages()`：生成本次 provider 请求的深拷贝；
- `snapshot()`：为 compact、估算和审计生成稳定快照；
- `replace_prepared()`：只接受严格递增的 generation；
- `clear_memory()`：仅用于已提交的会话创建或清理边界。

每条历史消息保存基于 `Message.to_dict()` 的 `content_digest`。下一次 append、snapshot 或
provider 请求前都会重新校验；旧消息若被原地改写会明确失败。摘要只覆盖 provider 协议
字段，timestamp、本地 UI 状态等不影响前缀判断。

### 4.3 消息分类

canonical history 同时保存普通协议消息和本地带 `metadata.kind` 的控制消息：

| kind | role | 含义 | 是否进入模型 |
| --- | --- | --- | --- |
| 空 | user/assistant/tool | 普通对话和工具协议 | 是 |
| `context_update` | user | world state 增量或完整快照 | 是 |
| `context_evidence` | user | RAG、显式 Skill、hook、运行通知 | 是 |
| `plan_state` | user | Plan 模式变化、批准、拒绝 | 是 |
| `tool_image_bridge` | user | `load_image` 产生的视觉输入 | 是 |
| `context_compaction` | user | compact handoff summary | 是 |
| `turn_failed` | user/assistant | 完整 provider 失败或轮数耗尽 | 是 |
| `turn_aborted` | user | 用户取消或进程中断的终态边界 | 是，UI 隐藏 |

UI 导出可以过滤维护消息，但过滤只发生在 `export_history()`，绝不影响 provider history。

## 5. 普通用户回合时序

### 5.1 回合开始

```text
读取本轮用户文本和附件
-> 消费回合开始前的后台任务通知
-> 计算最新 world state
-> 生成相对 baseline 的 context_update
-> 生成本轮 RAG / Skill / hook evidence
-> 追加用户消息
-> preflight 计算 system + 候选 history + tools
-> 必要时先 compact 旧 history
-> 重新生成最新 world state 和本轮输入
-> journal append(turn_input)
-> 内存 history append
-> 发起第一轮 provider 请求
```

只有 `turn_input` journal 事件成功后，用户消息才进入内存。这样 provider 不会看到一条
无法在重启后恢复的输入。

### 5.2 world state 与 turn evidence

world state 表示“当前仍成立的现场”，例如环境、instructions、工具说明和 Plan。系统保存
最近一次模型已见快照作为 baseline，下一轮只追加变化项和删除项。

turn evidence 表示“本轮发生过的事实”，例如：

- 当前查询的 RAG 结果；
- 用户显式加载的 Skill 正文；
- hook 生成的补充上下文；
- 父 Agent 消息和后台任务通知；
- 图片附件与 `load_image` 结果。

evidence 不进入 world-state baseline，但会正式进入 history。它在语义上可以只针对当前回合，
在缓存和审计上仍是模型已经看过的事实，不能在下一请求中从旧位置消失。

## 6. 工具循环时序

每轮工具循环按以下顺序运行：

```text
消费运行中通知并 append history
-> 从冻结 system + 全量 history 构造请求快照
-> soft-limit 检查，必要时 mid-turn compact
-> hard-limit 检查
-> 调用 provider
-> 完整 assistant.tool_calls 先写 journal/history
-> 执行工具
-> 每个终态写 tool checkpoint
-> 按 assistant 声明顺序组装全部 tool messages
-> 原子 append 工具结果批次
-> append load_image 图片桥接消息
-> 下一轮继续使用全量 history
```

限流重试和未产生正文的传输重试复用同一个 `request_messages` 对象快照。只有 provider
明确返回上下文超限并成功完成正式 compact 后，才允许重新派生请求。

### 6.1 工具协议

`agent/message_protocol.py` 不再修复正常 history，只做严格校验：

- tool call ID 必须存在且同一 assistant 内不重复；
- pending tool calls 后只能出现对应 tool 消息；
- tool 消息不能没有父 assistant；
- 正常 provider 请求不能带未配对尾部；
- 工具正在执行时的 UI 估算可以显式允许 pending tail。

append、replace、journal recover 和 provider 请求前都会经过协议校验。checksum 合法但工具
协议断裂的 journal 仍被视为损坏。

### 6.2 取消和进程退出

取消后的部分模型文本只用于 UI，不伪装成完整 assistant。已执行工具仍必须先形成成功、
失败、取消或未知终态。所有终态写入后追加：

```xml
<turn_aborted reason="user_cancelled">
本轮已中止；已记录的工具结果可能已经产生副作用，不得自动重放。
</turn_aborted>
```

该消息进入模型和 journal，但普通 UI 隐藏。`Cancelled` 事件晚于它发出，因此前端收到
最终取消事件时，历史已经可恢复。

如果进程在工具执行期间退出，恢复器按 assistant 声明顺序处理：

- 有 checkpoint：恢复真实终态；
- 无 checkpoint：生成 `unknown` 错误结果，明确禁止自动重放；
- 工具结果和 `process_interrupted` 的 `turn_aborted` 作为同一 append 事件提交。

## 7. `history.jsonl` v4

### 7.1 事件模型

正常运行只写以下事件：

| type | 作用 |
| --- | --- |
| `append` | 在当前 generation 尾部追加消息批次 |
| `tool_checkpoint` | 保存单个工具终态，不提前改变模型可见顺序 |
| `replace` | 正式 compact，安装下一 generation |
| `migration` | 首次把旧会话安装为 generation 1 |

每个事件包含：

- `version=4`；
- 严格递增的 `event_seq`；
- generation；
- 完整 Message payload；
- payload checksum；
- turn ID、事件类型和时间。

`replace` 还保存 `from_generation`，必须满足：

```text
from_generation == 当前 generation
generation == 当前 generation + 1
```

### 7.2 写入事务

一次 append 的提交顺序：

```text
校验旧 history digest
-> 冻结待追加消息
-> 校验追加后的工具协议
-> 写完整 JSON 行
-> flush，可选 fsync
-> 推进 event_seq
-> 追加内存 history
```

journal 写入失败时，event sequence 和内存都不推进。compact replacement 同样先写 journal，
再安装内存 generation。

### 7.3 损坏恢复

恢复只容忍一种损坏：最后一条未换行的半写 JSON。恢复器会删除该半行，再从最后一个
完整事件继续。完整 JSON 恰好只缺最后换行时保留事件并补换行。

以下情况阻止会话启动或切换：

- journal 已存在但为空；
- 中间物理行无法解析；
- version、event sequence、checksum 或 generation 不合法；
- migration 不在事件链起点；
- Message payload 无法验证；
- 工具协议逻辑损坏。

不能只恢复“最后一个合法前缀”后继续写，因为那会静默丢失模型已见事实。

## 8. 会话生命周期事务

### 8.1 创建

新会话先用局部 session ID 创建目录、`state.json`、`usage.json` 和 active index。全部写入
成功后，才更新 `LocalSessionStore.active_session_id` 并清空内存 history。创建失败时旧会话
的指针、状态和 history 保持不变。

新 `state.json.session_id` 显式使用目标 ID，不再在提交前错误继承旧 active ID。

### 8.2 切换

切换前先用绑定目标目录的独立 `HistoryJournal` 完成恢复和协议校验。只有恢复成功，才提交
store active index，再把目标 history 安装到 AgentSession。目标 journal 损坏不会让 store
指向目标而内存仍残留旧会话。

### 8.3 清理

清理先把 active 目录原子改名为隐藏墓碑，再删除 active index；失败时恢复原目录。磁盘
提交成功后才清空内存 history、world-state baseline 和 journal sequence。墓碑的物理删除
失败只保留审计数据，不会重新成为可见会话。

## 9. Compact

### 9.1 动态保留预算

最近完整旧回合的目标预算为：

```text
clamp(soft_limit * 10%, 16K, 128K)
```

实际预算还要扣除 system、tools、world state、活动回合和摘要预留。回合按 `turn_id` 分组，
旧迁移消息缺少 ID 时回退到真实 user 边界。任何 assistant/tool 协议块都不能切半。

### 9.2 普通、preflight 和 post-turn compact

旧 history 分为：

```text
要淘汰的旧前缀 | 最近完整旧回合
```

摘要只覆盖真正淘汰的左侧前缀，replacement 为：

```text
最近完整旧回合
handoff summary
```

summary 保持最后一项，这是 Codex 本地 compact 的续接语义。普通 compact 后 world-state
baseline 清空，下一用户回合会追加最新完整快照。

preflight 在用户输入写 history 前估算完整候选请求。达到 soft limit 时先压缩旧 history，
再重新计算现场和输入；达到 hard limit 且无法释放空间时不调用 provider。post-turn 在完整
回合已提交后检查下一轮基线。

### 9.3 Mid-turn compact

mid-turn 只发生在完整工具批次边界：

```text
旧前缀 | 最近完整旧回合 | 当前活动回合
```

当前活动回合绝不进入摘要。replacement 为：

```text
最近完整旧回合
最新完整 world state
当前活动回合原文
handoff summary
```

活动回合中的旧 `context_update` 会被最新快照替代，但 RAG、Skill、user、assistant、tool、
图片和此前 compact summary 原样保留。活动回合自身超过 hard limit 时明确停止，不能通过
摘要当前工具现场伪装成可继续。

### 9.4 摘要请求

摘要请求使用结构化历史加 Codex 风格 handoff prompt，不把 history 拼成自定义
`summary_source` 文本，不提供 tools schema，也没有规则摘要兜底。模型返回空摘要、结构化
tool call 或文本化工具协议时拒绝 replacement。

单次请求超窗时使用有界 hierarchical map/reduce，限制 chunk 数、请求次数、累计输入和
累计输出。每条淘汰消息至少进入一次真实摘要请求；任一上限失败时原 history 不变。

### 9.5 模型降档

Gateway 在模型切换前读取目标 `ModelChoice` 的完整上下文参数。如果当前请求基线超过目标
soft limit，先用旧模型生成摘要，并按目标窗口验证 replacement，再切换模型。只有旧模型
明确返回类型化 invalid-request 时才临时用目标模型重试；失败恢复旧模型运行时状态。

## 10. Plan Mode

Plan 状态同时有两个层次：

- `plan/` 文件保存当前、批准和历史版本，供 UI 与操作恢复；
- 影响模型行为的变化立即作为 `plan_state` 消息追加到 canonical history。

进入模式、批准、拒绝不会在下一请求时临时插入或移动一段提示词。已批准计划的当前完整
内容仍作为 world-state `plan` section 维护；历史中的状态事件提供发生顺序和审计证据。

## 11. 多模态现状

本次重构删除了附件的 `request_content/history_text` 双表示。用户图片的 data URL 是模型
实际看见的 content，也原样进入 canonical history；下一工具轮、下一用户回合和重启恢复
不会把它替换成 filepath 或占位符，因此普通追加路径保持请求前缀。

`load_image` 因 OpenAI-compatible provider 通常不允许 role=tool 直接携带 `image_url`，仍在
对应 tool results 后生成一条 user 图片桥接消息。区别是桥接消息现在正式写 journal/history，
不再只存在于临时请求数组。

token 估算和消息日志使用脱敏副本，避免把 base64 当普通文本计数或写入诊断日志；这不
改变 canonical history。

当前边界：完整 data URL 仍直接写入 `history.jsonl`，尚未实现内容寻址 `ImageRef` 和
`MediaBlobStore`。该持久化优化属于独立多模态重构，不应以再次引入双表示为代价。

## 12. 旧会话迁移

只有目标目录不存在 `history.jsonl` 时，`agent/legacy_history_migrator.py` 才读取旧文件。
迁移结果作为唯一 `migration` 事件写入 v4；之后正常运行永不再读旧链路。

迁移规则包括：

- v3 按 `transcript_cursor_seq` 与 `turn_seq` 选择 compact 后记录；
- v2 按非空物理行序号解释 `transcript_offset`；
- 同一 `turn_id` 重复记录只取最后一次；
- 未提交 active turn 恢复完整 assistant/tool 协议；
- 已开始无终态工具标为 unknown；
- 未开始工具标为 cancelled-before-start；
- 孤儿 tool 丢弃；
- pending user 仅在未被 transcript 或 active turn 覆盖时迁移。

迁移器是一次性兼容代码，不参与新会话正常运行。

## 13. 删除的旧链路

`agent/work_context.py` 从约 2252 行缩减到约 613 行，只保留：

- 会话索引和 UI 工作状态；
- usage 与 tokenizer 校准；
- 工具轨迹的轻量结构化索引。

从正常运行链路删除：

- transcript append/replay；
- pending user 文件；
- active-turn 事件写入与 offset；
- compact snapshot/offset 双状态；
- `TraceSummarizer` / `RuleTraceSummarizer`；
- 从临时 messages 提取协议增量；
- history window/max_messages 兼容路径；
- 请求前孤儿工具修补；
- 附件 `history_text`；
- 无生产消费者的恢复辅助字段。

相对 `main` 的最终工作差异，当前生产代码增加 2493 行、删除 3258 行，净减少 765 行。
其中一次性旧会话迁移器增加 412 行；排除迁移器后，正常运行代码净减少 1177 行。新增
journal 与不可变历史代码把隐式跨文件状态改成显式事务，代码减少不是以删掉恢复能力为代价。

## 14. 本次复查发现并修复的问题

重构提交后的合并前复查额外发现以下问题：

1. 已存在但为空的 `history.jsonl` 会跳过旧迁移并以空 history 启动。
2. journal 中段损坏、checksum 失败或 event sequence 缺口只告警并继续恢复残缺前缀。
3. replace generation 没有严格验证 `from_generation -> generation + 1`。
4. 切换会话先修改 active store，再恢复目标 journal；恢复失败会造成目录与内存错配。
5. 创建会话先清内存；磁盘创建失败会丢失当前运行内存。
6. 新会话 state 在切换前构造，`session_id` 可能继承旧 active ID。
7. `/clear` 先清内存且吞掉目录删除错误，可能继续在旧 journal 上追加空基线。
8. 用户取消和进程崩溃恢复缺少模型可见 `turn_aborted` 终态。
9. 非 Function Calling 流取消后会把部分文本保存成完整 assistant。
10. journal checksum 正确时仍可能包含逻辑断裂的工具协议。

上述问题均已修复并加入失败注入或协议回归测试。

## 15. 验证与基线

本分支的聚焦测试覆盖：

- canonical history append-only 与旧消息改写检测；
- journal append/replace/checkpoint、半写尾行和损坏拒绝；
- 会话创建、切换、清理失败事务；
- v2/v3 迁移；
- world state 与 turn evidence；
- 普通、mid-turn、hierarchical compact；
- 模型降档；
- 工具协议、取消和崩溃恢复；
- 多模态、`load_image`、Skill、子代理通知；
- provider 请求快照、错误分类和 Gateway RPC；
- OTUI Context/Usage 恢复。

最终验证结果：

```text
聚焦 Canonical/Session/Journal/Compact/多模态/Transport 集合：197 passed
OTUI bun test：23 passed, 0 failed
pytest -q test：650 passed, 9 failed, 5 errors, 6 subtests passed
py_compile/compileall：通过
git diff --check：通过
```

`pytest -q test` 的 9 个失败和 5 个错误均为本次重构之外的环境或既有基线问题：3 个
`cb_agents` 流式取消事件断言、1 个 Windows 路径断言、3 个 sandbox 禁止绑定端口的 MCP/QQ
用例、2 个缺 embedding 服务的 memory 用例，以及 5 个错误声明 `deps` fixture 的 RAG 用例。
仓库根目录的 `pytest -q` 另外会在收集独立 `test_vector_store.py` 时因未安装 `zvec` 直接
停止。以上基线失败没有修改其生产代码；聚焦通过与环境失败分开记录，不能把环境失败伪装
成重构回归。

## 16. 后续边界

本次重构没有承诺以下事项：

- provider 在 compact 边界仍能复用旧前缀缓存；正式 replacement 必然改变前缀；
- data URL 不进入持久化文件；后续需要独立的内容寻址媒体存储；
- 所有 provider 的 token 估算完全精确；安全判断仍使用校准估算与 margin；
- hierarchical compact 无限成本；它有明确预算并可失败；
- 旧会话损坏后自动猜测修复；新 journal 宁可阻止启动，也不静默丢消息。

重构后的长期维护标准很简单：任何模型可见动态内容先进入唯一 history；普通请求只从
冻结外壳和全量 history 派生；旧消息表示不变；确需改变历史时只能走正式、可审计、
可失败且事务化的 compact replacement。

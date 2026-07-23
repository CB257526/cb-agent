# 跨轮上下文管理重构技术报告 —— 转向 Claude Code 原始消息累积模式

> 2026-07-17 更新：本文记录的是当时的历史实现。文中 local microcompact 已从运行时和测试中移除；当前工具循环保持 append-only，体积控制只依赖工具结果入口硬截断与正式 compact，避免改写请求中段破坏 provider prefix cache。
>
> **2026-07-23 清理说明（以代码为准，勿再按下文操作）**：
> - `history_window` 构造参数与子代理 `history_window=8` 已删除；active history **永不**按消息数裁剪。
> - `load_latest_history(max_messages=...)` 生产接口与 `_trim_restored_history` 已删除；恢复始终返回完整 history。
> - hierarchical compact **禁止**未摘要丢最旧消息；`dropped_compact_messages` 已从 compact 返回字段移除。
> - `session_state` 默认不注入模型；bash 截断先落盘；附件全文进 artifact，请求只带 preview。

## 概述

本次重构将 cb-agent 的跨轮上下文管理策略从"每轮强制 trace 摘要 + work_record 文本注入"
切换到 Claude Code 的"惰性压缩"模式：原始 `assistant.tool_calls` / `role=tool` 消息
直接累积进下一轮 messages,只在 token 接近窗口阈值时才整段压缩,以 `compact_boundary`
锚点切片。这次改动延续 `result_cap.py` 单条/批量持久化的方向,把 cb-agent 的整个上下文
管理链条都向 CC 的算法对齐。

## 动机

旧策略有三个核心问题：

1. **prompt cache 失效**：每轮 history 都被改写(trace 替换、work_record 注入),
   前缀不稳定,OpenAI/DeepSeek 兼容 API 的 prefix cache 几乎每轮都失效,
   名义上的省 token 反而推高真实成本。
2. **持续有损**：摘要是确定性丢失,即使本轮的某个工具结果在 3 轮后才被需要也救不回来；
   模型只能再读一遍(双重 IO)或基于残缺记忆乱猜。
3. **`_latest_plain_turn_messages` 只保留 1 轮**的现状已经暴露了这个问题,
   其内部 TODO 也提示需要按 token 预算保留多轮——但仅靠摘要时这条 TODO 难以兑现。

CC 的做法是"惰性压缩"：原始 `tool_use` / `tool_result` 块直接累积进下一轮 messages,
只在 token 接近窗口阈值时才整段压缩,以 `compact_boundary` 标记切片。这个策略与
prefix cache 站在一起,且短任务零摘要损耗。

## 设计决策(已与用户确认)

1. `work_record` 文本注入彻底移除,`state.json` 结构化层(files_seen / recent_commands /
   decisions / pending)保留。
2. 旧 session 数据破坏性更新,不做兼容。
3. microcompact 用 LRU 替换 `tool_result` content,仅作为本地兼容策略。

## 目标架构

```
本轮 _tool_loop:
  messages 局部变量累积:
    user(来自 _build_chat_messages)
    → assistant(tool_calls) → role=tool 结果 → assistant(tool_calls) → ... → final
  ↓ 轮末
  把本轮新增的 [user, assistant_with_tool_calls, role=tool, ..., final_assistant]
  全部 commit 到 self.history

下一轮 chat:
  _build_chat_messages:
    1. system prompt(不变)
    2. state.json 渲染成 user message(保留)
    3. self.history 切片(compact_boundary 之后部分)
    4. 取 history_window 尾部
    5. 追加新 user_query
    6. apply_microcompact: tool_result 数 ≥ 10 时把最旧的若干条 content 替换为占位
  ↓
  preflight token 检查(按优先级):
    a. predictive: currentTokens + estimateMaxTurnGrowth > 完整窗口 → autocompact
    b. autocompact: currentTokens >= 完整窗口 - dynamicBuffer → 生成 boundary
    c. blocking: currentTokens >= 完整窗口 - 3000 → 报错拒绝
  ↓
  llm.think
```

## 新增模块

### `agent/compact_boundary.py`

| 函数 | 职责 |
|------|------|
| `make_compact_boundary_message(summary)` | 构造 `system` 角色 + `metadata.kind="compact_boundary"` 的消息,content 是 LLM 摘要 |
| `find_last_compact_boundary_index(messages)` | 倒序查找最后一个 boundary,没有返回 -1 |
| `get_messages_after_compact_boundary(messages)` | 切片返回 boundary(含)之后的所有消息 |

设计要点：boundary 用 `system` 角色而非 `assistant`，避免被部分模型解读为
"模型自己说过这话";多 boundary 时取最后一个,等价于"以最近一次压缩为准"。

### `agent/microcompact.py`

CC 的 server-side microcompact 是 provider-specific 能力,本地消息原封不动让
server 端忽略旧 tool_result。OpenAI 兼容协议(DeepSeek 等)没有这个能力,因此用
本地等同语义替代。

| 常量 | 值 |
|------|-----|
| `MICROCOMPACT_THRESHOLD` | 10 |
| `MICROCOMPACT_KEEP_RECENT` | 5 |
| `CLEARED_PLACEHOLDER` | `{"cleared": true, "hint": "..."}` |

`apply_microcompact(messages)` 原地修改：扫描所有 `role=tool` 消息,跳过已清理过的,
若剩余条数 > KEEP_RECENT,把"最旧 (剩余 - KEEP_RECENT)"条 content 替换为占位。
**保留 `tool_call_id` / `name`,确保 OpenAI 协议的 assistant.tool_calls ↔ tool 配对仍然合法**。

幂等性：被替换过的消息 content 已是 `{"cleared":true,...}`,扫描时跳过,二次调用不会产生重复处理。

## 修改文件

### `agent/work_context.py`

1. **持久化序列化扩展**：新增 `_message_to_persist_payload`,把 Message 序列化为
   可往返还原的 dict,保留 `tool_calls` / `tool_call_id` / `tool_name` / `metadata.kind`。
   `_message_payload_to_message` 同步扩展为可恢复 4 类 role(user / system / assistant / tool)。
2. **`append_turn` 签名变更**：从 `(user_query, final_answer, work_record)` 改为
   `(user_query, final_answer, committed_messages, work_record=None)`。transcript.jsonl
   行结构由 `{user_query, final_answer, work_record, trace_entries}` 改为
   `{user_query, final_answer, messages, trace_entries}`,messages 字段是本轮提交进 history 的完整序列。
3. **`load_latest_history`**：从 transcript.jsonl 读 `messages` 字段直接还原原始消息,
   恢复后模型在新一轮就能看到上一轮真实工具调用细节。
4. **`_trim_restored_history`**：识别 `compact_boundary` kind 而非旧的 `compact_record`。
5. **`RuleTraceSummarizer`**：不再生成 `text` 字段(置空),只提取 files_seen /
   files_modified / recent_commands 等结构化字段,驱动 `state.json` 更新。
6. **`TraceSummarizer`**：退役为 `RuleTraceSummarizer` 的薄壳(保留类名以兼容外部装配)。
7. **`merge_work_record`**：删除 `record.text` → `rolling_summary` 注入路径。
8. **删除**：`_create_work_record_message` / `make_work_record_message` /
   `_create_compact_record_message` / `make_compact_record_message`。

### `agent/session.py`

#### `_chat_impl` 改造

```python
commit_offset = len(messages)  # _tool_loop 启动前的 messages 长度
rounds_used, final_answer, trace_collector, _ = self._tool_loop(...)

# CC 模式跨轮累积
history_commit_start = len(self.history)
self.history.append(Message.create_user_message(history_user_text))
new_protocol_messages = self._extract_protocol_messages(messages, commit_offset)
if new_protocol_messages:
    self.history.extend(new_protocol_messages)
if final_answer and not self._history_tail_is_final_answer(final_answer):
    self.history.append(Message.create_assistant_message(final_answer))
committed_turn_messages = list(self.history[history_commit_start:])

# state.json 结构化层(独立于 history,不冲突)
work_record = self._make_work_record(...)
self._persist_turn(history_user_text, final_answer, work_record, committed_turn_messages)
```

新增两个 helper：
- `_extract_protocol_messages(messages, offset)`：从 _tool_loop 累积的 messages 中
  抽出 assistant(含 tool_calls) / role=tool 协议消息,转 Message 对象。
- `_history_tail_is_final_answer(final_answer)`：判断 history 末尾是否已经是
  最终 assistant 回答(避免 cancel 等异常路径双追加)。

#### `_build_chat_messages` 改造

```python
sliced_history = get_messages_after_compact_boundary(self.history)
for m in sliced_history[-self.history_window:]:
    messages.append(m.to_dict())
messages.append({"role": "user", "content": user_content})
apply_microcompact(messages)  # 必须在 user_query 追加之后
return messages
```

#### Preflight 三级阈值(重写 `_maybe_auto_compact_preflight`)

```python
PREDICTIVE_GROWTH_OUTPUT_BUDGET = 20_000
PREDICTIVE_GROWTH_TOOL_BUDGET = 15_000
AUTOCOMPACT_BUFFER_SMALL = 13_000   # 窗口 < 400k
AUTOCOMPACT_BUFFER_MEDIUM = 30_000  # 400k ≤ 窗口 < 800k
AUTOCOMPACT_BUFFER_LARGE = 50_000   # 窗口 ≥ 800k
BLOCKING_LIMIT_BUFFER = 3_000
```

按优先级从严到宽：

1. **Predictive**：`current + (20k + 15k) > 完整窗口` → 触发 autocompact
2. **Autocompact**：`current >= 完整窗口 - dynamic_buffer` → 触发 autocompact
3. **Blocking**：`current >= 完整窗口 - 3000` → emit Error,`_chat_impl` 收到
   `blocked=True` 后短路,直接 emit Done 并返回友好提示文本

`_auto_compact_history` 复用 `compact_context`(在 history 末尾追加 boundary),
返回审计事件供 `Done.auto_compact` 渲染。

#### `compact_context` 新流程

新语义：append boundary,不删 history。
- before_messages: 调用前 history 长度
- after_messages: before + 1(末尾追加 boundary)
- 落盘失败时 pop boundary 回滚,保证内存与磁盘一致。

#### 删除路径

- `_maybe_compress_tool_loop_messages`(本轮内 80% 触发原地替换 tool content)
- `_tool_result_message_summary`(生成本轮压缩摘要)
- `_latest_plain_turn_messages`(/compact 后只保留最近一轮的旧逻辑)
- `AUTO_TOOL_MESSAGE_LIMIT` 常量
- `_tool_loop` 内部的 `tool_message_summaries` / `compressed_tool_message_indices`
  状态机和相关日志路径

## 数据流对比

### 旧

```
本轮 messages(局部) → 末尾丢弃
              ↓
     trace_collector(压缩轨迹)
              ↓
     LM/规则生成 work_record.text
              ↓
   make_work_record_message(text)
              ↓
   self.history.append(text-message)  ← 文本摘要进 history
              ↓
     state.json 结构化字段更新
```

### 新

```
本轮 messages(局部) → _extract_protocol_messages
                            ↓
              raw assistant.tool_calls + role=tool
                            ↓
              self.history.extend(raw messages)  ← 原始消息进 history
                            ↓
              transcript.jsonl 落盘原始 messages 字段
                            ↓
              state.json 结构化字段更新(独立路径)
```

## prompt cache 影响

旧策略每轮 history 都被改写,前缀不稳定。新策略下：
- **system prompt**：每轮重生成(memory section / env_info 等),但内部已切块带 cache scope
- **state.json user message**：跨轮变化,但是 ContextBuilder 内部已分段
- **history**：每轮纯追加(boundary 之前的旧消息原封不动),前缀稳定
- **本轮 user_query**：唯一变动尾部

OpenAI/DeepSeek 兼容 API 的 prefix cache 命中率会显著提升,尤其在工具循环场景。

## 不影响的部分

- `result_cap.py` 单条 50k / 批量 200k 持久化(已对齐 CC,不动)
- `state.json` 结构化字段提取(files_seen / recent_commands 等)
- Chat prompt builder + memory_loader 的 system/context update 组装链路
- `/clear` 语义(彻底删除当前 session)
- 多 session 隔离 / 切换 / pending_user 崩溃恢复
- 多模态输入处理(image_url/base64 仍不进 history)

## 测试

### 新增

- `test/test_compact_boundary.py` —— 8 个用例
  - boundary 消息构造(role=system, kind=compact_boundary, 前缀)
  - find_last_compact_boundary_index 倒序与多 boundary 取最后
  - get_messages_after_compact_boundary 切片包含 boundary 本身

- `test/test_microcompact.py` —— 5 个用例
  - 阈值边界(< 10 不动,>= 10 清最旧 N - 5 条)
  - 清理后 assistant.tool_calls.id ↔ tool_call_id 配对仍合法
  - 占位符是合法 JSON,name 字段保留
  - 幂等性(已清理的不再重复处理)

### 改写

- `test/test_work_context.py` —— 6 个用例
  - trace_entry_from_tool_result 截断
  - append_turn 落盘 raw messages + 还原后 tool_call_id / tool_calls 仍配对
  - state.json 结构化字段提取
  - pending_user_message 不重复
  - 多 session 隔离
  - compact_boundary 持久化与切片

- `test/test_session_renderer.py` —— 替换 4 个旧用例
  - `test_tool_loop_keeps_raw_tool_result_for_next_round`：CC 模式工具结果原样回灌
  - `test_tool_call_blocks_when_full_window_overflows`：blocking 阈值返回友好提示
  - `test_tool_trace_persists_state_and_round2_sees_raw_tool_result`：state.json 更新 + 第 2 轮看到原始 tool_result
  - `test_session_store_restores_history_and_clear_deletes_active`：history 长度 4(user+assistant tool_calls+tool+final)
  - `test_compact_context_appends_boundary_and_slices_next_turn`：append boundary,after = before + 1

- `test/test_transport.py` —— 修 3 处旧 append_turn 签名

### 结果

直接运行修改影响的 6 个测试模块：

```
test/test_session_renderer.py    43/43 OK
test/test_transport.py           24/24 OK
test/test_work_context.py         6/6  OK
test/test_compact_boundary.py     8/8  OK
test/test_microcompact.py         5/5  OK
test/test_result_cap.py          10/10 OK
```

整套 unittest discover 跑下来 **446/448 通过**。剩 2 个 `_FailedTest` 是
`test_context_pipeline.py` / `test_memory_knowledge_architecture.py` 缺
pytest 依赖,与本次重构无关(原本就是这样)。

## Review 后续修复(P0 协议合法性 + P1 token 计数失真)

重构落地后做了一轮自查,发现两个问题:一个会直接触发 API 报错(P0),一个让
前端 Context% 系统性失真(P1)。两者根因相同——重构把 `self.history` 从"纯文本
摘要"改成"原始协议消息累积",但**消费 history 的两条下游链路没有同步对齐**。

### P0:history_window 截断切断 tool_calls ↔ tool 配对

`_build_chat_messages` 先按 `compact_boundary` 切片,再 `[-history_window:]` 取
尾部。重构前 history 全是纯文本,怎么切都合法;重构后累积的是
`user → assistant(tool_calls) → tool → tool → assistant(final)` 这种序列,
`history_window` 默认 12 而一轮多工具对话轻松超过,这一刀很容易落在
`assistant(tool_calls)` 和它的 `tool` 响应之间——切片开头变成一条**孤儿 tool**
(父 `assistant.tool_calls` 被切掉)。OpenAI 兼容协议(DeepSeek 等)会直接报:

```
messages with role 'tool' must be a response to a preceding message with 'tool_calls'
```

因为恢复时就截到 window、之后每轮还在 append,history 长期停在 window 以上,
**这一刀几乎每轮都在切**,触发概率很高。跨进程恢复路径
`_trim_restored_history` 的尾裁剪和 `[anchor] + tail` 有同样风险。

CC 本身不做固定条数截断、靠 autocompact 在 token 维度兜底,所以没这个问题;
cb-agent 既保留 window 截断又引入原始协议消息,就必须自己补这层对齐。

### P1:前端 Context% 系统性低估

前端两个 token 指标要分清:

- `usage {x}k`:来自 `token_usage` 事件,是 API 返回的真实 prompt+completion
  累加。**与重构无关,正确。**
- `Context {used}/{max} {percent}%`:来自 `context_window_usage()` →
  `_dynamic_context_text()`。**重构后失真。**

旧 `_dynamic_context_text` 有两个问题叠加:

1. **纯工具调用的 assistant 被整条丢弃**:`assistant(tool_calls, content=None)`
   经 `_message_content_to_text(None)` 返回 `""`,被 `if content` 过滤掉。
   file_write 的完整内容、bash 命令都藏在 `tool_calls.arguments` 里,在
   Context% 里一个 token 都不算 —— 大头被系统性漏算。
2. **不走 boundary 切片**:用物理尾部 `self.history[-window:]`,导致 `/compact`
   后真实请求变小、Context% 反而因为多了 boundary 摘要而上升,与直觉相反。

对照之下,autocompact 判定用的 `_estimate_request_tokens` 是把整个 messages
JSON 序列化的,**口径准确**。一准一不准、用了两条路径,是失真的直接原因。

### 修复

新增 [`agent/message_protocol.py`](cb-agent/agent/message_protocol.py),提供孤儿
tool 清理的两份实现(dict 版 / Message 版),判定规则一致:从前往后扫,
`assistant.tool_calls` 声明的 id 进"已见"集合,`role=tool` 只有 `tool_call_id`
已在集合里才保留。

| 改动点 | 内容 |
|--------|------|
| `session.py` 抽 `_sliced_history_dicts()` | 把"boundary 切片 + window 截断 + 孤儿清理"收敛成一个 helper,**请求构造和 Context% 估算共用同一口径** |
| `_build_chat_messages` | 改用 `_sliced_history_dicts()`,孤儿清理在 microcompact 之前(P0) |
| `_dynamic_context_text` | 改用 `_sliced_history_dicts()` 序列化整条 dict 计 token,含 tool_calls.arguments 与 tool.content,并走 boundary 切片(P1) |
| 删除 `_context_message_line` | 旧的单行渲染估算函数,口径统一后成死代码 |
| `work_context._trim_restored_history` | 两条返回路径都过 `drop_orphan_tool_message_objects`,跨进程恢复的双保险 |

`microcompact` 只改本轮临时 messages、不动 history,设计正确,本次不调整。

### 测试

- 新增 [`test/test_message_protocol.py`](cb-agent/test/test_message_protocol.py) ——
  9 个用例:dict/Message 两版的开头孤儿、中间断裂孤儿、合法配对不动、多
  tool_calls、原地引用保持、boundary/user/assistant 保留。
- `test_session_renderer.py` 加 3 个集成回归:
  - `test_history_window_cut_drops_orphan_tool_message`:window 压到 2 制造孤儿,
    断言发给 LLM 的请求体无孤儿(P0)
  - `test_dynamic_context_counts_tool_call_arguments`:大 arguments 工具调用被
    计入 Context% 估算(P1)
  - `test_dynamic_context_follows_compact_boundary_slice`:`/compact` 后估算只
    统计 boundary 之后(P1)
- **真阳性验证**:临时把孤儿清理改 no-op,确认 P0 场景请求体里确实出现 1 条
  孤儿 tool(`call_x`),证明测试能抓 bug 而非摆设。
- 全量 `unittest discover`:**460 个测试,仅 2 个 pytest 缺失的 `_FailedTest`**,
  与本次改动无关。

## 顺带清理:llm_dict 死字段移除 + 模型表扩充

Review 过程中发现 `ConstantLLM.llm_dict` 的 `json_output` 字段是死的——全项目
没有任何运行时代码读它(grep 仅命中定义处和测试 monkeypatch)。借这次一并清掉,
并把模型登记表补全。

### 改动

- **删 `json_output`**:`constant_llm.py` 4 个模型条目、`README.md` 字段清单、
  以及 6 处测试 fake config 全部移除。代码不读此字段,删除零行为影响。
- **修过时注释**:`DEFAULT_MAX_TOKENS` 的注释写"保留 8000",实际值是 128000,
  改正。并补一段 `llm_dict` 字段职责说明:运行时真正消费的只有 `is_tool`
  (FC 分流)、`image_ability`(图片原生视觉 vs 降级 OCR)、`max_tokens`
  (Context% 与自动 compact 阈值);`is_reasoning` 暂作标注,运行时未分流。
- **修缩进**:`gemini-3.5-flash` 条目原先多一个空格缩进,统一。
- **扩模型表**:从 4 个扩到 14 个,新增 Qwen / GPT / Claude 系列。窗口与视觉
  能力来自各厂商官方文档(截至 2026-06):
  - Qwen3-Max 262144 非视觉;Qwen3.5-Plus/Flash 1M 多模态(阿里云 Model Studio)
  - GPT-5/5-mini 400K 视觉;GPT-4.1 1M 视觉
  - Claude Opus-4.8 / Sonnet-4.6 稳定 200K(需 1M 时靠 `[1m]` 后缀由
    `window.py` 识别,不写死在表里)
  - DeepSeek v4-flash/pro 经官方文档核对维持 1M、Tool Calls、非视觉不变

### 已知遗留(未在本次处理)

`ConstantLLM.model_max_tokens` 和 `context/budget/window.py` 的
`get_context_window_for_model` 都能回答"模型窗口多大",存在两个入口的职责重叠
(`window.py` 五级推断 vs `constant_llm` 直读 `llm_dict`)。session.py 主链路当前
用前者。收敛成"`model_max_tokens` 委托 `window.py`"是更彻底的去重,但会动到
context/budget 链路,留待单独重构,不混入本次。

## 记忆系统 Review 修复（自动知识捕获退化 + 会话切换缓存）

重构后对"上下文加载与记忆系统"做了一轮系统性 review（规则加载 / 工作记忆 /
长期记忆 / 会话持久化）。结论:大部分链路健康——CLAUDE.md 四层加载每轮强制
重读、memory_query 真实驱动知识检索、消息序列化往返无损、会话目录隔离可靠、
compact 的 transcript_offset 锚点 + 文件快照回滚都正确。发现并修复两个问题。

### P1:自动知识页捕获退化且与 knowledge_write 工具重复（已移除）

`KnowledgeBase.capture_turn` 历史上做两件事:
1. MEMORY.md 长期记忆更新（`_looks_like_memory` 触发，只看 user_text）
2. 结构化知识页自动捕获（`_looks_like_knowledge` 触发 → `upsert_page`）

问题出在第 2 件:它的触发启发式 `if work_record_text and len(combined) > 600`
依赖 work_record 文本，而 CC 对齐重构后 `WorkRecord.text` 恒为空，这条分支
永久失效；`_body_from_turn` 的 `## Work trace` 章节也永远为空。更关键的是，
项目已有 `knowledge_write` 工具——由模型基于语义主动判断"这值得记"并整理成
结构化正文，写入同一个 KnowledgeBase，比字符长度启发式可靠得多。自动捕获
既退化又冗余。

处理:移除知识页自动捕获路径，保留 MEMORY.md 自动更新。
- `knowledge.py`:`capture_turn` 删掉 `_looks_like_knowledge` 触发的 `upsert_page`
  块，只留 MEMORY.md 更新;`work_record_text` 形参保留（`del` 标注）仅为兼容签名。
- 删除随之死掉的 `_looks_like_knowledge` / `_title_from_turn` / `_body_from_turn`
  / `_tags_from_text` 四个私有方法，以及 `KNOWLEDGE_TRIGGERS` 常量。
- `session.py`:`_auto_update_memory_and_knowledge` 调用不再传 `work_record_text`。
- 保留:`upsert_page`（knowledge_write 工具在用）、`_looks_like_memory`、
  `MEMORY_TRIGGERS`、`append_long_term_memory`。
- 澄清一个 review 误报:episodic/semantic memory 并非被本次重构"搞空转"——
  `record_turn` 从来只调 `capture_turn`，那两层一直靠 LLM 主动调 memory 工具
  写入，重构前后行为一致。

### P2:switch_session 切换后未清 system prompt section 缓存（已修）

`clear_history`（/clear）会调 `clear_system_prompt_sections()` + MemoryLoader
`reset_cache`，但 `switch_session` 漏了。env_info section 缓存键含 cwd，换会话
（尤其换项目目录）后可能注入上一会话的环境快照。`session.py:switch_session`
补上与 clear_history 一致的缓存清理。

### 验证

- 手动驱动（venv 无 pytest）验证:capture_turn 只更新 MEMORY.md、不再产生
  知识页;长 assistant 文本也不再触发自动捕获;`upsert_page`/knowledge_write
  显式写入仍正常;死方法与 `KNOWLEDGE_TRIGGERS` 确认删除。
- `test_knowledge_tools` 3 用例、`test_memory_knowledge_architecture` 的
  capture_turn 用例（改断言为 `not result.pages`）。
- P2 回归:`test_session_renderer` 46/46、`test_transport` 24/24、
  `test_work_context` 6/6 全过。

## 模型能力支持环境变量覆盖（换服务商兜底）

### 背景

`llm_dict` 用模型名作键登记能力（is_tool / image_ability / max_tokens）。换
API 服务商后——尤其用中转站——`LLM_MODEL_ID` 常和表里的键对不上，例如硅基流动
的 `deepseek-ai/DeepSeek-V4-Flash` vs 表里的 `deepseek-v4-flash`。lookup 落空
就退回默认值：function calling 误判、Context% 失准、多模态模型被当纯文本强制
走 OCR。

### 改动

在 `ConstantLLM` 加一个统一的能力解析层，所有消费点改走它，优先级
**env > llm_dict > 默认**：

- 新增 env 键 `IS_TOOL` / `IS_REASONING` / `MAX_TOKENS` / `IMAGE_ABILITY`。
- `_parse_bool_env`：识别 true/false/1/0/yes/no/on/off。
- `_parse_token_count_env`：支持 `1024K` / `1M` / `200000` 写法（中转站常用 K/M
  表述窗口）。
- `resolve_is_tool` / `resolve_is_reasoning` / `resolve_image_ability` /
  改造后的 `model_max_tokens`：统一三级取值。
- 消费点接线：
  - `cb_agents._is_able_Function_Calling` → `resolve_is_tool`
  - `multimodal_input.model_supports_image` → `resolve_image_ability`
  - `window.get_context_window_for_model` 新增 `MAX_TOKENS` 为最高优先级，
    与 session 主链路的 `model_max_tokens` 共用同一个 env 键——顺带收敛了之前
    "两条窗口路径各读各的" 的遗留重叠。
- `.env.example` 补充这 4 个键的注释与用法。

### 测试隔离副作用（重要）

`cb_agents.py` 顶部 `load_dotenv()` 在 import 时就把用户本地 `.env` 的这些值
灌进 `os.environ`。由于 env 现在优先级最高，依赖 `llm_dict` monkeypatch 的测试
会被真实 env 覆盖而失败（开发者本地设了 `MAX_TOKENS=1024K` 就触发）。因此给
`test_session_renderer` / `test_transport` / `test_multimodal_input` 的相关测试类
加了能力 env 隔离（setUp 清 4 键 + addCleanup 恢复）。这也暴露了一条经验：凡
依赖 llm_dict monkeypatch 的用例都必须隔离这组 env。

### 验证

- 新增 `test_constant_llm_env.py` 16 用例：K/M 解析、布尔解析、三级优先级、
  换服务商兜底、window 复用 `MAX_TOKENS`。
- 端到端模拟真实场景（`deepseek-ai/DeepSeek-V4-Flash` 对不上 llm_dict +
  `.env` 配 4 键）：能力全部从 env 正确解析，两条窗口路径结果一致。
- 全量 `unittest discover`：476 测试，仅 2 个 pytest 缺失的预存失败无关。

## 引用

- 现有 `_tool_loop`：[cb-agent/agent/session.py](cb-agent/agent/session.py)
- 新模块 boundary：[cb-agent/agent/compact_boundary.py](cb-agent/agent/compact_boundary.py)
- 新模块 microcompact：[cb-agent/agent/microcompact.py](cb-agent/agent/microcompact.py)
- 新模块 message_protocol：[cb-agent/agent/message_protocol.py](cb-agent/agent/message_protocol.py)
- 模型登记表：[cb-agent/constant/llm/constant_llm.py](cb-agent/constant/llm/constant_llm.py)
- result_cap(保持)：[cb-agent/agent/result_cap.py](cb-agent/agent/result_cap.py)
- CC 对照参考：`外部代码/claude-code/src/services/compact/autoCompact.ts`、
  `microCompact.ts`、`query.ts:2029`、`utils/messages.ts` 中的
  `getMessagesAfterCompactBoundary`

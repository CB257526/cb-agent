# Codex 风格本地 Compact 技术报告

## 1. 改造目标

cb-agent 原实现会先把待压缩历史转换为文本，再裁到固定 32K token 后调用摘要模型。
在 400K 或 1M 上下文中，一次 compact 可能移出数十万 token，但摘要模型只能看到
其中很小一部分，连续 compact 后还会形成反复压缩旧摘要的问题。

本次重构参考 Codex 本地 compaction 的核心语义：

1. 摘要请求直接使用模型原本看到的结构化消息历史。
2. 在完整历史末尾追加 handoff prompt，让模型为下一上下文窗口生成交接摘要。
3. compact 成功后安装新的 replacement history，而不是在旧 history 中插入切片锚点。
4. 保留最近原始回合，任务早期内容由 handoff summary 承接。
5. compact 后重新建立模型可见的运行现场基线。

核心代码位于：

- `agent/compaction.py`：结构化摘要请求、超窗重试和最近回合选择。
- `agent/session.py`：触发、事务安装、mid-turn 继续和 Context 预算。
- `context/world_state.py`：world state snapshot 与增量比较。
- `agent/work_context.py`：compact v2 快照、transcript 审计与恢复。

## 2. 结构化摘要请求

compact 请求按以下顺序组装：

```text
稳定 system instructions
当前完整 active history
user: CONTEXT CHECKPOINT COMPACTION handoff prompt
```

active history 保留原协议结构，包括：

- 真实 user 消息；
- assistant 正文；
- assistant.tool_calls；
- 与 tool_calls 配对的 tool result；
- 上一次 compact 生成的 handoff summary；
- 已提交的运行时 context update。

请求不包含 tools schema，压缩模型只能返回普通 assistant summary。摘要请求使用当前
模型配置的 `max_output_tokens` 和 `output_token_param`，不再维护固定 8K/20K 的第二套
输出上限。

模型返回摘要后，cb-agent 将其包装为 user 消息：

```text
Another language model started to solve this problem ...
<assistant 生成的 handoff summary>
```

该消息使用 `metadata.kind=context_compaction`。role 使用 user 是为了兼容不同
OpenAI-compatible provider，并与 Codex 的 replacement history 布局一致。

## 3. Compact 请求超窗处理

正常自动 compact 会在 soft limit 触发，因此摘要请求通常可以直接复用现有稳定前缀，
只新增一条 handoff prompt。

若模型降档或单条工具结果过大导致 compact 请求仍然超窗，处理顺序为：

1. 从最旧的协议完整段开始移除。
2. user 开始的 assistant/tool 链作为一个整体处理，不拆散 tool call/result。
3. 若只剩一个超大回合，逐步缩短其中最大的文本正文。
4. 保留消息角色、tool_call_id、工具名和配对关系。
5. provider 明确返回 context overflow 时继续采用相同策略重试。
6. 仍无法执行时抛出 `CompactionError`，不生成规则摘要。

原始完整历史始终保存在 transcript 审计流中，上述缩短只影响这一次 compact 模型请求。

## 4. Replacement History

compact 成功后的 history 顺序为：

```text
[mid-turn 时的完整 world state]
[最近若干完整原始回合]
[context_compaction handoff summary]
```

summary 始终是最后一条 user 消息。连续 compact 时，上一份 summary 就是当前完整历史
中的普通结构化消息，会自然进入下一次摘要请求，不需要专门的“上一摘要拼接”代码。

### 4.1 动态保留预算

最近完整回合的目标预算为：

```text
retained_target = clamp(soft_limit_tokens * 10%, 16K, 128K)
```

典型值：

| 模型 soft limit | 目标保留量 |
|---:|---:|
| 128K 附近 | 16K |
| 400K | 40K |
| 1M | 100K |
| 2M 及以上 | 128K |

目标值还会受压缩后真实剩余空间约束：

```text
available = soft_limit - system/tools/world_state/summary
retained_budget = min(retained_target, available)
```

选择从最新用户回合向前进行，只保留完整回合。最新回合自身超过预算时，保留用户输入
与最后的普通 assistant 回答；中间工具现场已经由结构化 compact 请求总结。

手动 `/compact` 如果当前历史低于动态目标且无法产生实际空间收益，会返回 `no_op`。

## 5. World State Baseline

world state baseline 不是任务摘要，而是“模型已经看过哪些运行现场信息”的精确快照。

例如模型已经看到：

```text
instructions = 当前 AGENTS.md/MEMORY.md
environment = cwd、平台、shell、模型
plan = 当前已批准计划
session_state = 当前任务、文件、命令、决策和待办
```

系统保存这些 section 的实际规范文本。下一轮如果只有 plan 变化，只追加 plan 的新值；
某个 section 被删除时发送显式 removed 标记。

长期 baseline 包含：

- instructions；
- environment；
- current_date 与语言偏好；
- MCP instructions；
- session guidance；
- SessionState；
- PlanState。

与当前用户查询绑定的 RAG knowledge，以及 hook 产生的本轮 runtime instructions，不进入
长期 baseline，每轮按当前请求独立注入。

### 5.1 不同 compact 阶段

- manual/pre-turn/post-turn：replacement history 不注入 world state，并清空 baseline；
  下一条正常请求完整重注入现场并建立新基线。
- mid-turn：模型需要马上继续同一工具回合，因此把当前完整 world state 插入 summary
  之前，并立即把该 snapshot 设为新基线。
- 重启恢复：从最近 context update 的 `world_state_snapshot` metadata 恢复实际值，继续
  计算增量变化。

## 6. 触发与模型降档

自动触发继续使用动态窗口：

```text
hard_limit = full_window - max_output_tokens
margin = clamp(full_window * 2%, 2K, 16K)
soft_limit = hard_limit - margin
```

支持以下入口：

- 用户手动 `/compact`；
- 下一用户请求发送前的 preflight；
- 工具循环中的 mid-turn compact；
- 完整回合结束后的 post-turn compact；
- 大窗口模型切换到小窗口模型时的 model downshift。

模型降档时先解析目标配置但不立即切换：

1. 如果当前上下文超过目标模型 soft limit，先使用旧大模型 compact。
2. 摘要请求按旧模型窗口组装，但原始回合保留预算按目标小模型 soft limit 计算。
3. compact 成功后再安装目标模型。
4. 旧 provider 明确返回 invalid request/400 时，允许临时切到目标模型重试。
5. 目标模型重试仍失败时恢复旧模型状态并返回错误。

## 7. 前缀缓存

未触发 compact 时，请求继续严格 append-only。

本地摘要请求本身沿用现有 system/history 前缀，只追加 handoff prompt，因此正常情况下
仍可复用旧前缀缓存。安装 replacement history 会产生一次明确的前缀重置，这是正式
compact 无法避免的语义边界。

compact 安装完成后，world state 增量和新的 user/assistant/tool 消息继续只追加，不会
回头改写旧工具结果或历史消息。

## 8. 事务与失败行为

compact 分为三个阶段：

1. 生成摘要；
2. 构造并验证 replacement history；
3. 保存 compact v2 快照后替换内存 history。

任一阶段失败时：

- 不替换 `self.history`；
- 不推进 world state baseline；
- 不清空 pending context；
- 不生成低质量规则摘要；
- 不删除 transcript。

自动 compact 失败且请求尚未达到 hard limit 时保留旧历史；已经达到 hard limit 时停止
本轮，不把必然超窗的正式请求发送给模型。

## 9. 持久化格式

`compact.json` v2 示例：

```json
{
  "version": 2,
  "summary": "Another language model started ...",
  "replacement_history": [],
  "world_state_snapshot": {},
  "transcript_offset": 12,
  "reason": "mid_turn",
  "model": "model-id",
  "target_model": "model-id",
  "provider": "provider-id",
  "before_messages": 80,
  "after_messages": 12,
  "tokens_before": 350000,
  "tokens_after": 42000
}
```

- `compact.json` 只保存最新安装快照。
- `compactions.jsonl` 追加保存每次 compact 的完整审计记录。
- `transcript.jsonl` 不删除、不重写。
- mid-turn 回合提交完成后推进 transcript offset，防止重启时重复追加当前回合。

本次是破坏性升级：旧版或没有 `version=2` 的 compact 快照直接忽略，不执行
`compact_boundary`/`compact_record` 迁移。旧 transcript 仍可用于人工审计。

## 10. 删除的旧实现

本次删除：

- `context/compact/history.py`；
- `context/compact/boundary.py`；
- 固定 64K retained 常量；
- 固定 32K 摘要输入上限；
- 固定 8K 摘要输出上限；
- `summary_source` 选择链路；
- compact boundary 查找和请求切片；
- `_history_text_for_compact()`；
- `_rule_compact_summary()`；
- 旧 compact 专用 state/plan 文本拼装。

## 11. 已知限制

compact 仍然是有损操作。结构化完整历史、原始回合保留和 world state 能显著降低退化，
但经过很多轮 compact 后，模型准确度仍可能下降。特别长且目标已经发生明显变化的任务，
应创建新会话，而不是无限依赖摘要继续压缩。

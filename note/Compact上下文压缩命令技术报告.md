# Compact 上下文压缩技术报告

> 更新于 2026-07-23。描述当前代码（`agent/compaction.py`、`agent/session.py`、`agent/work_context.py`），不再包含“丢弃最旧消息未摘要”“dropped_compact_messages 有语义”等已删除行为。

## 1. 目标

在上下文接近 soft limit 时，用正式 compact 把 active history 替换为：

```text
[可选 mid-turn world state]
[动态预算内的最近完整原始回合]
[context_compaction handoff summary]
```

不变量：

1. **只有**正式 compact 成功（摘要 → replacement 校验 → 持久化）后才能替换 history。
2. 失败时 history / compact 快照 / world state **完全不变**。
3. 不使用规则摘要兜底；不按消息数裁剪 active history。
4. hierarchical 路径下，每条 source 消息至少进入一次真正发出的摘要请求。

## 2. 触发路径

| 路径 | 入口 | 说明 |
|------|------|------|
| 手动 `/compact` | `compact_context(reason=manual/user_compact)` | 用户主动 |
| 轮后 auto | post-turn 达到 trigger tokens | 与 preflight 共用阈值 |
| preflight | 下一完整请求将超 soft limit | 可 `force=True` |
| 模型降档 | 切换到更小窗口 | 按旧模型摘要、按目标模型算 replacement |

摘要模型使用当前 `self.llm` 的非流式客户端；输出上限走模型配置的 `max_output_tokens` / `output_token_param`。

## 3. 结构化摘要请求

```text
可选稳定 system
当前完整 active history（协议消息，不序列化成大段文本）
user: SUMMARIZATION_PROMPT（CONTEXT CHECKPOINT COMPACTION）
```

- 不附带 tools schema；若模型返回文本化 tool-call 协议，校验失败并拒绝安装。
- 摘要包装为 `role=user`、`metadata.kind=context_compaction` 的 handoff 消息（`SUMMARY_PREFIX` + 正文）。

## 4. Single-pass 与 hierarchical

### 4.1 Single-pass

本地估算整份摘要请求 ≤ hard limit 时，发一次摘要请求（探测估算不计入 `summary_requests`）。

### 4.2 Hierarchical map/reduce

超窗时：

1. 按真实 user 回合拆成协议段（tool call/result 不拆散）。
2. 贪心打包不超过 hard limit 的 chunk。
3. 每个 chunk 生成局部 handoff；中间层用轻量 `[hierarchical partial handoff]` 消息，**不用**最终 `SUMMARY_PREFIX`（避免 reduce 再次超窗）。
4. 局部 handoff 再 reduce，直到一次装下并得到唯一最终 summary。

硬上限（可配置覆盖，默认）：

```text
max_chunks = 8
max_summary_requests = 12
max_total_prompt_tokens = 4 * hard_limit
max_total_completion_tokens = min(64K, 4 * max_output_tokens)
```

任一上限命中 → `CompactionBudgetExceeded`（`CompactionError` 子类）→ **不安装**局部 summary。

单条协议段本身超 hard limit → `CompactionError`，禁止静默砍用户/assistant 正文。

### 4.3 覆盖率

`CompactionModelResult` 字段：

- `strategy`: `single_pass` | `hierarchical`
- `summary_requests` / `summary_prompt_tokens` / `summary_output_tokens`
- `source_message_count` / `covered_message_count`（必须相等）
- `dropped_messages` 恒为 0（兼容字段，无“未摘要丢弃”语义）

session 返回的 compact payload **不再**包含 `dropped_compact_messages`。

## 5. Replacement 与 world state

- 保留回合数由 `dynamic_retained_token_target(soft_limit)` 与 soft limit 剩余空间共同约束。
- mid-turn compact 把当前 pending/baseline world state 装回 replacement，便于同工具回合继续。
- manual/pre-turn 通常清空 baseline，下一轮完整重注入 durable section。
- 成功后：`history = replacement`，清理 MemoryLoader 缓存，写 compact 快照（v3：`transcript_cursor_seq`、`target_model` 等）。

## 6. 与缓存的关系

- 未 compact：durable system + active history 前缀应稳定。
- 发生 compact：仅在 replacement 安装点断前缀一次。
- request-only（RAG 等）不在 compact baseline 中，见《跨轮工作上下文技术报告》。

## 7. 测试入口

- `test/test_hierarchical_compact.py`：single-pass、marker 覆盖、四种预算失败、tool 配对、单段超窗失败。
- `test/test_session.py` / `test/test_context_incremental_compact.py`：安装与恢复。

## 8. 禁止行为（执行红线）

- 未让摘要模型看见就 `pop` 最旧协议段。
- 无限 map/reduce 或无 token 预算。
- compact 失败仍写 history / compact.json。
- 用规则摘要或静默砍消息“救场”。

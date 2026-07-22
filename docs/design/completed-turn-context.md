---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-21
review_cycle: quarterly
---

# Completed Turn context contract

## Decision

后续 Turn 的唯一持久历史投影是版本化 `CompletedTurnContext`。它是 Conversation 的
append-only 派生记录，不是 event replay、Provider request、UI snapshot 或 semantic summary。
同一 completed Turn 的 canonical 顺序固定为：

```text
user
  → assistant_tool_use
  → tool_result_receipt
  → ...
  → assistant_final
```

没有 Tool 的 Turn 只有 `user → assistant_final`。platform contract、Agent instructions、
workspace `CLAUDE.md`、Git/UTC snapshot、Tool schema 与本轮 finalization marker 每轮从当前
受信来源重新编译，不进入这个 bundle，也不能伪装成 Conversation message。

## Schema and identity

`completed-turn-context-v1` 绑定 `agent_id`、`conversation_id`、`turn_id`、`run_id`、Turn
position、model/profile digest、context-plan digest 和按顺序排列的 items。每个 item 有稳定
`item_index`、枚举 `kind`、UTF-8 byte length 与 canonical content digest：

| kind | 必需字段 | Provider role / trust |
| --- | --- | --- |
| `user` | 原始用户正文 | `user` / conversation data |
| `assistant_tool_use` | call ID、Tool ID、canonical arguments、arguments digest | `assistant` tool call / model output |
| `tool_result_receipt` | call ID、Tool ID、outcome、projection reason、原始结果 digest、有界低权限 projection | `tool` / untrusted data |
| `assistant_final` | 最终回答正文 | `assistant` / model output |

Tool call ID 在一个 bundle 内唯一；每个 result 必须精确引用之前尚未收敛的 call，Tool ID 与
arguments/result digest 必须和 operation/event ledger 对账。`tool_result_receipt` 永远按低权限
不可信 data role 渲染，内容中的指令、system 文本或角色标签都不能提升权限。完整 Tool 原始
结果仍由对应 operation/event 所有者保存；context bundle 只保存允许进入后续模型窗口的
有界 projection 和原始结果 digest。

## Promotion transaction

只有 `run.completed` 且 final assistant、所有 Tool、provider usage 与 operation ledger 已规范
收敛时，ConversationStore 才在同一个 terminal SQLite 事务中一次性：

1. 校验 bundle 身份、顺序、digest 和大小；
2. 插入 immutable items；
3. 把 Turn 从 `running` 改为 `completed`；
4. 清除 Conversation 的 active Run 并提升 revision；
5. 提交 terminal event、usage/operation 收敛与 next-turn preview source revision。

failed、cancelled、interrupted、部分模型正文、未完成 Tool、overflow 的失败 attempt 和 recovery
前的临时 transcript 都不晋升。事务失败时旧 Conversation revision 与旧历史保持完整，整个
Turn 不可见；不得从裁剪后的 journal、live SSE 或 UI DOM 补写 bundle。

## Bounds

v1 writer 使用下列硬限制，所有限制针对 canonical UTF-8 encoding：

- 每 Turn 最多 1 个 user、2 个 `assistant_tool_use`、2 个 `tool_result_receipt`、1 个
  `assistant_final`，总 item 数最多 6；
- user 最大 8 KiB，assistant final 最大 12 KiB，单个 canonical arguments 最大 8 KiB；
- 单个 Tool result context projection 最大 16 KiB；
- 单 Turn bundle 最大 64 KiB；每 Conversation 最多 128 个 Turn，bundle 总量最大 8 MiB；
- 超限 projection 必须在 Tool 返回阶段生成带 digest、原始 byte count 与明确截断原因的
  bounded receipt；不能在下一轮静默截字符串。

这些是存储/资源上限，不等同于模型 token window。renderer、compaction、summary 与 preview
都消费同一 bundle 并分别执行模型画像对应的容量政策。

## Legacy migration and rollback

现有 `conversation_turns.user_content/assistant_content` 是 `pair-only-v0`。迁移是 additive：先
发布能读取 v0/v1 的 decoder 与空 v1 tables，再启用 v1 writer。旧 completed Turn 继续按
`user → assistant_final` 读取；缺失的 Tool items 不从可能受 retention 影响的 journal 猜测回填，
并在内部标记 `history_fidelity=pair_only_legacy`。新 Turn 只写 v1 bundle，同时保留 v0 final
columns 到一个完整 rollback release 周期。

Conversation 删除通过外键级联删除 bundle；Agent 删除先 drain runtime，再删除 Agent 私有
SQLite/Capsule，所以不会留下共享环境残留。backup/restore 必须复制同一 SQLite snapshot
（含 WAL checkpoint 后的 schema/user_version），不能单独导出 items。rollback 先关闭 v1 writer，
旧 binary 仍可读取保留的 v0 pair；回滚期间产生的新 v0 Turn 由未来 writer 正常识别。只有在
release qualification 证明无需回滚后，才可另立迁移删除兼容 columns，不能包含在本次变更。

## Consumers

Conversation history renderer、deterministic compaction、semantic-summary-v2、durable
next-turn preview、event replay inspection 和 Web transcript 都必须从本合同或它的明确低保真
v0 decoder 读取。任何“第一次 input + 最终 output”等启发式公式只可作为旧数据诊断，不能
成为 canonical next-turn baseline。

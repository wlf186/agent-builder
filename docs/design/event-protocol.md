---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-18
review_cycle: quarterly
---

# Canonical event and SSE protocol

事件协议把不受信 Worker 输出转换成可排序、可持久化、可投影的控制面事实。前端只
消费该协议，不从文本猜 Tool 或 Run 状态。

## Envelope

当前 `schema_version` 是 `2.0-prototype`：

```json
{
  "schema_version": "2.0-prototype",
  "event_id": "32-char-generated-id",
  "agent_id": "00000000-0000-4000-8000-000000000001",
  "conversation_id": "32-char-generated-id",
  "turn_id": "32-char-generated-id",
  "run_id": "32-char-generated-id",
  "parent_run_id": null,
  "seq": 1,
  "occurred_at": "2026-07-18T00:00:00.000Z",
  "kind": "run.started",
  "durability": "durable",
  "payload": {}
}
```

- `event_id`：事件唯一 ID；当前不作为 SSE cursor。
- 四级 identity：Agent / Conversation / Turn / Run。当前每次提交都新建 Conversation
  和 Turn，`parent_run_id` 恒为 `null`；这不是未来协议承诺。
- `seq`：Control Plane 为一个 Run 分配的、从 1 开始严格单调整数；SSE `id` 与之相同。
- `occurred_at`：Control Plane 赋值的 UTC 时间，不信任 Worker 时钟。
- `kind`：受 allowlist 和状态机约束的类型。
- `durability`：`durable` 或 `ephemeral`；它描述本事件的 journal 语义，不代表客户端
  已经收到或跨重启可重放。
- `payload`：每个 kind 有严格 object schema 与字节上限。未知字段被拒绝。

Worker 只能发送 `{kind,durability,payload}`。身份、时间、sequence 和 event ID 只能由
Control Plane 添加。

## 事件类型

| kind | durability | 关键 payload | 约束 |
| --- | --- | --- | --- |
| `run.started` | durable | prototype、model、visible_tools、sandbox | `seq=1`，由控制面发出 |
| `assistant.block.started` | durable | block_id、block_type | 同时最多一个 content block |
| `assistant.block.delta` | ephemeral | block_id、text | 只能作用于 open block，不写 SQLite |
| `assistant.block.finished` | durable | block_id、content | 完整内容，闭合 open block |
| `assistant.block.discarded` | durable/降级时 ephemeral | block_id、reason | 取消/失败时闭合 block |
| `tool.call.requested` | durable | call_id、tool_id、arguments | 当前 tool_id 只能是 `builtin/echo` |
| `tool.call.started` | durable | call_id、tool_id | 必须对应 pending call |
| `tool.call.finished` | durable/降级时 ephemeral | call_id、tool_id?、outcome、result | outcome 为 succeeded/failed/cancelled |
| `run.completed` | durable | reason、model_iterations | 唯一成功终态 |
| `run.failed` | durable/降级时 ephemeral | code、message、retryable | 唯一失败终态 |
| `run.cancelled` | durable | reason | 唯一取消终态 |

正常情况下除 delta 外均为 durable。只有 journal 在已接受 Run 中途不可用时，控制面
才用明确的 ephemeral recovery/`journal_unavailable` terminal 让 live stream 诚实
收敛；客户端不得把它当成已持久化历史。

## 顺序与状态机

```text
run.started
  ├─ assistant.block.started
  │    ├─ assistant.block.delta *
  │    └─ assistant.block.finished | assistant.block.discarded
  ├─ tool.call.requested
  │    └─ tool.call.started
  │         └─ tool.call.finished
  └─ run.completed | run.failed | run.cancelled
```

规则：

1. Run 先有且只有一个 `run.started`，最后有且只有一个 terminal。
2. terminal 后不得发布任何事件。
3. block ID 和 call ID 在一个 Run 内不可重复；delta/finish 必须引用已打开对象。
4. Tool requested → started → finished 严格有序；同一时刻只允许一个 pending Tool。
5. terminal 前所有 block/tool 必须闭合。Worker 崩溃或取消时由 Control Plane 生成
   recovery event，再发布 terminal。
6. Worker frame 先完成字段、类型、大小和状态转换验证，再写 journal/更新内存。
7. 前端按 `seq` 投影；不要依赖 HTTP chunk 边界或模型 token 边界。

## SSE transport

事件 endpoint：

```text
GET /api/runs/<run-id>/events
Last-Event-ID: <last applied seq>
```

每条消息：

```text
id: <seq>
event: <kind>
data: <compact JSON EventEnvelope>

```

- endpoint 要求有效 session；无效 Run/cursor 返回错误，不打开 stream。
- 没有新事件时约每 15 秒发送 `: heartbeat` comment。
- 响应使用 `Cache-Control: no-store` 与 `X-Accel-Buffering: no`。
- 重连时服务发送当前内存 Run 中 `seq > Last-Event-ID` 的事件；到 terminal 后结束流。
- cursor 范围当前为 0..1,000,000。

重要限制：当前没有从 SQLite 提供 replay 的 API，也没有 gap marker、snapshot 或服务
重启后的 Run reconstruction。Run 已从 64 个内存 retention 中淘汰或进程重启后，
`Last-Event-ID` 不能恢复它。不要把当前 SSE 称为 durable resume。

## 容量与磁盘语义

- 单个 Worker/event envelope 最大 65,536 bytes；用户消息最大 8,192 UTF-8 bytes。
- 每 Run 最多 512 live events、1 MiB live bytes、256 KiB durable bytes，并为 recovery
  和 terminal 预留空间。
- 控制面最多 4 active Runs、保留 64 个近期 live Run；journal 保留 256 个近期 Run。
- durable event 先提交 SQLite/WAL，才加入 live window；delta 从不调用 journal append。
- SQLite 主文件、WAL、SHM 必须是本用户拥有、单 hardlink、非 symlink 的普通文件。

## 前端投影和安全

UI 根据 block ID 追加 delta，根据 terminal 停止 stream，根据 Tool events 展示阶段。
事件时间线保存收到的完整 envelope；点击条目以只读 JSON 查看。所有模型/Tool/event
字符串用 DOM `textContent`，禁止 `innerHTML` 或把 payload 当 markup。

## 协议演进

字段、kind、durability、顺序或 cursor 语义变化必须：

1. 更新 contracts 和控制面 validator；
2. 增加合法/非法序列、过大 frame、重连和 terminal convergence 测试；
3. 同步前端投影和本文件；
4. 若破坏现有消费者，提升 schema version 并写迁移/拒绝策略；
5. 不允许 Worker 与 Control Plane 静默接受彼此未知版本。

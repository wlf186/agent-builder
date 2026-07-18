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

当前 `schema_version` 是 `2.2-prototype`：

```json
{
  "schema_version": "2.2-prototype",
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
- 四级 identity：Agent / Conversation / Turn / Run。Conversation 由用户显式新建并在
  多轮间保持；每次提交新建 Turn 和 Run。当前一个 Turn 只有一个 Run，
  `parent_run_id` 恒为 `null`；retry/branch 不是当前协议承诺。
- `seq`：Control Plane 为一个 Run 分配的、从 1 开始连续递增的整数；SSE `id` 与之相同。
  durable journal 不保存 `assistant.block.delta`，因此它的 durable 子序列可在 open block
  期间出现由这些 ephemeral delta 占用的 gap；无 open block 的 durable gap 非法。
- `occurred_at`：Control Plane 赋值的 UTC 时间，不信任 Worker 时钟。
- `kind`：受 allowlist 和状态机约束的类型。
- `durability`：`durable` 或 `ephemeral`；它描述本事件的 journal 语义，不代表客户端
  已经收到或跨重启可重放。
- `payload`：每个 kind 有严格 object schema 与字节上限。未知字段被拒绝。

Worker 只能发送 `{kind,durability,payload}`。身份、时间、sequence 和 event ID 只能由
Control Plane 添加。

`2.2-prototype` 相对 `2.1-prototype` 的语义变化是：`conversation_id` 在同一持久
Conversation 的多个 Run 间保持稳定；`run.started.context_plan` 新增历史消息计数、
历史来源 digest 和 windowing strategy 元数据。旧版本消费者不得静默假设每个 Run 都
对应一个新 Conversation。

## 事件类型

| kind | durability | 关键 payload | 约束 |
| --- | --- | --- | --- |
| `run.started` | durable | prototype、model、visible_tools、sandbox、context_plan | `seq=1`，由控制面发出；plan 仅摘要元数据 |
| `assistant.block.started` | durable | block_id、block_type | 同时最多一个 content block |
| `assistant.block.delta` | ephemeral | block_id、text | 只能作用于 open block，不写 SQLite |
| `assistant.block.finished` | durable | block_id、content | 完整内容，闭合 open block |
| `assistant.block.discarded` | durable/降级时 ephemeral | block_id、reason | 取消/失败时闭合 block |
| `tool.call.requested` | durable | call_id、tool_id、arguments | 当前只能首轮调用一次 `builtin/echo`，参数上限 8192 UTF-8 bytes |
| `tool.call.started` | durable | call_id、tool_id | 必须对应 pending call |
| `tool.call.finished` | durable/降级时 ephemeral | call_id、tool_id?、outcome、result | outcome 为 succeeded/failed/cancelled |
| `run.completed` | durable | reason、model_iterations、usage | 唯一成功终态 |
| `run.failed` | durable/降级时 ephemeral | code、message、retryable、usage | 唯一失败终态 |
| `run.cancelled` | durable | reason、usage | 唯一取消终态 |

正常情况下除 delta 外均为 durable。只有 journal 在已接受 Run 中途不可用时，控制面
才用明确的 ephemeral recovery/`journal_unavailable` terminal 让 live stream 诚实
收敛；客户端不得把它当成已持久化历史。

`run.started.context_plan` 当前包含 `plan_id`、plan/toolset digest、section 数、完整/
纳入/省略的历史消息计数、历史来源 digest、`full|completed-turn-tail-v1` 窗口策略、
估算输入、原生/运行窗口、硬输入预算、80% 触发阈值、60% 目标、输出预留、`256`
tokens 的 provider template reserve 和 estimator ID。它不包含 section 正文、用户消息、
assistant 历史、platform/Agent instructions 或 Tool schema。当前
`qwen3.5:2b` 的对应容量值为 `262144 / 32768 / 30720 / 24576 / 18432 / 2048`；这些
来自受信模型资格与策略，不是 Worker 声明。`completed-turn-tail-v1` 只表示从最旧端按
完整 completed Turn pair 选择窗口，不是模型 summary 或语义摘要；SQLite 中的完整
transcript 不因窗口选择而删除。

每个 terminal 的 `usage` 当前固定为
`{input_tokens,output_tokens,last_input_tokens,complete}`。Provider 在完整 Ollama 终帧
报告 `prompt_eval_count`/`eval_count`；Control Plane 对照 ContextPlan 的模型 profile
校验单轮上限，累计前两项，并把最近完整轮次的 prompt count 写入
`last_input_tokens`。Worker 发出的 terminal 不含 usage，不能伪造该字段；只有 Control
Plane 在 canonical publication 时附加。`complete=false` 表示当前 provider 轮次未产生
可校验终帧，此时累计数字可能只覆盖此前已经完整验证的轮次，不能视为本 Run 的完整
用量。

受信 Model Broker 的 error code/retryability 保留在 Control Plane。Worker 只会看到
有界 error frame，并可能发出泛化 runtime failure；canonical `run.failed` 必须使用控制面
持有的 Broker 语义。每次调用前对完整 transcript 重做动态硬预算 admission，超限使用
`model_context_limit`，排队超时使用可重试 `model_busy`。

## Conversation 与 Turn 投影

Conversation/Turn 是 `state.sqlite` 中独立于 Run event stream 的 durable 投影：

- `GET|POST /api/sessions` 列出或创建 Conversation；
- `GET|DELETE /api/sessions/<conversation-id>` 恢复或删除 Conversation；
- `POST /api/sessions/<conversation-id>/runs` 新建一个 Turn 和其唯一 Run；
- 每个 Conversation 同时至多一个 `running` Turn/active Run。

Turn 接受使用 completed-history snapshot 的 expected revision 做 CAS；active Run 绑定和
`run.started` 在同一 SQLite 事务提交，历史漂移时整个接受失败。只有经过完整
`assistant.block.finished` 校验并到达 `run.completed` 的回答，才与 terminal 一起提交为
`completed` user/assistant pair。`run.failed`、`run.cancelled` 或 Gateway 重启留下的
`interrupted` Turn 没有 assistant history；其 partial block、Tool result 和 ephemeral
delta 不进入后续 ContextPlan。

Conversation 读取可以跨 Gateway 重启恢复上述 durable transcript。初始化会把遗留
`running` Turn 转为 `interrupted`；若 durable 序列仍有开放的 assistant block 或 Tool，
恢复 sequencer 会先按协议有界补齐 discard；requested-only Tool 补
started → finished，已 started Tool 补 finished，然后追加 `run.failed(control_restarted)`。
非法、超限或无法完整闭合的 journal 会整体回滚并 fail closed。当前不为旧 Run 伪造
可续接的 SSE，也不会重新启动旧 Worker。
删除只允许没有 active Run 的 Conversation，并原子移除其 Turn 与关联 Run events；
活跃时返回冲突，先取消或等待终态。

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
   一个或多个 closure/recovery events，再发布 terminal。
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
- 浏览器刷新后可从 session 详情的 `running` Turn 取得 `run_id`，在该 Run 仍位于同一
  Gateway 内存 retention 时重新附着；前端最多按 `Last-Event-ID` 有界重连 3 次。

重要限制：会话详情从 SQLite 恢复 durable user/assistant/status 投影，但当前没有从
SQLite 提供 event replay，也没有 gap marker、event snapshot 或服务重启后的 Run
reconstruction。Run 已从内存 retention 中淘汰或 Gateway 重启后，`Last-Event-ID` 不能
恢复它；ephemeral delta 也不会补写。不要把会话恢复称为活跃 SSE 的 durable resume。

## 容量与磁盘语义

- 单个 Worker/event envelope 最大 65,536 bytes；用户消息最大 8,192 UTF-8 bytes。
- 每个 Conversation 同时最多一个 active Run；Conversation/Turn 数量、消息字节和 Agent
  `state.sqlite` 大小都有硬上限，达到上限时拒绝，不自动扩展成无界历史。
- 每 Run 最多 512 live events、1 MiB live bytes、256 KiB durable bytes；普通事件为最坏
  的 block discard、Tool started/finished 和 terminal 预留 4 个事件位置及 32 KiB。
- 控制面最多 4 active Runs、保留 64 个近期 live Run；journal 保留 256 个近期 Run。
- Model Broker 最多并行 2 路 provider stream；其它 active Run 最多排队等待 30 秒，
  超时以可重试 `model_busy` 失败。
- 每轮最多 4096 个 provider 原始帧，Control/Broker 合并为最多 128 个 content IPC frame。
- Turn `running`/terminal 转换与对应 boundary event 以 SQLite/WAL 事务提交；其它 durable
  event 先提交才加入 live window。delta 从不调用 journal append，也不逐 token 更新
  Conversation transcript。
- SQLite 主文件、WAL、SHM 必须是本用户拥有、单 hardlink、非 symlink 的普通文件。

## 前端投影和安全

UI 从 session API 列出、新建、恢复和删除 Conversation；在同一 Conversation 内投影
多个 Turn。活跃会话禁止并发发送或删除。Run UI 根据 block ID 追加 delta，根据 terminal
停止 stream，根据 Tool events 展示阶段；终态后重新读取 durable Conversation。事件
时间线保存当前流收到的完整 envelope，点击条目以只读 JSON 查看。所有模型/Tool/event
字符串用 DOM `textContent`，禁止 `innerHTML` 或把 payload 当 markup。

## 协议演进

字段、kind、durability、顺序或 cursor 语义变化必须：

1. 更新 contracts 和控制面 validator；
2. 增加合法/非法序列、过大 frame、重连和 terminal convergence 测试；
3. 同步前端投影和本文件；
4. 若破坏现有消费者，提升 schema version 并写迁移/拒绝策略；
5. 不允许 Worker 与 Control Plane 静默接受彼此未知版本。

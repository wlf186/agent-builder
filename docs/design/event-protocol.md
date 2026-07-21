---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-20
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
- `seq`：Control Plane 为一个 Run 分配的、从 1 开始严格递增的整数；SSE `id`、
  durable replay cursor 与之相同。durable journal 不保存
  `assistant.block.delta`，因此 durable 子序列可在 open block 期间出现由这些
  ephemeral delta 占用的 gap。Turn 接受时还会在 `run_journal_state` 中为该 Run
  保留至 `512` 的 cursor high-water；Gateway 重启恢复只能从
  `max(last durable seq, reserved_through) + 1` 继续，因此无 open block 时只允许这一
  种明示的 recovery gap。其它 durable gap 非法，已发给客户端的 cursor 不会在
  重启后被重用。
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

模型调用边界作为 `2.2-prototype` 的 additive feature 发布，以兼容当前 checkout 已存在的
durable Run。新 Run 的 `run.started.protocol_features` 必须精确为
`["model-call-boundaries-v1"]`；当前多 Tool Run 进一步精确声明
`["model-call-boundaries-v1","sequential-multi-tool-v1",
"one-shot-overflow-recovery-v1"]`，随后必须遵守下面的
request/response 配对；没有该字段的
Run 只能按 legacy 序列验证，不能包含新 kind。因而删除新 Run 的全部 model boundary 并
重算普通 digest 也不能把它伪装成 legacy Run。未知 kind 的旧消费者应 fail closed 或以
中性事件展示，不能自行推断方向。

## 事件类型

| kind | durability | 关键 payload | 约束 |
| --- | --- | --- | --- |
| `run.started` | durable | prototype、model、visible_tools、sandbox、context_plan、protocol_features? | `seq=1`，由控制面发出；新 Run 必须声明 model boundary feature；plan 仅摘要元数据 |
| `model.request.started` | durable | request_id、iteration、attempt、recovery_id、provider_call_index、context plan ID/digest、request digest/bytes、runtime estimated input、message/tool counts、Tool result call IDs | exact request 已完成 admission、准备释放给 provider；每 provider attempt 一次，只由控制面发出 |
| `model.response.finished` | durable | request_id、iteration、attempt、recovery_id、provider_call_index、outcome、单调用 input/output usage、usage_complete、error_code | 与 request 一一配对；outcome 为 tool_use/end_turn/error/cancelled，只由控制面发出 |
| `model.recovery.started` | durable | recovery_id、iteration、attempt=1、overflow_code、旧/新 context plan ID/digest、boundary digest | 仅首个零 provider-frame 的精确 context/media overflow 后出现一次；证明恢复投影的 CAS 边界，不授权重放 Tool/Capability |
| `assistant.block.started` | durable | block_id、block_type | 同时最多一个 content block |
| `assistant.block.delta` | ephemeral | block_id、text | 只能作用于 open block，不写 SQLite |
| `assistant.block.finished` | durable | block_id、content | 完整内容，闭合 open block |
| `assistant.block.discarded` | durable/降级时 ephemeral | block_id、reason | 取消/失败时闭合 block |
| `tool.call.requested` | durable | call_id、tool_id、arguments | 当前最多顺序调用两次有效 Tool；Echo、stat/read/glob/grep/edit/write/exec 各按冻结 ToolSpec 校验，call ID 唯一 |
| `tool.call.started` | durable | call_id、tool_id | 必须对应 pending call |
| `tool.call.finished` | durable/降级时 ephemeral | call_id、tool_id?、outcome、result | outcome 为 succeeded/failed/cancelled |
| `run.completed` | durable | reason、model_iterations、usage | 唯一成功终态 |
| `run.failed` | durable/降级时 ephemeral | code、message、retryable、usage | 唯一失败终态 |
| `run.cancelled` | durable | reason、usage | 唯一取消终态 |

`ToolSpec` v3 把 canonical result 与 provider projection 分开。`tool.call.finished.result`
仍保存 Worker 产生且通过 Tool 上限的 canonical 完整值；Control Plane 不改写这条 event。
送入下一次模型请求前，`identity_or_digest_placeholder_v1` 对结果做确定性投影：不超过
该 Tool `max_provider_projection_bytes` 时保持原文，超过时替换成包含 call ID、原始 UTF-8
bytes、domain-separated SHA-256 和 `provider_projection_limit` reason 的有界 receipt。
receipt 自身和 projection metadata 再绑定一个 projection digest。当前 Echo canonical
request/result 各最多 8192 bytes；文件结果最多 12288 bytes且 provider projection 最多
8192 bytes；Echo 单结果 provider projection 最多 4096 bytes，单 Run
全部 projected Tool result 最多 16 KiB；每个后续 provider request 都在替换后重新执行
完整 Token/请求字节 admission，仍超过 hard budget 就以 `model_context_limit` 拒绝。

projection 只存在于受信 Run 内存和 exact provider request，不增加 journal event 或
artifact。canonical result 随既有 Run event retention 管理：每 Run durable journal 最多
256 KiB、Agent SQLite 最大 512 MiB，保留策略最多 256 个近期 Run；当前没有外部 Tool
artifact/reference 文件，因此也没有逐 chunk 写入或孤立 artifact 清理路径。相同
ToolSpec/call ID/canonical bytes 必须重建相同 content/projection digest。历史 v1/v2、
Echo-only v3、三 Tool read-runtime、五 Tool search-runtime 和七 Tool mutation-runtime digest
仍由 replay allowlist验证；新 Run 使用包含 Echo/stat/read/glob/grep/edit/write/exec 的
runtime digest。

`capability.request/response` 是同一 stdio 上的内部限帧协议，不是 canonical event。
Worker 只能在匹配的 `tool.call.requested → started` 之后提交一次 request；Control Plane
逐字段核对 request/call/tool/arguments、冻结 ToolSet、pending model call、Agent generation
和 policy。文件调用先产生 permission 与 operation 审计，再由 Control Plane executor
执行；返回 Worker 的 outcome/result 被控制面保留并与后续 `tool.call.finished` 字节对账，
因此 Worker 不能替换读取结果。canonical event 中的文件内容仍标记为
`untrusted_tool_data`，read receipt 携带 path identity、完整 content digest 与 truncation。
成功的完整 `file/read_text` receipt 只在同一 live Run 内成为 mutation 前置条件，不写成
另一份授权 token；terminal/delete 时释放。`file/edit`/`file/write` 的 permission preview
保存 path、receipt、准确 unified diff、diff/new-content digest，批准后才允许 operation
dispatch。最终 Tool result 返回 committed content receipt；若原子 rename 可能已发生但
无法证明结果，则 operation 为 `outcome_unknown`、Tool 失败，后续 replay 不重新执行。
`exec/run` request 只包含 allowlisted command ID；permission preview/arguments digest 绑定
受信 executable/source/catalog/sandbox 与随机 runner identity。operation dispatched 后，独立
payload 先发 `runner.ready` 内部 attestation，Control Plane 用 PIDFD/`/proc` 复核，再发送
单字节 release。ready/release 和 PID record 都是 Capability executor 内部协议，不获得
canonical `seq`；用户可从 permission/operation audit 证明动作边界，从 canonical Tool result
看到有界 exit/stdout/stderr/sandbox 结果。released 后状态不能证明时 operation 为
`outcome_unknown`，replay 不能重新 release 或 spawn。

正常情况下除 delta 外均为 durable。只有 journal 在已接受 Run 中途不可用时，控制面
才用明确的 ephemeral recovery/`journal_unavailable` terminal 让 live stream 诚实
收敛；客户端不得把它当成已持久化历史。若 Conversation 事务仍可写，该 memory-only
terminal 会在同一事务中把 Turn 置为 failed/cancelled、遗留 provider `started` 置为
`incomplete`、遗留 dispatched operation 置为 `outcome_unknown`、删除该 Run 的 durable
prefix/snapshot，并把 `run_journal_state` 转为
event/byte 计数归零的 `pruned` unavailable tombstone。它保留有界 Run identity 和 usage
事实，但 replay 必须明确失败，retention 不会把半截 Run 永久当作 active。删除
Conversation 会级联删除 tombstone。若整个 SQLite 暂时不可写，则不伪造 durable 清理；
现有 durable prefix 保持原子旧状态，存储恢复后的 startup recovery 再按下述规则收敛。

当前 `run.started` 还固化受信 Catalog 的 `model_id` 和完整 `model_profile_digest`；它们只
标识本 Turn 已资格检查的模型快照，不包含 endpoint 或 generation options 正文。旧事件没有
这两个字段时仍按旧 schema 严格回放。`run.started.context_plan` 当前包含 `plan_id`、
plan/toolset digest、section 数、完整/
纳入/省略的历史消息计数、历史来源 digest、`full|completed-turn-collapse-v2` 窗口策略、
估算输入、原生/运行窗口、硬输入预算、80% 触发阈值、60% 目标、输出预留、`256`
tokens 的 provider template reserve 和 estimator ID。它不包含 section 正文、用户消息、
assistant 历史、platform/Agent instructions 或 Tool schema。当前
`qwen3.5:2b` 的对应容量值为 `262144 / 32768 / 30720 / 24576 / 18432 / 2048`；这些
来自受信模型资格与策略，不是 Worker 声明。`completed-turn-collapse-v2` 只表示把最旧
完整 completed Turn pairs 投影成绑定 source/collapsed/preserved digest 的 content-free
marker，并至少保留最近一个完整 pair；它不是模型 summary 或语义摘要，SQLite 中的完整
transcript 不因投影而删除。旧 tail-v1 snapshot 仍可版本化解码，但不能静默套用 v4
renderer/v3 section registry。

`model.request.started.request_digest` 是对 exact encoded provider request 加固定 domain
separator 后的 SHA-256；它用于对账而不能还原 Prompt。事件不含 messages、system prompt、
Tool schema 或请求正文。`estimated_input_tokens` 是该 iteration 完整 runtime transcript 与
当次 EffectiveToolSet 的保守估算，因此 Tool result 回流后的后续调用不再复用 base
ContextPlan 的初始估算。`request_bytes`、`message_count`、`tool_count` 和
`tool_result_call_ids` 均有严格上限；首轮结果 ID 为空，后续轮必须按完成顺序引用全部
canonical finished Tool result。`sequential-multi-tool-v1` 下，未消费两次调用预算时
仍暴露冻结的 ToolSet，达到预算后精确收窄为空；旧 feature-only Run 仍按 one-shot 规则验证。

`model.response.finished` 表示该 provider/Broker stream 已经规范收敛，不表示它的正文被
复制进事件。正常 `tool_use|end_turn` 必须带完整、经验证的单调用 usage 和空 error code；
`error|cancelled` 没有可信终帧 usage 时固定为 `0/0/false` 并带安全 error code。后续
`assistant.block.*`、`tool.call.*` 仍是 Worker 对规范化结果的语义投影，不得与 provider
boundary 合并或互相冒充。

`iteration` 是 Worker 对话主循环的逻辑轮次；`provider_call_index` 是该 Run 内真实 HTTP
调用的严格递增序号；`attempt` 在普通调用为 0，在唯一恢复调用为 1。attempt 1 的
`request_id` 固定为 `model-<iteration>-recovery-1`，并与前一错误 response 和中间
`model.recovery.started` 使用同一 32-hex `recovery_id`；初始 attempt 的 recovery_id 为
`null`。只有状态 `400|413`、内容类型 JSON、对象恰好只有有界字符串 `error`，且窄规则
同时命中 context/media 与超限含义时才生成 `model_context_overflow` 或
`model_media_overflow`。认证、网络、其它状态、额外字段、错误 content-type、状态 200
部分流和协议错误都不能进入恢复分支。

首个 attempt 的 usage 在错误 response 事务中立即变为 `incomplete`。恢复投影在同一冻结
模型 profile 下重新 admission，并以 context boundary digest CAS 后才能发布唯一 recovery
事件；第二笔 request/usage 使用独立 provider_call_index。恢复成功时 terminal 只累加完整
usage，但任一 incomplete attempt 都使整体 `complete=false`。第二次 overflow、取消、
deadline、部分输出或投影安装失败直接结束，不生成第二个 recovery boundary，也不重放
已经完成的 Tool/Capability。

每个 terminal 的 `usage` 当前固定为
`{input_tokens,output_tokens,last_input_tokens,complete}`。每次 provider 调用前，Control Plane
在 exact request observer 中把 `provider_usage.started` 与 request boundary 放进同一
SQLite 事务，并绑定 provider/model、模型 profile digest、ContextPlan ID、该调用的
runtime 估算和硬输入预算；Ollama 完整终帧的 `prompt_eval_count`/`eval_count` 经当前
profile 校验后，`provider_usage.complete` 与 response boundary 也在同一事务提交。
任一侧事务失败都不会把 HTTP/Worker 结果冒充为已完整持久化边界。价格未知时
cost/currency/pricing profile 一律为 `NULL`，不暗示零成本。

terminal 事务会先把仍为 `started` 的调用转为 `incomplete`，再只汇总
`complete` 调用；任一 incomplete 记录都使 `usage.complete=false`。如果已有
provider ledger 记录，terminal payload 必须与该汇总完全一致，否则 terminal、
Turn 状态和 snapshot 整个事务回滚。Worker 发出的 terminal 不含 usage，不能
伪造该字段。不持久完整 prompt、provider 原始帧或逐 token usage。

`run.failed.code` 是 1..64 字符的安全标识，只允许 ASCII 字母、数字、`.`、`_`、`:`、
`-`；message 仍可为有界 UTF-8 文本。Control 入站和 durable replay 使用同一规则，避免
运行时接受但重启后不可回放的协议漂移。

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
started → finished，已 started Tool 补 finished。若新协议 Run 有未配对的
`model.request.started`，还会先补唯一
`model.response.finished(error, usage_complete=false, error_code=control_restarted)`，再追加
`run.failed(control_restarted)`。
非法、超限或无法完整闭合的 journal 会整体回滚并 fail closed。旧 Worker 不会重启，
但 events endpoint 可把已经收敛的 durable 事实通过 replay source 发送给重连客户端；
这不是旧模型流或 ephemeral delta 的续跑。
删除只允许没有 active Run 的 Conversation，并在一个事务中移除其 Turn、Run
events、journal metadata、snapshot、provider usage 及与该会话绑定的 operation
ledger 记录；
活跃时返回冲突，先取消或等待终态。

### 浏览器回放投影

浏览器同时展示两个不同的事实面，不能把它们混成一份伪事件历史：

- 右侧 Conversation 面按 `turn_id` 展示 session API 返回的 durable user message、状态和
  仅在 `completed` 时存在的 assistant message；实时 assistant delta 只临时附着在拥有该
  active Run 的 Turn 上，终态后由 durable transcript 对账；
- 左侧 Run 面按 canonical `seq` 展示 live/replay EventEnvelope，并映射到
  User/Harness/LLM/Tool 四条视觉泳道；过滤和播放只改变可见节点/选择游标，不重新取数、
  重排事件或改变 replay cursor；
- 为说明提交边界，界面可以从拥有该 Run 的 Turn user message 推导一个
  `User → Harness` 节点。该节点必须明确标为 projection，没有 `seq`、`event_id` 或
  `durability`，不得序列化成 EventEnvelope，也不得写入 journal；
- 节点检查器分开呈现逻辑消息、canonical payload 和完整 EventEnvelope。模型调用边界
  只显示协议允许的受限元数据，并明确说明原始 provider request/response、system/hidden
  prompt 没有持久化、不可查看；
- 选择 Turn 时定位其 Run，选择节点时反向定位其拥有 Turn。session 切换、注销或 Run
  切换必须停止播放定时器并使旧异步响应失效；live refresh 不能擅自把用户正在检查的
  历史 Run 重置到最新 Run。

所有 Conversation 正文、payload、错误和 envelope JSON 都使用 inert text 渲染；前端不
从文本猜 Tool/Run 状态，也不把 replay control frame 当作 canonical EventEnvelope。

## Authenticated Context inspection

正文隐藏的只读检查接口是：

```text
GET /api/runs/<run-id>/context
```

它要求有效 browser session、拒绝所有 query parameter，并返回 `Cache-Control: no-store`。
浏览器不能借此提交、替换或重排任何 section。响应绑定完整 Agent/Conversation/Turn/Run
identity，并有两种诚实 availability：

- `exact`：该 immutable ContextPlan 仍在当前 Gateway 的有界 Run retention 中。Control
  Plane 每次先重新验证 plan/digest，再返回 public ContextPlan metadata、renderer version、
  leading system sections 的 merge 规则、provider message count，以及按序 section 的
  `id/role/trust/provenance/dependency_digest/cache/budget/truncation_reason/estimated_tokens/
  content_bytes/content_digest`，并返回 renderer 与 prompt-section registry version。
- `summary_only`：RunRecord/ContextPlan 已因重启或 retention 不在内存。接口只从经过完整
  replay 验证的 snapshot 中返回 durable `run.started.context_plan` 摘要；renderer、provider
  message count 和逐 section metadata 明确不可用，不尝试从当前源码重建过去的 Prompt。

`exact` 路径还把 Run identity、首个 canonical `run.started.context_plan` 摘要与内存 plan
逐字段交叉绑定；即使两个各自有效的 plan 被错误交换也会 fail closed。`content_digest`
是使用每个 Gateway/Registry 独占、进程内随机 256-bit key 的 domain-separated HMAC-SHA256：
同一 Gateway 内可对账，key 不持久、不返回、不复用登录 token，重启后允许变化，浏览器
不能将其用作隐藏 section 的离线猜测 oracle。

两种响应都不包含 section content、provider messages、用户/assistant 正文、system/hidden
prompt 或 Tool schema。`content_exposure` 分别是 `withheld|unavailable`。这不是第二个
ContextPlan persistence、prompt dump 或 debug trace；Conversation 正文由原有会话 API
读取，request digest 由 model boundary event 单独对账。匿名请求为 `401`，无效/foreign
Run 为 `404`，Engine 冲突为 `409`，受信 projection 或 durable replay 不可用为 `503`。
若 RunRecord 在检查期间被 retention 淘汰，接口转入同一 validated `summary_only` 路径，
不会把正常淘汰误报为 foreign `404`。

正文提升默认关闭。只有启动前显式设置 `HARNESS_V2_CONTEXT_REVEAL=1` 才创建独立的
`.runtime/secrets/context-reveal-token` 和有界审计库；提升请求还必须同时通过 browser
session、same-origin、CSRF 与 `X-Context-Operator-Token`。platform/Agent/workspace section
始终隐藏，其它 section 只返回逐 section 最多 2048 bytes 的 credential-redacted excerpt；
只支持仍驻留的 exact ContextPlan，历史 summary 返回 `409`。审计最多保留 4096 条
agent/run/availability/section-count 记录，不保存 token、prompt 或 excerpt。

## Model-view projection boundary

`context-projection-v2` 是独立于 EventEnvelope 与 `run-ui-v2` 的模型视图恢复边界；v1
仍可严格解码但不能静默按 v2/v5 renderer 复用。
`run-ui-v2` 只投影已发生的 Run 事件；context boundary 只描述“用哪些已验证来源和策略构建
模型输入”，两者不能互相冒充。首次 admission 时 boundary 与 Conversation revision CAS、
Turn、`run.started` 和 `run_journal_state` 在同一 SQLite 事务写入。

boundary 固定绑定 Agent/generation、Conversation/Turn/Run 和 source revision、ContextPlan
ID/digest、完整 history source digest、被保留 recent message IDs/digest、system instruction
dependency digest、model profile、compression policy、ToolSet/catalog/policy、renderer/section
registry version、window strategy 与估算 Token。普通 projection 不保存 section、
user/assistant、Tool result 或 provider request 正文。`semantic-summary-v1` 时同一 boundary
额外保存五类有界派生摘要、source message IDs/content digest、model/profile、prompt/policy、
renderer、provider request digest 和校验后 usage，但不保存被折叠原文。单条 canonical JSON
最大 16 KiB；每 Run 只有一条当前值，
`admission|replay|manual_compact|semantic_summary` 使用同一 codec。替换必须提供旧 boundary
digest 做 SQLite CAS，因此崩溃后只会读到前一完整 boundary 或后一完整 boundary，不能读到
半写状态。

复用前必须从当前受信 runtime snapshot 重建 boundary 并逐字段相等；Conversation revision、
model/profile、instructions、history、Tool policy 或 renderer 任一变化都拒绝复用，调用方只能
确定性重算。canonical completed transcript 始终 append-only，不被 boundary replacement
修改。boundary 随 Conversation 删除级联清理；最多 100 Conversations × 128 Turns × 16 KiB，
理论硬上限约 200 MiB，仍受 512 MiB Agent SQLite 页上限约束。CAS replacement 更新同一行，
不会按 compact 次数线性累积或逐 token/chunk 写盘。

## 持久 permission、operation audit 与 provider ledger

Capability audit 是 Agent SQLite 中独立的管理面状态流，不是 `EventEnvelope`，也不占用
Run 的 canonical `seq`。这样加入审批不会改变已发布的 Run projector 或伪造 Worker 事件。
其迁移顺序为：

```text
permission.requested(allow|ask|deny)
  → permission.resolved(approved|denied|expired|cancelled)
operation.intent → operation.dispatched
  → operation.outcome(succeeded|failed|cancelled|outcome_unknown)
```

每条记录只含 Agent/Run、permission/operation ID、状态、受限 digest 和时间，不保存
arguments、result、credential 或 executor input。准确、最大 4096 bytes 的 preview 保存在
permission row，arguments 只保留 domain-separated digest。每 Agent 最多 4096 个 permission、
64 个 pending、4096 个 operation 和 16384 条 capability audit；容量耗尽即拒绝新请求，
不丢弃未审计动作。`GET /api/agents/<agent-id>/runs/<run-id>/capability-audit` 以最大 128
条分页只读重放；读取或重复审批永远不会释放 executor。pending 在 Run terminal/cancel、
Gateway restart、Agent drain/upgrade/delete、TTL 到期或 binding 漂移时原子收敛为
`cancelled|expired`。

`operation_ledger` 为有外部副作用的 capability broker 提供持久底座：

```text
intent → dispatched → succeeded | failed | cancelled | outcome_unknown
```

- 先写 `intent`，再释放外部操作；`dispatched` 在释放前绑定有界 executor kind
  和 executor identity digest。同一 idempotency-key hash 只有在 capability、policy、
  Run/Turn/call 身份和 request digest 全部相同时才返回同一记录；漂移会冲突。
- 只有 `dispatched` 可转入终态。重启恢复把所有遗留 `dispatched` 标记为
  `outcome_unknown`，不填 outcome digest，也不自动重放外部操作。
- 只读 file capability 也通过 ledger 保留统一审计；`file/edit`/`file/write` 是首个使用
  `ask` 与副作用恢复语义的 executor。现存 target 绑定 same-Run 完整 read receipt，create
  绑定 target absence/parent identity，commit 绑定 operator 已批准的 diff digest。每 target
  串行、同目录 temp 与 atomic rename 只缩小不确定窗口，并不把本地 journal 变成
  “恰好一次”。`exec/run` 复用相同状态机，executor identity 额外绑定 runner ID/catalog/
  singleton policy；固定 compile 命令已支持，但 Shell/Skill/MCP 仍未支持。

`provider_usage` 是独立的每调用台账，不是 EventEnvelope kind。它的
`started → complete|incomplete`、聚合与 terminal 绑定语义以上述 terminal `usage`
规则为准。

## Slash Command 不进入 Run protocol

Slash Command 是 authenticated Web control result，不是 `EventEnvelope`、Conversation
message、Turn 或 Tool。`GET /api/commands` 返回稳定 registry/help；
`POST /api/sessions/<id>/commands`（以及现有 Conversation Run 输入边界中的 `/` 分支）返回：

```text
schema_version=1 · kind=slash_command_result · command_id
modifies_state · bounded result · bounded ui_effect
model_invoked=false · turn_created=false
```

因此 `/status|context|model|compact|permissions|cancel|clear` 不消耗 canonical `seq`，也不应
在 replay 时间线中伪造 Harness/LLM 节点。status/context/permission 只是已有受信读模型的
瞬时投影；cancel 由既有 Run cancel/terminal protocol 审计，clear 使用既有 Conversation
delete transaction。model/compact 的 `ui_effect` 只影响浏览器下一次普通 Turn request，
下一轮仍由 ModelCatalog 和 ContextCompiler 固化权威 boundary。客户端可以把完整 command
result 显示为独立控制面卡片，但不能写回 transcript。

## Background Task protocol 不伪装成 Run event

Task 是绑定 parent Run 的独立 durable control-plane record，不占用该 Run 的 canonical
`seq`，也不写成 `tool.call.*` 或 assistant message。认证 API 返回固定身份、generation、
Conversation/Turn/parent Run、command、state、bounded result/digest、error、byte/count 与
timestamps；通知严格为 `task.queued → task.running → task.<terminal>`，每条有 sequence 和
payload digest。状态机只有：

```text
queued → running → completed | failed | cancelled
   └──────────────→ interrupted   # Gateway recovery; never redispatch
```

terminal 只在 singleton runner 已 reap 且精确 Task root 清除后提交。每 Task 最多 4 条
4 KiB 通知、16 KiB result；每 Agent 最多 4 active、128 retained，终态 7 天。API replay
只是读取 `task_notifications`，不会释放 executor。未来 UI 可把它作为独立父子投影，但普通
文本和 Run EventEnvelope 都不能冒充 brokered Task 消息。

`agent-delegate` 复用同一 Task 状态机，但执行事实属于两条独立 canonical Run。父 Run 只
出现常规 `tool.call.requested/started/finished`；link 另行记录 `parent_run_id` 与 child
Agent/Conversation/Turn/Run ID，mailbox 最多两条带 digest 的 `parent_to_child` /
`child_to_parent` 消息。child Run 保持自己的 `seq`、model boundaries、Tool events 和唯一
terminal，Web 通过 Agent-scoped replay 单独读取。UI 可以把 parent Task/link 与 child Run
并列关联，但不得合并 `seq`、复制 child event 到 parent journal，或从 assistant 字符串
推断一条 mailbox message。

MCP/LSP 不引入第二套 Run event。配置存在时，`extension/call` 与其它 brokered Tool 一样只
产生 canonical `tool.call.requested|started|finished`，privileged lifecycle 仍在同一
permission/operation/capability-audit ledger。远端 JSON-RPC wire frame、endpoint、DNS 和
credential 不写 EventEnvelope；Tool result 只保留经 ID/schema/size 校验的 bounded projection。
release 空 catalog 时该 Tool 不进入 `run.started.visible_tools`，普通文本不能动态启用扩展。

Skill 同样不新增隐藏 event stream。只有受信 registry 非空时，`skill/run` 才能出现在未来
Run 的 ToolSet；一次调用沿标准 `tool.call.*` 和 permission/operation audit。archive/source/
environment identity 与完整 input preview 绑定 approval，但包正文、环境路径和 runner env 不写
EventEnvelope。result 只保留 12 KiB bounded command result；install/upgrade/delete 是独立
authenticated control-plane mutation，不伪造成 Conversation message 或模型 Tool 成功。

## 顺序与状态机

```text
run.started [model-call-boundaries-v1 + sequential-multi-tool-v1 + one-shot-overflow-recovery-v1]
  ├─ model.request.started #1 [no Tool result; effective ToolSet]
  │    └─ model.response.finished #1 [tool_use | end_turn | error | cancelled]
  │         └─ iff exact overflow before any provider frame:
  │              model.recovery.started [same iteration; attempt 1; new projection]
  │              └─ model.request.started → model.response.finished [one retry only]
  ├─ assistant.block.* and/or tool.call.requested → started → finished
  ├─ model.request.started #2 [first Tool result ID; effective ToolSet]
  │    └─ model.response.finished #2 [tool_use | end_turn | error | cancelled]
  ├─ optional second tool.call.requested → started → finished
  ├─ model.request.started #3 [both Tool result IDs; empty ToolSet]
  │    └─ model.response.finished #3 [end_turn | error | cancelled]
  ├─ assistant.block.started
  │    ├─ assistant.block.delta *
  │    └─ assistant.block.finished | assistant.block.discarded
  └─ run.completed | run.failed | run.cancelled
```

规则：

1. Run 先有且只有一个 `run.started`，最后有且只有一个 terminal。
2. terminal 后不得发布任何事件。
3. 声明 model boundary feature 的 Run，iteration 必须从 1 连续递增；唯一 overflow
   recovery 可在同一 iteration 追加 attempt 1，其 provider_call_index 仍严格递增。每次
   request 有且只有一个匹配 response；request 只能在上次 response、必要 Tool result 或
   唯一匹配 recovery boundary 完成后出现。
   `run.completed.model_iterations`、Tool 次数和 response outcome 必须一致。
4. block ID 和 call ID 在一个 Run 内不可重复；delta/finish 必须引用已打开对象。
5. Tool requested → started → finished 严格有序；同一时刻只允许一个 pending Tool。
6. terminal 前所有 model call、block 和 Tool 必须闭合。Worker/Gateway 崩溃或取消时由
   Control Plane 生成
   一个或多个 closure/recovery events，再发布 terminal。
7. Worker frame 先完成字段、类型、大小和状态转换验证，再写 journal/更新内存。model
   boundaries 只能由受信 Control Plane 产生，Worker 不能提交同名 canonical event。
8. 前端按 `seq` 投影；不要依赖 HTTP chunk 边界或模型 token 边界。

## Durable replay、cursor、gap 与 snapshot

持久 replay 有独立认证读接口，并也是 events endpoint 在没有 live RunRecord 时唯一允许的
只读后备 source：

```text
GET /api/runs/<run-id>/replay?after=<cursor>&limit=<page-size>
```

`after` 范围是 `0..1,000,000`，`limit` 范围是 `1..128`。Run-only API 先从
SQLite 解析 Agent/Conversation/Turn/Run 完整 identity，再在该 Conversation 的逻辑
QueryEngine 下校验并读取；不需要 live `RunRecord`。响应包含：

```json
{
  "identity": {
    "agent_id": "...",
    "conversation_id": "...",
    "turn_id": "...",
    "run_id": "..."
  },
  "availability": "complete",
  "oldest_cursor": 0,
  "latest_cursor": 8,
  "next_cursor": 8,
  "has_more": false,
  "events": [],
  "gaps": [],
  "snapshot": {}
}
```

- `oldest_cursor` 是当前保留形式的最早 cursor；完整 event 保留态为 `0`，
  snapshot-only 为 snapshot `through_seq`。`next_cursor` 是该页已应用的最后
  cursor（snapshot-only 时直接为 snapshot `through_seq`）。按 event 分页的客户端只能
  在成功应用整页 events 并处理对应 gaps 后才保存它；`has_more` 表示
  `next_cursor < latest_cursor`。响应 snapshot 始终投影到 `latest_cursor`，可作为独立
  完整投影资料，不是该 event page 的增量 delta。
- 每次读取在返回任何 page 前，先在同一 SQLite read transaction 内读取整个
  有界 durable Run，严格验证 JSON shape、SQL 列与 envelope 重复字段、四级
  identity、event ID、序列、每个 kind 的 canonical payload（包括 ContextPlan budget、
  model request/response pair、terminal usage 与当前 `ToolSpec` 参数/结果）和完整状态机。
  digest 正确但 document
  语义非法的 snapshot 同样拒绝。任一损坏都 fail closed，不返回一段看似合法的
  prefix。
- `gaps` 是显式区间，协议 allowlist 为 `ephemeral_not_durable`、`retention`
  和 `journal_unavailable`。当前成功响应只产生前两种；journal 不可用直接以
  `503` fail closed，不返回带 `journal_unavailable` gap 的伪成功页。gap 不合成遗失
  delta，也不允许客户端把 gap 视为空文本。reserved recovery range 当前以
  `ephemeral_not_durable` 报告；它表示这段 cursor 不可恢复，不声称每个位置都
  曾经存在一个 delta。
- `snapshot` 当前为确定性 `run-ui-v2` 投影，digest 绑定完整 identity、
  `through_seq`、`complete` 和包含 `model_calls` 的 document。terminal 事务会原子保存
  严格终态 snapshot；legacy `run-ui-v1` 仍按旧 document shape 严格解码并可
  snapshot-only replay，不会被静默解释为含 model boundaries。这是 UI 恢复资料，不是
  Conversation transcript、ContextPlan 或语义 compaction summary。
- 当前成功响应的 availability 为 `complete`、`partial` 或 `snapshot_only`；
  `unavailable` 为协议保留值，当前不以 `200` 返回。正常保留态从 `events` 分页。
  当 journal
  retention 移除旧 terminal Run 时，先验证完整序列并生成/刷新 snapshot，再在
  同一 `BEGIN IMMEDIATE` 事务中删除该 Run 的全部 events，并把
  `run_journal_state` 转为 `snapshot_only`。活跃、无 terminal 的受管 Run 不会被
  prune；上述降级 tombstone 使用内部 `pruned` 状态且永不作为成功 replay 返回；损坏或
  metadata 不一致会回滚整个 prune。
- `snapshot_only` 响应不再返回 events；若 `after` 落后，返回一个 `retention`
  gap 和经验证 snapshot。大于 Run 最新 durable cursor 的 `after` 被拒绝，不伪造
  空页或成功恢复。未知/foreign Run 为 `404`，超过最新 durable cursor 为
  `416`，Engine/会话状态冲突为 `409`，损坏或不可用 journal 为 `503`，不支持的
  query 为 `400`。

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
- endpoint 在预检时固定 source：有 live RunRecord 时只发送当前内存中
  `seq > Last-Event-ID` 的事件；没有 live RunRecord 时只分页读取 durable replay。
  一个连接绝不在两种 source 之间切换，因此不会双发。
- cursor 范围当前为 0..1,000,000。
- 浏览器刷新后可从 session 详情的 `running` Turn 取得 `run_id`，在该 Run 仍位于同一
  Gateway 内存 retention 时重新附着；前端最多按 `Last-Event-ID` 有界重连 3 次。
- durable source 按真实 cursor 穿插 `stream.gap` control frame；普通
  `ephemeral_not_durable` gap 的 SSE `id` 是 `to_seq`。`snapshot_only` retention gap
  不确认 cursor，紧随其后的 `stream.snapshot` 才以 `through_seq` 作为 SSE `id`，避免
  两个 control frame 之间断线使重连跳过 snapshot。
- durable page 在发送前重新验证完整 Run identity；并发删除时静默结束，不泄漏预取页。

重要限制：durable source 只重放已持久、已验证的语义事实。旧 Worker/model stream
不会重建，ephemeral delta 永久缺失并以 gap 表示；到 recovery/原 terminal 后结束。
不要把它称为活跃执行或 Worker 的 durable resume。

## 容量与磁盘语义

- 单个 Worker/event envelope 最大 65,536 bytes；用户消息最大 8,192 UTF-8 bytes。
- 每个 Conversation 同时最多一个 active Run；Conversation/Turn 数量、消息字节和 Agent
  `state.sqlite` 大小都有硬上限，达到上限时拒绝，不自动扩展成无界历史。
- 每个 provider 调用只增加一个 request 和一个 response durable event，不按 provider
  frame/token 写入。每 Run 最多 512 live events、1 MiB live bytes、256 KiB durable bytes；
  普通事件为最坏
  的 block discard、Tool started/finished 和 terminal 预留 4 个事件位置及 32 KiB。
- 控制面最多 4 active Runs、保留 64 个近期 live Run；journal 保留 256 个近期 Run。
- replay 每页最多 128 events，cursor 上限 1,000,000；返回前必须在 512 events /
  256 KiB 内验证完整 durable Run。单个 durable event 与 `run-ui-v1|v2` snapshot
  各最大 65,536 bytes。受信 JSON decoder 限制深度 16、节点 4,096、object
  fields 128、array items 256、单字符串 16,384 UTF-8 bytes 和字段名 128 bytes。
- 每 Agent 最多 4,096 条 operation ledger 记录；每 Run 最多 64 次 provider usage
  调用。Agent `state.sqlite` 最大 512 MiB，SQLite journal size limit 为 16 MiB。
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
时间线按会话中的全部 Turn/Run 选择，严格单列展示 seq，并用文字而非仅靠颜色标明
Harness/LLM/Broker/Tool/Replay 的主体、方向、动作和 model iteration 摘要。点击条目查看的
完整 JSON 是 canonical envelope/control，不是 provider 原始请求/响应。选择已结束
Conversation 时，前端通过 replay API 恢复所选 Run 的 durable timeline、gap 或 snapshot；
活动 stream 重连则理解上述 control frame。上下文检查只在用户点击时调用上述 no-store
接口，严格验证 identity/字段/availability 后再以正文隐藏的 metadata dialog 显示；Run
切换会使旧响应失效。所有模型/Tool/event 字符串用 DOM
`textContent`，禁止 `innerHTML` 或把 payload 当 markup。

## 协议演进

字段、kind、durability、顺序或 cursor 语义变化必须：

1. 更新 contracts 和控制面 validator；
2. 增加合法/非法序列、过大 frame、重连和 terminal convergence 测试；
3. 同步前端投影和本文件；
4. 若破坏现有消费者，提升 schema version 并写迁移/拒绝策略；
5. 不允许 Worker 与 Control Plane 静默接受彼此未知版本。

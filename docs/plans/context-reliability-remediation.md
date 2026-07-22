---
owner: runtime-maintainers
status: active
last_reviewed: 2026-07-21
review_cycle: quarterly
---

# Context reliability remediation plan

## Objective

本计划记录 2026-07-21 已完成的多轮、长上下文、自动压缩、Tool 回流和 overflow recovery
可靠性整改。目标不是让系统“更少报错”，而是建立以下可验证合同：

1. 一个合法完成的 Turn 不会让同一 Conversation 的下一 Turn 永久不可提交；
2. 硬安全准入、软压缩决策、Provider 实际用量和 UI 预测使用明确且不可混淆的计数域；
3. Tool 已经执行后，总能向模型回流一个有界结果或 receipt，不会因本地预算检查中途挂掉；
4. 压缩只移动完整的历史 Turn bundle，canonical transcript 不被修改；
5. overflow recovery 后，实际 Provider request、durable boundary、事件、usage ledger、replay
   和 context inspector 始终引用同一个 active ContextPlan；
6. semantic summary 只有在角色隔离、持久复用、总大小预算、可观测状态和回滚路径全部
   通过资格后才重新启用；
7. 用户看到的“下一条消息还能安全写多少”由服务端版本化投影提供，并明确区分近似值、
   硬限制和上一轮计算量。

本计划是 [Runtime rebuild plan](runtime-rebuild.md) 中 `CTX-R01` 的窄执行子计划。
原计划的历史实现和 RR 证据继续保留，但 `GATE-05 context` 与 `GATE-07 release` 在本计划
执行期间保持 reopened，现已随最终资格重新关闭。这里的 15 个子项是本次整改的唯一细粒度
状态账本；不得在其它文档另建一份 Now/Next/Later 状态副本。

## Scope and non-goals

范围包括：

- completed history、Tool transcript 和 ContextPlan renderer；
- ModelProfile、token/byte accounting、next-turn preview 和 compaction policy；
- deterministic collapse、semantic summary 和 reactive overflow recovery；
- Conversation capacity、错误合同、Web composer 和上下文用量展示；
- SQLite migration、replay、故障注入、真实模型资格和文档治理。

本计划不扩大以下范围：

- 不增加新的模型 Provider、网络 endpoint、模型或用户可配置 generation options；
- 不扩大 Worker 的 Landlock/seccomp、文件、进程、网络或 Tool 权限；
- 不引入 LangGraph、LangChain、第二套 Agent loop 或第二份 canonical transcript；
- 不承诺 TLS、多用户、HA、新平台或未完成的长期 SSD/SMART 资格；
- 不用静默删除历史、VACUUM、整库重写或会话内容截断解决容量问题。

## Audit baseline

`AUDIT-CTX-20260721-01` 基于
`3c52fd8ed190e1a669888313f63261893159c1d0` 完成只读审计：

- 全量测试为 `581 passed in 23.56s`，说明以下缺口尚未被现有 suite 捕获，或被负向
  fail-closed 测试明确固化；
- 当前 20815 health 正常，真实 `iollama:11434/qwen3.5:2b` 和
  `landlock+seccomp` 可用；
- 12 条、每条 512 ASCII bytes 的历史在完整模型请求中，byte estimator 为 `23055`，
  Provider 实际 `prompt_eval_count` 为 `3623`；普通编译却已折叠 12 条中的 10 条；
- 当前 journal 样本中 estimator / Provider 首次 input 的比例为 `8.38x` 至 `10.59x`；
- 合法的 8192-byte user 与 8000-byte assistant pair，可使下一条仅为 `next` 的 Turn 在
  admission 阶段永久失败，手动 compact 也不能恢复；
- 较长合法 user 加 8192-byte `file/read_text` projection，可在 Tool 已执行后、第二次
  Provider HTTP 前以 `model_context_limit` 失败；
- semantic summary 声明 48 KiB source，但当前 profile 的内部 byte admission 实际约在
  27184/27185 canonical source bytes 处发生接受/拒绝切换；
- 合法的 8117-byte summary content 加 132 个 source IDs 可通过 ContextPlan，却超过
  16 KiB projection boundary，导致整个 Turn 无法接受；
- recovery 后返回 Tool 时，Ollama session 使用 recovery plan，但后续事件和 ledger 重新
  标成原 admission plan。

这些数字是复现证据，不是新的永久策略常量。最终实现的容量和阈值必须从受信
ModelProfile、ToolSet、renderer 和版本化 policy 推导。

### Finding-to-work-item map

| Finding | 已确认缺口 | 负责整改项 |
| --- | --- | --- |
| F-01 | 合法 completed pair 可毒死下一 Turn | `CTX-R01-05` |
| F-02 | byte estimator 被同时用作硬准入和软压缩阈值 | `CTX-R01-04`、`CTX-R01-07` |
| F-03 | UI 把 Provider actual 与 byte 阈值直接相减 | `CTX-R01-12`、`CTX-R01-14` |
| F-04 | Tool result 回流没有 headroom 或动态 receipt | `CTX-R01-06` |
| F-05 | recovery 后 active ContextPlan 身份漂移 | `CTX-R01-08` |
| F-06 | recovery boundary 与 event 非原子 | `CTX-R01-08` |
| F-07 | summary 默认启用、隐藏调用、无跨 Turn/重启复用 | `CTX-R01-02`、`CTX-R01-11` |
| F-08 | summary 角色提升、prompt/policy digest 不绑定真实请求 | `CTX-R01-10` |
| F-09 | summary source/输出/boundary 上限互相冲突 | `CTX-R01-11` |
| F-10 | 跨 Turn 不保留成功 Tool call/result | `CTX-R01-03`、`CTX-R01-09` |
| F-11 | 128 Turn 容量错误不可区分且无 continuation | `CTX-R01-13` |
| F-12 | failed/recovery/model-switch/UTF-8 场景下 UI 预算错误 | `CTX-R01-12`、`CTX-R01-14` |
| F-13 | CLAUDE、README、SECURITY、architecture 的现状声明漂移 | `CTX-R01-02`、`CTX-R01-15` |

## Non-negotiable invariants

以下不变量优先于局部实现便利：

- **Canonical transcript authority**：完整 Conversation/Turn/Tool 事实只在 Agent SQLite
  中有一份权威记录；ContextPlan、summary 和 preview 都是可丢弃、可重建的有界投影。
- **Completed-only promotion**：failed、cancelled、interrupted 和 partial Turn 永不晋升为
  后续模型历史；已产生的 operation/capability 审计仍按既有合同保留。
- **Turn-bundle atomicity**：压缩、摘要和历史渲染不能切开 tool call/result/final answer；
  不得产生孤立 Tool message。
- **Count-domain safety**：只有相同 basis、model profile、renderer、ToolSet 和 policy 版本的
  数值可以比较；byte、token、累计计算量和持久上下文占用不得隐式换算。
- **Provider observation semantics**：Provider usage 只对已经发送的 exact request 权威；对
  下一条尚未提交的消息只能给出带 basis/置信信息的投影，不能伪装成精确计数。
- **No authority promotion**：user、Tool result、summary 和其它派生数据永不进入 Provider
  system authority；内部 `trust` 标签必须映射成真实 provider role 边界。
- **No stranded side effect**：Tool dispatch 后即使完整结果放不进窗口，也必须有预先保留的
  bounded receipt 能回流并进入唯一终态。
- **Durable transition consistency**：active plan、boundary、canonical transition event 和
  usage ledger 的切换必须具有明确的原子/崩溃语义。
- **Bounded storage**：不得按 token、delta、provider frame 或重复 preview 写盘；新增表、
  cache、WAL、事件和日志都必须有条目/字节/retention 上限。
- **Fail early and precisely**：若确实无法保证 continuation 或 headroom，必须在首次 Provider
  或 privileged Tool dispatch 前以专用错误拒绝，不能留到下一轮或副作用之后才失败。
- **Safe rollback**：新 decoder 先于新 writer 发布；旧 snapshot 只允许解码/回放，不按新
  renderer 静默复用；不做 destructive down migration。

## Status model

状态仅允许：`not_started`、`in_progress`、`blocked`、`done`、`deferred`、`superseded`。

- 开始一个子项前先把总状态表对应行改为 `in_progress`；
- 只有该子项全部 checkbox、测试、迁移/回滚、文档和要求的真实纵向证据都完成后才能标
  `done`；
- `blocked` 必须记录阻塞条件和解除条件；
- 拆项时保留原 ID，并用 `superseded` 指向完整 replacement IDs；
- 每次推进在 Execution evidence 中追加稳定记录，不保存 token、cookie、prompt 正文、
  不稳定 Run ID 或 runtime 日志。

## Dependency graph

```text
CTX-R01-01 regression baseline
 ├─ CTX-R01-02 interim containment and documentation truth
 ├─ CTX-R01-03 completed-Turn context ADR
 ├─ CTX-R01-04 count-domain contract
 │   ├─ CTX-R01-05 continuation invariant
 │   │   ├─ CTX-R01-06 Tool-loop headroom
 │   │   └─ CTX-R01-07 deterministic compaction policy
 │   └─ CTX-R01-08 overflow recovery consistency
 └──────────────────────────────────────────────────────────┐

CTX-R01-03 + 04 + 06 + 07 ── CTX-R01-09 completed Turn bundle
CTX-R01-02 + 03 + 04 ─────── CTX-R01-10 summary trust protocol
CTX-R01-07 + 09 + 10 ─────── CTX-R01-11 durable summary lifecycle
CTX-R01-04 + 08 + 09 ──────── CTX-R01-12 next-turn preview
CTX-R01-01 + 03 ───────────── CTX-R01-13 capacity and continuation
CTX-R01-02 + 12 + 13 ──────── CTX-R01-14 Web UX
CTX-R01-02..14 ─────────────── CTX-R01-15 qualification and release
```

可以并行设计 `CTX-R01-08` 与 `CTX-R01-05..07`，也可以并行实现 `CTX-R01-13`；任何并行
工作都不能绕过依赖项的 contract 决策。

## Work-item ledger

本次整改总计 **15 个工作项**。

| 顺序 | ID | Priority | 目标 | depends_on | 状态 |
| ---: | --- | --- | --- | --- | --- |
| 1 | CTX-R01-01 | P0 | 固化审计复现和行为级回归基线 | — | done |
| 2 | CTX-R01-02 | P0 | 临时关闭 summary v1 并纠正文档/功能声明 | 01 | done |
| 3 | CTX-R01-03 | P0 | 定义 completed Turn/Tool 历史合同和迁移 ADR | 01 | done |
| 4 | CTX-R01-04 | P0 | 建立不可混用的 context 计数域合同 | 01 | done |
| 5 | CTX-R01-05 | P0 | 保证合法 Turn 的下一轮可继续性 | 03, 04 | done |
| 6 | CTX-R01-06 | P0 | Tool-loop headroom 与动态结果 projection | 04, 05 | done |
| 7 | CTX-R01-07 | P0 | 重做 deterministic autocompact trigger/target | 03, 04, 05, 06 | done |
| 8 | CTX-R01-08 | P0 | 修复 overflow recovery active plan 与事务一致性 | 01, 04 | done |
| 9 | CTX-R01-09 | P1 | 持久化并渲染完整 completed Turn bundle | 03, 04, 06, 07 | done |
| 10 | CTX-R01-10 | P1 | semantic-summary-v2 角色、prompt 和版本合同 | 02, 03, 04 | done |
| 11 | CTX-R01-11 | P1 | summary 持久复用、增量、总预算和可观测生命周期 | 07, 09, 10 | done |
| 12 | CTX-R01-12 | P1 | 服务端 durable next-turn preview | 04, 08, 09 | done |
| 13 | CTX-R01-13 | P1 | Conversation 容量错误与显式 continuation | 01, 03 | done |
| 14 | CTX-R01-14 | P1 | Web composer、预算、失败和压缩状态 UX | 02, 12, 13 | done |
| 15 | CTX-R01-15 | release | 完整故障矩阵、真实模型、SSD 和文档资格 | 02–14 | done |

## Detailed checklist

### CTX-R01-01 — Regression baseline

目标：先把本次审计的最小复现变成确定性行为测试，避免修复只覆盖 happy path。

- [x] 为 8192-byte user + 8000-byte assistant + `next` 增加 current-profile/ToolSet 回归；
- [x] 为长 user + 最大合法 Tool projection 增加“不得 Tool 后本地失败”回归；
- [x] 增加 `overflow → recovery → Tool → final` 的 provider request/event/ledger/boundary 对账；
- [x] 增加 estimator 与 Provider observed usage 相差 10x 时的软压缩和 UI 语义测试；
- [x] 增加 summary source、summary output × source IDs、boundary aggregate size 边界测试；
- [x] 增加 failed/cancelled/recovery/model-switch 后 next-turn baseline 状态测试；
- [x] 增加 ASCII、中文、emoji、JSON escape 的 8192/8193 UTF-8 byte 输入测试；
- [x] 保存 red-phase 结果摘要，再随对应修复把测试转为永久 green regression；
- [x] 确认测试只写 `.runtime/test-results`/pytest 临时目录，不修改生产 `data/`。

完成标准：所有 F-01 至 F-13 至少有一个行为级测试或明确归属的故障注入测试；不能只用
源码字符串断言代替状态转换验证。

### CTX-R01-02 — Interim containment and documentation truth

目标：在 summary v2 资格完成前，默认只运行 deterministic collapse，消除隐藏模型调用、
system-role 风险和 boundary-size admission 失败。

- [x] 增加 operator-owned、启动前固定的 summary feature gate，默认关闭 v1；
- [x] Web、Worker、Agent prompt 和 request body 均不能开启该 gate；
- [x] 自动和手动 compact 在 gate 关闭时不产生 summary Provider HTTP；
- [x] 手动 compact 即使回退 deterministic projection，boundary reason 仍记录
  `manual_compact`，并返回明确状态；
- [x] `semantic-summary-v1` 仅保留严格 decoder/replay，禁止新建或复用到新 renderer；
- [x] 当前 CLAUDE、README、SECURITY、architecture、event/release 文档准确描述临时行为和
  已知限制；
- [x] 关闭 summary 后的普通多轮、manual compact、overflow recovery、重启和删除测试通过；
- [x] 记录重新启用 v2 所需的 operator gate、migration 和 rollback 条件。

重新启用条件：只接受 operator 在进程启动前固定的 v2 gate；先部署 v1/v2 decoder 和
disabled writer，再通过角色隔离、aggregate boundary budget、跨 Turn/重启复用、真实模型与
故障注入资格后开启 writer。rollback 先关闭 gate，保留 v1/v2 decoder 和 canonical
transcript，不删除或降级 Conversation 数据。

完成标准：默认运行路径没有任何 summary 模型调用；deterministic collapse 和 canonical
transcript 行为不变；文档不再把未资格能力描述为安全/完成。

该完成标准描述 containment checkpoint。随后 `CTX-R01-10/11/15` 依 expand-then-enable 顺序
完成 v2 资格并把 v2 设为默认；v1 仍保持上述永久禁写，operator 设 v2 gate 为 `0` 时恢复本
checkpoint 的 deterministic-only 行为。

### CTX-R01-03 — Completed-Turn context ADR and migration contract

目标：在修改存储、计数和 UI 公式前，先确定后续上下文究竟持久化哪些消息。

推荐合同是版本化 `CompletedTurnContext`：

```text
user
  → assistant tool_use
  → tool_result / bounded receipt
  → ...
  → final assistant
```

- [x] 在 architecture/event protocol 中定义 `CompletedTurnContext`、item schema、身份和顺序；
- [x] 明确 system/platform/Agent/workspace sections 每轮重编，不伪装成持久 Conversation message；
- [x] 明确只有 completed Turn 在终态事务中一次性晋升 context bundle；
- [x] failed/cancelled/interrupted/partial Tool 内容永不晋升；
- [x] Tool call ID、Tool ID、arguments/result digest、projection reason 和 final answer 被绑定；
- [x] Tool result 始终保持低权限、不可信 data role；
- [x] 定义每 Turn/item/Conversation 的条目与字节上限；
- [x] 定义旧 pair-only Turn 的 additive migration：继续可读，不从可能裁剪的 journal 猜测回填；
- [x] 定义 Conversation 删除、Agent 删除、backup/restore 和 schema rollback 行为；
- [x] 确认 next-turn baseline 不能在 ADR 后继续硬编码为“first input + final output”。

完成标准：存储、renderer、compaction、summary、preview 和 replay 都引用同一份 ADR；没有
第二份历史或从 UI/journal 临时拼接的隐式合同。

### CTX-R01-04 — Count-domain contract

目标：建立类型化、版本化且不可混用的容量计数。

- [x] 定义 `AdmissionUpperBound`：只用于安全/资源硬边界，包含 exact provider schema、模板、
  Tool growth/output reserve 和 estimator basis；
- [x] 定义 `ProviderObservedUsage`：只对 exact request digest 权威，区分 input/output、完整/
  partial、normal/recovery/summary；
- [x] 定义 `SoftContextEstimate`：只用于 autocompact 和用户预测，绑定 profile、renderer、
  ToolSet、history revision、basis、版本和误差策略；
- [x] 定义 `NextTurnProjection`：表示当前 committed state 加最小下一 Turn 前缀，不包含尚未
  提交的用户正文；
- [x] 资格阶段探测模型 tokenizer/provider counting 能力；无精确 tokenizer 时定义经实际
  request 校准的 profile-scoped fallback，不把 UTF-8 byte 冒充 token；
- [x] Tool 估算使用实际 Provider Tool schema，不使用只存在于内部 canonical manifest 的字段；
- [x] 所有阈值比较在类型/API 上拒绝不同 basis/profile/policy；
- [x] Provider actual、估算误差和 cache key 不跨模型、binary digest 或 renderer 复用；
- [x] 对已有 run.started/context projection/provider usage codec 定义兼容 decoder；
- [x] public metadata 暴露 basis/version/availability，不暴露 endpoint、prompt 或 secret。

完成标准：代码审查可从类型和 schema 看出每一个数字是什么；UI 和 Backend 不再把 byte
上界、累计计算量或 Provider actual 与另一计数域阈值直接相减。

### CTX-R01-05 — Completed-Turn continuity invariant

目标：同一 profile、ToolSet、generation 和 prompt sources 不变时，一个成功 Turn 不得把
下一条最小用户消息留到下一轮才拒绝。

- [x] 在当前 Turn 首次 Provider request 前计算 carry-forward/continuation 预算；
- [x] 预算包含最大可提交 final assistant、将晋升的 Tool bundle、最小下一 user 和固定 sections；
- [x] 根据预算限制 `num_predict`、选择 projection 或提前以专用
  `context_continuation_unavailable` 拒绝；
- [x] 若 recent Turn 确实过大，定义可审计的 emergency projection，不能只因历史长度为 2
  让 `/compact` 失效；
- [x] profile/ToolSet/generation/workspace prompt 在 Turn 间变化时使旧保证失效，并返回准确
  原因而非 active-Run 冲突；
- [x] admission reject 发生在模型、Worker、summary 和 privileged Tool 调用之前，且零部分写；
- [x] 对 ASCII/CJK/emoji、0/1/2 history pairs、输出边界前后一个单位做属性/边界测试；
- [x] 保持 canonical transcript 完整，不通过截断 SQLite assistant content 实现 continuation。

完成标准：合法完成的最大边界组合后至少能提交最小 next user；若当前 Turn 无法保证，
必须在它开始前以稳定、可解释、无副作用的错误拒绝。

### CTX-R01-06 — Tool-loop headroom and adaptive projection

目标：为 Tool call/result、assistant-before-tool、第二次 Tool 和 finalization/final answer
预留有界空间。

- [x] 每次 Provider request 计算剩余 model/tool iteration 的最坏/最小可用 headroom；
- [x] `ToolSpec` 提供最小 receipt 与最大 full projection 预算，均进入 ToolSet digest；
- [x] Control Plane 根据当前剩余预算选择 full/excerpt/receipt，不在 Tool 执行后才发现无处回流；
- [x] receipt 至少绑定 call ID、Tool ID、原始 byte count、content digest、truncation reason 和
  可恢复/不可恢复语义；
- [x] 首次 request 只暴露至少能容纳最小 receipt 的 Tool；
- [x] 两次最大合法 Tool result 后仍为 finalization marker 和最小 final answer 留空间；
- [x] 本地 `model_context_limit` 不冒充 Provider overflow，也不能在 side effect 后成为普通结局；
- [x] Tool full result 继续只在现有 canonical event/audit 边界内持久化，不增加 sidecar；
- [x] file/read/search/write/exec/research/skill/subagent result 分别有 projection 回归；
- [x] 取消、deadline、Tool 失败和 receipt validation failure 收敛到唯一终态。

完成标准：任一已暴露并成功 dispatch 的 Tool 都能向模型返回有界结果；不存在“Tool 已执行，
第二次模型请求尚未打开 HTTP 就因预算失败”的路径。

### CTX-R01-07 — Deterministic autocompact policy

目标：soft compression 使用模型兼容计数，hard admission 继续独立 fail closed。

- [x] autocompact trigger/target 使用 `SoftContextEstimate`，不直接使用 byte upper bound；
- [x] `trigger-1 / trigger / trigger+1` 语义和文档一致，明确采用 `>=` 或 `>`；
- [x] hard input、output reserve、Tool headroom 和 request byte cap 分别保留；
- [x] 压缩单位升级为完整 `CompletedTurnContext` bundle，不能切开 Tool identity；
- [x] 优先从最旧端折叠，recent bundle 保护与 continuation invariant 冲突时有明确策略；
- [x] 每层 projection 后重新计算相同 basis 的 soft/hard budget；
- [x] semantic summary 加入后若超过 target/trigger，必须缩小、丢弃或继续合法 projection，
  不能接受 immediate compact loop；
- [x] manual compact intent、auto trigger、hard recovery 的 reason 分开记录；
- [x] SQLite canonical transcript 不变；换用更大 profile 可重新纳入旧 Turn；
- [x] run.started/context inspector 显示 included/omitted Turn bundles、basis 和 projection reason。

完成标准：真实 qwen 在 Provider usage 远低于窗口时不再因 UTF-8 byte 比例过早折叠；压缩后
满足目标策略，且只移除完整旧 Turn bundle。

### CTX-R01-08 — Overflow recovery consistency

目标：把 immutable admission identity 与可单次切换的 active provider identity 分开。

- [x] `RunRecord`/runtime 明确定义 `admission_plan` 与 `active_provider_plan`；
- [x] Worker 仍只提交受信引用，不能选择或伪造 recovery target；
- [x] recovery transition 后所有后续 request event、usage ledger、inspection 和 replay 使用
  active recovery digest；
- [x] 新增 Store 原子操作：expected boundary CAS、recovery transition event、journal state 和
  active-plan metadata 同事务提交；
- [x] 设计 session install 与 durable commit 的顺序，使崩溃只能留下完整旧态或可解释的新态；
- [x] 当前阶段 recovery 明确 deterministic-only，不在 overflow 热路径临时调用 summary；
- [x] 首个零 frame 精确 context/media overflow 最多恢复一次；第二次、partial output、取消、
  deadline、auth/network/format 错误绝不循环；
- [x] recovery usage 分开记录失败 attempt 计算量和最终可作为 next-turn baseline 的成功 request；
- [x] 在 boundary CAS、event commit、session install 和下一 Tool iteration 各点做 fault injection；
- [x] Gateway 重启后的 boundary、transition event、active plan 和 terminal 对账一致。

完成标准：`overflow → recovery → Tool → Tool result → final` 的 exact Provider transcript、全部
durable metadata 和 UI context inspection 指向同一 recovery plan。

### CTX-R01-09 — Completed Turn bundle implementation

目标：实现 `CTX-R01-03` 的存储和 renderer 合同，让后续追问可使用上一轮成功 Tool 事实。

- [x] 使用 additive schema/version 存储完整 completed context bundle；
- [x] bundle 与 Turn completed、assistant final content 和 terminal event 在同一事务晋升；
- [x] renderer 输出原生、顺序正确的 user/assistant-tool/tool/final-assistant provider messages；
- [x] context/history digest 覆盖结构化 role、call ID、Tool ID、arguments/result projection；
- [x] 当前 Run Tool transcript 与历史 bundle 来源分离，禁止重复追加；
- [x] failed/cancelled/interrupted/partial Turn 没有 bundle；
- [x] 旧 pair-only row 继续渲染为 legacy bundle，不猜测不存在的 Tool history；
- [x] journal 已 prune/snapshot-only 时仍能从权威 bundle 重建下一 ContextPlan；
- [x] Conversation/Agent 删除、backup/restore 和 migration fault 后无孤儿行；
- [x] 两 Agent 使用相同 call ID、相同相对路径时仍完全隔离；
- [x] 增加“第一轮 Tool 结果未复述，第二轮仍可回答”的真实多轮回归。

完成标准：跨 Turn Tool memory 可恢复、可压缩、可摘要且保持低权限；不会因为 replay
retention 丢失，也不会扩大 Worker 权限。

### CTX-R01-10 — Semantic-summary-v2 trust protocol

目标：定义唯一、真实绑定的 summary request/renderer/version 合同。

- [x] summary 规则使用独立可信 Provider system message；untrusted transcript JSON 只在
  user/data role；
- [x] 主模型中的 summary 也只作为低权限 synthetic conversation data，禁止拼进 Provider
  system message；
- [x] canonical prompt manifest 直接生成实际 request bytes 和 `prompt_digest`，不手工复制描述；
- [x] policy digest 包含 source/output/item 总预算、timeout、logical/transport retry、circuit、
  model/tool policy 和 renderer 版本；
- [x] summary ToolSet 固定为空，任何 Tool frame fail closed；
- [x] schema 绑定 facts/decisions/open tasks/file state/references 及明确的非权威语义；
- [x] source/parent/profile/prompt/policy/renderer/Tool schema 任一漂移拒绝复用；
- [x] `semantic-summary-v2` 使用新 renderer/section registry；v1 只解码，不升级复用；
- [x] 恶意 source、恶意模型原样输出、Unicode/control/JSON/Markdown 注入做端到端 role 测试；
- [x] 至少两个不同窗口 profile 运行真实事实保持和 prompt-injection 评测，不以手工 snapshot
  编译代替质量测试。

完成标准：Provider system message 中不存在来自 user、Tool result 或 summary 的字节；实际
prompt/policy 变化必然改变 digest，旧 v1 不能获得 v2 authority。

### CTX-R01-11 — Durable summary lifecycle

目标：summary 成为有界、可观测、可复用的派生 projection，而不是 admission 前隐藏调用。

- [x] 增加 conversation-scoped 当前 summary projection，最多一个 current row 或等价常数上限；
- [x] exact source/profile/prompt/policy/renderer 匹配时跨 Turn、跨 Gateway 重启零 HTTP 复用；
- [x] source 扩展时使用 `parent_snapshot_digest + delta complete Turn bundles` 做有界增量更新；
- [x] 无合法 parent 时明确 deterministic fallback，不每 Turn 重试同一确定性 source-limit；
- [x] aggregate budget 在 Provider 调用前覆盖 summary content、source IDs、boundary metadata、
  event 和 DB codec；
- [x] summary 结果使 plan 超 target/trigger 或 boundary 超限时安全丢弃，Turn 仍可 admission；
- [x] summary lifecycle 有 canonical `disabled/generated/reused/source_limit/timeout/invalid/
  circuit_open` 状态和独立 auxiliary usage，不记录正文；
- [x] manual compact 的用户 intent 与 summary outcome 分开保留；
- [x] circuit/negative cache 按 model profile + prompt/policy digest 隔离，条目数和 TTL 有上限；
- [x] admission/summary 并发在 active Run/Provider semaphore 之前有容量保护，可取消、可删除；
- [x] Conversation 删除级联清除 projection；不逐 token/chunk 写入，不执行 VACUUM；
- [x] 采用 expand-then-enable：decoder/schema → v2 generation off → exact reuse pilot → incremental
  pilot → 默认启用；
- [x] 紧急回滚只关闭 v2/reuse/incremental，保留新表和 decoder，不做 destructive migration。

完成标准：相同 source 不重复付费；source 增长不反复发送完整旧原文；summary 失败不阻塞
Turn、不隐藏、不形成 compact loop，并能随时回退 deterministic collapse。

### CTX-R01-12 — Durable next-turn preview

目标：服务端成为“下一条消息还能安全写多少”的唯一语义权威，浏览器只展示。

- [x] 定义 authenticated/no-store preview API 或 Session projection schema；
- [x] 响应绑定 Agent、Conversation revision、model/profile、generation、ToolSet、renderer、
  projection strategy 和 basis/version；
- [x] 返回 availability/stale reason、observed/projected baseline、soft trigger、hard input、
  operational window、output/Tool reserve 和单消息 byte cap；
- [x] pair-only 与 Tool-bundle 两种历史不能共享硬编码公式；根据实际 committed projection 计算；
- [x] failed/cancelled/interrupted Turn 后继续使用最近 completed revision；
- [x] overflow recovery 使用最终 active recovery projection 的首次完整成功 request；
- [x] incomplete provider usage、profile drift 或无法可靠推导时显示明确 unavailable，不猜数；
- [x] 模型、Agent、Conversation、generation、ToolSet 或 workspace prompt 变化立即失效/重算；
- [x] 页面刷新、SSE 重连、snapshot-only replay 和 Gateway 重启后结果一致；
- [x] 上一轮所有 Provider 调用的累计 input/output 作为独立弱化指标，不与 baseline 相加；
- [x] projection cache/持久化只按 Turn 语义边界更新，不按 UI 输入或 token 写盘；
- [x] 响应不含 prompt、Tool result、summary 正文、endpoint 或 secret。

完成标准：前端不再自行从事件拼装 authoritative budget；任一显示数字都有服务端
basis/version，并能解释“固定上下文”“本条 byte 限制”“压缩前余量”“硬余量”和“上一轮
计算量”的区别。

### CTX-R01-13 — Conversation capacity and continuation

目标：把 128 Turn 物理容量变成明确合同，并提供不删除审计历史的继续路径。

- [x] 增加专用 `ConversationTurnCapacityError`/stable API code，不复用 active Run/CAS conflict；
- [x] snapshot/preflight 提前检查，begin-turn 事务内再次检查防竞态；
- [x] capacity reject 在 ContextCompiler、summary、Broker、Worker 和 journal side effect 前发生；
- [x] Session metadata 返回 `turn_count/turn_limit/turns_remaining/submission_blocker`；
- [x] failed/cancelled/interrupted 仍计入物理存储容量，不能通过失败洪泛绕过上限；
- [x] 提供显式“继续为新 Conversation”流程；携带上下文时使用经审计的 bounded continuation
  projection，绝不静默复制/删除完整旧历史；
- [x] 两连接并发争抢最后一个位置时只成功一个；
- [x] 旧 Conversation 保持可读、可删除、可 backup/restore；
- [x] UI 区分 active Run、revision drift、capacity exhausted 和 store unavailable；
- [x] 不通过自动 VACUUM、整库复制或隐藏删除回收容量。

完成标准：第 129 个 Turn 有稳定、准确、零 Provider 调用的响应；用户可显式新建 continuation，
原会话及审计证据保持不变。

### CTX-R01-14 — Web context and composer UX

目标：让用户看到准确、简洁、可操作的上下文状态。

- [x] 使用 `TextEncoder` 实时显示本条消息 `bytes / 8192 bytes`；
- [x] 8192 bytes 可发送，8193 bytes 本地阻止且保留草稿；服务端继续最终校验；
- [x] 单消息 byte cap 与模型 token/window 余量分开展示；
- [x] context budget 完全使用 `CTX-R01-12`，不在 JS 中直接相减不同计数域；
- [x] completed 后刷新 preview；failed/cancelled 后回退最近 completed；recovery 后使用 active plan；
- [x] 模型切换立即清除旧 model/window projection 并请求新 preview；
- [x] 自动/手动 compact 显示 strategy、omitted Turn count、summary status 和非权威说明；
- [x] capacity exhaustion 提供“继续为新会话”，不再误报 active Run；
- [x] 上一轮累计 Provider 用量继续弱化显示，并解释 Tool 循环会重复计算共享上下文；
- [x] 键盘 Enter、发送按钮、IME composition、超限、网络/API 错误均不丢草稿；
- [x] 增加可执行浏览器行为回归；源码字符串断言不能作为唯一证据；
- [x] 保持多轮自动滚动、历史 Run 检查和事件时间线交互不回归。

完成标准：用户可以区分“这条消息能不能提交”“何时可能自动压缩”“硬窗口还剩多少”
和“上一轮花了多少计算量”；失败或切换模型不会留下过期数字。

### CTX-R01-15 — Qualification, documentation and release

目标：只有完整矩阵、真实模型和 lifecycle 证据通过后，才重新关闭 context/release Gate。

- [x] 运行相关最小测试、完整 `pytest`、`./governance.sh`、`git diff --check`；
- [x] deterministic profiles 覆盖 8K no-tools、16K、当前 32K tools 和两个 model IDs；
- [x] 真实 `qwen3.5:2b` 覆盖普通多轮、trigger 前/触发轮/压缩后、Tool follow-up、manual compact、
  summary v2、overflow recovery（可控 provider fault）和模型切换；
- [x] 10–20 Turn completed/failed/cancelled/restart 混合回归，另做有界 deterministic 长会话 soak；
- [x] 两个真实 RunService/Capsule 共享 Broker 的跨 Agent 隔离回归；
- [x] summary accuracy/reference/open-task/file-state 与恶意注入评测达到写明的门槛；
- [x] SIGKILL/SQL fault/cancel/delete 在 admission、summary、recovery、bundle commit 和 preview
  各阶段只恢复完整旧态或完整新态；
- [x] cold start、normal/force stop、Gateway restart、Conversation/Agent delete 后无 Worker、Run root、
  summary projection、临时文件或进程残留；
- [x] 记录 WAL/log/temp/cache/process I/O/fsync 增量；无逐 token/delta/frame 写，无重复 summary
  写放大或日志洪泛；
- [x] migration 从现有 pair-only/v1 boundary 数据升级和兼容回滚通过；backup/restore 可验证；
- [x] 同步 CLAUDE、README、SECURITY、architecture、event protocol、release contract、
  runtime-rebuild parent 状态和本计划；
- [x] 生成稳定 `RR-CTXREL-YYYYMMDD-NN`，记录 implementation SHA、命令、平台、模型、结果和
  residual audit；
- [x] 只有上述全部完成后，把本项和 parent `CTX-R01` 标为 done，并重新关闭 GATE-05/GATE-07。

完成标准：当前受支持 release scope 中不存在本计划记录的 P0/P1 缺口；未验证平台、TLS、
多用户、HA、长期 SMART/物理磨损声明没有扩大。

## Mandatory test matrix

| 维度 | 必测值 |
| --- | --- |
| 模型窗口 | 8K no-tools、16K、32K tools；同 Conversation 在两个 profile 间切换 |
| 文本编码 | ASCII、中文、emoji、组合字符、引号/反斜杠/换行、边界前后 1 byte |
| 历史 | 0/1/2 pairs，Tool bundle，trigger−1/trigger/trigger+1，target 不可达，128 Turn |
| Turn 状态 | completed、failed、cancelled、interrupted、running-at-restart；仅 completed 晋升 |
| Tool | 无 Tool、小结果、最大 full result、动态 receipt、两次 Tool、带 assistant preamble |
| Compression | full、manual、auto、deterministic fallback、summary reuse/incremental/failure |
| Recovery | overflow→final、overflow→Tool→final、第二次 overflow、partial output、cancel/deadline |
| Usage | 单调用、多 Tool 累计、summary auxiliary、snapshot-only、incomplete + recovery success |
| UI | reload、SSE reconnect、failed/cancelled/recovery、model switch、8192/8193 bytes、capacity |
| Persistence | live、Gateway restart、SIGKILL/SQL fault、backup/restore、delete cascade、old codec |
| Isolation | 两 Agent、相同 call/path/model ID、共享 Broker、并发最后容量和并发 summary |
| Resource | WAL/log/temp/cache 峰值、Provider semaphore、memory/event/DB caps、无逐 token 写 |

任何路径、schema、archive、URL、subprocess、auth 或 capability 变更仍需执行其原有负面安全
矩阵；本表不会替代 [Runtime rebuild plan](runtime-rebuild.md#强制负面测试矩阵)。

## Rollout and migration

采用 expand-then-enable，不在一次变更中同时改变 writer、decoder、summary 和 UI：

1. **Containment release**

   - 固化 regression；默认关闭 summary v1；纠正文档和 UI 状态；
   - 不改 canonical transcript，不做 DB destructive migration。

2. **Compatibility release**

   - 先发布新 count-domain codec、Turn bundle/summary projection additive schema 和 decoder；
   - writer 仍使用旧 pair/deterministic projection；备份/恢复和旧数据读取先通过。

3. **Deterministic reliability release**

   - 启用 continuation invariant、Tool headroom、compaction policy 和 recovery active plan；
   - summary 继续关闭；完成真实多轮、Tool、restart 和 SSD 基线。

4. **Context bundle and preview release**

   - 新 completed Turn 使用 bundle writer；旧 Turn 继续 legacy read；
   - 启用服务端 preview 和新 Web；验证模型切换和 failed/recovery 语义。

5. **Semantic summary v2 pilot**

   - 先启用 v2 generation，随后分别开启 exact reuse 和 incremental update；
   - 每阶段由独立 operator gate 控制并有真实模型/注入/资源证据；
   - v1 永不重新送入模型。

6. **Qualified default**

   - 完成 `CTX-R01-15` 后才把 summary v2 和新 context contract 纳入默认 release；
   - 更新 parent/gates 和所有权威文档。

### Rollback rules

- 紧急回滚首先关闭 summary v2/reuse/incremental，立即回到 deterministic collapse；
- 只能回滚到认识新 schema/version 的兼容二进制，不能让旧 binary 误读新 boundary；
- 新增表/列保留但停止写入，不做 destructive down migration，不重写 `state.sqlite`；
- active Turn/summary/recovery migration 失败时启动 fail closed，不删除 canonical transcript；
- Tool bundle writer 回滚后，新 decoder 仍可读取已提交 bundle；旧 pair-only rows 保持不变；
- UI 回滚不能重新自行推导 authoritative context budget；preview unavailable 时明确降级；
- 每次 migration 前使用既有 backup 流程，并验证 restore 到 checkout 内路径；
- 回滚后重新执行 health、真实最小 Run、restart、stop 和 residual audit。

## Phase gates

| Gate | 当前状态 | 退出条件 |
| --- | --- | --- |
| CTX-R-GATE-01 containment | done | 01–02 done；summary v1 默认关闭，文档与行为一致 |
| CTX-R-GATE-02 deterministic reliability | done | 03–08 done；continuation、Tool headroom、compaction、recovery 通过 |
| CTX-R-GATE-03 durable multi-turn | done | 09、12–14 done；Tool bundle、preview、capacity 和 Web 通过 |
| CTX-R-GATE-04 semantic v2 | done | 10–11 done；角色、复用、增量、预算、accounting 和回滚通过 |
| CTX-R-GATE-05 release | done | 01–15 done；RR 证据完成，parent/GATE-05/GATE-07 重新关闭 |

## Global definition of done

每个工作项标记 done 前还必须满足：

- 实现、失败路径、迁移、兼容 decoder 和回滚行为已提交；
- 相关 unit/integration/browser/fault tests 通过，负面安全用例没有减少；
- 当前受支持 profile 的真实模型纵向验证与预期相符；
- 没有新的无界内存、事件、SQLite/WAL、cache、temp、日志或 Provider 并发增长；
- 没有 secret、prompt、Tool result 或 summary 正文进入公开 metadata/日志/trace；
- 没有 checkout 外写入、全局依赖安装、Worker 权限扩大或跨 Agent 影响；
- 权威文档与最终行为同改，`./governance.sh` 和本地链接检查通过；
- `git diff --check` 和完整测试通过，工作树中不提交 runtime state；
- Execution evidence 记录稳定 ID、完整 commit SHA、命令结果和 residual audit。

## Execution evidence

使用以下模板追加证据：

```text
RR-CTXREL-YYYYMMDD-NN
implementation_ref: <full commit SHA or reviewed worktree>
work_items: <CTX-R01-NN...>
platform: <arch/kernel/sandbox/model qualification>
commands: <reproducible commands>
result: <pass/fail and bounded metrics>
migration_rollback: <tested paths and result>
residual_audit: <none or explicit findings>
```

初始审计记录：

```text
AUDIT-CTX-20260721-01
implementation_ref: 3c52fd8ed190e1a669888313f63261893159c1d0
work_items: CTX-R01 planning baseline
platform: x86_64; qwen3.5:2b 32768 operational context; landlock+seccomp
commands: source ./env.sh && ./.venv/bin/python -m pytest
result: 581 passed; confirmed F-01 through F-13; no implementation changes
migration_rollback: not applicable
residual_audit: working tree clean; GATE-05/GATE-07 must be reopened
```

最终实现与资格记录：

```text
RR-CTXREL-20260721-01
implementation_ref: reviewed worktree atop 3c52fd8ed190e1a669888313f63261893159c1d0
work_items: CTX-R01-01..15
platform: x86_64; Linux 6.17.0-29-generic; Landlock+seccomp; qwen3.5:2b;
          operational profiles 8192/16384/32768
commands: source ./env.sh && node --check src/agent_builder_v2/static/app.js &&
          ./.venv/bin/python -m pytest && ./governance.sh && git diff --check;
          ./stop.sh --force && ./start.sh && curl http://127.0.0.1:20815/health
result: pass; 638 tests including real Chromium behavior, context boundaries, multi-Turn,
        Tool bundle/headroom, recovery active-plan, summary reuse/incremental, preview,
        capacity/continuation, migration/backup and fault injection
migration_rollback: additive bundle/summary/continuation tables and v1/previous-renderer decoders pass;
                    v2 gate=0 returns deterministic-only without data rewrite
residual_audit: default Gateway healthy on 0.0.0.0:20815; no application state outside checkout;
                no Worker/Run/Conversation test residual;
                external distribution still requires a clean reviewed commit per release contract
```

真实模型与资源证据：

```text
RR-QUA-20260721-05
implementation_ref: same reviewed worktree
work_items: CTX-R01-09..15
platform: qwen3.5:2b; Landlock+seccomp
commands: bounded real-provider qualification workload
result: pass; 8 completed + 1 cancelled + 1 rejected; 2 Conversations created/deleted;
        10.552s; gateway write_bytes +2695168; state allocated +1835008;
        WAL peak 4120032 logical; logs +78 bytes; temp peak 781 bytes; cache +0
migration_rollback: normal Gateway restart and delete cascade passed
residual_audit: zero test Conversation, Run-root, Worker, temp and process residual

RR-QUA-20260721-06
implementation_ref: same reviewed worktree
work_items: CTX-R01-15 SSD/lifecycle qualification
platform: qwen3.5:2b; instrumented lifecycle sync counter
commands: ./start.sh --qualification-sync-count; four real Turns; stop/restart audit
result: pass; 12 successful fsync; 0 fdatasync/msync/sync/syncfs; 0 sync failures;
        gateway write_bytes +1380352; state/WAL +1355512; temp peak 781; cache +0; logs +78
migration_rollback: normal and instrumented restart passed
residual_audit: zero test Conversation, Run-root, Worker, temp and process residual
```

补充真实 summary 资格使用同一 `iollama:11434/qwen3.5:2b` 分别在 8K、16K profile 运行，
每次 `1100 input + 207 output` tokens；均保留 `CTX-REAL-427`、SQLite WAL、migration task、
`report.md` 和 `RFC-8259`，且恶意 `PWNED` 指令未进入结果。Gateway 三 Turn 资格生成并跨 Turn
复用 `semantic-summary-v2`，最终正确回忆 `CTX-GATEWAY-731`。这些评测不把摘要提升为事实源；
canonical transcript 仍是唯一权威记录。

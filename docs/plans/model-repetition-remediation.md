---
owner: runtime-maintainers
status: active
last_reviewed: 2026-07-24
review_cycle: quarterly
---

# Model repetition containment and continuation remediation

## Objective

本计划修复 Conversation `e018b18b9a634e338e500d089589295d` 暴露的两个连续问题：

1. 普通回答在小模型采样下进入精确重复循环，直到耗尽 4096 output tokens；
2. 重复正文被作为 completed history 提升后，下一轮再次长输出，并因 Provider 原始流超过
   512 KiB 而以 `model_protocol_error` 失败。

目标是在不伪造 Provider usage、不扩大 Worker 权限、不修改旧 canonical transcript 的前提
下，让新的重复循环立即止损、以有界正文完成，并保证下一轮从明确标记的 completed bundle
继续。该工作在 [Runtime rebuild plan](runtime-rebuild.md) 中登记为 `REP-R03`。

## Reproduction evidence

2026-07-24 对目标 Conversation 的 SQLite、durable events 和 exact Provider request 完成了
只读复现：

- 第 4 Turn 的 Provider 请求正常流式返回；持久正文与 raw stream 的内容前缀逐字节一致，
  因而不是客户端把 delta 当 full snapshot 重复消费；
- 第 4 Turn 输出达到 4096 tokens，正文中相同笑话结尾连续出现百余次，最终按现行
  `max_output` 合同完成并进入 `CompletedTurnContext`；
- 第 5 Turn 的 exact request 为 15397 bytes；Broker 复现约 100 秒后因原始 NDJSON 超过
  `MAX_STREAM_BYTES=512 KiB` 失败，Conversation 本身没有遗留 active Run；
- 相同 `ok` 请求移除第 4 Turn history 后在 16 tokens 自然结束，证明第 4 Turn 的重复尾部
  是第 5 Turn 长生成的重要条件；
- 当前 response phase 固定 `temperature=0.7/top_p=0.8`。对 exact 第 4 Turn prompt 使用当前
  qwen 模型已资格的 `temperature=1/top_p=0.95/top_k=20/presence_penalty=1.5` 时自然结束，
  而提高 repetition penalty 在当前 Ollama/Qwen runner 上没有改变坏输出。

这些数字是本次 finding 的证据，不是对任意模型或任意文本的通用性能承诺。

## Scope and non-goals

范围包括：

- trusted response-phase generation policy；
- tool-free ordinary assistant stream 的精确重复检测与主动 HTTP 取消；
- normalized stop、provider usage boundary、Run terminal、replay 和 Web 展示；
- Provider 原始流总字节上限与当前 4096-token profile 的一致性；
- deterministic fake-provider 回归、真实 qwen 取消资格，以及目标五轮输入的三次纵向复验。

不包括：

- 不让浏览器、Worker、Agent prompt 或请求数据配置 generation options/阈值；
- 不把语言模型语义相似、主题重复或一般低质量回答误称为可确定检测的问题；
- 不在有 Provider Tool schema 的选择阶段把重复文本伪装成成功 Tool 执行；
- 不静默重写既有 Conversation 的 user/assistant canonical transcript；目标 Conversation
  只作为只读复现来源，三次资格使用新的隔离 Conversation；
- 不把中止请求的本地 byte/字符计数伪装成 Provider token usage；
- 不增加后台 drain、额外模型调用、逐 token 持久化或第二套 Agent loop。

## Non-negotiable invariants

- **Immediate resource stop**：重复 guard 确认后关闭当前 `/api/chat` response，不继续排空到
  Provider terminal frame；不重试，不继续占用 Broker slot。
- **Completed semantic outcome**：存在可用普通正文时，以
  `run.completed.reason=repetition_truncated` 完成；该 Turn 的正文和 marker 原子进入唯一
  `CompletedTurnContext`。
- **No usage fabrication**：对应 `model.response.finished` 使用
  `outcome=repetition_truncated`、`usage_complete=false`、零 request-bound token fields 和
  `error_code=null`；provider usage row 标为 incomplete，不进入 soft calibration。
- **Count-domain separation**：下一轮 `NextTurnProjection` 从新 committed revision 重算；
  hard admission 继续基于 exact rendered request 的 `AdmissionUpperBound`，绝不从中止请求
  的 usage 相减。
- **Canonical/live agreement**：已经发给 Worker/UI 的 content delta 不回写或隐藏；guard 只
  删除尚未发出的尾部并追加 marker。最终 `assistant.block.finished`、ConversationTurn 正文和
  completed bundle 必须逐字节相同。
- **Explicit cancellation precedence**：若 operator cancellation 已先被观察到，则仍收敛为
  `run.cancelled`，partial output 不进入历史；不得把用户取消改写为 repetition completion。
- **Bounded detector**：检测只使用当前已受 12 KiB commit ceiling 约束的内存正文，不保存
  Provider frame，不写逐 token journal，不做无界 regex/backtracking。

## Selected B+ design

### 1. Trusted sampling policy

将普通无 Tool response phase 升级为新的 generation policy version，并显式固定当前 qwen
资格已经验证的 sampling options：

```text
temperature=1.0
top_p=0.95
top_k=20
presence_penalty=1.5
seed=0
```

Tool selection phase 继续使用 `temperature=0/seed=0`。policy version 和完整 options 继续
进入 `generation_options_digest`；Web/Worker 不能覆盖。repetition guard 是安全兜底，测试和
正确性不能只依赖当前 runner 是否实现 penalty。

### 2. Exact suffix detector

新增 provider-normalization 层拥有的纯函数 detector。它只检查普通、无 Tool schema 的
assistant 正文，并使用以下固定上限：

- 只匹配连续、逐 Unicode code point 完全相同的 suffix cycle；
- candidate unit 的 UTF-8 大小在 32..512 bytes；
- 至少连续 3 份，并且整个重复证据不少于 512 bytes；
- 每累计 64 个新的 accepted UTF-8 bytes 才检查一次，并只扫描最多 2048 个尾部 Unicode
  code points；terminal 前不足 64 bytes 的余量不单独触发，因为 Provider 已自然结束；
- 从最短合法 unit 开始匹配，并向前扩展到当前内存正文中最早的连续相同 unit；
- 只有命中后才在当前 12 KiB bounded content 中向前扩展 cutoff；常规检查的次数、尾窗和
  candidate unit 都有独立固定上限，不使用语义模型、模糊匹配或外部状态。

短词/单字符死循环可以组成达到 32-byte 最小 unit 的周期，仍会在累计 512 bytes 后被捕获；
正常 prose 中偶然出现两次相同句子不会触发。显式要求大段文本重复的请求仍可能触发，这是
本地安全 guard 的已知取舍，因此阈值要求至少三份和 512 bytes 证据。

### 3. Streaming and truncation

Broker 在把新 content 加入 coalesced output 后、发送下一批 content IPC frame 前执行检测：

```text
Provider frame
  -> UTF-8 / size / Tool validation
  -> append to bounded pending content
  -> exact suffix detector
       normal: follow existing coalescing path
       repeated:
         preserve every already-emitted character
         discard pending duplicate suffix where possible
         append one trusted marker
         emit remaining bounded content
         close the HTTP response immediately
```

固定 marker 为：

```text
[回答因重复死循环被截断；后续忽略重复尾部。]
```

marker（含前导换行）固定为 67 UTF-8 bytes，与现有 output-limit marker 的预留相同；它最多
出现一次并计入 12 KiB assistant commit ceiling。检测阈值内、已经实时发出的少量
重复证据不会被 retroactive rewrite；其最坏大小受 3 × 512-byte cycle evidence、64-byte
check interval 与现有 coalescing 边界约束，不会继续增长到 12 KiB。这样无需升级 Worker
IPC v2，也不会让 ephemeral delta、durable block finish 和 SQLite canonical assistant
出现不同正文。

若 Provider terminal frame 已先到达，则沿现行 `end_turn/max_output` 路径结算完整 usage；
guard 不把已经自然结束的调用重新分类。若有 Tool schema、已出现 Tool call、没有可见正文，
或显式 cancellation 已生效，则不得产生 repetition-completed Turn。

### 4. Provider cancellation and usage

检测完成后正常关闭 `OllamaSession` 的 raw async iterator；`httpx` response context exit 负责
断开当前 stream，`_stream_response` 的 `finally` 立即释放全局 model slot。这个本地终止：

- 不进入 zero-first-frame health 计数；
- 不开启 circuit breaker；
- 不标记 retryable，不触发透明 retry；
- 不等待 Ollama terminal frame，因此拿不到可信 `prompt_eval_count/eval_count`。

normalized stop 扩展为 `repetition_truncated` 且不携带 usage。Control Plane 产生一个完整的
durable logical request/response pair，但 response boundary 明确为 incomplete usage。已有
terminal rollup 只累计 complete observations，并把 `complete=false`；该调用不会更新
`SoftContextEstimate` calibration。

### 5. State, replay and UI contract

允许以下新 canonical 值：

- `model.response.finished.outcome = repetition_truncated`；
- `run.completed.reason = repetition_truncated`；
- Worker normalized stop reason `repetition_truncated`。

`model.response.finished` 此 outcome 固定要求：

```json
{
  "input_tokens": 0,
  "output_tokens": 0,
  "usage_complete": false,
  "error_code": null
}
```

Sessions、replay projector、snapshot validator、Control Plane sequencing 和前端枚举同步接受
该组合；其它 successful/failed combinations 继续 fail closed。Run terminal 的 usage rollup
保持既有 schema，并明确 `complete=false`。Web 把该 terminal 显示为“检测到重复循环，回答
已截断并提交”，而不是错误、取消或完整 Provider usage。

Control Plane 只在收到该 stop 后设置 `final_assistant_content`；Worker 仍以现有 content delta
和 `assistant.block.finished` 关闭 block。终态事务照常同时提交 `run.completed`、Turn 正文和
`CompletedTurnContext`，所以失败/重启不会留下半个 completed bundle。

### 6. Next-turn context accuracy

B+ 不改变下一轮 context 的权威来源：

1. repetition Turn 原子提交有效前缀、有限检测证据和固定 marker；
2. Conversation revision 递增；
3. 下一次 preparation 从该 revision 的 `CompletedTurnContext` 重新渲染；
4. preview/admission 对 exact rendered content 重新计算；
5. incomplete Provider observation 被排除，既不污染 calibration，也不冒充下一轮占用。

因此损失的只是被中止调用的实际 Provider token 统计，不是 committed context 内容、硬预算
或 continuation identity。现有不复用 Provider model session/KV state 的边界保持不变。

### 7. Raw stream budget

将 `MAX_STREAM_BYTES` 从 512 KiB 提高到 1 MiB，同时保留：

- 4096 raw frame ceiling；
- 64 KiB 单 NDJSON line ceiling；
- 12 KiB normalized assistant ceiling；
- 128 content IPC frame ceiling；
- 现有 first-frame/idle/turn timeout。

1 MiB 覆盖本次实测的约 128 raw bytes/output token 与 4096-token profile，并仍是攻击者不可
扩大的固定总量。超过任一 raw/normalized limit 的非重复流继续以 `model_protocol_error`
fail closed；该变更不把 Provider 原始帧持久化。

## Failure and race behavior

- detection 与 user cancellation 同轮发生时，先观察到的 explicit cancellation 优先；
- detection 后 HTTP close 或 Worker/IPC 随后失败时，只有 canonical terminal 成功提交才算
  completed；持久化失败继续走既有 journal failure 收敛，不伪造 completed；
- guard 只在 tool-free response phase 生效；Tool phase 重复继续按现有模型失败合同处理，
  不能跳过需要审批/执行的能力；
- final marker 不能单独构成“可用正文”；没有非空前缀时保持 `model_empty_response`/failure；
- process restart 不续跑已关闭 stream；若 completed terminal 已提交，bundle 可恢复，否则
  Turn 按现行规则收敛为 interrupted；
- 已有历史数据无需 schema migration；decoder 只扩展受限枚举。回滚 writer 时必须保留新
  outcome/reason decoder，避免已提交 journal 在旧 writer rollback 后不可回放。

## Test and qualification design

### Deterministic automated tests

先写失败测试，再实现：

- suffix detector：阈值前不触发、边界触发、中文/emoji/UTF-8、最短/最长 unit、非 suffix、
  两次合法重复、12 KiB 上界和 marker 单例；
- Ollama fake transport：检测后 response iterator 被关闭、后续 frame 未消费、slot 释放、
  无 retry/circuit failure、incomplete usage stop 正确；
- sampling manifest：response phase 新 options/digest，Tool phase 不漂移，请求不能覆盖；
- Control/Worker：新 stop completed、无 Tool/无正文拒绝、cancel race、final assistant 一致；
- Sessions/replay/snapshot：新 outcome 的唯一合法 usage 组合、corrupt variants fail closed、
  completed bundle 跨重启恢复；
- preview/calibration：incomplete observation 被排除，下一轮 projection 绑定新 revision，hard
  admission 与 exact committed marker 一致；
- Web：terminal label、usage incomplete、后台完成和 canonical refresh 不重复正文；
- raw budget：介于 512 KiB 和 1 MiB 的合法流成功，超过 1 MiB 失败。

### Target Conversation three-pass qualification

从目标 Conversation 读取并冻结以下五条用户输入作为 harmless regression fixture：

```text
hello
who are you
932748+29183/11=?
write a joke for around 300 words
ok
```

在真实固定 `iollama:11434/qwen3.5:2b` 上创建 3 个独立 Conversation，分别按原顺序提交
全部五轮。每一 pass 必须满足：

- 5 个 Turn 全部 completed，零 failed/cancelled/interrupted；
- 无 `model_protocol_error`，每个 Run 的 request/response boundary 完整配对；
- 第 3 Turn 给出 `935401`；
- 第 4 Turn 要么自然结束，要么恰好一次 `repetition_truncated` marker/reason，正文不超过
  12 KiB 且 detector 不再识别出未处理的连续循环尾部；
- 第 5 Turn 非空完成，且其 ContextPlan 包含第 4 Turn 的 committed revision；
- 第 4 Turn 若 guard 触发，对应 provider usage 明确 incomplete，但第 5 Turn preview 和 hard
  admission 可用且不使用该 observation 校准；
- 三轮结束后 active Run、Worker、Run temp root 和未关闭 stream residual 均为零。

由于 sampling 修正可能使三次第 4 Turn 都自然结束，还要增加一次受信 test-process
qualification：仅在测试进程中复用旧 response options 诱发已知循环，验证真实 Ollama stream
在 guard 后快速关闭、下一请求不被旧 Broker slot 阻塞。该 hook 不进入 Web/API/Worker 或
production catalog。

最终门禁：最小相关测试、完整 `pytest`、`./governance.sh`、cold start、`/health`、上述真实
模型资格、正常/强制 stop 和 residual audit 全部通过。证据只写入 `.runtime/test-results/`，
不保存 token、完整 Provider request、用户秘密或原始 frame。

## Work-item ledger

状态仅允许 `not_started`、`in_progress`、`blocked`、`done`。

| 顺序 | ID | 目标 | 状态 |
| ---: | --- | --- | --- |
| 1 | REP-R03-01 | 固化 exact-stream、history isolation 和 raw-budget red tests | in_progress |
| 2 | REP-R03-02 | 实现 bounded exact suffix detector 与 marker contract | not_started |
| 3 | REP-R03-03 | 实现主动 HTTP close、slot/health/retry 语义 | not_started |
| 4 | REP-R03-04 | 扩展 normalized stop、usage boundary、terminal 与 replay | not_started |
| 5 | REP-R03-05 | 更新 sampling policy 和 raw stream budget | not_started |
| 6 | REP-R03-06 | 完成 Web、preview/calibration 和 restart/corruption matrix | not_started |
| 7 | REP-R03-07 | 三次五轮真实模型资格与强制 guard 取消验证 | not_started |
| 8 | REP-R03-08 | 完整门禁、文档、回滚和 release Gate 收口 | not_started |

`GATE-05 context` 与 `GATE-07 release` 在 `REP-R03-01..08` 全部完成前保持 reopened。完成后
删除本活动计划，由 Git 历史保留设计和证据；最终行为进入 architecture/event/release 等
权威文档，稳定 RR 摘要进入 Runtime rebuild plan。

逐文件、逐测试和逐提交的执行顺序见
[implementation plan](2026-07-24-model-repetition-implementation.md)。

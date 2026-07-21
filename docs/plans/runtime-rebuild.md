---
owner: runtime-maintainers
status: active
last_reviewed: 2026-07-21
review_cycle: quarterly
---

# Runtime rebuild plan

## Objective

在不继承旧系统依赖和隐式状态的前提下，把 Claude Code 风格运行骨架发展为可维护、
可恢复、可扩展的本地智能体运行时。每个阶段必须保持 P1-P8、明确的
单一 Agent loop、规范事件流、checkout containment 和 fail-closed sandbox。

本计划描述方向与验收，不承诺日期。逐项完成状态只使用下方 Execution ledger 定义的
状态机；上面的 Implemented baseline 只是带日期的现状说明，不是第二份状态账本。

## Implemented baseline

- 根级 bootstrap/start/stop/purge/governance 生命周期与 checkout-local 环境边界；
- `0.0.0.0:20815` 认证 Web UI、CSRF、Conversation create/list/read/delete、同会话
  多轮 Run/cancel、SSE 和完整事件详情；
- 有界 `QueryEngineRegistry` 与每 Conversation 一个逻辑 `QueryEngine`；Engine 固定
  Agent/Conversation 身份，提供 restore/submit/interrupt/delete，返回不可变 Run handle，
  删除后 retire/驱逐，重启后从 SQLite 懒重建且不复制 durable 或 per-Run 状态；
- 固定 demo Agent Capsule、Agent 专属 `worker-env`、每 Run 独立目录和进程组；
- 单一 `HarnessKernel` context → model → tool → model loop，无 LangGraph/LangChain；
- operator-owned ModelCatalog 与受信 Ollama Broker；默认 entry 为
  `iollama:11434/qwen3.5:2b`，启动时从 `/api/show` 资格检查 binary、能力和原生上下文窗口，
  并应用 entry 的 operational cap/output reserve；Web 只能选择目录内 ID，endpoint 不公开；
- Agent-scoped `state.sqlite` 中的 Conversation/Turn repository；一个 Conversation 同时
  最多一个 active Run，Turn 接受/终态与 boundary event 事务提交，Gateway 重启将遗留
  `running` Turn 收敛为 `interrupted`；
- Control Plane 从仅含 completed user/assistant pairs 的 durable history 编译不可变、
  带 provenance/digest/budget 的 ContextPlan；完整 plan 不进入 Worker，Model Broker
  IPC v2 只传摘要引用；
- 共享不可变 `ToolSpec` 同时生成 provider schema、Worker/Control Plane 校验和
  toolset digest；
- 当前模型画像为 native `262144`、operational `32768`、output reserve `2048`、硬输入
  `30720`，并已生成 80% trigger / 60% target 的动态压缩策略；
- admission estimator 是 `utf8-bytes-upper-bound-v1`，没有模型 tokenizer 时每个 UTF-8
  byte 按一个 token 做保守上界，并计入实际 renderer、Tool manifest 与 `256` tokens
  provider template reserve；初始 plan 和每个后续完整 transcript 都执行相同 admission；
  超过动态 80% trigger 时从最旧端把完整 completed Turn pair 投影为 content-free collapse
  receipt，至少保留最近一个完整 pair，直到不高于 60% target；所有可折叠旧 pair 耗尽后
  仍超硬预算则 fail closed；
- 所有 terminal 都附带 `{input_tokens,output_tokens,last_input_tokens,complete}`；provider
  报告 usage，Control Plane 按模型 profile 校验并累计，Worker 不可伪造；
- Model Broker 最多同时执行 2 路 provider stream，其它 active Run 在总容量内有界排队，
  30 秒无 slot 时以可重试 `model_busy` 失败；
- 每轮最多 4096 个 provider 原始帧，合并为最多 128 个 content IPC frame；Broker error
  code/retryability 由 Control Plane 绑定 canonical terminal；
- Worker 无网络，Landlock + seccomp + rlimits + attestation，无 unconfined fallback；
- runtime ToolSet 包含有界 Echo、descriptor-anchored stat/read/glob/grep，以及要求
  same-Run receipt 和 operator diff 审批的原子 edit/write；每 Run 最多两次顺序 Tool 调用，
  预算耗尽后 provider ToolSet 收窄为空；
- canonical event sequencing、durable semantic SQLite/WAL、ephemeral delta、取消和唯一终态；
- 新 Run 以 feature marker 声明每次真实 provider 调用严格配对的
  `model.request.started`/`model.response.finished`；runtime admission、usage ledger 与边界
  原子对账，不保存 prompt 或逐 token/provider frame；
- Web 右侧按 durable Turn 展示多轮 user/assistant transcript，左侧把所选 Run 按
  User/Harness/LLM/Tool 四泳道与 canonical `seq` 回放；Turn/Run/node 双向定位，支持过滤、
  步进/播放和常驻节点检查。推导的 User → Harness 提交节点不冒充 EventEnvelope；详情
  分开显示逻辑消息、payload 和完整 canonical envelope。按需 context inspector 只返回
  `no-store`、正文隐藏的 ContextPlan/section 元数据或经验证历史摘要；
- bounded Run/event/log/journal/tree 资源与安全的 PID/process-group 清理；
- 旧系统隔离到 `_legacy-reference/`，当前 runtime 不依赖它；
- P1-P8 权威文档和自动文档/边界治理。

“implemented baseline”只表示固定路径已跑通；受支持范围必须以
[release contract](../design/release.md)为准，不能扩大成互联网、多租户或未资格平台承诺。

## Current validation evidence

2026-07-18 在当前 `x86_64` checkout 完成了受控环境全量测试、文档治理、diff/脚本语法
门禁和真实 lifecycle 复验：Gateway 在 `0.0.0.0:20815` 以真实
`iollama:11434/qwen3.5:2b`、Landlock + seccomp 资格启动；同一 Conversation 的前两轮
用依赖历史的暗号问答验证 completed history，重启 Gateway 后第三轮仍从恢复的 transcript
得到相同答案。另一个已完成 Run 的会话删除后，session detail 与 event endpoint 都返回
404；运行结束无 Worker PID 文件或 Run 根残留，Gateway 保持健康。
同日引入逻辑 QueryEngine 后再次完成受管 stop/start、全量 `200 passed` 和真实两轮 API
验收：同一 Engine/Conversation 产生两个独立 Run，第二个 `run.started` 报告纳入前一轮
2 条 completed history messages，两轮均以 `run.completed` 收敛；删除后 Conversation 和
两个 event endpoint 均为 404。自动集成测试还覆盖 Registry/Service 关闭后从同一 SQLite
懒创建新 Engine，并把重启前 completed pair 投影进下一轮。

以上 2026-07-18 证据不是 cold-checkout、多架构或 production qualification：当时依赖
缓存已存在，也没有长期 load/soak、SSD SMART 对照、故障注入矩阵或 TLS/多用户边界。

2026-07-19 用最终候选源码新建
`.runtime/qualification/cold/RR-COLD-20260719-02/checkout`；复制前确认其中不存在
`.tools/`、`.venv/`、`.runtime/` 或 `data/`，随后从网络下载固定 `uv 0.11.7`、受管
CPython 3.11.15 和 frozen 依赖，所有产物均落在该 cold stage。该 stage 的
`RR-QUA-20260719-17` 以 `qwen3.5:2b` 和 Landlock + seccomp 完成 16 个真实 Turn、1 个
cancelled Run、1 个 admission reject 和 2 个 Conversation 删除；总耗时 13.479 秒，
终态 Run root、Worker PID record 和 API 资源残留均为 0。workload 中实际 Gateway
`write_bytes/syscw` 增量为 `2,842,624/1,593`，state logical/allocated growth 为
`2,743,920/2,744,320` bytes，WAL logical peak 为 `2,859,312` bytes，log growth 为
78 bytes，temp peak 为 `1,038/4,096` logical/allocated bytes，cache growth 为 0；19 个
插桩进程镜像全部登记，workload 内只观察到 34 次成功的 preloaded-libc `fsync` symbol
call。正常 `stop.sh` 后完整插桩生命周期共 51 次成功 `fsync`，其它五类 symbol 为 0，
没有 slot overflow 或 registration failure。

`RR-QUA-20260719-11..16` 另保留为 final-candidate 之前的三组 instrumented/plain 4-Turn
A/B 基线：每轮均为 4 completed、1 cancelled、1 rejected、2 deleted、零残留；插桩组/
普通组 duration median 分别为 3.379/3.398 秒，Gateway `write_bytes` median 均为
1,036,288 bytes，state logical growth median 均为 1,009,432 bytes。三轮插桩 workload
各观察到 12 次成功的 preloaded-libc `fsync` symbol call，普通轮不观察精确 sync-call。
这些应用层数据不观察 direct syscall、kernel/block flush 或 SSD/NAND 写入，也没有
SMART 对照，不能解释成物理磨损测量。

同日 recovery final candidate 完成严格 replay/snapshot/outer-metadata 复审、真实
SIGKILL 与尾部 SQL fault、不可写 journal、SSE gap/delete race/restart 唯一 terminal
负面矩阵；该 recovery 候选当时全量为 `317 passed`，`./governance.sh` 扫描 12 个
Markdown、8 个 shell script 和 81 个文本文件后通过。这关闭 REC-01/GATE-02，但不表示
operation ledger 能为
未来任意副作用提供 exactly-once。

最终发布 checkout 的普通模式 `RR-QUA-20260719-20` 再次 PASS：4 completed、1 cancelled、
1 rejected、2 deleted、零残留，真实模型与沙箱仍为 `qwen3.5:2b` / Landlock + seccomp，
Gateway 维持 `0.0.0.0:20815` 健康。此前 `RR-QUA-20260719-18/19` 在 workload 前分别因
pytest 和浏览器遗留在 `.runtime/tmp` 的 symlink fail closed；没有 Run 或 API side effect，
相关可再生临时树被可恢复地移到 `.runtime/test-results/qualification-quarantine/` 后，
RR20 才执行成功。失败记录保留且不从证据链中隐藏。当前仍没有原生 `aarch64`、长期
failure/restart soak、SSD SMART、TLS 或多用户隔离证据。

同日完成 single-operator 本地 token rotation 纵向验证：隐藏 TTY 双输入不回显，受管
Gateway stop 后以 private temp + file fsync + atomic replace + parent fsync 轮换，再次
stop 收敛并重启；secret 保持 owner `0600`/single-link，Web login/logout 为 `200/204`，
token 未出现在受管进程 argv/environ 或 Gateway log。默认创建仍为 256-bit 随机值；该
证据不关闭 production 无中断轮换、多用户凭据治理或弱运维口令风险。

同日完成 model-call boundary、全 Turn/Run 时间线和 authenticated context inspection 的
开发门禁：合并定向测试 `278 passed`，完整测试 `348 passed`，`./governance.sh` 扫描
12 个 Markdown、8 个 shell script 和 80 个文本文件后通过，Python/JavaScript 语法与
`git diff --check` 通过。事务内 SIGKILL 会让 usage/response boundary 整体回滚；事务已
提交但 terminal 尚未提交时重启会保留 exact usage，并以唯一失败终态收敛；open request
则保持 incomplete。受管 Gateway 随后在 `0.0.0.0:20815` 以真实
`qwen3.5:2b` 和 `landlock+seccomp` 启动。认证开发 smoke 留下题为
“模型边界与多轮时间线验收”的两轮 Conversation：每个 Run 均有两组严格配对的
request/response，第二个 Run 的 `run.started` 显示历史 `2/2/0`（完整/纳入/省略），
ContextPlan section role 为 system/system/user/assistant/user，replay 为 `run-ui-v2`；context
inspection 为 `exact`、`no-store`、正文隐藏且正文提升 query 返回 `400`。终态无 Worker PID
record 或 Run root 残留，Gateway 保持健康。该开发 smoke 不是新的 platform/SSD 资格 RR，
也不关闭 LOOP-01 或 CTX-02 的剩余验收。

2026-07-20 完成双栏事件回放 UI 的发布态复验：右侧 Conversation 按 Turn 分组，左侧
使用 User/Harness/LLM/Tool 四泳道、独立 Replay control、常驻 Inspector、过滤和有界
播放游标；active Run 与历史 Run 使用分仓内存投影，后台 SSE 不会污染正在查看的历史。
定向 frontend/web 为 `65 passed`，全量为 `355 passed`，JavaScript/diff 门禁与治理检查
通过。受管 Gateway 重启后仍监听 `0.0.0.0:20815`，健康检查报告真实
`qwen3.5:2b` 与 `landlock+seccomp`。认证 smoke 新建“双栏时序回放验收”两轮
Conversation，两轮均以 `run.completed` 收敛，durable replay 分别为 11/6 个有序事件；
第二轮 ContextPlan 纳入首轮 `2/2/0`（完整/纳入/省略）历史消息并正确回答依赖首轮的
暗号问题，context inspection 为 `exact`、`no-store`。终态无生产 Worker PID record 或
Run root 残留；该 smoke 不是新的 platform/SSD qualification RR。

`RR-CTX-TOOL-20260720-01`（implementation_ref:
`fdb676aa53b2ae5e729e1654f0c7006456145587`）完成 CTX-03、CTX-04、TOOL-01
候选验证：全量 `392 passed`，governance 扫描 12 个 Markdown、8 个 shell script、93 个
文本文件通过，`git diff --check` 通过。受管 stop/start 后 20815 健康报告
`qwen3.5:2b` 与 `landlock+seccomp`；两个真实 Run 均为 3 次 model request、2 次 Echo、
唯一 completed terminal。无 workspace 文件时 section 为 platform/agent/environment/user；
临时 private CLAUDE.md 时增加独立 workspace trust section，移除后无残留。unsafe mode
真实 admission 返回不含路径的 409。Git negative matrix 覆盖 parent-repository no-op、
metadata symlink、外部 config include 的 Landlock 拒绝、输出洪泛和 non-repository；collector
及 prompt/tool snapshots 不落盘，Git optional lock 和全部 workspace 写入由 kernel policy
拒绝，不增加逐 token/chunk/frame 写。v1 Echo toolset digest `77c47868160c…` 保持可回放。
该记录关闭三项 contract，不扩大 QUA-01 的平台/SSD release 结论。

`RR-PERM-20260720-01` 完成 PERM-01 的通用安全底座：默认 deny 的 Capability Broker
绑定 Agent generation、Conversation/Turn/Run/call、ToolSet/policy、规范化 arguments、准确
preview 与 expiry；认证/CSRF Web 管理面只能解析既有 pending ID。permission 与 operation
的 request/resolve/intent/dispatch/outcome 在同一 Agent SQLite 事务写入有界审计流，重启
只读分页重放不会 dispatch，遗留 dispatched 只收敛为 `outcome_unknown`。负面矩阵覆盖
deny precedence、无交互 ask、过期、取消、generation/policy/binding 漂移、深层/循环 JSON、
approve/deny race、重启、Conversation 删除、CSRF/越权字段和 at-most-once dispatch；定向
为 `110 passed`，完成文档后的全量候选为 `401 passed`，governance 扫描 12 个 Markdown、
8 个 shell script 和 95 个文本文件通过。每次成功测试 operation 固定产生 5 条语义审计
记录，没有 token/chunk/frame 写路径。受管 stop/start 后 20815 健康报告真实
`qwen3.5:2b` 与 `landlock+seccomp`；认证 smoke 完成真实 Run、空 capability audit 读取和
Conversation 删除，终态为 completed 且测试会话零残留。该项不暴露 File/Shell/Skill/MCP
executor，也不扩大生产或 exactly-once 声明。

`RR-CMP01-20260720-01` 完成 ToolSpec v3 的 deterministic micro-compaction：canonical
Tool event 保留完整受限 result，provider projection 对超 4096 bytes 的 Echo 结果生成含
call ID、原始 bytes、content digest 和固定 reason 的有界 receipt，单 Run projected history
上限 16 KiB，替换后复用每次 provider request 的 Token/byte admission。v1/v2 ToolSet digest
仍可严格 replay；v3 digest 为 `022728f8ba99…`。8 KiB canonical 结果、receipt tamper、旧
digest、Control Plane 分流、Ollama 二次 admission 和真实 Worker 集成的定向集合为
`171 passed`；全量 `406 passed`，governance 扫描 12 个 Markdown、8 个 shell script 和
94 个文本文件通过。该 projection 只在 Run 内存中构建，不增加 event/artifact/逐 chunk
写入；canonical 结果继续受既有 256 KiB/Run、512 MiB/Agent SQLite 与 256 个近期 Run
retention 约束。受管重启后真实 `qwen3.5:2b` Run 完成 2 次 Echo、唯一 completed terminal，
`run.started` 使用 v3 digest；测试 Conversation 删除后零残留，20815 保持健康。

`RR-CMP02-20260720-01` 完成独立 `context-projection-v1` durable boundary：首次 boundary
与 Conversation revision CAS、Turn、`run.started` 和 journal state 同事务提交，绑定
Agent/generation、history/recent segment、model/profile、instructions、compression、ToolSet/
catalog/policy、renderer/section registry；canonical JSON 仅 1429 bytes（本次真实 smoke），
不含 prompt/message 正文，硬上限 16 KiB。四种 admission/replay/manual/summary reason 复用
同一 codec，替换用旧 digest 做单行 CAS；revision/model/instruction/history/policy 漂移测试
全部拒绝。定向 `61 passed`、全量 `409 passed`，governance 扫描 12 个 Markdown、8 个
shell script 和 96 个文本文件通过。真实 `qwen3.5:2b` Run completed 后 boundary 跨受管
Gateway stop/start 保持严格可解码，删除 Conversation 后对应行降为 0，20815 继续报告
`qwen3.5:2b` 与 `landlock+seccomp`。每 Run 单行 CAS，不按 compact/token/chunk 追加；最坏
100×128×16 KiB 约 200 MiB，并受 512 MiB Agent SQLite 上限约束。

`RR-READ01-20260720-01` 完成 READ-01：runtime ToolSet 新增 `file/stat` 与
`file/read_text`，Worker 只通过绑定 call/tool/arguments 的限帧 capability IPC 请求；
Control Plane 在 permission/operation 五阶段审计后，以逐组件 dirfd + `O_NOFOLLOW`、
同设备和稳定 identity 读取单 Capsule workspace。目标限定 owned、单 hardlink、非危险
mode、非稀疏、最多 1 MiB、UTF-8 regular file；单次返回最多 4096 bytes/256 行并携带
path identity、完整 content digest、range/truncated receipt。负面矩阵覆盖 traversal、
absolute/noncanonical path、symlink/跨 Agent、hardlink、FIFO、binary、unsafe mode、sparse、
UTF-8 offset、取消、增长竞态与 IPC response confusion。定向集 `64 passed` 后，全量为
`428 passed`。真实 `qwen3.5:2b` 主动读取一次性 `read-demo.txt`，准确回答
`cedar-20815`，Run completed 且审计顺序为 request/resolve/intent/dispatch/outcome；测试
Conversation 删除、fixture 清除、Run root 为 0，20815 继续报告 `landlock+seccomp`。
读取不创建内容 sidecar/index/temp 或逐 chunk 写入，只有既有 canonical Tool event 和
有界语义审计进入 Agent SQLite。

`RR-SEARCH01-20260720-01` 完成 SEARCH-01：新增 `file/glob` 与 `file/grep`，复用
READ-01 capture，目录通过 dirfd/no-follow 同设备遍历，不调用 shell、不建立持久索引或
先收集全树路径。固定 depth 16、entries 4096、files 1024、read 2 MiB、matches 128、
result 12 KiB、wall 1 秒；按 UTF-8 path 稳定排序，结果带 Capsule provenance、receipt 和
truncation reason。Grep 默认 literal，regex 限制为无 group/alternation/backreference/
brace/`*`/`+` 的 256-byte 子集。负面矩阵覆盖换行文件名、deep/wide tree、entry/byte/
match/result cap、病态 regex、symlink/跨 Agent、hardlink、FIFO、cancel、Capsule delete
race 和零 sidecar/index；同一真实 Worker 的 fake broker search→read→answer 集成通过。
全量为 `447 passed`，governance 扫描 12 个 Markdown、8 个 shell script 和 100 个文本文件通过。受管重启后真实 `qwen3.5:2b` 在同一 Run 依次调用 `file/glob` 与
`file/read_text`，第三次模型迭代准确回答 `juniper-581`，两次调用各有完整五阶段审计；
Conversation/fixture 删除、Run root 为 0，20815 保持健康。该工作负载只增加既有有界
canonical Tool event/审计事务，不产生逐 entry/match/chunk 磁盘写。

`RR-CMP03-20260720-01` 完成 CMP-03：`context-collapse-v1` projection 以
`completed-turn-collapse-v2` 把最旧完整 Turn pairs 替换为 content-free receipt marker，
至少保留最近一个完整 pair，并绑定来源 history、collapsed/preserved message IDs、被折叠
内容与保留 segment digest。renderer/registry 升级到 v4/v3；旧 tail-v1 boundary 可按旧
版本解码，但严格匹配会拒绝跨版本静默复用。确定性、tamper、protected section、recent
Turn、hard-limit fail-closed 与 replay 定向测试纳入全量 `454 passed`；governance 扫描
12 个 Markdown、8 个 shell script 和 102 个文本文件通过。真实 `qwen3.5:2b` 四轮会话
依次得到 `full/full/collapse-v2/collapse-v2`，最终准确回答 `COLLAPSE-OK`；最后一轮投影
保留 2、折叠 4 条历史消息。受管 stop/start 后 boundary 可严格解码，删除 Conversation
后该 Run 的 Turn、events、boundary、usage、snapshot、permission/operation/audit 均为 0，
20815 继续报告 `landlock+seccomp`。projection 仅随首次 Run boundary 做一行事务写入，
不增加按 token/chunk/compact 次数的磁盘写。

`RR-MODEL01-20260720-01` 完成 MODEL-01：operator-owned `ModelCatalog` 固定 endpoint、
provider model、能力、窗口上限、输出预留和 generation options；公开 `/api/models` 只返回
最多 16 个受限 profile，不含 host/port。Turn 接受前按 catalog ID 选择资格对象，完整
profile digest 进入 runtime snapshot/run.started/provider usage boundary；无 Tool profile
自动得到空 ToolSet。未知 ID、endpoint 形态注入、重复/rebound entry、能力缺失、错误模型
response 和 profile/ContextPlan 不匹配均 fail closed。32k+Tool 与 8k+text-only 两画像的
资格、动态 projection、provider request 和回滚测试通过，完成后的全量为 `462 passed`。
受管 stop/start 后 20815 的 `/api/models` 仅列默认真实 `qwen3.5:2b`；显式选模的两轮真实
Run 均 completed 并回答 `MODEL-CATALOG-271`，第二轮从 durable transcript 纳入前一轮
2/2 条历史消息。Conversation 删除返回 204，endpoint 未出现在公开响应。目录、profile 和
ContextPlan 均为内存/每 Turn 固化数据，不引入按 token/chunk 的磁盘写。

`RR-CMP04-20260720-01` 完成 CMP-04：摘要子调用复用受信 normal model session，但
ContextPlan ToolSet 为空，无法发起 Capability；输入 48 KiB、输出 8 KiB、15 秒、单次
attempt，结构固定为 facts/decisions/open_tasks/files/references 各最多 16×384 bytes。snapshot
绑定 collapsed message IDs/content、model/profile、prompt/policy、v5/v4 renderer、exact
provider request digest 与校验 usage；`context-projection-v2` 在同一 Turn admission 事务保存
该 snapshot，单行仍限 16 KiB。失败立即沿用 CMP-03，连续三次失败熔断 60 秒；constructor
可关闭语义层。双窗口、关键事实/决定/任务/文件/引用、prompt injection 数据化、tamper、
schema/bytes、空 Tool、熔断和 v1 migration 测试纳入全量 `470 passed`。真实 qwen 独立摘要
资格为 326/151 tokens；发布态 manual compact 三轮为 `full/full/semantic-summary-v1`，最后
ContextPlan 从 4 条历史保留最近 2 条并以摘要准确恢复 `SEMANTIC-428`。v2 boundary 为
2701 bytes、reason `manual_compact`、summary usage 311/152；受管 stop/start 后 snapshot
digest、6-event completed replay 和答案保持一致。删除 Conversation 后 Turn/event/boundary/
usage/snapshot/permission/operation/audit 全为 0，Run root 为 0，20815 保持健康。每 Run 仍
只有一行 projection，不按 token/chunk/summary frame 写盘。

`RR-CMP05-20260720-01` 完成 CMP-05：Broker 只把 `400|413`、严格 JSON 单字段 error、
窄 context/media 超限语义且零 provider frame 的响应分类为可恢复 overflow；认证、网络、
其它状态、额外字段、错误 content-type、状态 200 部分流和协议错误均不进入该分支。
Control Plane 在 Turn admission 时预编译同一冻结 profile/ToolSet 下、保留最近完整 pair 和
当前 user input 的 cheap recovery projection；真实 overflow 后先把 attempt 0 的 usage 与错误
response 原子收敛为 incomplete，再以 projection digest CAS、唯一 32-hex recovery ID 和
`model.recovery.started` 切换 boundary，仅发起 attempt 1。逻辑 iteration、attempt 与递增
provider_call_index 分离；第二次 overflow、取消、deadline、部分输出或安装失败不再重试，
Tool/Capability transcript 不回滚、不重放。严格 replay/snapshot、running-Run recovery 和
UI 均理解新 additive feature，旧事件/`context-projection-v1` 仍可解码。分类、单次成功、
第二次失败、partial、cancel、双 provider ledger、canonical transcript、terminal usage、
重启回放、边界 tamper 和恢复材料释放已进入完整 `483 passed`；受管 stop/start 后真实
`qwen3.5:2b` 正常路径完成 2 次配对 provider 调用、0 次 recovery，回答
`NORMAL-RECOVERY-OK`，Conversation 删除后返回 404，20815 保持健康。恢复只新增有界语义
事件和每 attempt 一行 usage，不逐 token/provider frame 写盘。

`RR-WRITE01-20260720-01` 完成 WRITE-01：runtime ToolSet 新增独立 `file/edit` 与
`file/write`；现存文件要求同一 live Run 的完整 read receipt 和完全一致的 path/content
identity，create 使用 target-absent + parent identity。Broker 生成最大 4 KiB 的准确 diff
preview 并等待 authenticated same-origin operator one-shot 审批；批准后写 durable intent/
dispatch，按 canonical target 串行进入 descriptor-anchored executor。同目录 `0600` temp
最多承载 8 KiB 新内容，成功路径固定 file/parent 两次 fsync；create 用
`RENAME_NOREPLACE`，replace 用 `RENAME_EXCHANGE` 并核验交换出的旧 inode/content，竞态时
回滚，无法证明回滚时以 `outcome_unknown` 收敛且不重放。保留 temp 命名空间不可作为
target，启动最多扫描 4096 entries、清理 16 个安全 temp。receipt 缺失/partial/stale、零/
多 Edit 匹配、symlink、取消、审批后 create/replace race、fsync failure、rollback failure、
重复 dispatch 和重启恢复负向矩阵纳入全量 `497 passed`；governance 扫描 12 个 Markdown、
8 个 shell script 和 108 个文本文件通过。受管真实 `qwen3.5:2b` Run 请求 create，Web
审批后文件内容精确为 `WRITE-ATOMIC-731`，五阶段 permission/operation audit 完整；删除
Conversation 并移除 smoke fixture 后无 Run root 或 temp 残留，20815 保持健康。mutation
不逐 token 写盘，每次成功只分配一个有界 temp 并执行两次 sync。

`RR-EXEC01-20260720-01` 完成 EXEC-01：新增 `exec/run`，v1 只解析受信 catalog ID
`runtime-compile`，模型不能提交 executable/argv/env/PATH/shell。approval 与 operation
identity 绑定 Capsule Python device/inode/size/content、固定 argv、完整 source digest、随机
runner ID 和 `singleton-landlock-seccomp-v1`；dispatch 后 payload 在同一 PID 内先安装
Landlock、seccomp、rlimit、PDEATHSIG/no-dump/no-new-privileges，主动证明 fork/socket/后续
exec 均为 `EPERM`，Control Plane 用 PIDFD 与 `/proc` 复核 ready handshake 后才 release。
固定 CPU 10 秒、wall 12 秒、AS 256 MiB、NPROC 1、FD 32、单文件 2 MiB、source
256 entries/2 MiB、allocated output 8 MiB、stdout+stderr/result 12 KiB；clean env 19 keys，
workspace 不可见，唯一 writable 是预验证空的 Run output。成功、预 release cancel、wall
timeout、source/executable/output race 和失败均收敛 exact PID、pipe/pidfd/PID record/output；
无法证明 stop 时沿用 `outcome_unknown` 且不重放。fork/network/exec、unknown command、
source drift、timeout、output flood、approval、旧 ToolSet replay 与完整 Worker cleanup
纳入全量 `503 passed`；governance 扫描 12 个 Markdown、8 个 shell script 和 111 个文本文件
通过。受管重启后真实 `qwen3.5:2b` 只调用一次 `exec_run(runtime-compile)`，operator
批准后回答精确 `EXEC-REAL-OK`；五阶段 audit 各一次，Conversation 删除、Run root 和所有
runner PID record 均为零，20815 保持健康。固定 compile 临时写入最多 8 MiB 后立即清空，
无逐 token/line flush 或持久 build cache。

`RR-CMD01-20260720-01` 完成 CMD-01：`SlashCommandRegistry` 固定 7 个稳定 ID、3 个 alias、
参数 schema、availability、help 与 feature gate；完整输入 4096 bytes、最多 4×256-byte
参数、结果 64 KiB，control result 固定 `model_invoked=false`/`turn_created=false`。Web 的
Conversation Run 输入边界和独立 command endpoint 都先解析 `/`，无 Conversation 的兼容
入口拒绝 Slash，不会降级为 prompt。status/context/model/compact/permissions/cancel/clear
只组合既有 QueryEngine、Context inspection、ModelCatalog、permission 和 delete/cancel；
显式 Run ID 做 Conversation ownership 检查，active Run 冲突 fail closed，model/compact
只有 bounded next-Turn UI effect，不建第二状态源。Web 新增 registry help、纯文本 command
result、运行中 750 ms 有界 pending-permission poll、完整 preview/diff 与 one-shot approve/
deny；所有修改 POST 保持 same-origin/CSRF。parser/control/service/no-Turn/ownership/active
conflict/Web XSS 与 route 负向矩阵纳入全量 `506 passed`。受管重启后真实 20815 顺序执行
status/model/compact/permissions 和 clear；4 个结果均未调用模型或创建 Turn，SQLite
conversation_turns/events/provider_usage 增量全为 0，Conversation 删除无残留，registry 7 项
和三处 UI surface 均可读取。命令查询不产生逐 token/chunk 磁盘写；permission poll 仅在
active Run 内存态存在，terminal 后停止。

`RR-TASK01-20260720-01` 完成 TASK-01：`TaskStore` 与 `BackgroundTaskManager` 在 Agent
`state.sqlite` 和独立 `tasks/<task-id>` root 中实现 queued/running/terminal 状态、parent
Run/generation/executor/request 绑定、结果与 digest notification。硬上限为每 Agent 4 active、
128 retained、result 16 KiB、4×4 KiB 通知、7 天 retention；复用 EXEC-01 的 12 秒 PIDFD/
PDEATHSIG/Landlock/seccomp singleton，因此 fork/double-fork/setsid/exec/socket 均无法产生
descendant。重启把遗留 active 标为 interrupted 且不 dispatch；取消、stop、Conversation
delete 和 Agent delete 先收敛 runner、清 Task root 再发布终态。定向与全量回归最终为
`511 passed`；受管 stop/start 后真实 qwen parent Run 与 background compile 均 completed，
通知严格 queued/running/completed，sandbox probes 全部 denied，terminal root 与删除后的
Task row/notification 都为零。状态只写三个语义边界，不逐 stdout/stderr chunk fsync。

`RR-EXEC02-20260720-01` 完成 builtin-only bounded Bash：显式 parser 只允许单个
`printf|pwd|true|false`，将 normalized AST、Bash executable、fixed cwd/env、无 redirection
与完整 preview 绑定 one-shot approval；所有 expansion/control/ambient command 在 dispatch 前
拒绝。runner 以 `--noprofile --norc` 进入已打开 Bash FD，Landlock 不暴露 workspace 或
Capsule Python，seccomp 继续拒绝 fork/clone/setsid/socket。全量 `528 passed`；真实 qwen
foreground Tool 获得一次审批和五阶段 audit 后精确完成，background 只经 TASK-01 完成，
终态 Task root 与删除后的状态均为零。

`RR-EXT01-20260720-01` 完成默认关闭的 MCP/LSP capability adapter：两种 protocol 复用同一
Tool/permission/operation/result boundary；operator spec 固定 ID/method/HTTPS endpoint/DNS，
远端调用使用 pinned IP + hostname TLS，stdio/request endpoint/credential/redirect/SSRF/
rebind/schema drift 均 fail closed。release catalog 为空，因此扩展 Tool 不进入 EffectiveToolSet；
受管重启后认证 catalog 为空，真实 qwen Run completed、visible tools 无 extension，删除零
残留。外部协议正向与负向由无网络 fake transport 可重复验证。

## Phase 1 — Clean-root qualification

目标：让清洁后的根目录成为唯一可运行系统，并用可重复证据封住旧/新边界。

- 冷 checkout bootstrap/start/health/real Run/stop smoke；
- lifecycle 并发、启动回滚、stale/伪造 PID、orphan Run 负面测试；
- 验证所有 cache/temp/home/data/process 路径留在 checkout；
- 验证 `_legacy-reference/` 与 Claude Code materials 不进入 import/build/test/runtime 图；
- 在所有首发支持平台完成原生资格记录；`0.2.0` 只支持 `x86_64`，`aarch64` 在加入矩阵前
  必须完成等价记录；
- 建立基础 load/soak 与磁盘写入基线，记录而非推测 SSD 影响。

完成标准：全门禁通过，真实模型 Run 无重大运行错误，停止后无受管进程/Run 残留；
多架构未验证前仍保留平台限制声明。

## Phase 2 — Durable conversations and replay

目标：从“有 durable events”升级为“可验证恢复的 conversation runtime”。

范围包括 durable cursor availability、gap/replay/live 交界、幂等 snapshot/projector、
journal prune/retention、WAL 不可写或损坏、bounded usage ledger，以及未来副作用的
intent/outcome 恢复协议。canonical transcript 和 durable semantic events 保持权威；
ephemeral delta 丢失必须明确呈现，不能伪装成完整重放。

逐项状态只在 REC-01、CMP-02 和 GATE-02 维护；现有能力与验证证据只在本计划顶部
Implemented baseline / Current validation evidence 记录。

## Phase 3 — General Agent Capsule lifecycle

目标：安全提供 create/list/upgrade/delete，而不破坏 Agent 隔离。

- 引入持久 Agent registry 和 generation/version 状态机；
- provisioning 只使用 project-local、allowlisted、binary-only 依赖；
- Agent instructions/context policy 作为版本化 Capsule 内容；
- upgrade 有 staging、资格检查、原子切换和 rollback；
- delete 实现 draining → process proof → data/runtime/env cleanup → residual audit；
- 中断/崩溃后 create/upgrade/delete 均能幂等恢复；
- 多 Agent 并发与交叉污染负面测试。

完成标准：[Agent Capsule delete contract](../design/agent-capsule.md#通用-agent-删除尚未实现的契约)
全部满足，删除一个 Agent 不改变其它 Agent 的进程、文件或状态。

## Phase 4 — Capability and permission brokers

目标：逐项增加 Claude Code 风格实用能力，同时不扩大 Worker 的直接权限。

推荐顺序：只读文件元数据/读取 → bounded search → atomic edit/write → controlled command
execution → MCP/Skill。每项都需要：

- 稳定 Tool ID 和严格 request/result schema；
- 用户/Agent/Run capability policy 与必要的交互授权；
- 路径 containment、输入/输出/时间/并发/进程/磁盘限制；
- cancellation、terminal convergence、审计和 secret redaction；
- 独立 broker 或更细子沙箱；不向主 Worker 开放 socket/fork/exec/任意写；
- archive、URL、subprocess、MCP transport 的专项负面测试。

Skill 与 stdio MCP 是代码执行，默认禁用。kernel 能力不足时 fail closed，不提供开发
环境 unconfined fallback。

## Phase 5 — Context and instruction system

目标：让 `ContextCompiler` 真正拥有 provider-agnostic model view。

范围包括分层 Prompt section、可审计 context inspection、Capsule workspace instructions、
有界 Git/date/env、Tool-result projection、durable compaction boundary、semantic summary
和单次 overflow recovery。模型视图与 canonical transcript 分离，所有预算和压缩策略
从受信 ModelProfile/ContextPolicy 推导。

逐项状态只在 CTX-*、CMP-* 和 GATE-05 维护；最终 section、event、storage 和安全契约
分别写入 architecture、event protocol 和 SECURITY。

## Phase 6 — Tasks and sub-agents

目标：在已有 Agent/Run identity 上增加显式 parent-child orchestration，而不是嵌套隐式
graph。

- `parent_run_id`、Task 状态、mailbox 和结果回收协议；
- 每个子 Agent 使用自己的 Capsule/环境/沙箱，父 Run 只持有有界 capability；
- 全局/每 Agent/每父 Run 并发、深度、预算和 wall deadline；
- 取消传播、孤儿处理、循环检测和唯一终态；
- Web 展示 parent/child timeline 和独立事件详情。

完成标准：父/子故障、取消和重启测试无 orphan process/state；一个 Agent 无法读取或
修改另一个 Capsule，除非有显式 brokered message capability。

## Phase 7 — Supported release qualification

目标：在功能边界稳定后再决定受支持部署形态。

- TLS/可信 reverse proxy、多用户/租户边界和 production token lifecycle；当前仅有
  single-operator 的本地 stop/atomic-rotate/restart，不等同于无中断轮换或多用户凭据治理；
- Control Plane 与 Web BFF 是否拆进程，以故障与权限分析决定，不为“微服务化”而拆；
- bounded/redacted tracing、metrics、audit 和 retention；
- load、soak、chaos、upgrade/rollback、backup/restore 与磁盘磨损测试；
- dependency/SBOM/vulnerability、release artifact、cold-checkout 和运维 runbook；
- 明确支持矩阵和 capacity envelope。

只有所有 release gates 都有可重复证据，才能以冻结的 single-operator 本地支持合同替换
早期阶段免责声明；不能因此扩大 TLS、多用户、HA、未资格平台或长期 soak 结论。

## Execution ledger — Claude Code 核心机制演进

本节是本计划中唯一的逐项状态账本。上面的 Phase 说明目标和边界；这里用稳定 ID
记录依赖、退出门槛和验收证据。后续实现不得另建一份长期总路线复制这些状态。只有当
某一项跨多个变更、包含独立迁移或故障矩阵时，才临时建立窄执行子计划；子计划完成后
把最终契约写回对应 design/security 文档，并在这里记录证据和状态。

### 研究输入映射

下列文章只提供设计心智模型，不是本项目的需求规范或安全证明。实现判断以 P1-P8、
本计划、[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和本地
[Claude Code 2.1.88 来源说明](../../references/claude-code/PROVENANCE.md)为准。
外部文章链接便于回看研究上下文，但 governance 的本地链接检查不保证其长期可用；
可复现判断必须落到上述 checkout-local 权威资料、设计契约和测试证据。

| 研究主题 | 主要启发 | 对应 work item |
| --- | --- | --- |
| [QueryEngine](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc5) | Conversation-scoped 编排、跨 Turn 边界、模型/Tool 闭环 | QE-01、REC-01、LOOP-01 |
| [提示词工程](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc18) | 分层 Prompt、角色和 Tool prompt、运行时装配 | CTX-01、CTX-02、CTX-03 |
| [Tool 系统](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc6) | 统一契约、动态有效工具集、执行上下文和权限 | TOOL-01、PERM-01 |
| [Slash Commands](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc7) | 人类显式控制面与模型 Tool 分离 | CMD-01 |
| [项目上下文](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc8) | Git、CLAUDE.md 和高信号工程上下文 | CTX-03、CTX-04 |
| [上下文压缩](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc8b) | 结果裁剪、projection、summary、reactive recovery | CMP-01 至 CMP-05 |
| [文件链路](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc13) | Read/Search/Edit/Write 风险分层和 diff | READ-01、SEARCH-01、WRITE-01 |
| [Bash 子系统](https://www.xuanyuancode.com/learn-claude-code/tutorials/cc14) | 命令解析、权限、sandbox、Task、取消和回流 | EXEC-01、TASK-01、EXEC-02 |

不得照搬参考实现中的固定压缩阈值、可关闭 sandbox、symlink 跟随、atomic 失败后原地
写入等取舍。当前 Worker 的无网络、无子进程和无任意写边界继续保持。

### 账本维护规则

- 状态只有：`not_started`、`in_progress`、`blocked`、`done`、`deferred`、`superseded`。
- 开始实现时先把该项改为 `in_progress`；方向、依赖或范围变化必须同步更新本计划。
- 只有该项所有 checkbox、全局 DoD 和要求的真实 lifecycle 证据都满足，才能标记
  `done`。代码已合入但故障、文档或清理证据缺失时仍不是完成。
- `blocked` 必须在证据栏记录具体阻塞条件和解除条件；不能用它代替未开始。
- `deferred` 表示明确移出当前 release scope，并记录重审条件；`superseded` 只用于已由
  新稳定 ID 完整取代的旧项，证据栏必须列出 replacement IDs，二者都不是 `done`。
- 证据记录测试文件、命令结果摘要、真实模型纵向验收、故障注入和权威文档链接；
  不提交 runtime 日志、token、会话 cookie、绝对秘密或不稳定 Run ID。
- 每项的数值资源上限应在实现时写入对应权威设计/安全文档；本账本只追踪是否完成，
  不复制会漂移的常量。
- ID 永不复用；拆分时把原 ID 标为 `superseded` 并记录 replacement。后续治理脚本应检查 ID 唯一、
  dependency 存在且无环、状态枚举合法、done 有完整证据、blocked 有解除条件；自动
  检查落地前由每次 plan review 人工执行。
- 本账本建立前已经完成的 baseline 项，可用顶部带日期的 Current validation evidence、
  实现和自动测试链接作为初始 `done` 证据；下一次实质修改该项时补齐 RR 记录和发布 SHA。

真实 lifecycle、故障或资源验收使用稳定记录 ID，格式为：

    RR-VER-YYYYMMDD-NN
    implementation_ref: <full commit sha; pre-publication validation may say worktree>
    work_items: <stable IDs>
    platform: <arch/kernel/sandbox/model qualification>
    commands: <reproducible entrypoints>
    result: <pass/fail and bounded metrics>
    residual_audit: <none or explicit findings>

### 总状态

| 顺序 | ID | Priority | 目标 | depends_on | 状态 |
| ---: | --- | --- | --- | --- | --- |
| 1 | QE-01 | baseline | Conversation-scoped 逻辑 QueryEngine | — | done |
| 2 | QUA-01 | P0/parallel | clean-root、支持平台、SSD 与 lifecycle 资格 | — | done |
| 3 | REC-01 | P0 | durable replay、gap、snapshot 与副作用恢复协议 | — | done |
| 4 | AGT-01 | P0 | 通用 Agent Capsule create/upgrade/delete | REC-01 | done |
| 5 | LOOP-01 | P0 | 有界多步 model → Tool → model loop substrate | REC-01, AGT-01 | done |
| 6 | CTX-01 | P0/parallel | Prompt section registry | — | done |
| 7 | CTX-02 | P0/parallel | authenticated context inspection | CTX-01 | done |
| 8 | CTX-03 | P0 | workspace CLAUDE.md | CTX-01, AGT-01 | done |
| 9 | CTX-04 | P0 | bounded Git/date/environment context | CTX-01, AGT-01 | done |
| 10 | TOOL-01 | P0 | Tool contract v2 与 EffectiveToolSet | LOOP-01, AGT-01 | done |
| 11 | PERM-01 | P0 | Capability Broker 与 durable permission 状态机 | REC-01, AGT-01, TOOL-01 | done |
| 12 | CMP-01 | P0 | Tool-result budget 与 micro-compaction | CTX-01, TOOL-01 | done |
| 13 | CMP-02 | P0 | durable projection boundary 与 snapshot | REC-01, CTX-01, CMP-01 | done |
| 14 | READ-01 | P1 | descriptor-anchored file metadata/read | AGT-01, PERM-01, CMP-01 | done |
| 15 | SEARCH-01 | P1 | bounded Glob/Grep | READ-01 | done |
| 16 | CMP-03 | P1 | deterministic context-collapse projection | CMP-02 | done |
| 17 | CMP-04 | P1 | semantic summary 与 autocompact | CMP-03, MODEL-01, READ-01, SEARCH-01 | done |
| 18 | CMP-05 | P1 | one-shot reactive overflow recovery | CMP-04 | done |
| 19 | WRITE-01 | P1 | diff-bound atomic Edit/Write | READ-01, PERM-01 | done |
| 20 | EXEC-01 | P1 | allowlisted `shell=False` argv runner | AGT-01, PERM-01, CMP-01 | done |
| 21 | MODEL-01 | P1 | trusted ModelCatalog 与动态模型切换 | LOOP-01, CTX-01 | done |
| 22 | CMD-01 | P1 | Slash Command/Web 显式控制面 | CTX-02, PERM-01, CMP-04, MODEL-01 | done |
| 23 | TASK-01 | P2 | durable background Task substrate | REC-01, AGT-01, EXEC-01 | done |
| 24 | EXEC-02 | P2 | bounded foreground Bash 与可选后台执行 | EXEC-01, TASK-01 | done |
| 25 | EXT-01 | P2 | MCP/LSP 通过同一 capability 边界 | TOOL-01, PERM-01, EXEC-01 | done |
| 26 | SKILL-01 | P2 | versioned Skill registry 与隔离执行 | AGT-01, TOOL-01, PERM-01, EXEC-01 | done |
| 27 | SUB-01 | P2 | Task/mailbox 驱动的子智能体 | REC-01, AGT-01, PERM-01, TASK-01 | done |
| 28 | REL-01 | release | 首个受支持版本资格与运维契约 | QUA-01 | done |

### Phase exit gates

Gate 只在依赖 work item 均为 done 且验收证据可重复时关闭。Gate 状态不替代上面的
work item 状态。

| Gate | 当前状态 | 退出条件 |
| --- | --- | --- |
| GATE-01 clean-root | done | QUA-01 全部完成；受支持平台 cold checkout、真实 Run、完整停止、containment、soak 和磁盘基线有证据 |
| GATE-02 recovery | done | REC-01 完成；所有支持的崩溃点恢复到最后 durable boundary，客户端能区分 replay/gap/live |
| GATE-03 Agent lifecycle | done | AGT-01 完成；create/upgrade/delete 可恢复、可回滚、无跨 Agent 影响或残留 |
| GATE-04 capability/read | done | LOOP-01、TOOL-01、PERM-01、CMP-01、READ-01、SEARCH-01 完成，Worker 权限未扩大 |
| GATE-05 context | done | CTX-01 至 CTX-04、CMP-02 至 CMP-05、MODEL-01 完成；projection 可复现、summary/overflow recovery 可恢复且 canonical transcript 不变 |
| GATE-06 tasks/sub-agents | done | TASK-01、SUB-01 和纳入范围的执行能力完成；取消/崩溃/重启/删除无 orphan |
| GATE-07 release | done | GATE-01 至 GATE-06 和 REL-01 完成；首发 scope 的平台、容量、安全、备份恢复和运维证据通过 |

依赖只决定“可以完成”的顺序，不禁止安全设计、评测和测试脚手架并行；唯一推进顺序
由总状态表中的顺序与 `depends_on` 共同确定。

### 所有 work item 的统一完成门槛

每一项在标记 `done` 前都必须满足：

1. 权威 contract/API/event schema、状态所有者、禁止拥有项、迁移/回滚或恢复行为已定义；
2. 输入、输出、时间、并发、内存、进程、文件、磁盘和 retention 上限有明确数值与
   fail-closed 语义；
3. cancel、deadline、Gateway/Worker/executor 崩溃和 partial side effect 的收敛行为已测；
4. 相关负面矩阵通过，normal/force stop 和 Agent delete residual audit 无残留；
5. 磁盘写放大有实测，增长只与新增语义数据近似线性，不逐 token/chunk/line flush；
6. 最小测试、完整 `pytest`、`./governance.sh` 和受影响时当前受支持真实模型的
   纵向流程通过；
7. architecture/security/event/Capsule/README/CLAUDE 中受影响的权威文档与本计划同改；
8. 未验证平台和 production 声明没有被扩大。

所有有外部副作用的 capability 还必须遵守统一恢复原则：

```text
durable intent → at-most-once automatic dispatch → durable outcome | outcome_unknown
```

本地 journal 不能证明 exactly-once。dispatch 前必须持久化 executor/runner/cgroup 等
可核验身份，并在适用时通过受信 start handshake 释放执行；intent 与 outcome 之间崩溃
标记 `outcome_unknown`，它既可能“未执行”也可能“已执行但未记录”，重启后不得自动
重放。只有能用幂等键和外部事实证明安全的专用操作，才可设计显式 reconcile；模型
重试、SSE 重连或普通 Run 恢复本身永远不是重复执行授权。

### QE-01 — 逻辑 QueryEngine 基线

Authority：[架构](../design/architecture.md)和本计划 Implemented baseline。

- [x] [架构中的 QueryEngine contract](../design/architecture.md)已实现：Conversation-scoped
  canonicalization、窄 ownership、生命周期和禁止拥有项保持单一权威。
- [x] 身份串线、取消/删除竞态、关闭、删除后 handle 失效和重启懒恢复有自动测试。
- [x] 当前真实 lifecycle 复验覆盖同 Engine 多 Turn、重启恢复和删除收敛。

证据：[实现](../../src/agent_builder_v2/query_engine.py)、
[测试](../../tests/test_query_engine.py)和 Current validation evidence。

### QUA-01 — Clean-root、平台和 SSD 资格

Authority：[P1-P8](../PRINCIPLES.md)、[README](../../README.md)和
[安全边界](../../SECURITY.md)。

- [x] 当前 `x86_64` checkout 完成受管 start/health/真实 Run/stop 基础复验。
- [x] 空依赖缓存 cold-checkout 完成 bootstrap/start/Run/stop，并证明受管路径全部留在
  cold checkout；运行时没有项目级 checkout 外写入。
- [x] lifecycle 并发、启动回滚、stale/伪造 PID、orphan 和强制停止矩阵通过。
- [x] 首发支持矩阵冻结为原生 `x86_64` 并留下 host/kernel/model qualification；`aarch64`
  明确移出首发支持范围，原生等价 RR 是未来加入条件，不用 QEMU 冒充。
- [x] 建立代表性 N-turn、取消、失败、重启、删除 workload 的 load/soak 和磁盘增长基线。
- [x] 记录 logical/allocated bytes、WAL/log/cache/temp 峰值、fsync 次数和终态残留上限。

证据：Current validation evidence、[qualification contract](../design/qualification.md)、
[lifecycle identity tests](../../tests/test_lifecycle_identity.py)和
[qualification tests](../../tests/test_qualification.py)。首发 qualification scope 决议只支持
GNU/Linux `x86_64`；当前宿主没有 `/dev/nvme*`、`smartctl` 或 `nvme` 管理接口，故所有 RR
保留 `ssd_smart_not_observed`，只关闭应用写放大 gate，不声称物理 NAND/设备健康。

`RR-QUA-20260720-04/05` 在完整 stop/start 两侧各完成 16 个真实 completed Turn、1 cancel、
1 admission reject 和 2 个删除，共 32/2/2/4；两轮 duration 20.447/17.335 秒，Gateway
write_bytes 4,407,296/4,022,272，syscw 2314/2241，state logical growth
3,414,232/3,922,272 bytes，WAL logical peak 4,128,272/3,922,272 bytes，temp after growth、
Run roots、Worker PID 和 API residual 均为 0。`RR-QUA-20260720-07` 在当前源码显式插桩
模式再完成相同 16-Turn workload：17.341 秒、Gateway write 3,993,600 bytes、syscw 2219、
36 次成功 preloaded-libc `fsync`，17 个 Worker image 全部登记；完整 stop 后 supervisor/
Gateway 合计再有 6 次，生命周期总计 42，另外五类 sync symbol 均为 0，无 slot/registration
失败。

`RR-QUA-20260720-02/03/06` 因测试/浏览器的可再生 tmp symlink 在 workload 前 fail closed，
没有 API side effect；目录被可恢复地隔离到 test-results 后才运行通过候选。04 后首次启动
还捕获一次 child identity publication 前退出；确认 PID/PGID/20815 均不存在后保留记录，
并把 supervisor 加固为 1 秒有界 identity 收敛、失败先 reap/capture child，再仅按精确 bytes+
inode 删除自身 incomplete record。专项 lifecycle/log-supervisor 回归通过，随后普通、插桩、
恢复普通三次启动和两次完整停止均成功。TASK/EXEC/EXT/SKILL/SUB 的真实纵向 RR 与删除零
残留共同构成代表性 P2 workload；指标仍不得解释为 SMART 或物理 flush。

### REC-01 — Durable replay、snapshot 与恢复语义

Authority：[event protocol](../design/event-protocol.md)和[架构](../design/architecture.md)。

- [x] 定义旧 Run durable replay API、oldest/latest cursor、availability 和明确 gap marker。
- [x] 定义有版本的 snapshot 与幂等 UI projection；ephemeral delta 丢失不伪装成完整回放。
- [x] 对 journal prune、不可写、损坏和每个事务/发布崩溃点建立可验证失败行为。
- [x] 建立通用 operation intent/outcome ledger、幂等 identity 和 `outcome_unknown` 语义。
- [x] context/token/cost usage 的记录、redaction、retention 和查询均有硬上限。
- [x] 重启、SSE 重连和重复请求不会重复 terminal、权限决定或未来文件/进程副作用。

证据：[event protocol](../design/event-protocol.md)、[strict replay/projector](../../src/agent_builder_v2/replay.py)、
[journal](../../src/agent_builder_v2/state.py)、[conversation recovery](../../src/agent_builder_v2/sessions.py)、
[replay tests](../../tests/test_replay.py)、[journal tests](../../tests/test_event_journal.py)、
[session crash tests](../../tests/test_sessions.py)、[Control Plane failure tests](../../tests/test_control.py)
和 Current validation evidence。operation ledger 是未来副作用 capability 的恢复 substrate；
当前只读 echo 不产生 privileged side effect，本项不宣称 exactly-once。

### AGT-01 — 通用 Agent Capsule 生命周期

Authority：[Agent Capsule design](../design/agent-capsule.md)。

- [x] 持久 Agent registry 和 generation/version 状态机支持 create/list/get/upgrade/delete。
- [x] provisioning 仅使用 checkout-local、allowlisted、binary-only 依赖和 Agent 专属环境。
- [x] upgrade 使用 staging、资格检查、原子 promotion 和 rollback；旧 generation capability 失效。
- [x] delete 实现 draining → process proof → data/runtime/env cleanup → residual audit。
- [x] create/upgrade/delete 在每个 staging、rename、registry commit 崩溃点可幂等收敛。
- [x] 删除清理 Engine、Conversation、approval、Task、queue、env/cache/log/WAL/SHM/lock
  等完整资产清单，且其它 Agent 的目录、进程和 journal 不变。

证据：`agents.py`、`agent_runtime.py` 与 `capsule.py` 已贯通持久 registry、按 generation
惰性 runtime、共享有界 Broker、Agent-scoped session/Run/context API、drain/upgrade/delete；
`tests/test_agents.py`、`tests/test_agent_runtime.py` 和 Capsule/lifecycle tests 覆盖 provisioning、
promotion、旧 generation retire、active commit、runtime-tree delete 中断恢复，旧 generation
失效、跨 Agent 不变、live cwd 引用与 residual fail-closed。2026-07-20 真实模型 smoke 在新
Agent 完成 3 次 provider 调用/2 次 Tool 调用，升级至 generation 2 后恢复原会话，删除返回
204 且 data/runtime root 均不存在。独立 lifecycle 写入样本：create 的 data/runtime 分配量
为 16,384/73,728 bytes，upgrade 后为 16,384/81,920 bytes，delete 后两 root 均不存在；
没有逐 token/chunk 写入或 delete VACUUM。不存在的 approval/Task/queue 资产为零，当前完整
资产由 Capsule roots、registry row 和惰性 runtime map 覆盖。正式跨平台/长期 SSD 资格仍由
QUA-01 追踪。

### LOOP-01 — 有界多步 Agent loop substrate

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

- [x] 每次 submit 固化不可变 TurnRuntimeSnapshot：Agent generation、model profile、当前
  canonical tool manifest/digest、ContextPlan、max turns/tool calls、usage budget 和 deadline。
- [x] 将当前 one-shot 路径提炼为通用、有界、顺序执行的多步状态机；动态 EffectiveToolSet
  与 permission wait 由 TOOL-01/PERM-01 在此 substrate 上启用，不反向成为本项依赖。
- [x] 多 Tool call 保持 call/result identity；基础 loop 一律顺序执行，未来只有经
  TOOL-01 明确声明 concurrency-safe 的工具才可并行。
- [x] 每次 provider call 都重新做完整 transcript admission，并校验 usage 和 terminal。
- [x] model、现有 bounded Tool、cancel、deadline 和 provider failure 均收敛到唯一终态。
- [x] 真实模型完成至少两次 Tool 决策再回答；没有第二套 graph、loop 或隐藏持久状态。

证据：`runtime.py`/`contracts.py` 固化 snapshot 与数值预算；`kernel.py`、`ollama.py`、
`control.py` 和 `replay.py` 以 `sequential-multi-tool-v1` 实现并验证两次顺序 call/result、
预算耗尽后 ToolSet 收窄、逐调用 admission/usage 与旧协议兼容回放。单元/集成覆盖见
`tests/test_runtime.py`、`test_kernel.py`、`test_ollama.py`、`test_worker_integration.py`、
`test_replay.py` 和 Control failure/cancel tests。2026-07-20 在 20815 的真实
`qwen3.5:2b` smoke 对默认 Agent 和新建 Agent 均得到 3 次 provider request、2 次唯一
Tool call 和单一 `run.completed`；全量测试与治理结果见本轮验证记录。

### CTX-01 — Prompt section registry

Authority：[架构](../design/architecture.md)和[安全边界](../../SECURITY.md)。

- [x] 建立稳定有序的 Prompt section provider registry，记录 trust、provenance、依赖
  digest、独立 budget、cache scope、truncation 和 renderer version。
- [x] 平台安全 contract 永不可被 browser、Agent、workspace 或 Worker replace。
- [x] 受信平台/Agent/Tool contract 位于动态 workspace/session/Git/history section 之前；
  受信 instruction 保持独立 role/section，绝不伪装成 synthetic user content。
- [x] 相同 source/history/profile/generation/policy 产生字节一致的 plan 和 digest；
  单一依赖变化只失效相关 section。
- [x] section cache 仅为有界内存优化；当前 Ollama 不支持的 cache API 不用伪 marker 模拟。

证据：`PromptSectionRegistry` 以 sealed platform/Agent providers 和稳定顺序装配
conversation window/history/user section，记录 dependency digest、独立 budget、cache scope、
truncation reason、renderer/registry version；cache 固定最多 32 项。确定性、单依赖失效、
顺序、角色和不可替换平台 contract 由 `tests/test_context.py` 覆盖；Web exact inspection 与
真实 provider request 使用同一不可变 ContextPlan。

### CTX-02 — Authenticated context inspection

Authority：[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和
[event protocol](../design/event-protocol.md)。

- [x] 提供 typed、authenticated context inspect，展示 section identity/source/digest、
  bytes/token estimate、history projection、budget 和 truncation reason。
- [x] 默认不回显正文、secret、hidden prompt 或可用于重建它们的高精度片段；诊断提升
  必须走独立受信 operator policy、审计和有界 redaction。
- [x] inspect projection 与 `run.started` metadata 和实际 provider request digest 可对账，
  但浏览器不能提交或覆盖任何 provider-bound section。
- [x] active Run、历史 Run、restart、deleted Agent 和 foreign Conversation 的授权、404/
  redaction 语义有负面测试，查询与响应大小有硬上限。

证据：prototype alias 与 Agent-scoped inspect 都要求认证、`no-store`、拒绝 query 正文提升；
驻留 Run 返回现场重验的 exact typed 元数据，重启/retention 后只返回严格 replay summary。
提升默认 404，显式 policy 另需 same-origin、CSRF 和独立 256-bit operator token；受信 section
永久 withheld，其它 excerpt 每 section 最大 2048 bytes 并做 credential redaction，审计先于
响应且最多 4096 行。负向测试见 `tests/test_web.py`、`test_context.py`、
`test_context_audit.py`。2026-07-20 真实 20815 验证 exact registry version、脱敏、审计绑定；
恢复默认启动后 reveal 为 404。

### CTX-03 — Workspace CLAUDE.md

Authority：[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [x] 首版只从当前 Agent Capsule 内精确的 `workspace/CLAUDE.md` 加载，不向上遍历、
  不读取 HOME、不支持 checkout 外 include。
- [x] regular-file、no-follow、containment、owner/mode、UTF-8、硬字节上限和 stable-read
  digest 全部 fail closed；缺文件是确定性 no-op。
- [x] workspace instruction 只能形成独立 trust section，不能伪装 platform/system contract。
- [x] oversize、非 UTF-8、symlink/hardlink、rename race、跨 Agent 和删除中 Capsule
  都不会接受 Run 或泄露其它路径。

证据：`workspace_context.py` 的 descriptor-anchored 32 KiB stable read 与
`tests/test_workspace_context.py` negative matrix；`context.py` 的独立 workspace trust provider；
`RR-CTX-TOOL-20260720-01` 真实 private/unsafe/missing 三路径。

### CTX-04 — Bounded Git/date/environment context

Authority：[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [x] Git collector 使用固定 executable identity、固定 Capsule workspace cwd、clean env、
  禁用 optional lock/pager/hooks/user config，并限制子命令、时间、输出和提交数。
- [x] date/timezone 与环境 section 只来自受信 Control Plane allowlist；不复制继承环境、
  secret、host path 或高基数机器信息。
- [x] 每个 section 记录 source digest/provenance；Git、branch、commit message 和文件名
  一律按 untrusted project data 渲染，不得提升为 instruction。
- [x] non-repository、detached/unborn、恶意 config/ref/message、超时、输出洪泛、rename/
  delete race 和跨 Agent 路径均确定性收敛且不扩大 Run 权限。

证据：`workspace_context.py`/`git_probe.py` 固定 `/usr/bin/git`，Git log/commit message
数量固定为零，status 总输出 16 KiB、2 秒；helper 在 exec 前进入只读 Landlock。
`tests/test_workspace_context.py` 覆盖 non-repo、unborn、parent discovery、symlink、外部
config include、洪泛和 allowlisted environment；`RR-CTX-TOOL-20260720-01`。

### TOOL-01 — Tool contract v2 与 EffectiveToolSet

Authority：[架构](../design/architecture.md)、[event protocol](../design/event-protocol.md)
和[安全边界](../../SECURITY.md)。

- [x] 扩展受控 schema vocabulary、结构化 result/progress、风险、只读性、并发、超时、
  cancellation、结果 trust/source 和版本语义。
- [x] 明确 ToolCatalog → Policy Resolver → EffectiveToolSet → Broker/Executor 边界。
- [x] provider 暴露、ContextPlan、Worker 校验和 executor 使用同一 canonical manifest/digest。
- [x] 工具在模型暴露前按 policy 过滤，但每次调用仍重新校验 identity、schema、语义和权限。
- [x] unknown/duplicate tool/provider、contract drift、foreign/replayed/out-of-order result、
  oversize frame 和调用次数超限全部拒绝。
- [x] ToolUseContext 不成为跨信任边界的 ambient authority；只传窄 capability/reference。

证据：`tools.py` 的 v1/v2 canonical manifest、Catalog/Policy/EffectiveToolSet、窄
ToolUseContext 与同源 Registry；`runtime.py` 固化 catalog/policy/toolset digest；Worker 仅按
有效 ID 从 sealed catalog 重建并对账。`test_tools.py`、`test_worker.py`、broker/kernel/control/
replay negative suites 与 `RR-CTX-TOOL-20260720-01`；历史 v1 digest 有显式 replay test。

### PERM-01 — Capability Broker 与 permission 状态机

Authority：[安全边界](../../SECURITY.md)和[event protocol](../design/event-protocol.md)。

- [x] 文件、runner、MCP 和 Skill 等 privileged work 由 Control Plane/独立 executor
  执行，主 Worker 仍无 socket/fork/exec/任意写。
- [x] capability 绑定 agent_id、generation、conversation/run/call、toolset/policy/args/
  preview digest 和 expiry；upgrade/delete 后旧 capability 永久失效。
- [x] policy 支持 deny precedence 和 `allow|ask|deny`；pending queue、TTL 和容量有硬上限。
- [x] one-shot approval 绑定规范化参数与准确 preview，由认证/CSRF Web action 决定；
  模型、Worker 和浏览器请求体不能伪造内部 approval/result/identity。
- [x] cancel、delete、restart、expiry、policy revision、approve/deny race 和无交互
  sub-agent/background 场景默认拒绝且零 executor side effect。
- [x] permission requested/resolved 与 operation intent/outcome 可 durable replay，但批准本身
  不会因重放再次执行副作用。

证据：[Capability Broker](../../src/agent_builder_v2/permissions.py)、
[持久状态与审计](../../src/agent_builder_v2/sessions.py)、
[Web 边界](../../src/agent_builder_v2/web.py)、
[权限/竞态测试](../../tests/test_permissions.py)、[Web 负面测试](../../tests/test_web.py)和
Current validation evidence 的 `RR-PERM-20260720-01`。

### CMP-01 — Tool-result budget 与 micro-compaction

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

- [x] 每种 Tool 定义 request/result/provider-projection 硬上限和明确 truncation policy。
- [x] 超长结果替换为有界 placeholder/reference，保留 call ID、原始 bytes、digest 和
  truncated reason，不破坏 tool_use/tool_result 配对。
- [x] micro-compaction 优先清理低价值 Tool payload；canonical transcript/event 不重写。
- [x] artifact/output retention、单 Run/Agent 总量和清理策略明确，无逐 chunk 落盘。
- [x] 每次 provider call 在替换后重新 admission；仍超 hard budget 时 fail closed。
- [x] 同一 canonical source 和 policy 重建出相同 projection 与 digest。

证据：[ToolSpec/projection](../../src/agent_builder_v2/tools.py)、
[provider admission](../../src/agent_builder_v2/ollama.py)、
[Control Plane 分流](../../src/agent_builder_v2/control.py)、
[Tool 测试](../../tests/test_tools.py)、[Ollama 测试](../../tests/test_ollama.py)、
[进程集成测试](../../tests/test_worker_integration.py)和 Current validation evidence 的
`RR-CMP01-20260720-01`。

### CMP-02 — Durable projection boundary 与 snapshot

Authority：[event protocol](../design/event-protocol.md)和[架构](../design/architecture.md)。

- [x] projection boundary/snapshot 绑定 conversation revision、history/model/profile/
  instruction/toolset/policy digest 和 renderer version。
- [x] canonical completed transcript 保持 append-only；snapshot 只描述模型视图。
- [x] snapshot 不匹配任何绑定字段时拒绝复用并确定性重算，不“尽量恢复”陈旧视图。
- [x] crash/restart 在上一个 durable boundary 恢复；preserved recent segment identity 可验证。
- [x] replay、manual compact 和后续 semantic summary 使用同一 boundary protocol。
- [x] snapshot/prune/retention 的磁盘上限和增长斜率有测试。

证据：[boundary codec](../../src/agent_builder_v2/context_projection.py)、
[SQLite 原子状态](../../src/agent_builder_v2/sessions.py)、
[binding/重算测试](../../tests/test_context_projection.py)、
[进程/重启/CAS/删除测试](../../tests/test_worker_integration.py)和 Current validation evidence 的
`RR-CMP02-20260720-01`。

### READ-01 — 安全文件元数据与文本读取

Authority：[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

状态：`done`（2026-07-20）。

- [x] 首版提供 workspace-relative `file/stat` 和 `file/read_text`，限定 offset/line/
  byte/time，结果带 path identity、content digest 和 truncated 标志。
- [x] 使用 descriptor-anchored containment：可用时采用 openat2-style BENEATH、
  NO_SYMLINKS/NO_MAGICLINKS/NO_XDEV；否则使用逐组件 dirfd + no-follow 的等价验证，
  若 host 无法提供等价原语就禁用该 capability，绝不退化为 path-based 普通 open。
- [x] 只接受符合 owner/mode 策略的普通 UTF-8 文件；hardlink、FIFO、socket、device、
  sparse/growing/oversize/binary 和特殊 proc 路径 fail closed。
- [x] read receipt 可供后续 mutation CAS 使用；文件内容始终是 untrusted Tool data。
- [x] 真实模型完成 read → reasoning → answer，且 checkout/其它 Agent 零读取、零写入。
- [x] read-only workload 除规定的 durable semantic event 外不产生文件内容日志或临时索引。

证据：[读取执行器](../../src/agent_builder_v2/file_read.py)、
[runtime Tool 合约](../../src/agent_builder_v2/tools.py)、
[Control/Worker IPC](../../src/agent_builder_v2/control.py)、
[负面矩阵](../../tests/test_file_read.py)、
[真实进程闭环](../../tests/test_worker_integration.py)和 Current validation evidence 的
`RR-READ01-20260720-01`。

### SEARCH-01 — 有界 Glob/Grep

Authority：[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

状态：`done`（2026-07-20）。

- [x] Glob/Grep 不通过 shell，固定 workspace root，并限制 depth、entries、files、bytes、
  matches、result bytes、regex complexity 和 wall time。
- [x] 遍历与打开复用 READ-01 的 descriptor-anchored primitive，不先收集未经验证的路径
  再二次打开；不具备等价 containment 的 host 上 fail closed。
- [x] 遍历拒绝 symlink/magiclink/xdev/cycle、特殊文件和其它 Agent/runtime/data root。
- [x] 结果稳定排序，包含 provenance/truncated reason，恶意换行文件名不能破坏协议/UI。
- [x] deep/wide tree、pathological glob/regex、output flood、cancel 和 Capsule delete race
  均有负面测试，不创建无界磁盘索引。
- [x] 真实模型完成 search → bounded read → answer 的同一 Run 闭环。

证据：[搜索执行器](../../src/agent_builder_v2/file_search.py)、
[Tool 合约](../../src/agent_builder_v2/tools.py)、
[负面矩阵](../../tests/test_file_search.py)、
[同 Run 集成](../../tests/test_worker_integration.py)和 Current validation evidence 的
`RR-SEARCH01-20260720-01`。

### CMP-03 — Deterministic context-collapse projection

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

状态：`done`（2026-07-20）。

- [x] 定义无模型调用的有序 collapse layers：低价值 Tool payload projection、已完成旧
  Turn group placeholder/collapse、保留最近完整 Turn 与 Tool call/result identity。
- [x] trigger、target、hard input、output reserve 和各层 budget 全部从受信
  ModelProfile/ContextPolicy 推导，不按模型名或某个固定窗口/比例建立分支。
- [x] 相同 canonical source、projection boundary、profile 和 policy 产生字节一致的模型视图；
  canonical transcript/events 保持 append-only。
- [x] 从当前 tail-window 迁移时显式版本化 renderer/snapshot；旧 snapshot 不静默套用新规则。
- [x] 每层后重新 admission，所有可省略层耗尽仍超 hard budget 时 fail closed，不截断
  instruction、user input 或 tool_use/tool_result 配对。

证据：[collapse projection](../../src/agent_builder_v2/context_collapse.py)、
[ContextCompiler](../../src/agent_builder_v2/context.py)、
[projection boundary](../../src/agent_builder_v2/context_projection.py)、
[确定性/负面测试](../../tests/test_context_collapse.py)和 Current validation evidence 的
`RR-CMP03-20260720-01`。

### CMP-04 — Semantic summary 与 autocompact

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

状态：`done`（2026-07-20）。

- [x] 只有 deterministic collapse 的质量/容量基线完成且 summary 评测门槛通过后，才启用
  semantic summary；summary Run 使用空 ToolSet 且不能产生 privileged side effect。
- [x] summary 绑定 source Turn IDs/digest、model/profile/prompt/policy/renderer version，结构、
  输入、输出、时间、重试和持久化大小均有硬上限，并保留最近完整 pairs/Tool identities。
- [x] manual compact 与 autocompact 使用 CMP-02 的同一 durable boundary；重启可复用匹配
  snapshot，不匹配时确定性重建，不修改 canonical transcript。
- [x] summary failure/timeout/cancel 沿用上一个有效 projection；有限重试和熔断不会形成
  immediate compact loop，也不会阻塞显式 cancel/delete。
- [x] 以关键事实、决定、未完成任务、文件状态、引用准确性和 prompt-injection resistance
  评测至少两个不同窗口 profile，退化时可关闭并回滚到 CMP-03。

证据：[summary contract](../../src/agent_builder_v2/semantic_summary.py)、
[ContextCompiler](../../src/agent_builder_v2/context.py)、
[空 Tool 模型调用](../../src/agent_builder_v2/ollama.py)、
[v2 boundary](../../src/agent_builder_v2/context_projection.py)、
[双画像/安全测试](../../tests/test_semantic_summary.py)、
[Broker 熔断测试](../../tests/test_ollama.py)和 Current validation evidence 的
`RR-CMP04-20260720-01`。

### CMP-05 — One-shot reactive overflow recovery

Authority：[架构](../design/architecture.md)和[event protocol](../design/event-protocol.md)。

状态：`done`（2026-07-20）。

- [x] 对 provider 的真实 context-length/media overflow 分类；认证、网络、格式和其它错误
  不冒充 overflow，也不触发压缩重试。
- [x] 每个 provider call 最多一次 recovery：先重做受信 profile 下的 cheap projection，
  必要时复用/生成一个符合 CMP-04 的 summary，然后重新 admission 并发起一次 provider call。
- [x] recovery identity 和 attempt 进入 durable boundary/terminal；Gateway/Worker 重启、SSE
  重连或模型再次请求 Tool 都不能重复既有 Tool/文件/进程副作用。
- [x] recovery 仍超限或再次 overflow 时收敛为 canonical failure，并记录 estimator/profile
  偏差供重新 qualification；不循环缩短、不静默丢失当前 user input。
- [x] cancel、deadline、provider partial stream、summary failure 和换模型竞态有故障矩阵。

证据：[Broker 与分类](../../src/agent_builder_v2/ollama.py)、
[Control Plane](../../src/agent_builder_v2/control.py)、
[provider ledger/recovery](../../src/agent_builder_v2/sessions.py)、
[strict replay](../../src/agent_builder_v2/replay.py)、
[Broker 负向测试](../../tests/test_ollama.py)、
[真实 Worker/重启集成](../../tests/test_worker_integration.py)和 Current validation evidence 的
`RR-CMP05-20260720-01`。

### WRITE-01 — Diff-bound atomic Edit/Write

Authority：[安全边界](../../SECURITY.md)、[event protocol](../design/event-protocol.md)和
[Agent Capsule design](../design/agent-capsule.md)。

状态：`done`（2026-07-20）。

- [x] Edit 与 Write 是不同 Tool：Edit 要求精确唯一匹配；Write 明确 create/full-replace。
- [x] Edit/full-replace 需要现存 target 的完整 read receipt；create 使用独立的“target absent
  + parent directory identity”receipt，并以 no-clobber commit 保证不会覆盖竞态新文件。
- [x] receipt 绑定 agent generation、canonical target identity/content digest，以及审批时的
  准确 diff/preview digest；所有路径操作复用 READ-01 的 descriptor-anchored primitive。
- [x] Broker 按 canonical target 串行化 mutation，并在同一 conditional commit boundary 内
  重验 receipt/CAS；使用同目录 private temp 和 fail-closed atomic/no-clobber rename，atomic
  失败不跟随 symlink、不原地 fallback。
- [x] intent/approval/temp/write/sync/rename/outcome 各崩溃点遵守统一 at-most-once dispatch；
  rename 已发生但 outcome 未 durable 时标记 `outcome_unknown`，重启不自动重放，残留 temp
  有界回收。
- [x] 单文件、单调用、单 Run mutation/diff/output bytes 和 fsync 次数有量化上限。
- [x] 用户批准的 diff digest 与最终文件 digest 对账；所有失败点都只能留下旧内容或完整的
  已批准新内容，绝不留下 torn/out-of-scope write；commit 后确认不明可呈现
  `outcome_unknown`，但不能自动重做。

证据：[mutation executor](../../src/agent_builder_v2/file_write.py)、
[Capability Broker](../../src/agent_builder_v2/permissions.py)、
[Control Plane integration](../../src/agent_builder_v2/control.py)、
[原子性/竞态矩阵](../../tests/test_file_write.py)、
[真实 Worker/审批集成](../../tests/test_worker_integration.py)和 Current validation evidence 的
`RR-WRITE01-20260720-01`。

### EXEC-01 — Allowlisted argv runner

Authority：[安全边界](../../SECURITY.md)、[event protocol](../design/event-protocol.md)和
[Agent Capsule design](../design/agent-capsule.md)。

状态：`done`（2026-07-20）。

- [x] 首版只执行固定 project-local executable identity + argv，`shell=False`、固定 cwd、
  clean env/PATH，无 shell rc、用户配置、继承 secret 或请求级 executable。
- [x] 使用独立更细 runner sandbox；主 Worker 权限不变，默认无网络、无 checkout/
  workspace 外读写和无未授权 package install；整个 workspace 默认只读，每个 command
  仅获得预声明、精确、带 quota 的输出目录，源文件 mutation 只能走 WRITE-01。
- [x] CPU、memory、process、FD、file/output/disk/entry/time 限制明确；执行前必须建立可验证
  descendant container（cgroup/PID namespace 或等价机制），仅有 process group 时 fail closed。
- [x] durable intent 在 dispatch 前记录 executor/runner/descendant-container identity，并通过
  受信 start handshake 释放执行；spawn/exit/outcome 不明时不自动重跑。
- [x] cancel/timeout/stop/delete 清理整个 descendant container、pipe/FD、PID record、
  output/temp；setsid/double-fork 不能逃逸，process-group cancel 只作为辅助而非安全边界。
- [x] v1 只支持 foreground；fork bomb、setsid/double-fork、signal ignore、output flood、
  network、PATH/executable swap 和环境注入负面测试通过。
- [x] 真实模型可运行 allowlisted test/build 命令并把有界结果回流同一 Run。

证据：[command catalog/executor](../../src/agent_builder_v2/command_exec.py)、
[singleton payload](../../src/agent_builder_v2/command_child.py)、
[kernel policy](../../src/agent_builder_v2/sandbox.py)、
[runner 负向矩阵](../../tests/test_command_exec.py)、
[真实 Worker/审批集成](../../tests/test_worker_integration.py)和 Current validation evidence 的
`RR-EXEC01-20260720-01`。

### MODEL-01 — Trusted ModelCatalog

Authority：[架构](../design/architecture.md)、[安全边界](../../SECURITY.md)和
[README](../../README.md)。

状态：`done`（2026-07-20）。

- [x] 建立受信 provider/model catalog；endpoint 只能来自 operator allowlist，不能由
  browser、Worker、Tool 参数或普通会话消息覆盖。
- [x] 每个模型记录并资格检查 native/operational window、output reserve、Tool/streaming
  能力、tokenizer/estimator version 和 generation options 边界。
- [x] 每个 Turn 固化模型快照；只允许在 Turn 之间切换，并从完整 durable transcript
  按新 profile 重编 ContextPlan。
- [x] provider usage 经 Control Plane 校验后进入 bounded conversation/run ledger；
  incomplete usage 不作为完整费用或预算事实。
- [x] 默认模型只由受信 operator 配置和 README 记录；至少用两个不同窗口/能力 profile
  验证动态 projection、切换、回滚和不按 model name 分支。

证据：[ModelCatalog](../../src/agent_builder_v2/model_catalog.py)、
[Ollama qualification](../../src/agent_builder_v2/ollama.py)、
[Turn snapshot](../../src/agent_builder_v2/runtime.py)、
[目录/切换负面测试](../../tests/test_model_catalog.py)、
[双画像 Broker 测试](../../tests/test_ollama.py)、[Web 边界测试](../../tests/test_web.py)和
Current validation evidence 的 `RR-MODEL01-20260720-01`。

### CMD-01 — Slash Command 与 Web 控制面

Authority：[架构](../design/architecture.md)、[README](../../README.md)和
[event protocol](../design/event-protocol.md)。

状态：`done`（2026-07-20）。

- [x] 建立 command registry、稳定 ID/schema、alias、availability、help 和 feature gate。
- [x] Slash Command 在认证 Web 输入边界解析，不作为 user Turn 发送给模型，也不依赖
  模型猜测管理意图。
- [x] 首批提供 status、context、model、compact、permissions、cancel 和 clear；
  修改态保持 CSRF、active Run 冲突和审计语义。
- [x] 命令只调用已有受信服务，不创建第二套 context、permission、model 或 persistence 状态。
- [x] Web 展示 Context budget、compaction boundary、permission、diff、Tool/Task progress
  和完整事件详情；正文/secret/hidden prompt 保持不可见。

证据：[registry/CommandBus](../../src/agent_builder_v2/commands.py)、
[Web boundary](../../src/agent_builder_v2/web.py)、
[Web control UI](../../src/agent_builder_v2/static/app.js)、
[command contract tests](../../tests/test_commands.py)、
[CSRF/no-Turn route tests](../../tests/test_web.py)和 Current validation evidence 的
`RR-CMD01-20260720-01`。

### TASK-01 — Durable background Task substrate

Authority：[架构](../design/architecture.md)、[event protocol](../design/event-protocol.md)
和[Agent Capsule design](../design/agent-capsule.md)。

- [x] 定义 Task identity/state、owner generation、parent Run、intent/outcome、result 和
  bounded output/notification protocol。
- [x] 前后台切换、stop、deadline、restart-interrupted 和 Agent delete 可收敛，无隐式 daemon。
- [x] descendant ownership 使用可验证 process tree/cgroup 或等价机制；PID/PGID 单独不足
  时不开放 background execution。
- [x] output 批量持久化/轮转并有总量和 retention 上限，不逐 stdout/stderr chunk fsync。
- [x] runner crash、Gateway crash、double-fork、zombie、FD/socket/cwd 引用和 orphan
  cleanup 负面测试通过。

证据：`tasks.py`/`command_exec.py`/`capsule.py` 实现 Agent-scoped TaskStore 与
BackgroundTaskManager；状态绑定 generation、Conversation/Turn/parent Run、executor/request
digest，只在 queued/running/terminal 边界提交。每 Agent 最多 4 active/128 retained，
结果 16 KiB、通知 4×4 KiB、终态 7 天；独立 Task root 复用 12 秒、PIDFD、parent-death、
Landlock、NPROC=1 及 fork/exec/network denial singleton。`tests/test_tasks.py` 覆盖持久转换、
容量、真实 runner、取消、重启 interrupted 与 orphan root；command/web/control 负面回归和
全量 `511 passed`。`RR-TASK01-20260720-01` 在受管重启后的 20815 以真实 qwen parent Run
提交固定 Task，通知严格 queued/running/completed，fork/network/exec probe 均被拒，terminal
时 Task root 已不存在；删除 Conversation 后 Task row/通知无残留。治理结果见本轮
Current validation evidence；不扩大到 Bash/Skill/MCP/子智能体。

### EXEC-02 — Bounded Bash

Authority：[安全边界](../../SECURITY.md)和[event protocol](../design/event-protocol.md)。

- [x] 在 EXEC-01 之上增加明确 shell grammar/parser；parse error、unknown expansion 和
  无法证明的结构默认 ask/deny。
- [x] read/search 分类只服务 UI/策略提示，不能作为 sandbox 或只读安全证明。
- [x] command/normalized AST、cwd、env、redirection 和 preview digest 与 one-shot approval
  绑定；无 `dangerouslyDisableSandbox` 或 unconfined fallback。
- [x] substitution、backtick、env assignment、redirection/heredoc/subshell/pipe/glob、
  shell rc、git hook/pager/config 和 package-manager network 有专项策略和负面测试。
- [x] background Bash 只通过 TASK-01；cancel/delete/stop 清除整个 descendant tree 和输出资产。

证据：`bounded_bash.py` 只接受单个 `printf|pwd|true|false` builtin、最多 8×256-byte 参数；
所有 substitution/backtick/env/redirection/heredoc/subshell/pipe/glob/control/newline/rc/git/
package/unknown command 在 executor 创建前拒绝。`command_exec.py` 把 normalized AST digest、
Bash inode/content、固定 `--noprofile --norc -c`、isolated cwd、clean env 和空 redirections
绑定既有 one-shot approval/operation ledger。Landlock 不暴露 workspace/Capsule Python，
seccomp 保持 NPROC=1 与 process/network denial；唯一 exec 用于进入已打开 Bash FD。foreground
沿 `exec/run`，background 只能沿 TASK-01。parser、真实 kernel runner、注入矩阵、Task 和旧
ToolSet replay 测试纳入全量 `528 passed`。`RR-EXEC02-20260720-01` 受管重启后真实 qwen
发起一次 bounded-bash，完整 preview 经 operator 批准，五阶段 capability audit 各一次并精确
回答 `EXEC02-REAL-OK`；同 parent Run 的 background Bash completed，Task root 和删除后的
Conversation/Task 资产为零。治理结果见 Current validation evidence；不宣称任意 Bash。

### EXT-01 — MCP/LSP

Authority：[安全边界](../../SECURITY.md)和[架构](../design/architecture.md)。

- [x] MCP/LSP capability 适配同一 Tool contract、policy、approval、event 和 result budget。
- [x] stdio/local process 按代码执行默认禁用；remote endpoint 固定 allowlist，实施 SSRF、
  DNS rebinding、credential isolation、transport frame/time/concurrency 限制。
- [x] 不把 control-plane token、浏览器 session 或不相关环境变量传给 child/remote。
- [x] connect/disconnect、schema drift、恶意 payload、timeout/cancel/restart 和 Agent delete
  均无旁路、无 orphan、无跨 Agent capability。

证据：`extensions.py` 实现最多 8 个 operator-construction spec 的 MCP/LSP 共用 adapter，
固定 protocol/ID/32 methods/credential-free HTTPS endpoint 与首次 DNS set；dispatch 重解析
必须一致，直接连接 pinned IP 并做 hostname TLS。SSRF、private DNS（仅精确 IP literal 可
显式 allow）、redirect/userinfo/query/fragment/HTTP/stdio/request endpoint 均无启用路径。
request/result/frame 为 8/16/64 KiB，JSON depth/nodes 12/2048，timeout 5 秒、并发 4；严格
校验 JSON-RPC version/id/result xor error。MCP/LSP、rebind、method/schema/frame/cancel、空目录
测试与 capability 分支纳入全量候选；release catalog 为空时 `extension/call` 不进入
EffectiveToolSet。`RR-EXT01-20260720-01` 受管重启后认证 catalog 精确为空，真实 qwen Run
completed 且 `run.started.visible_tools` 不含 extension，删除零残留；外部 transport 的正向
协议由注入 fake endpoint 单元测试覆盖，未伪造公网依赖或扩大 production 声明。

`RR-SKILL01-20260720-01` 完成 versioned Skill：digest-bound 两文件 archive、空 dependency/
capability manifest、Agent-private copied no-pip venv、动态 ToolSet 与 always-ask singleton
execution 已贯通。全量 `541 passed`；受管重启后真实 Skill 经 qwen Tool + operator approval
完成，五阶段 audit 完整，result 证明 fork/network denied；删除后 package、environment、
registry 和 Conversation 均无残留。

### SKILL-01 — Skills 与插件化能力

Authority：[安全边界](../../SECURITY.md)和
[Agent Capsule design](../design/agent-capsule.md)。

- [x] versioned registry、package integrity、safe archive extraction、声明式 capability 和
  Agent 专属环境均有明确 contract。
- [x] 依赖只允许 binary-only allowlist；source distribution/build hook 和运行时任意安装禁用。
- [x] Skill 代码执行使用独立 sandbox、资源/输出/网络 policy，无 unconfined fallback。
- [x] 文件触碰或 prompt 声明不能动态建立 Skill 信任；启用必须来自受信配置/审批。
- [x] install/upgrade/delete/crash 后 environment/cache/process/file residual audit 通过。

证据：`skills.py` 实现最多 16 个 Agent-scoped version registry；SHA-256 archive 12 KiB/
expanded 16 KiB 只允许根级 `skill.json+main.py`，拒绝 traversal/symlink/special/encryption/
compression/CRC/UTF-8/syntax/manifest 漂移。schema v1 要求空 capabilities/dependencies，故首版
binary allowlist 为空且无 pip/sdist/PEP517/runtime install。package 与 `venv --without-pip
--copies` 分别 staging 到 Capsule data/runtime；active execution 拒绝 upgrade/delete，内容、
source、Skill interpreter 和 input 在 approval/dispatch 重验。registry 非空时才为未来 Turn
启用 `skill/run`，始终 ask；singleton Landlock/seccomp/rlimit 拒绝 network/fork/exec/workspace，
source 8 KiB/input 4 KiB/result 12 KiB/wall 12 秒。archive、upgrade、tamper、真实 sandbox、
Web CSRF 与 residual 测试纳入全量 `541 passed`。`RR-SKILL01-20260720-01` 受管重启后安装
真实 Skill，qwen 发起一次 `skill_run`、operator 审批、五阶段 audit 完整，Tool succeeded
且输出证明 fork/network denied；随后 package 与专属 environment 删除，Conversation/Skill
目录和 registry 均为零。不扩大依赖或任意插件声明。

### SUB-01 — Tasks、mailbox 与子智能体

Authority：[架构](../design/architecture.md)、[event protocol](../design/event-protocol.md)
和[Agent Capsule design](../design/agent-capsule.md)。

- [x] parent_run_id、Task 状态、mailbox、result collection 和 brokered message capability
  是显式协议，不引入嵌套 hidden graph。
- [x] 每个子 Agent 使用自己的 Capsule/environment/sandbox；父 Run 只持有有界 capability。
- [x] 全局、每 Agent、每父 Run concurrency/depth/token/cost/wall budget 和循环检测明确。
- [x] cancel/deadline/delete/restart 传播、orphan cleanup 和唯一 terminal 有故障矩阵。
- [x] Web 展示 parent/child timeline 和独立事件详情；普通文本不能冒充跨 Agent message。
- [x] 一个 Agent 无法读取或修改另一个 Capsule，除非持有精确 brokered capability。

证据：`subagents.py` 以 ask-only `agent/delegate` 实现显式 parent Task、最多两条各 4 KiB
mailbox message、独立 child Conversation/Run 与 8 KiB answer collection；全局最多 2 个、
每 parent Run 1 个、depth 1、45 秒 wall，child 仍受自己的模型/Run/Tool budget 约束。父侧
不持有 child workspace、environment、模型 session 或文件描述符。重启将未完成 Task/link
各收敛为一次 `interrupted` 且不重放；取消、父 Conversation 删除、Agent drain 和 stop
传播到 child Run，父删除继续删除 child Conversation，Agent 删除清整个 child Capsule。
Web 从 authenticated Agent-scoped endpoint 读取 link/mailbox，并使用 child Agent-scoped
replay/context API 展示独立 canonical 序列，不从 assistant 文本推断委派。
[专项测试](../../tests/test_subagents.py)覆盖 parent/child identity、双向 mailbox、三条 Task
语义通知、自委派/unknown/depth/oversize 拒绝和重启唯一中断；frontend/web/replay/全量回归
通过。`RR-SUB01-20260720-01` 在受管 20815 上由真实 qwen 发起一次 `agent_delegate`、一次
operator approval，child qwen 返回 `SUBAGENT-REAL-OK`，parent Tool succeeded；child replay
为 complete、context 为 exact。删除 parent Conversation 和 child Agent 后 child Capsule
不存在，无资格数据残留。该记录不授予跨 Capsule 文件访问，也不引入隐藏 graph。

### REL-01 — 首个受支持版本资格

Authority：[README](../../README.md)、[安全边界](../../SECURITY.md)、
[P1-P8](../PRINCIPLES.md)和本计划。

- [x] 冻结首个 release scope；未纳入的 P2 能力明确 deferred，不以空实现冒充支持。
- [x] cold-checkout、支持架构、真实模型、load/soak/chaos、upgrade/rollback、backup/restore、
  stop/delete residual 和 SSD 写放大 gate 全部可重复。
- [x] 明确受信网络/TLS 或 reverse-proxy contract、single-operator 边界和 production
  token lifecycle；当前本地 stop/atomic-rotate/restart 不能替代无中断轮换、审计或
  多用户凭据治理，未实现多用户隔离时保持明确声明。
- [x] bounded/redacted logs、metrics、audit、retention、SBOM/dependency/vulnerability 和
  release artifact/runbook 完成。
- [x] capacity envelope、平台支持矩阵和已知限制有证据；所有权威文档与实际 release 一致。
- [x] 只有 GATE-01 至 GATE-06、冻结 scope 中的所有 work item 和本项其余 checkbox 均通过，
  才关闭 GATE-07 并以冻结的 single-operator 本地支持合同替换早期阶段免责声明。

证据：首发 scope 已冻结在 [release contract](../design/release.md)：`0.2.0` 只支持
GNU/Linux x86_64、受信防火墙网络和 single operator；任意 Shell/请求可控 package/stdio MCP、默认
extension、RAG、TLS/可信反代、多用户/HA、`aarch64` 与长期 soak 均明确 deferred。
`backup.sh`/`restore.sh` 只操作 checkout 内私有 `data/`、`backups/`、staging/recovery；6 个
专项测试覆盖 private round-trip、旧 data 保留、source symlink/hardlink、archive traversal、
digest/link 和外部 archive 拒绝。真实 data 备份
`pre-release-0.2.0-20260720.tar` 为 `0600`/single-link/768000 bytes，同名覆盖被拒绝。

`RR-QUA-20260720-11` 用最终 release gate 在受管 stop/start 间完成 16 个真实
`qwen3.5:2b` Turn、1 cancel、1 admission reject 和 2 个删除验证，17.456 秒 PASS；API/
Run/PID findings 为 0。Gateway `write_bytes/syscw` 增量为 3,964,928/2,223，state logical/
allocated growth 为 3,889,312/3,891,200 bytes，WAL peak 3,889,312 bytes，log growth 78
bytes，cache/temp after growth 均为 0；结论保留 `ssd_smart_not_observed`，不冒充 NAND/
物理健康。此前 QUA-01 的 cold checkout、sync-count RR、Agent upgrade/delete、command、
Task、extension、Skill 与 subagent 纵向 RR 共同关闭 bounded load/chaos/residual 范围。

同一门禁完整 `554 passed`，governance 扫描 13 个 Markdown、11 个 shell、129 个文本文件
通过；`pip-audit==2.10.1` 对 25 个环境 component 报告 0 known vulnerability，并生成
CycloneDX 1.4 SBOM。checkout-local source archive 为 571967 bytes，SHA-256
`222e0c3ac5a5d6b885be45614657b6e3e868fa3d4596d82585919dfebb006552`；manifest 绑定 131
个 source、SBOM、RR 和当前 commit。该轮发生在最终提交前，故显式记录
`source_dirty=true`；外部分发必须按 runbook 在 clean reviewed commit 上重跑 release gate，
且该重跑不得再修改 source。两次 release wrapper 参数契约错误
分别在 workload 前被 argparse 拒绝，失败目录已移入 checkout-local quarantine；新增 EXIT
恢复保证后 Gateway 均保持健康，未把失败记录伪装为 PASS。GATE-01 至 GATE-06 已先关闭，
本项通过后 GATE-07 和全部 28 项同步关闭。

### Follow-on record — Agent-scoped research environment

这不是冻结路线的第 29 项，也不改变上方 28 项的完成口径。2026-07-21 在已关闭 gate 上追加
固定 `research-documents` capability：认证 Agent-scoped API/管理抽屉负责显式安装与删除；
Control Plane 以 checkout-local uv、精确版本、binary-only/no-deps 原子发布 Agent 私有
dependency source/venv，相同 identity 跨 Conversation 幂等复用。只有环境完整有效时，
`document/extract_text` 才进入未来 Turn ToolSet；PDF/DOCX/HTML/Markdown/text 使用
descriptor-anchored snapshot 和现有 singleton Landlock/seccomp runner，运行期无网络、
无 fork/exec、无 workspace/跨 Agent 直接访问，Run staging 在终态删除。

验收证据：全量 `562 passed`；governance 扫描 13 个 Markdown、11 个 shell、132 个文本文件
通过；`pip-audit==2.10.1` 对实际 research site-packages 报告 0 known vulnerability。20815
实机首次安装固定 `lxml/pypdf/python-docx/typing-extensions` 后重复 POST 保持相同
`installed_at`；真实 `python-docx` 文件在 sandbox 中成功提取且 staging residual 为 0。临时
第二 Agent 默认未安装，删除后 data/runtime residual 均为 0，同时系统 Agent 环境保持有效。
当前系统 Agent bundle metadata 16 KiB、可重建 venv 81 MiB；安装按 Agent 显式发生，不按
Conversation/Run 重建，也没有逐 token/chunk 写盘。

### 强制负面测试矩阵

以下矩阵是对应 work item 的退出门槛，不是可选加固：

- **NEG-CAPSULE（AGT-01）**：非法/碰撞 ID、预置 symlink/hardlink/特殊文件、错误
  owner/mode、mount/device 变化、恶意 registry/manifest；create/create、upgrade/run、
  delete/run/approval 并发；PID reuse、伪造 record、foreign live process、leader 已死但
  descendants 存活；每个 staging/rename/commit 点崩溃；删除后完整资产为零且其它 Agent
  hash/mtime/process/journal 不变。
- **NEG-BROKER（TOOL-01、PERM-01）**：unknown/duplicate/schema drift、extra/missing/NaN/
  oversize/foreign/replay/out-of-order；重复、过期、取消、删除、重启、foreign approval；
  args/preview/policy revision 偷换和 approve/deny race；任何失败均零 privileged side effect。
- **NEG-FILE（READ-01、SEARCH-01）**：traversal、absolute/NUL/超深/Unicode 路径、
  symlink/magiclink/hardlink/rename/xdev race、special file、wrong owner/mode、huge/sparse/
  growing/binary/invalid UTF-8；深宽树、cycle、pathological glob/regex、恶意文件名和
  output flood；跨 Agent、取消、超时和 Capsule 删除竞态。
- **NEG-WRITE（WRITE-01）**：stale/partial receipt、零/多匹配、审批后修改/rename/
  symlink/mode race；ENOSPC/EDQUOT/EIO 在 temp/write/sync/rename/dir-sync 各点；每个
  intent/approval/commit/outcome 点崩溃；重复调用、超 mutation/diff 限制、UI XSS/ANSI/
  secret payload；无原地 fallback、无双写、无跨 Agent 变化。
- **NEG-EXEC（EXEC-01、TASK-01、EXEC-02）**：command substitution、backtick、env/
  PATH/loader 注入、redirection/heredoc/subshell/pipe/glob/newline、shell rc、git hook/
  pager/config、package-manager network；socket/ptrace/proc/mount/device/outside read/write；
  fork bomb/setsid/double-fork/zombie/FD leak；CPU/memory/process/disk/output flood、
  timeout/cancel/restart；终态 descendant/output/temp/PID/socket/lock 全清。
- **NEG-SSD-LIFECYCLE（所有新增 capability）**：read-only、mutation、command、取消、
  失败、重启、删除 workload 记录 logical/allocated bytes、process I/O、fsync、WAL/log/
  cache/temp 峰值；连续 N Turn 的增长只依赖新增语义数据；无 delta/provider frame/
  command chunk 逐条落盘，无 delete VACUUM、整树每 Turn 扫描或无法证明的 secure erase。

## Architecture guardrails

- 不重新引入旧系统、LangGraph 或 LangChain；若未来要采用编排框架，必须先证明其
  解决了具体缺口且不会产生第二套状态机、事件序列或不可控持久化。
- 保持 Control Plane 为 identity/sequence/persistence authority，Worker 永不自报身份。
- 保持 QueryEngine 为轻量 Conversation orchestration；不得让它缓存 transcript/event、
  另开 SQLite、拥有 Worker/model session 或形成第二套 agent loop。
- 保持 token-level streaming，但只在语义边界持久化。
- 模型窗口和压缩阈值来自受信 qualification/profile，不按 provider/model 名称硬编码；
  原生容量、运行上限、输出预留和估算器版本都必须进入可审计 plan metadata。
- Provider usage 必须先由 Control Plane 对照 profile 校验后才进入 canonical terminal；
  `complete=false` 必须阻止调用方把部分累计误当成本 Run 完整用量。
- 新能力走显式 broker/capability，不扩大主 Worker 权限。
- 所有资源先定义硬上限和失败语义，再实现 happy path。
- 活动计划、设计、安全和用户文档与实现同改。

推进顺序、当前状态和下一可执行项只以总状态表、`depends_on` 和 Phase exit gates 为准；
不要另建 Now/Next/Later 状态副本。当前 deterministic collapse 不能宣称为语义摘要，也
不能绕过依赖直接加入文件 mutation、Bash、大量 Tool 或多 Agent。

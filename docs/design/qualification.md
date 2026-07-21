---
owner: runtime-maintainers
status: maintained
last_reviewed: 2026-07-20
review_cycle: quarterly
---

# Runtime qualification contract

本文件定义 QUA-01 的可重复本机 workload、资源测量和通过门槛。逐项状态和最终 RR
证据仍只记录在[活动计划](../plans/runtime-rebuild.md)，这里不建立第二份状态账本。

`scripts/qualify_runtime.py` 是本契约的标准库实现。它只验收一个已经由根 `start.sh`
启动的 Gateway；cold checkout、启动故障注入、正常/强制停止、原生 `aarch64`、内核或
设备实际 flush 和 SSD SMART 在 host 授权可用时由外层 qualification 记录；不可用时必须
显式记录观测缺口，不能用应用指标冒充物理设备结论。

## 首发支持平台与物理设备观测

首个 release 的受支持平台冻结为 GNU/Linux `x86_64`、glibc 2.28+、Landlock ABI 6+ 和
seccomp 可用。`aarch64` 代码路径保留但不在首发支持矩阵；只有原生主机完成等价 cold
checkout、真实模型、sandbox、soak 和 lifecycle RR 后才能加入，QEMU/cross build 不能替代。
因此本合同的 release gate 只要求所有**受支持平台**有原生证据，不把未承诺平台写成假通过。

SMART/NVMe health 是部署设备的运维观测，不是可移植应用测试。若 `/dev/nvme*` 与受信只读
工具可用，release/operator RR 应记录前后 Data Units Written、Host Write Commands、
Percentage Used、Available Spare、Media/Data Integrity Errors、Unsafe Shutdowns、Critical
Warning 和温度；若容器未暴露设备或工具，必须记录 `ssd_smart_not_observed`，并以 `/proc`
I/O、logical/allocated growth、WAL/temp/log peak 和显式 libc sync-call 上界关闭软件写放大
gate。后一种结论只表示“未发现应用层不合理写入”，绝不声称测量了 NAND 磨损或设备健康。

## 调用与证据边界

```bash
source ./env.sh
./.venv/bin/python scripts/qualify_runtime.py \
  --rr-id RR-QUA-20260718-01 \
  --implementation-ref worktree
```

脚本固定连接 `http://127.0.0.1:20815`，登录 token 只能从
`.runtime/secrets/web-bootstrap-token` 读取；没有 endpoint、token 或输出目录覆盖参数。
默认 64 位 lowercase-hex token 与通过 `set-access-token.sh` 明确轮换的 10..128 字符
窄 ASCII token 使用同一认证格式验证；读取只容许至多一个末尾换行，拒绝空白、控制字符、
symlink、hardlink、非 owner、非 `0600` 或超限文件。token 仍只进入内存登录请求和
redaction set，不进入 summary、argv、环境或日志。
可用 `--pid label=PID` 增加最多 7 个受测进程，label 只允许短 ASCII 标识。默认总是从
owner-only `gateway.pid` 解析并逐次验证 supervisor → Web Gateway 的 PID/start-marker、
父子关系、同一 PGID、cwd 和 exact argv，再分别测量 `supervisor` 与真正处理
HTTP/SQLite 的 `gateway` 子进程；PID record 只有 supervisor identity、Web child identity
和可选 sync-counter ABI 三者完整时才可作为资格证据。

每次执行创建唯一 `.runtime/qualification/<RR-ID>/summary.json`。已有 RR 目录、symlink、
hardlink、错误 owner/type 或越出 checkout 的路径一律拒绝，记录不会覆盖。summary 使用
原子 no-replace publication，禁止包含 token、cookie、CSRF、Conversation/Turn/Run ID、
用户/模型正文、绝对 checkout 路径或原始异常文本。原始 SSE、HTTP body 和日志不作为
资格产物复制。

## 显式 libc sync-call 计数模式

正常启动不加载动态插桩。仅在独占、fresh Gateway 的资格轮次中可以使用：

```bash
./start.sh --qualification-sync-count
source ./env.sh
./.venv/bin/python scripts/qualify_runtime.py \
  --rr-id RR-QUA-20260718-01 \
  --implementation-ref worktree
./stop.sh
./.venv/bin/python scripts/sync_counter_tool.py report
```

如果 20815 已有普通 Gateway，资格 flag 会拒绝，不会在线改装或重置计数。准备阶段使用
checkout 中的固定 C source 和 host `gcc`，只在
`.runtime/qualification-sync/` 生成 owner-only shared library、digest build record 和一个
固定 **4096 bytes** counter page；先用 RAM-backed memfd 自测所有 wrapper 的成功/失败、
返回值和 `errno`，再把 page 重置为空。构建、自测和重置发生在受测 Gateway 启动前，
不会进入 workload 计数。

`start.sh` 只把固定、经 owner/type/mode/hardlink/symlink/size/digest 校验的 library 和
counter page 传给 log supervisor。supervisor 清洁环境 re-exec 后把角色收窄为 Gateway；
Control Plane 创建 Worker 时再收窄为 Worker。内部 enable/path/role 不接受 HTTP、命令
body、Agent、Worker 或模型输入，且不会进入 Gateway 日志、HTTP/SSE、事件或前端。
qualification summary 只保存按角色聚合的数量，不保存 PID 和绝对路径。

library 对以下动态 libc symbol 分别累计 attempts、successes 和 failures：`fsync`、
`fdatasync`、`msync`、`syncfs`、`sync_file_range`、`sync`。每次调用真实函数后只执行两个
进程共享原子加法并恢复 `errno`；不逐调用 write、log、flush 或 `msync`。counter page 最多
20 个固定 slot，正好覆盖 supervisor、Gateway 和本契约最大 16 completed Turn 加一个
cancel Worker；slot 未完成、溢出、进程覆盖不足、generation 变化或计数回退均 fail
closed。Worker 即使没有发起 sync call，也必须成功注册，才能证明 preload 确实进入其
进程镜像。

应用 qualification summary 的 `libc_sync_calls` 是 workload 前后 delta；停止后读取的
`sync_counter_tool.py report` 才包含 supervisor 启动、Gateway 初始化、workload 和正常
shutdown 的整个已插桩进程生命周期。两者都不包含 `start.sh`/bootstrap 本身或显式清空
环境的 Capsule `venv` helper；必须按这个覆盖范围命名证据，不能宣称全 host syscall。

该指标的准确名称是 **preloaded libc symbol calls**。它不观察 direct syscall、static/
hidden libc path、`io_uring`、`O_SYNC|O_DSYNC`、`RWF_DSYNC`、ext4/jbd2 writeback、block
flush 或 NVMe FTL/NAND 写入；真实函数返回与原子递增之间遭遇进程崩溃仍有极小的丢失
窗口。共享页不会主动落盘，但内核可周期性回写这一固定脏页。因此它不能被描述为物理
flush 次数、写入字节或 SSD 磨损。

插桩会增加固定映射和原子操作，并可能改变 SQLite WAL checkpoint 的时序。用于关闭
SSD 写放大风险前，RR 必须另做至少一次 warm-up 和三组相同 bounded workload 的
instrumented/uninstrumented 配对，比较终态、duration、`/proc` IO、WAL/state/log peak，
报告 median/range，不能静默扣除 counter page 开销。应用指标无法替代 host 上
`smartctl`/`nvme smart-log` 的 Data Units Written、Host Write Commands、Percentage Used、
Available Spare、Media/Data Integrity Errors、Unsafe Shutdowns、Critical Warning 和温度；
当前共享 ext4/NVMe 环境还必须处理其它 host workload 的噪声。

## Workload v1

运行前要求 Gateway healthy，模型和沙箱 ready，且没有遗留/并行 Run root。默认 workload
固定为：

1. 前置 `GET /health` 一次并登录；
2. 新建一个 Conversation，顺序执行 **4** 个真实模型 Turn，每个必须唯一
   `run.completed`；`--turns` 只允许 **2..16**；
3. 提交一个超过 Run message 边界但仍低于 HTTP body 边界的请求，必须以 `400` 拒绝且
   不产生 Run；
4. 新建第二个 Conversation，启动一个 Run，立即请求 cancel，必须唯一
   `run.cancelled`；
5. 删除两个 Conversation，并证明其详情和全部 Run event endpoint 均为 `404`；
6. 后置 `GET /health` 一次并注销；
7. 确认没有 Run root、`worker.pid`、资格创建的 API 状态或变化后的 token inode 残留。

单 HTTP/SSE 等待上限为 **75 秒**，完整 workload wall deadline 为 **900 秒**；JSON 响应
最多 **1 MiB**，单 SSE 行最多 **128 KiB**，每 Run 最多读取 **1024** 个 event。响应
redirect、错误 content type、seq gap、错误 Run identity、未知/重复 terminal 均 fail
closed。

## 测量策略

逻辑字节是 regular file `st_size` 之和，实际分配字节是 `st_blocks * 512` 之和；不跟随
symlink，不跨 device，每次扫描最多 **100000** 个条目。state/WAL/log/temp 中的 symlink
直接失败；可复现 package cache 中的 symlink 只以 `lstat` 计入链接自身，绝不读取目标。
以下均记录 before、element-wise
peak、after 和 after-before：

| 类别 | 内容 | 采样 |
| --- | --- | --- |
| `state` | Agent 的 `manifest.json`、`state.sqlite[-wal|-shm]` | 每 0.5 秒 |
| `wal` | 所有 Agent 的 `state.sqlite-wal` | 每 0.5 秒 |
| `logs` | `gateway.log` 和 3 个受管 rotation | 每 0.5 秒 |
| `temp` | `.runtime/tmp` 与每 Agent 的 `runs/` | 每 0.5 秒 |
| `cache` | `.runtime/cache` | 每 15 秒及 before/after |
| `libc_sync_calls` | 显式资格模式下 supervisor/Gateway/Worker 的六类 libc symbol delta | workload 前后稳定 snapshot |

低频 cache 扫描是有意的 SSD/metadata 保护，完整 workload 最多触发 60 次。supervisor、
实际 Web Gateway 及每个额外指定 PID 同时采集 `/proc/<pid>/io` 的 `rchar`、`wchar`、
`syscr`、`syscw`、`read_bytes`、`write_bytes` 和 `cancelled_write_bytes`；Linux start
marker 变化、进程消失或 supervisor/Web 父子链漂移即失败。PID 和 marker 不写入
summary。

## Workload v1 通过门槛

所有门槛必须同时满足；负 growth 可以记录但不能抵消其它指标：

| 指标 | 上限 |
| --- | ---: |
| `state` after logical growth | 8 MiB |
| `state` after allocated growth | 16 MiB |
| `wal` peak logical bytes | 20 MiB |
| `logs` peak logical bytes | 20 MiB |
| `cache` after logical growth | 2 MiB |
| `cache` after allocated growth | 4 MiB |
| `temp` peak logical / allocated bytes | 20 MiB / 36 MiB |
| `temp` after logical / allocated growth | 64 KiB / 64 KiB |
| 每个指定 PID `write_bytes` delta | 64 MiB |
| 每个指定 PID `syscw` delta | 20000 |
| after Run roots / `worker.pid` | 0 / 0 |
| API、Gateway identity、token identity findings | 0 |

这些上限是当前单 Agent、顺序 workload 的资格 envelope，不是未来容量承诺。提高 Turn
并发或改变存储路径时，必须先更新本契约、写放大分析和测试。Task、Skill、extension 与
subagent 另需各自真实纵向 RR，证明语义边界写、终态 cleanup 和零 residual；它们不需要把
不可信 stdout/token chunk 追加到本 workload。

## RR summary schema

summary 顶层稳定字段为 `schema`、`policy`、`rr_id`、`implementation_ref`、`result`、
`recorded_at`、`duration_seconds`、`platform`、`runtime`、`workload`、`metrics`、`thresholds`、
`residual_audit`、`failure` 和 `limitations`。`result` 只有 `pass|fail`；failure 只记录受控
stage/code，不记录异常消息。

普通启动的 summary 明确记录未观察精确 libc sync-call；显式资格模式会用
`metrics.libc_sync_calls` 收窄这一缺口。两种模式都固定保留：未观察内核物理 flush、
direct/non-libc durability path 和 SSD SMART。应用层 summary 通过必须与受支持平台的
cold checkout/lifecycle、当前能力真实纵向 RR 和 stop/restart residual audit 合并评审，
不能单独声称物理 SSD 健康或扩大平台范围。

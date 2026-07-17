---
title: 本地可观测性
---

# 本地可观测性

Agent Builder 通过 OpenTelemetry 输出厂商中立的追踪数据，并采用
OpenInference 属性约定。默认生命周期会启动项目本地的 Phoenix 面板，
SQLite 数据全部保存在 `.runtime/phoenix`。

## 打开追踪面板

点击应用侧栏的“本地追踪”，或在运行 Agent Builder 的主机上访问
`http://127.0.0.1:6006`。面板会按会话关联智能体、模型、工具、检索和
子智能体 Span。

## 隐私与磁盘控制

- 导出前会脱敏凭据和授权字段。
- 属性字符串、集合的大小均有硬上限。
- 进行中的每条 Trace 还有 Span 数量和内存双上限；超限时会保留有界的
  部分 Trace，而不会无限增长。
- 正常 Span 批量导出，不会逐 Token 落盘。错误和慢请求在成功请求的
  有界批处理队列之前走优先导出；正常请求默认按 20% 采样。
- Phoenix 数据默认保留 7 天；达到托管 5 GiB 容量的 90% 后会持续阻止
  新写入，启动时若已达到硬上限也会拒绝启动。

路径、采样率、批大小和保留期均在 `env.sh` 中声明。托管生命周期强制
Phoenix 仅监听回环地址、使用本地 SQLite 和本地 OTLP 端点，并清除继承的
PostgreSQL 选择变量。当整个 Phoenix 工作目录达到配置的告警阈值时，启动
脚本会给出提示。停止整套服务后可清理追踪数据：

```bash
./stop.sh
./purge.sh observability --yes
```

若需同时关闭追踪导出和本地面板，请使用：

```bash
./start.sh --no-observability
```

只有在脱离生命周期脚本单独启动后端时，才通过
`OBSERVABILITY_ENABLED=false` 选择无操作追踪器。

## 导出到其他后端

业务代码不依赖具体面板实现。只有在脱离 `start.sh` 独立托管后端时，才可将
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 指向兼容 OTLP/HTTP 的收集器；官方
生命周期会有意覆盖为本地 Phoenix。远程收集器属于数据出站边界：请求头
凭据只能放在运行时环境变量中，并需审查传输安全、访问控制和保留期。

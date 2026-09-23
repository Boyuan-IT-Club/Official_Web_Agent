# 可观测性规范（日志为纲）

> **何时读**：给任何服务/模块加日志时；排查线上问题**之前**（先确认日志够不够定位）；设计服务形态时。
> **一句话**：可观测性的目标是「不出门就能定位 90% 的问题」——日志结构化、分级、带上下文、全链路串联；只建你真会去看的观测面。

## 日志四律

1. **结构化**：`key=value` 或 JSON，禁自然语言长句；字段名全库一致（`thread_id`、`duration_ms`、`channel`……）。
2. **分级语义**（用途错位 = 分级白做）：
   - `DEBUG` 调试细节与循环体（生产默认关）；
   - `INFO` 关键**业务事实**（一次请求完成、一次推送投递成功、一次状态变更）；
   - `WARN` 可恢复异常与降级（重试生效、回退路径、外部超时但兜住）；
   - `ERROR` 影响业务的失败，**必带堆栈 + 上下文**（`logger.warning(..., exc_info=True)` / `logger.exception`）。
3. **上下文自带**：每条日志带定位 ID（`thread_id`/`user_id`/`request_id`）。入口生成 `request_id` 并全链路透传（HTTP header → service → 外部调用），日志才能串成一条线——这是排障时最值钱的一条。
4. **红线**：禁敏感信息（密码/token/cookie/身份证）；循环与高频路径降 DEBUG + 采样；`INFO` 不许刷屏。

## 各层记什么（对齐 layering.md 四层）

| 层 | 记什么 | 级别 |
|---|---|---|
| 接口层 | 出入口（method/path/status/duration + request_id）；校验失败 | INFO / WARN |
| 应用层 | 业务事实（「推送已投递 channel=feishu」）、事务边界、错误命运决定（重试/降级） | INFO / WARN |
| 基础设施层 | 外部调用（目标、耗时、错误码）；**网络失败必须留痕** | WARN / ERROR |
| 领域层 | 原则上不打日志（纯函数无副作用）；要留痕由上层记 | — |

与错误处理联动（[error-handling.md](error-handling.md)）：catch 了必须 `log` 或 `rethrow` 二选一——既吞又不记 = 事故时的盲区。

## ERROR 日志质量

可行动的 ERROR = 堆栈 + 请求上下文 + 关键入参（脱敏）。`"something failed"` 式 ERROR 是噪音，评审按不合格处理。

## 三支柱起步最小集（防过度设计）

- **日志**（必有，本文件）。
- **指标**：从黄金信号里挑当下真会看的（流量/延迟/错误率/饱和度），起步四条以内。
- **链路追踪**：单体阶段 `request_id` 足够；真拆成多服务再上 trace。
- 判据：每建一个观测面先回答「明天出事时我会打开它吗」——不会就不建。

## 来源

- [12-Factor: Logs](https://12factor.net/logs)（日志为事件流）
- [Google SRE: 黄金信号](https://sre.google/sre-book/monitoring-distributed-systems/)
- 结构化日志共识（pino/zerolog/logback 生态的实践交集）

# 初筛评测线可观测性 Runbook(#183)

一次 AI 初筛(job)在四个观测面共享同一个 **job 级 correlation id**(W3C
32-hex trace id,由 `observability.eval_job_trace_id(job_id)` 从 job_id
确定性派生,同 job 四面可复算、可互查):

| 观测面 | 位置 | 用法 |
| --- | --- | --- |
| 结构化日志 | Agent 服务 stdout,`eval_event stage=... job_id=...` | grep job_id 看阶段时间线 |
| Langfuse trace | metadata.`correlation_id` = correlation id | 按 metadata 搜 trace,看 LLM 调用细节(prompt 版本/token/耗时) |
| 出站 Backend 请求 | `traceparent` 头(服务账号与用户令牌通道都带;span-id 每请求随机,W3C 合法) | 后端侧按 trace-id 段过滤访问日志 |
| 审计 | `agent_audit_log.trace_id` | 合规侧对账;**仅成功 job 的完成审计**带 job 级 id,触发审计带管理员轮上下文 id |

## 结构化事件时间线

每个 job 依次产生(`stage` 维度):

- `started`:`attempts`(第几次执行,1 基)、`model`、`resume_id`
- `scorecard_saved`:`card_version`、`duration_ms`(评分模型耗时)、`prompt_version`、`input_tokens`/`output_tokens`(评分调用)
- `qbank_done`:`status`、`qbank_version`、`duration_ms`;失败时 `error_class`=异常类名
- `succeeded`:`card_version`、`qbank_status`、总 `duration_ms`
- `failed`:`error_class`(异常类名)、`attempts`、`duration_ms`;完整堆栈在服务端日志

PII 纪律:事件行只含 id/阶段/耗时/版本/错误类别;简历原文与用户身份
字段**禁入**(红线见 `src/official_agent/observability.py` 头注)。

## 分钟级定位失败阶段

```bash
# 1. 按 job 拉时间线:哪个 stage 断了、耗时多少
grep "job_id=123" ~/.official-agent/logs/official-agent.log | grep eval_event

# 2. 复算 correlation id(与日志/Langfuse/后端日志对账)
uv run python -c \
  "from official_agent.observability import eval_job_trace_id; print(eval_job_trace_id(123))"

# 3. Langfuse:按 metadata.correlation_id=<correlation id> 搜 trace,
#    看模型调用细节与报错 span(评分线已挂 callbacks 并带 metadata)

# 4. 后端侧:取 trace id 的第 2 段(即整串,格式 00-<id>-000...-01)
#    过滤 Backend 访问日志,确认 Agent 取数/写状态位是否成功

# 5. 审计与用量对账(PG)
#    agent_audit_log.trace_id = <correlation id>
#    agent_conversation_log.thread_id = 'eval:<job_id>'(token 四列)
```

失败模式速查:

| 现象 | 定位 |
| --- | --- |
| 只有 `started` 无后续 | 取数阶段挂(fetch_scoring_fields / 闸门1 归属错位),查后端日志 |
| `failed error_class=...` 反复出现且 attempts 递增到 3 | 模型/评分链路不稳;Langfuse 按 trace id 看 span |
| `qbank_done status=failed` 但 `succeeded` | 评分有效、题库缺失,管理面可见,单独重试题库 |
| job 长期 pending/running 无事件 | 进程重启残留;启动恢复(requeue_stale_all_cycles)会接手,attempts 超限转人工 |

## 告警建议(运维看板)

- `failed` 事件 5 分钟窗口 > N 次或 `attempts=3` 的 `failed`(转人工)即时告警;
- `qbank_done status=failed` 占比突增(题库线降级);
- `duration_ms`(succeeded)P95 突破阈值(模型退化/排队);
- Backend 503 / 5xx 按 traceparent trace-id 关联后端日志共同分析。

## 诚实边界

- correlation id 的锚点在 Agent 侧 job;浏览器→Backend→Agent 的请求级
  透传(前端生成/后端 MDC 落日志)需要 Backend/Frontend 配合接线,
  当前 Backend 侧可按 traceparent trace-id 段过滤(格式见上),
  请求级 MDC 透传列为后续跨仓票。
- Langfuse 未配置时 trace 面缺失(fail-open),结构化日志/审计/后端头不受影响。
- 评分线(run_evaluation)已挂 Langfuse callbacks并以 metadata.correlation_id 关联;
  出题线 bundle 暂未挂 callbacks——其 token 经 conversation_log(eval:{job_id})可见。
- correlation id 截断 32 位后与升级前落库的 64 位历史 id 不可互查(一次性过渡断层)。

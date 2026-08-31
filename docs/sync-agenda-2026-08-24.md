# 同步议程——与原设计者对齐(2026-08-24 晚)

> 背景:架构评审(grill,穷举式追问)会话产出 ADR-0003~0006 与缺漏扫描(docs/gap-scan-2026-08-24.md)。
> 原则:**不单方面改原设计者的代码**——所有涉及骨架代码的清理已冻结,待本晚同步后再动。

## 一、归属地图(谁的东西)

| 内容 | 归属 |
|---|---|
| Agent 仓库骨架(8f532d2 脚手架 + 885a169 文档修正,289 行,含 12 工具桩/router/CI) | 原设计者 |
| CLAUDE.md「Agent skills」块 + Official_Web_Agent/docs/agents/*(tracker/domain 配置) | Zewang 侧(2026-08-24) |
| 工作区根文档仓库(CONTEXT.md 增补 / ADR-0001..0006 / gap-scan / 本文) | Zewang + 架构评审会话;**ADR-0003/0005 含对原设计的变更,尚未与原设计者同步** |
| 65 个 issues(upstream Boyuan-IT-Club) | 原设计者起草 |

## 二、分歧/待同步清单

### A. 需要拍板的分歧
1. **GRA-02 意图分类**:原设计=轻量模型分类→硬分发 4 子图;grill=砍除,A 走
   ReAct 单循环+按身份装配工具集(ADR-0003)。理由:装配已覆盖分类的安全价值,
   每消息多一跳多一份 eval 面,分错即死胡同。router.py 未动。
2. **move_interview 写工具**:原设计 3 写;grill 拟砍 1。依据是后端事实非偏好:
   端点已 deprecated 下线(openapi:「已下线,改用 preferences/assign」)。
   write.py 未动。
3. **checkpointer 存储:Redis(原设计者)vs Postgres(Zewang 倾向)**——对比见附录。
4. **A 是否接飞书(INF-05..07 范围)**:原设计飞书一等入口(M3);Zewang:C 已定
   官网前端,其余入口不一定需要飞书,可能收缩 INF 范围。
5. **B 触发方式(三方提案,不互斥)**:原设计 EVA-07=对话触发+cron;grill=遥控器
   工具(trigger_evaluation / get_evaluation_progress);Zewang 提案=简历导入后
   管理端「开始审核」按钮→自动触发。共识底线:流水线本体固化为 graph+调度队列,
   变的只是触发面(对话/按钮/cron/后端事件),全部汇入同一队列。
6. **记忆路线**:原设计 MEM-01..06 全排期;grill=v1 checkpointer only,MEM-02..06
   等第一个真实需求再启。RAG:倾向 Day1 接口化(RAG 形接口+小语料实现,后端可换)。
7. **EVA-05 批判回炉**:原设计=批判节点;grill=重定义为「证据核验」(代码核依据
   引用真实性+模型核维度矛盾;纯「再想想」自省无新信息)。待解悖论:核验需对照
   原文 vs 原文已脱敏——分工:字符串核验在脱敏文本内,语义矛盾查给模型。

### B. 事实性修正(非偏好分歧,建议直接修;修不修、谁修待同步)
8. 工具↔端点漂移 12 条:幽灵端点×2(get_resume_detail / get_recruit_statistics
   指向不存在或未实现的端点)、方法漂移×4(search_resumes POST→GET、
   handle_reschedule POST→PUT)、签名缺必填 cycleId×2(get_my_interview /
   list_reschedule_requests)、submit_resume_score 的 comment 后端不收
   (PUT /score body 仅 score int)。
9. mcp_server.py docstring 仍写「经 langchain-mcp-adapters 消费」+ pyproject 残留
   该依赖(ADR-0003 已定进程内直连,MCP 纯对外)。
10. README 架构图仍画「意图分类」与记忆全量排期,与 ADR 表述漂移。
11. get_my_interview 未注册 MCP(8/9)——若有意(需本人令牌)请注明。

### C. 双方都没设计过的空白(需共同决策)
12. **thread 生命周期五点**(见下节)+ 属主权限。
13. **PII 分层边界**:trace 在脱敏前记录原始简历(工具返回→Langfuse 采集顺序);
    模型输出侧无 PII 守卫;checkpointer 挂起载荷含 PII。脱敏从「节点」升级为「边界」。
14. **SEC-01 后端谈判清单终版**:服务账号 + X-On-Behalf-Of + GET /api/auth/me +
    审计写入端点 + B 草稿/评审端点 + score 依据落库扩展——合并一次谈。
15. **设计方案 §05 工具表**:请入库(现只存于飞书/本地,upstream docs/ 404)。

## 三、thread 生命周期五点(已记录,双方共建)
① **诞生**:thread_id 命名规则=入口:用户:随机段(防猜防跨入口互踩)。
② **属主**:恢复/读取必须校验 thread 属主——否则可枚举翻别人会话(PII)。
③ **并发**:同 thread 消息串行化(锁/队列),防 checkpointer 冲突与确认令牌错配。
④ **终结**:TTL 与 SEC-06 留存对齐;挂起确认载荷按 PII 处理。
⑤ **跨入口**:同一用户换入口接旧 thread 还是新开(v1 建议:新开)。
原则(Zewang):每个用户只能控制/访问自己的信息;权限校验在恢复路径,不在仅创建路径。

## 附录:checkpointer 存储——Redis vs Postgres

| 维度 | Redis(langgraph-checkpoint-redis) | Postgres(langgraph-checkpoint-postgres) |
|---|---|---|
| 写延迟 | ~0.2-1ms | ~1-5ms(本地网) |
| **延迟在本负载的意义** | 每个 super-step 含 LLM 调用(0.5-5s),checkpoint 写入 <1% 步耗时——**毫秒差不可感知** | 同左 |
| 持久性 | AOF everysec 仍有 ≤1s 丢失窗口;默认配置重启全丢=**挂起中的写确认蒸发** | WAL+fsync 提交即持久;崩溃恢复成熟 |
| 内存/逐出 | 全内存,随会话数线性涨;内存压力下两难:noeviction=写报错,allkeys-lru=**可能逐出挂起 interrupt(确认静默丢失)** | 磁盘,无逐出风险 |
| 运维账 | 多一个有状态服务;AOF/TTL 要正确配置(ADR-0005 已列为检查项) | **PG 反正在部署图里**(Langfuse 自带 PG;M4 pgvector)→checkpointer 用 PG 则 **Redis 可整个退出 agent 栈**(v1 限流/预算=进程内);状态服务 2→1 |
| 过期/TTL | 原生 TTL,会话自动蒸发 | 需清理 job(一条 SQL/cron)——Redis 唯一真实优势 |
| 可查询性 | SCAN 键结构不友好 | SQL 运维查询:找挂起确认/僵尸 thread/与审计交叉 |
| 备份恢复 | AOF 快照,语义弱 | pg_dump / PITR 成熟 |
| B 批处理断点续跑 | 同上持久性短板(跑一半崩=丢进度) | 崩溃后从 checkpoint 稳定恢复 |

**评审建议:Postgres**——① LLM-bound 工作负载使 Redis 的延迟优势归零;② 挂起确认
丢失/被逐出是生产事故级风险,与「写路径 fail-closed」哲学对齐的是 PG 的持久性;
③ PG 已在部署图,选 PG 大概率让 Redis 整体退栈,运维更简。**Redis 方案的主要论点**
是原生 TTL 与「聊天热数据直觉」;回应:清理 job 一条 SQL 可替代 TTL,而热数据
直觉在 LLM 步耗面前不成立。若保留 Redis,必须 AOF + noeviction + 内存容量规划
三件都做到位才算安全。

# 本地容器栈启动/恢复流程(runbook,2026-09-09 固化)

> 目的:让「删除损坏数据 → 全新起栈 → 灌 seed → 可联调」成为一条可复现命令链。
> 适用:本地 dev(管理端 3000 / 用户端 3001 / Agent 8001 / Backend 8080)。
> compose 文件:`deploy/docker-compose.local.yml`(在 `/tmp/agent-eval-wt`,worktree 持有 `feat/rag-kb`)。

## 0. 栈组成(compose `official-agent-local`,8 容器)

| 服务 | 端口 | 数据 |
|---|---|---|
| mysql | 3307 (host) | 业务库 `official`,**bind 卷** `/tmp/Official_Web_Backend/volumes/mysql_data`(勿删 bak) |
| agent-pg | 5433 | pgvector,知识库 KB + eval scorecard/qbank(与 mysql 独立) |
| backend | 8080 | Spring Boot + Flyway(启动即迁移到 v42) |
| agent | 8001 | 客服/评估 agent,`/health`→`{"status":"ok"}` |
| admin-web | 3000 | 管理后台 SPA |
| user-web | 3001 | 官网 SPA |
| redis / rabbitmq | — | 基础设施 |

## 1. 全新起栈(数据可丢时)

```bash
cd /tmp/agent-eval-wt
# 彻底清空 mysql datadir(必须停容器,否则旧 mysql 持 open FD 挡路)
docker compose -f deploy/docker-compose.local.yml down --remove-orphans
docker run --rm -v /tmp/Official_Web_Backend/volumes:/vol alpine:latest \
  sh -c 'rm -rf /vol/mysql_data && mkdir -p /vol/mysql_data && chown 999:999 /vol/mysql_data'
docker compose -f deploy/docker-compose.local.yml up -d --force-recreate
```

> ⚠️ **必须 `--force-recreate mysql` + 先 `rm -f` 停住旧容器**:docker-desktop 的 bind
> mount 会缓存旧目录句柄;若只 `up -d` 会复用仍持损坏数据/旧 FD 的容器,导致 V37 校验和不匹配
> 或 `official` 表全回来。彻底清 datadir 后让 mysql 真正 re-init(空目录 → MYSQL_DATABASE 建空
> `official`)。

## 2. 灌 seed(顺序重要,seed 自足)

```bash
MYSQL="docker exec -i official-agent-local-mysql-1 mysql -uroot -proot"
# ① cycle 3 必须先有(local-test-data 依赖它)
echo "INSERT INTO recruitment_cycle (cycle_id,cycle_name,description,start_date,end_date,academic_year,status,is_active) SELECT 3,'2026 秋季招新','本地种子周期','2026-09-01','2026-10-31','2026-2027',1,1 WHERE NOT EXISTS (SELECT 1 FROM recruitment_cycle WHERE cycle_id=3);" | $MYSQL official
# ② 业务种子(M6 客服 + 四候选人)
(echo "SET @local_seed_confirm='YES-I-AM-LOCAL-DEV';"; cat deploy/seed/local-test-data.sql) | $MYSQL --default-character-set=utf8mb4 official
# ③ B 评估种子(评分字段 + 四种典型简历 + cycle 3 兜底)
(echo "SET @local_seed_confirm='YES-I-AM-LOCAL-DEV';"; cat deploy/seed/local-dev-b-seed.sql) | $MYSQL --default-character-set=utf8mb4 official
# ④ KB 知识库(agent-pg,幂等)
cd Official_Web_Agent && uv run python deploy/seed/kb_dev_seed.py
```

> seed 自足要求(M-2):
> - `local-test-data.sql` 顶部已补「cycle 3 基底字段 1姓名/2专业/3年级/4意向部门/5自我介绍」
>   (`ON DUPLICATE KEY UPDATE`,fresh flyway 库无此行);不再依赖历史库携带 def。
> - `local-dev-b-seed.sql` 自带 cycle-3 兜底 + 四种典型简历。
> 若从这里再改 seed,保持「seed 可对 fresh flyway 库独立执行」而不依赖某次历史库。

## 3. 常见问题与排障

- **Backend exit + `FlywayValidateException: checksum mismatch for migration version 37`**
  → 旧 mysql 容器持损坏数据/旧 schema_history。根治:按「1. 全新起栈」彻底清 datadir + recreate,
  别只 `restart`(旧容器 open FD 会复活数据)。V37 文件被改过但历史校验和固定,唯有干净库能过。
- **mysql `CREATE`/`DROP` 报 `error 168 / error 3664 (SDI) / Unable to open '#innodb_redo/#ib_redo9'`**
  → InnoDB datadir 损坏(redo 坏 + `official` schema 目录缺失)。恢复:清 datadir 全新 init,数据用 seed 重生。
  备份 `mysql_data.bak-20260908` 仍在(它带 V37 校验和污染,V37 修复前不可直接挂)。
- **admin-web/user-web 起但白屏 / agent 404** → 分支/镜像与 compose 不同步;确认在 `feat/rag-kb`
  worktree 起栈。
- **端口 3307 被占用** → 残留旧 mysql 容器(本项目已清,nr-*/langfuse/old official 勿再起)。

## 4. 验证命令

```bash
docker ps --filter name=official-agent-local --format '{{.Names}}\t{{.Status}}'   # 全 Up/healthy
curl -s http://127.0.0.1:8001/health                    # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000  # 200 管理后台
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3001  # 200 官网
```

## 5. /evaluation-review(简历评估页)401→logout→/login 的两大根因(2026-09-09 打通)

1. **agent 容器跑旧镜像(缺 eval/kb 路由)**
   - 症状:/api/admin/agent/** 全线 401(「身份解析失败」)或 404;`/openapi.json` 只有 M6 的
     config/conversations/sessions,无 `/api/agent/admin/evaluation/**`、`/kb/**`。
   - 修复:改了 agent 代码必须重建镜像再起:
     `docker compose -f deploy/docker-compose.local.yml build agent`
     && `docker compose -f deploy/docker-compose.local.yml up -d --force-recreate agent`
2. **backend `AGENT_BASE_URL` 默认 host-gateway,但 agent 在栈内(containerized)**
   - 症状:kb/sources 等经 backend 代理 401/500;docker-desktop 里 `host.docker.internal` 解析成
     IPv6 `fdc4::254`,够不到只 publish 于 `127.0.0.1` 的 agent → 后端连不到,透出 401/500。
   - 修复:整栈容器化用内网名覆盖后重建 backend:
     `AGENT_BASE_URL=http://agent:8001/api/agent docker compose -f deploy/docker-compose.local.yml up -d --force-recreate backend`
   - 只有 agent 以宿主进程跑(night-run E2E 形态)才用默认 host-gateway。

### B-map 打通验证(浏览器 :3000)
简历评估页 → 评审队列有数据 + 0 分/初筛不过过滤 + 「维卡」「题库」抽屉
(评分卡/维度分+原文依据);别再出现点开即跳 /login。
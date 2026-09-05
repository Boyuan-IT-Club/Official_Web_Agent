# 部署运行手册(M6 #116)

两种环境,同一套镜像/配置语法:本地整栈联调(`docker-compose.local.yml`)与
Agent 生产两件套(`docker-compose.prod.yml`)。决策依据:#100/#104
(独立容器、uv 构建、共享 agent PG、nginx SSE 反代、单 worker、日志卷、限流只预留)。

## 本地整栈联调

```bash
# 0) 前置一次性
cd ../Official_Web_Backend && mvn -q package -DskipTests     # Backend jar(容器挂载运行,无需 build 镜像)
cd ../Official_Web_Frontend && npx craco build               # 静态产物(build-admin/build-user)

# 1) 一键起全栈(agent + backend + 管理面板 + mysql/redis/rabbitmq/agent-pg)
cd deploy && docker compose -f docker-compose.local.yml up -d --build

# 2) 验证
curl -s http://127.0.0.1:8001/health                          # agent liveness
docker compose -f docker-compose.local.yml logs backend | grep V37   # agent:monitor 权限种子
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/      # 管理面板 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/api/agent/admin/conversations
# ↑ 401 = nginx→backend→agent 链路通(未带 token 被 Backend JWT 过滤器拦,预期)

# 3) 浏览器验收(#115 AC):http://localhost:3000 管理员登录 → 运营/用量/配置
#    造对话数据:用户站(build-user 同模式部署或 dev server)+ 浮窗聊(CLI 不落行)
```

注意:
- 与 Backend 仓库自带 compose **二选一**(共用 `volumes/mysql_data`,同时跑会坏卷)。
- Agent 密钥经 `env_file: ../.env` 注入,不进镜像(SEC-01)。
- 本地栈不起 langfuse(fail-open);要观测另起 `docker compose -f ../deploy/langfuse/docker-compose.yml up -d`。

## 生产上线(Agent 两件套;Backend/前端/nginx 均为既有设施,不动)

```bash
# 1) 镜像(发布机):docker build -t boyuanclub/official-agent:<tag> . && docker push ...(#104 同名 org)
# 2) 服务器:克隆 Agent 仓库到 deploy 目录,cp .env.prod.example .env.prod 填真实值
#    (BACKEND_BASE_URL / AGENT_PG_PASSWORD / LLM key;gitignore,永不进仓库)
# 3) cd deploy && docker compose -f docker-compose.prod.yml up -d
# 4) nginx:把 deploy/nginx/agent.prod.conf.example 的 location 块并入现有官网
#    server{}(假设①:不另起 nginx 容器),nginx -t && nginx -s reload
# 5) 验证:curl 127.0.0.1:8001/health(服务器本机)→ 经域名 /api/agent/health
# 6) 切流:官网前端浮窗 REACT_APP_AGENT_URL 留空走同源 → 上线路径即 nginx 反代
```

运维:
- 日志:`deploy/logs/`(RotatingFileHandler,轮转 5×10MB);容器 stdout 亦有。
- 重启策略:`unless-stopped` + HEALTHCHECK(/health 30s×3)自动拉起。
- 扩副本:`--scale agent=N` 目前**不可用**(进程内会话表 `_sessions` 单机语义);
  会话层外置(Ably,M3)后才放开 —— #116 已声明单 worker 起步。
- 回滚:`AGENT_VERSION=<上一 tag> docker compose -f docker-compose.prod.yml up -d`。
- 限流:nginx `limit_req_zone/limit_req` 与 agent 层位置均已预留,参数等 #56/SEC-05。

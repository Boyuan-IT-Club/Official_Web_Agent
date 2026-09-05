# ── 阶段 1:依赖构建(uv sync,uv.lock 锁定;依赖层与代码层分离)────────
# 仿 Backend 分层思想:依赖不变时阶段 1 的产物逐字节稳定,代码改动只重跑
# 最后一个 COPY + sync。uv 官方镜像 = python 官方镜像 + uv,解释器路径一致,
# .venv 可直接整目录搬运到运行镜像。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 先只拷依赖清单:这一层不变时,依赖安装走缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 再拷源码并安装项目本体
COPY src ./src
RUN uv sync --frozen --no-dev

# ── 阶段 2:运行镜像 ──────────────────────────────────────────────
# python 官方 slim 镜像(与 builder 同底);运行期无需编译工具链
# (同 Backend 只装 JRE 不装 JDK 的取舍)
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# 日志/凭证目录(RotatingFileHandler 落 deploy 卷;凭证走 env 不落盘,SEC-01)
RUN mkdir -p /app/logs && chmod 0700 /app/logs
ENV CREDENTIALS_DIR=/app

EXPOSE 8001

# 健康检查:app.py 内置 /health(liveness;依赖 PG 可达性由 lifespan 保证)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4)" || exit 1

# 单 worker 起步(#116:checkpointer 共享 + 进程内会话表无状态可复制,多副本留扩展)。
# --proxy-headers:nginx 反代后信任 X-Forwarded-*。
# 限流接入位置(SEC-05/#56 定后接):nginx limit_req 或应用层中间件,此处只预留。
CMD ["uvicorn", "official_agent.web.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8001", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

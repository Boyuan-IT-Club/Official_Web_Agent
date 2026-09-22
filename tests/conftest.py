"""web 路由测试的 PG 隔离:`create_app()` 路径不得打真 PG。

背景(会话删除期间误续聊的同型问题):Web 测试用 TestClient + monkeypatch 假造库交互,
但 `create_app()` 走到两条会真连 PG 的路径,本机有没有 PG 会改变断言结果
(无 PG → 503 / 有 PG → 200),即同一提交在 CI 与开发机结论不同:

1. lifespan 自举与启动恢复,按失败语义分两类:
   a. 第一个 try(`app.py:40-55`)里的 `ensure_*` 系列建表 + 评测 job 表自举
      (`ensure_evaluation_job_ready`)。无 PG 时抛错 → 该 `except` 把
      `app.state.checkpointer` 置 None → 恢复路径按 fail-closed 返 503。
   b. 其后**独立**的 fail-open try(`app.py:60-67`)里的残留 job 全量扫回
      (`get_runner().recover_stale_on_startup`),以及每 6h 一轮的挂起载荷
      TTL 清理(`purge_expired_interrupts`,`app.py:99-106`)。失败只记
      warning(suppress),**不**置 `checkpointer=None`。
2. 路由侧 fail-closed 写审计——`/admin/evaluation/{adopt,reject}` 先落
   `audit.write_audit` 再改卡态,审计失败按 ADR-0006 返 503「未执行」;
   无 PG 时管理端 200/502 断言全被顶成 503。

本 fixture 只做「不碰真 PG」的隔离,**不放宽任何生产语义**——503、
`_deleting_sessions`、fail-closed 等安全行为一律保留;要验这些行为的用例
自己就地 patch 覆盖(如 `test_adopt_intent_audit_failure_is_clean_503`)。

非 autouse 的原因:`test_state_{config_store,conversation,audit}.py` 等直接
调用 `ensure_*` 断言 DDL,若全局替换这些模块属性会把那些用例架空;故由
构造 `create_app()` 的文件在自己的 `client` fixture 里显式请求。
真 PG 往返用例自带 `POSTGRES_URL` gate 自行 skip,且不依赖 lifespan 建表,
不受本 fixture 影响。
"""

import pytest


@pytest.fixture
def web_no_real_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 create_app() 的 lifespan 与路由侧审计都不落真 PG(checkpointer 由调用方自备)。"""
    # ── 1) lifespan 幂等建表 + 评测 job 表自举 ──
    monkeypatch.setattr("official_agent.state.threads.ensure_agent_threads_table", lambda: None)
    monkeypatch.setattr(
        "official_agent.state.conversation.ensure_conversation_table", lambda: None
    )
    monkeypatch.setattr("official_agent.state.config_store.ensure_config_table", lambda: None)
    monkeypatch.setattr("official_agent.state.audit.ensure_audit_table", lambda: None)
    monkeypatch.setattr(
        "official_agent.state.evaluation.ensure_evaluation_job_ready", lambda: None
    )
    monkeypatch.setattr(
        "official_agent.state.evaluation.ensure_evaluation_scorecard_ready", lambda: None
    )
    monkeypatch.setattr("official_agent.state.qbank.ensure_qbank_ready", lambda: None)
    # 自举已改成「每进程一次」的惰性标志。上面几个 ready 被替成 no-op 后
    # 标志不会置位,数据路径首次调用仍会自己去连真 PG——这里直接置位
    # (bootstrap 的 done 集合整体替换为已含两键,store 的 ensure_once 直通)。
    monkeypatch.setattr(
        "official_agent.state.evaluation.bootstrap._done", {"scorecard", "job"}
    )
    monkeypatch.setattr("official_agent.state.qbank._bootstrapped", True)
    # 挂起载荷 TTL 清理后台任务(每 6h)首轮即打真 PG
    monkeypatch.setattr("official_agent.state.pg.purge_expired_interrupts", lambda **_k: 0)

    # ── 2) 闸门3 启动恢复 ──
    # lifespan 是 `await get_runner().recover_stale_on_startup()`:get_runner 同步
    # 返回 runner,recover_stale_on_startup 才 await(runner.py:548 / :529),
    # 故替身工厂是同步函数、其返回对象带 async 方法。
    class _NoopRunner:
        async def recover_stale_on_startup(self, **_kw: object) -> list[int]:
            return []

    monkeypatch.setattr("official_agent.evaluation.runner.get_runner", lambda: _NoopRunner())

    # ── 3) 路由侧 fail-closed 审计写入(adopt/reject 前置门)──
    monkeypatch.setattr("official_agent.state.audit.write_audit", lambda **_k: None)


@pytest.fixture(scope="session", autouse=True)
def _close_evaluation_pool():
    """会话结束关池:psycopg_pool 的后台线程留到解释器退出会报
    PythonFinalizationError(join at shutdown),显式关掉。"""
    yield
    from official_agent.state.evaluation import _connection

    _connection.reset_pool()

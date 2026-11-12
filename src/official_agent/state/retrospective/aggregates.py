"""周期复盘的只读聚合:从 agent 自己的表里按周期数出原始计数,不写任何业务行。

每个函数对应一个数据域,返回未经抑制的原始聚合(计数、合计、分位数)——只给
retrospective.metrics 用,那里过小格子抑制后才进快照;本模块的返回值不得直接
出边界。

对话日志与检索日志没有周期字段,按时间窗 [start, end) 切;评估侧的表都有
cycle_id,直接按周期取。查询失败照常抛出,由调用方决定该域是否降级。

知识库运营的主题清单经 state.kb_ops 的读函数取,不直接查它的表:那几张表的结构与
自举归它管(读函数在表还没建时会先把表建出来,这是本模块唯一可能触发的 DDL)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from official_agent.state import kb_ops
from official_agent.state.pools import pooled_conn
from official_agent.state.qbank import flatten_v2_pickable

# 评估任务的用量行写在对话日志里(channel='evaluation'),不是对话轮次
_EVALUATION_CHANNEL = "evaluation"

# 评估用量行的 thread_id 形如 eval:<job_id>;位数上限防超长数字串转 bigint 溢出
_EVAL_THREAD_PATTERN = "^eval:([0-9]{1,18})$"
# 评分段用量行在 tools 列里的标记;题库段的行是 explore / grilling
_SCORING_TOOL = "scoring"
# 评分卡上的低置信档(初筛合议的汇总规则给出;单评委模式的卡没有置信度)
_LOW_CONFIDENCE = "low"
# 知识库运营只保留最近几次运行(kb_ops_run_retention,缺省 8),取这么多条足以拿全
_KB_OPS_RUN_SCAN = 50


def conversation_aggregates(start: datetime, end: datetime) -> dict[str, Any]:
    """时间窗内的对话轮次聚合(不含评估通道的用量行)。

    返回 turns/sessions/error_turns、耗时分位(timed_turns 是有耗时记录的轮数)、
    token 合计(metered_turns 是有 token 记录的轮数;cache_turns 是缓存命中与
    未命中都有记录的轮数,缓存两项合计只算这些轮),以及 by_channel(渠道 → 轮数)
    与 tool_calls(工具名 → 调用次数,一轮里调用几次算几次)。
    """
    window = (_EVALUATION_CHANNEL, start, end)
    with pooled_conn() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS turns,
                   COUNT(DISTINCT thread_id) AS sessions,
                   COUNT(*) FILTER (WHERE error_code IS NOT NULL) AS error_turns,
                   COUNT(duration_ms) AS timed_turns,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS duration_p50_ms,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_ms) AS duration_p90_ms,
                   percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) AS duration_p99_ms,
                   COUNT(*) FILTER (
                       WHERE input_tokens IS NOT NULL OR output_tokens IS NOT NULL
                   ) AS metered_turns,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COUNT(*) FILTER (
                       WHERE cache_hit_tokens IS NOT NULL AND cache_miss_tokens IS NOT NULL
                   ) AS cache_turns,
                   COALESCE(SUM(cache_hit_tokens) FILTER (
                       WHERE cache_miss_tokens IS NOT NULL
                   ), 0) AS cache_hit_tokens,
                   COALESCE(SUM(cache_miss_tokens) FILTER (
                       WHERE cache_hit_tokens IS NOT NULL
                   ), 0) AS cache_miss_tokens
            FROM agent_conversation_log
            WHERE channel <> %s AND created_at >= %s AND created_at < %s
            """,
            window,
        ).fetchone()
        channel_rows = conn.execute(
            """
            SELECT channel, COUNT(*) AS turns
            FROM agent_conversation_log
            WHERE channel <> %s AND created_at >= %s AND created_at < %s
            GROUP BY channel
            """,
            window,
        ).fetchall()
        # CASE 兜住非数组的 tools:jsonb_array_elements_text 遇到非数组直接报错
        tool_rows = conn.execute(
            """
            SELECT tool, COUNT(*) AS calls
            FROM agent_conversation_log,
                 jsonb_array_elements_text(
                     CASE WHEN jsonb_typeof(tools) = 'array' THEN tools ELSE '[]'::jsonb END
                 ) AS tool
            WHERE channel <> %s AND created_at >= %s AND created_at < %s
            GROUP BY tool
            """,
            window,
        ).fetchall()
    result = dict(totals or {})
    result["by_channel"] = {r["channel"]: int(r["turns"]) for r in channel_rows}
    result["tool_calls"] = {r["tool"]: int(r["calls"]) for r in tool_rows}
    return result


def kb_aggregates(start: datetime, end: datetime, *, min_score: float) -> dict[str, int]:
    """时间窗内的知识库检索聚合:searches/hits/not_found/unavailable/low_score_hits。

    low_score_hits 是命中但最高向量分低于 min_score 的检索——这类命中只靠关键词
    门槛过关。
    """
    with pooled_conn() as conn:
        # ::real:best_vec_score 是 real 列,门槛按同一精度比,免得恰在门槛上的
        # 命中因 real 转 double 的误差被算成低分
        row = conn.execute(
            """
            SELECT COUNT(*) AS searches,
                   COUNT(*) FILTER (WHERE status = 'ok') AS hits,
                   COUNT(*) FILTER (WHERE status = 'not_found') AS not_found,
                   COUNT(*) FILTER (WHERE status = 'unavailable') AS unavailable,
                   COUNT(*) FILTER (
                       WHERE status = 'ok' AND best_vec_score < %s::real
                   ) AS low_score_hits
            FROM kb_query_log
            WHERE created_at >= %s AND created_at < %s
            """,
            (min_score, start, end),
        ).fetchone()
    return {k: int(v) for k, v in (row or {}).items()}


def screening_aggregates(cycle_id: int) -> dict[str, Any]:
    """一个周期的初筛聚合。

    - jobs/failed_jobs 与 by_attempts(执行次数 → job 数)来自 evaluation_job
    - scored_resumes/hard_zero_resumes 按每份简历最新一版评分卡数;card_versions
      是全部版本数
    - decisions 是 (决定, 第一志愿部门) → 简历数,取每份简历**历史上最后一次**人工
      决定(与评审队列的 decided_status 同口径:复评写出的新 draft 卡不抹掉它);
      部门为 None 表示卡上没有志愿(没填,或旧版卡不带这项)
    """
    with pooled_conn() as conn:
        jobs = conn.execute(
            """
            SELECT COUNT(*) AS jobs,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed_jobs
            FROM evaluation_job WHERE cycle_id = %s
            """,
            (cycle_id,),
        ).fetchone()
        attempt_rows = conn.execute(
            "SELECT attempts, COUNT(*) AS jobs FROM evaluation_job "
            "WHERE cycle_id = %s GROUP BY attempts",
            (cycle_id,),
        ).fetchall()
        cards = conn.execute(
            """
            SELECT COUNT(*) AS scored_resumes,
                   COUNT(*) FILTER (WHERE latest.hard_zero) AS hard_zero_resumes,
                   (SELECT COUNT(*) FROM evaluation_scorecard WHERE cycle_id = %s)
                       AS card_versions
            FROM (
                SELECT DISTINCT ON (resume_id) resume_id, hard_zero
                FROM evaluation_scorecard WHERE cycle_id = %s
                ORDER BY resume_id, card_version DESC
            ) latest
            """,
            (cycle_id, cycle_id),
        ).fetchone()
        decision_rows = conn.execute(
            """
            SELECT decided.status, decided.first_dept, COUNT(*) AS resumes
            FROM (
                SELECT DISTINCT ON (resume_id)
                       resume_id, status, card->'intended'->>'first' AS first_dept
                FROM evaluation_scorecard
                WHERE cycle_id = %s AND status IN ('adopted', 'rejected')
                ORDER BY resume_id, card_version DESC
            ) decided
            GROUP BY decided.status, decided.first_dept
            """,
            (cycle_id,),
        ).fetchall()
    result = {k: int(v) for k, v in {**(jobs or {}), **(cards or {})}.items()}
    result["by_attempts"] = {int(r["attempts"]): int(r["jobs"]) for r in attempt_rows}
    result["decisions"] = {
        (r["status"], r["first_dept"]): int(r["resumes"]) for r in decision_rows
    }
    return result


def qbank_aggregates(cycle_id: int) -> dict[str, Any]:
    """一个周期的出题与选题聚合。

    - generated/degraded:有题库的简历数,以及其中最新一版题库带降级标记的份数
      (任一仓线题组的 qbank_v2.degraded 为真:探索预算触顶,或没取到材料)
    - picked_resumes:有选题记录的简历数
    - offered_by_group/picked_by_group:题组类别 → 可挑的题数 / 其中被选用的题数,
      口径见 _pick_usage_by_group
    """
    with pooled_conn() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS generated,
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM jsonb_array_elements(
                           CASE WHEN jsonb_typeof(latest.envelope->'groups') = 'array'
                                THEN latest.envelope->'groups' ELSE '[]'::jsonb END
                       ) AS g
                       WHERE g->'qbank_v2'->>'degraded' = 'true'
                   )) AS degraded
            FROM (
                SELECT DISTINCT ON (resume_id) resume_id, envelope
                FROM interview_qbank WHERE cycle_id = %s
                ORDER BY resume_id, qbank_version DESC
            ) latest
            """,
            (cycle_id,),
        ).fetchone()
        picked_resumes = conn.execute(
            "SELECT COUNT(DISTINCT resume_id) AS n FROM qbank_pick_log WHERE cycle_id = %s",
            (cycle_id,),
        ).fetchone()
        offered, picked = _pick_usage_by_group(conn, cycle_id)
    return {
        "generated": int((totals or {}).get("generated") or 0),
        "degraded": int((totals or {}).get("degraded") or 0),
        "picked_resumes": int((picked_resumes or {}).get("n") or 0),
        "offered_by_group": offered,
        "picked_by_group": picked,
    }


def _pick_usage_by_group(conn: Any, cycle_id: int) -> tuple[dict[str, int], dict[str, int]]:
    """各题组类别的 (可挑题数, 其中被选用的题数),只看有选题记录的简历。

    两个数都从这些简历**最新一版**题库展开(与挑题界面同一个展开函数),一道题算
    被选用 = 它的题引用 id 出现在该简历的选题记录里。分子分母同出一版题库,
    所以选用数不会超过可挑数;题库在选题之后重新生成过的简历,旧版本上的选题记录
    对不上新题,不计入。

    题库信封一份可达几十 KB,用服务端游标逐批取,不把整个周期的信封一次读进内存。
    """
    offered: dict[str, int] = {}
    picked: dict[str, int] = {}
    with conn.cursor(name="retrospective_qbank_scan") as cur:
        cur.execute(
            """
            SELECT latest.resume_id, latest.qbank_version, latest.envelope, picks.ref_ids
            FROM (
                SELECT DISTINCT ON (resume_id) resume_id, qbank_version, envelope
                FROM interview_qbank WHERE cycle_id = %s
                ORDER BY resume_id, qbank_version DESC
            ) latest
            JOIN (
                SELECT resume_id, array_agg(DISTINCT question_ref->>'ref_id') AS ref_ids
                FROM qbank_pick_log WHERE cycle_id = %s
                GROUP BY resume_id
            ) picks ON picks.resume_id = latest.resume_id
            """,
            (cycle_id, cycle_id),
        )
        for row in cur:
            envelope = row["envelope"] if isinstance(row["envelope"], dict) else {}
            picked_ref_ids = set(row["ref_ids"] or [])
            for question in flatten_v2_pickable(
                envelope,
                resume_id=int(row["resume_id"]),
                cycle_id=cycle_id,
                qbank_version=int(row["qbank_version"]),
            ):
                kind = str(question.get("group_kind") or "")
                offered[kind] = offered.get(kind, 0) + 1
                if question.get("ref_id") in picked_ref_ids:
                    picked[kind] = picked.get(kind, 0) + 1
    return offered, picked


def evaluation_usage_aggregates(cycle_id: int) -> dict[str, int]:
    """一个周期里评估任务落下的 token 用量,题库段与评分段分开数。

    用量行按 thread_id(eval:<job_id>)关联回 evaluation_job 才知道属于哪个周期;
    只算真有 token 记录的行。题库段是 metered_resumes/input_tokens/output_tokens,
    评分段是带 scoring_ 前缀的同名三项;*metered_resumes 是该段有用量记录的简历数。

    两段按用量行 tools 里有没有 scoring 标记区分。同一份简历重评几次就有几行
    评分段用量,都计入——花掉的 token 就是这么多。
    """
    with pooled_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT u.resume_id) FILTER (WHERE NOT u.scoring)
                       AS metered_resumes,
                   COALESCE(SUM(u.input_tokens) FILTER (WHERE NOT u.scoring), 0)
                       AS input_tokens,
                   COALESCE(SUM(u.output_tokens) FILTER (WHERE NOT u.scoring), 0)
                       AS output_tokens,
                   COUNT(DISTINCT u.resume_id) FILTER (WHERE u.scoring)
                       AS scoring_metered_resumes,
                   COALESCE(SUM(u.input_tokens) FILTER (WHERE u.scoring), 0)
                       AS scoring_input_tokens,
                   COALESCE(SUM(u.output_tokens) FILTER (WHERE u.scoring), 0)
                       AS scoring_output_tokens
            FROM (
                SELECT j.resume_id, c.input_tokens, c.output_tokens,
                       COALESCE(jsonb_exists(c.tools, %s), FALSE) AS scoring
                FROM agent_conversation_log c
                JOIN evaluation_job j
                  ON j.job_id = substring(c.thread_id from %s)::bigint
                WHERE c.channel = %s AND j.cycle_id = %s
                  AND (c.input_tokens IS NOT NULL OR c.output_tokens IS NOT NULL)
            ) u
            """,
            (_SCORING_TOOL, _EVAL_THREAD_PATTERN, _EVALUATION_CHANNEL, cycle_id),
        ).fetchone()
    return {k: int(v) for k, v in (row or {}).items()}


def agreement_aggregates(cycle_id: int) -> dict[str, int]:
    """一个周期里带置信度的评分卡与人工决定的对照。

    每份简历取**历史上最后一次**人工决定所在的那张卡(与 screening_aggregates 的
    decisions 同口径),看那张卡上的置信度:
    - graded_decided:卡上有置信度的已决简历数(单评委模式的卡没有,不计)
    - low_confidence_decided / low_confidence_rejected:其中置信度为低的,以及
      低置信里被驳回的
    """
    with pooled_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE decided.confidence IS NOT NULL)
                       AS graded_decided,
                   COUNT(*) FILTER (WHERE decided.confidence = %s)
                       AS low_confidence_decided,
                   COUNT(*) FILTER (
                       WHERE decided.confidence = %s AND decided.status = 'rejected'
                   ) AS low_confidence_rejected
            FROM (
                SELECT DISTINCT ON (resume_id)
                       resume_id, status, card->'panel'->>'confidence' AS confidence
                FROM evaluation_scorecard
                WHERE cycle_id = %s AND status IN ('adopted', 'rejected')
                ORDER BY resume_id, card_version DESC
            ) decided
            """,
            (_LOW_CONFIDENCE, _LOW_CONFIDENCE, cycle_id),
        ).fetchone()
    return {k: int(v) for k, v in (row or {}).items()}


def kb_topic_aggregates(start: datetime, end: datetime) -> dict[str, Any] | None:
    """与时间窗 [start, end) 有重叠的最近一次成功的知识库离线分析,及它的主题清单。

    没有这样的运行返回 None。返回 run(run_id 与那次分析自己的时间窗)和 topics
    (每个主题的原始行:模型写的主题名、归类、甄别状态、检索次数、会话数、近失数)。

    离线分析按「最近若干天」取数,不按周期:它的时间窗通常只盖住周期的一段,
    也可能伸到周期外面。主题的计数是那个时间窗里的,不是整个周期的。
    """
    for run in kb_ops.list_runs(limit=_KB_OPS_RUN_SCAN):  # 新的在前
        if run["status"] != kb_ops.RUN_SUCCEEDED:
            continue
        if run["window_end"] <= start or run["window_start"] >= end:
            continue
        return {
            "run": {
                "run_id": run["run_id"],
                "window_start": run["window_start"],
                "window_end": run["window_end"],
            },
            "topics": kb_ops.list_topics(run["run_id"]),
        }
    return None

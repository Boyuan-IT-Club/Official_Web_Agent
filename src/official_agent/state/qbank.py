"""预置题库与 pick log 数据面(B5,#127):面试官挑题的持久层。

- interview_qbank:调查/兜底产出的题集(候选+周期+版本,JSONB 信封)
- qbank_pick_log:面试官实际勾选(候选/场次/面试官/题)——反哺出题的证据,
  候选人永不可见(#135 用户故事 19)
- 表自举 L-1 先例;调用方管理事务
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def ensure_qbank_tables(conn: psycopg.Connection[dict[str, Any]]) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_qbank (
            id            bigserial   NOT NULL PRIMARY KEY,
            resume_id     bigint      NOT NULL,
            cycle_id      int         NOT NULL,
            qbank_version int         NOT NULL,
            source        text        NOT NULL,
            envelope      jsonb       NOT NULL,
            prompt_version text       NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (resume_id, cycle_id, qbank_version)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qbank_resume ON interview_qbank (resume_id, cycle_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qbank_pick_log (
            id                   bigserial   NOT NULL PRIMARY KEY,
            resume_id            bigint      NOT NULL,
            cycle_id             int         NOT NULL,
            schedule_id          bigint,
            interviewer_user_id  int         NOT NULL,
            question_ref         jsonb       NOT NULL,
            picked_at            timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qbank_pick_resume ON qbank_pick_log (resume_id, cycle_id)"
    )


def save_qbank(
    *,
    resume_id: int,
    cycle_id: int,
    source: str,
    envelope: dict[str, Any],
    prompt_version: str,
) -> int:
    """落题集:版本递增旧版保留(与 scorecard 同语义);返回 qbank_version。

    #153:只收 evaluation_qbank/v2 信封(D13 直接替换,不兼容旧形状)——
    旧结构数据在本地开发库直接清(TRUNCATE interview_qbank),不迁移。"""
    if envelope.get("schema_name") != "evaluation_qbank/v2":
        raise ValueError(
            "envelope 非 evaluation_qbank/v2(#153 直接替换):"
            "本地开发库请清空 interview_qbank 旧结构数据后重跑"
        )
    # MAX+1 并发窗口:撞唯一键重读重试(同 evaluation.save_scorecard 先例)
    for attempt in range(2):
        try:
            with _conn() as conn:
                ensure_qbank_tables(conn)
                row = conn.execute(
                    "SELECT COALESCE(MAX(qbank_version), 0) AS v "
                    "FROM interview_qbank WHERE resume_id = %s AND cycle_id = %s",
                    (resume_id, cycle_id),
                ).fetchone()
                version = (row["v"] if row else 0) + 1
                conn.execute(
                    """
                    INSERT INTO interview_qbank
                        (resume_id, cycle_id, qbank_version, source, envelope, prompt_version)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resume_id,
                        cycle_id,
                        version,
                        source,
                        json.dumps(envelope, ensure_ascii=False),
                        prompt_version,
                    ),
                )
            return version
        except psycopg.errors.UniqueViolation:
            if attempt:
                raise
    raise RuntimeError("unreachable")


def latest_qbank(resume_id: int, cycle_id: int) -> dict[str, Any] | None:
    """最新题集(含全部版本号元数据);无则 None。"""
    with _conn() as conn:
        ensure_qbank_tables(conn)
        row = conn.execute(
            "SELECT resume_id, cycle_id, qbank_version, source, envelope, "
            "prompt_version, created_at "
            "FROM interview_qbank WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY qbank_version DESC LIMIT 1",
            (resume_id, cycle_id),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if isinstance(result["envelope"], str):
        result["envelope"] = json.loads(result["envelope"])
    return result


def record_pick(
    *,
    resume_id: int,
    cycle_id: int,
    interviewer_user_id: int,
    question_ref: dict[str, Any],
    schedule_id: int | None = None,
) -> int:
    """记一道勾选题(#179:question_ref 必须是 resolve_picks 解析出的权威
    条目,含 ref_id 与定位索引)。返回 pick id。"""
    with _conn() as conn:
        ensure_qbank_tables(conn)
        row = conn.execute(
            """
            INSERT INTO qbank_pick_log
                (resume_id, cycle_id, schedule_id, interviewer_user_id, question_ref)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (
                resume_id,
                cycle_id,
                schedule_id,
                interviewer_user_id,
                json.dumps(question_ref, ensure_ascii=False),
            ),
        ).fetchone()
    return int(row["id"]) if row else 0


def list_picks(resume_id: int, cycle_id: int) -> list[dict[str, Any]]:
    """某候选的勾选记录(时间倒序)。"""
    with _conn() as conn:
        ensure_qbank_tables(conn)
        rows = conn.execute(
            "SELECT id, resume_id, cycle_id, schedule_id, interviewer_user_id, "
            "question_ref, picked_at "
            "FROM qbank_pick_log WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY picked_at DESC",
            (resume_id, cycle_id),
        ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        if isinstance(item["question_ref"], str):
            item["question_ref"] = json.loads(item["question_ref"])
        result.append(item)
    return result


def _ref_id(
    ref: dict[str, Any],
    resume_id: int,
    cycle_id: int,
    qbank_version: int,
) -> str:
    """稳定题引用 id(#179):sha256 前 16 位,输入含 resume/cycle/题库版本
    + 组内定位(索引+角色+题文)。同文题靠索引区分,题库重跑换版本即换 id。"""
    parts = [str(resume_id), str(cycle_id), str(qbank_version)]
    for k in (
        "group_index",
        "group_kind",
        "role",
        "category",
        "chain_index",
        "layer_index",
        "reserve_index",
        "question_index",
        "question",
    ):
        parts.append(str(ref.get(k, "")))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def flatten_v2_pickable(
    envelope: dict[str, Any],
    *,
    resume_id: int,
    cycle_id: int,
    qbank_version: int,
) -> list[dict[str, Any]]:
    """v2 信封 → 可挑题扁平视图(#153):UI 挑题不用懂组内嵌套。

    每题带定位引用 question_ref(group_kind/role/category/chain_index/
    layer_index/question),record_pick 原样落 qbank_pick_log。
    #179:传入 resume_id/cycle_id/qbank_version 时每题附加稳定 ref_id,
    pick 时服务端据此权威绑定(见 resolve_picks),杜绝按题文反查串源。"""
    out: list[dict[str, Any]] = []

    def _ref(**kw: Any) -> dict[str, Any]:
        ref = dict(kw)
        ref["ref_id"] = _ref_id(ref, resume_id, cycle_id, qbank_version)
        return ref

    for gi, g in enumerate(envelope.get("groups", [])):
        kind_raw = g.get("group", "")
        kind = kind_raw if isinstance(kind_raw, str) else "repo"
        # repo v2 组(#153):题目在 qbank_v2.group{entry/chains/reserves}
        qbank_v2 = g.get("qbank_v2")
        inner = (
            (qbank_v2 or {}).get("group")
            if isinstance(qbank_v2, dict)
            else (g if ("entry" in g or "chains" in g) else None)
        )
        if isinstance(inner, dict):
            entry = inner.get("entry")
            if entry:
                out.append(
                    _ref(
                        group_index=gi,
                        group_kind=kind,
                        role="entry",
                        category=entry.get("category", ""),
                        question=entry.get("question", ""),
                        evidence_path=(entry.get("evidence") or {}).get("path", ""),
                        time_minutes=entry.get("time_minutes", 3),
                    )
                )
            for ci, chain in enumerate(inner.get("chains", [])):
                for li, layer in enumerate(chain.get("layers", [])):
                    out.append(
                        _ref(
                            group_index=gi,
                            group_kind=kind,
                            role="chain",
                            category=chain.get("category", ""),
                            chain_index=ci,
                            layer_index=li,
                            question=layer.get("question", ""),
                            expected_signal=layer.get("expected_signal", ""),
                            theme=chain.get("theme", ""),
                        )
                    )
            for ri, r in enumerate(inner.get("reserves", [])):
                out.append(
                    _ref(
                        group_index=gi,
                        group_kind=kind,
                        role="reserve",
                        category=r.get("category", ""),
                        reserve_index=ri,
                        question=r.get("question", ""),
                        evidence_path=(r.get("evidence") or {}).get("path", ""),
                        time_minutes=r.get("time_minutes", 3),
                    )
                )
            continue
        for qi, q in enumerate(g.get("questions", [])):
            out.append(
                _ref(
                    group_index=gi,
                    group_kind=kind,
                    role="question",
                    category=q.get("anchor", ""),
                    question_index=qi,
                    question=q.get("question", ""),
                )
            )
    return out


def resolve_picks(
    resume_id: int, cycle_id: int, submitted: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把客户端提交的题引用解析为**当前题库**的权威引用(#179)。

    服务端是题引用的唯一真源,客户端文本不可信(同文题按题文反查会串
    来源)。匹配优先级:
    1. ref_id 精确命中(端到端绑定,前端直用 pickable 条目即走此路);
    2. 旧形状投影 {anchor, question, evidence_path?}:文本+anchor+证据
       全部对上且**唯一**才收——同文多题必拒,逼客户端升级带索引/ref_id;
    3. 其他形状:提交键值对全部与某权威条目相等(索引级绑定)。
    返回权威条目列表(含 ref_id/索引);对不上抛 LookupError(含原因)。
    """
    row = latest_qbank(resume_id, cycle_id)
    if row is None:
        raise LookupError("该候选暂无预置题库")
    entries = flatten_v2_pickable(
        row.get("envelope") or {},
        resume_id=resume_id,
        cycle_id=cycle_id,
        qbank_version=int(row.get("qbank_version") or 0),
    )
    resolved: list[dict[str, Any]] = []
    for q in submitted:
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            raise LookupError("勾选题缺少 question 文本,拒绝记录")
        match, ambiguous = _match_entry(entries, q)
        if match is None:
            if ambiguous:
                raise LookupError(
                    "该题文在当前题库不唯一(同文多题),需带 ref_id/定位索引才能勾选;"
                    "旧版前端请升级后重试"
                )
            raise LookupError("勾选题与当前题库不符,请刷新题库后重试")
        resolved.append(match)
    return resolved


def _match_entry(
    entries: list[dict[str, Any]], q: dict[str, Any]
) -> tuple[dict[str, Any] | None, bool]:
    """单题匹配。返回 (权威条目, 是否歧义):None+True=同文多题需 ref_id,
    None+False=题库中无该题(含 ref_id 过期且题文已不存在)。"""
    ref_id = str(q.get("ref_id") or "")
    if ref_id:
        by_id = [e for e in entries if e.get("ref_id") == ref_id]
        if len(by_id) == 1:
            return by_id[0], False
    if "anchor" in q:
        # 旧形状投影(#179 之前的 frontend doPick):文本+anchor+证据全对上;
        # 注意 ref_id 未命中会落到这里按题文重查——题文仍唯一时绑定当前版条目
        cands = [
            e
            for e in entries
            if str(e.get("question", "")) == str(q.get("question", ""))
            and (
                not str(q.get("anchor") or "") or str(e.get("category", "")) == str(q.get("anchor"))
            )
            and (
                "evidence_path" not in q
                or str(e.get("evidence_path", "")) == str(q.get("evidence_path"))
            )
        ]
    else:
        # 新形状:提交的每个键值都必须与权威条目相等(ref_id 已单独处理)
        cands = [
            e
            for e in entries
            if all(str(e.get(k, "")) == str(v) for k, v in q.items() if k != "ref_id")
        ]
    if len(cands) == 1:
        return cands[0], False
    return None, len(cands) > 1

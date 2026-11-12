"""预置题库与 pick log 数据面:面试官挑题的持久层。

- interview_qbank:调查/兜底产出的题集(候选+周期+版本,JSONB 信封)
- qbank_pick_log:面试官实际勾选(候选/场次/面试官/题)——反哺出题的证据,
  候选人永不可见
- 表自举:DDL 进仓库,幂等;调用方管理事务。自举**只在进程启动跑一次**
  (lifespan 或首次调用),不在数据路径上——DDL 即使 no-op 也持表锁
  (实测 PG 17:CREATE INDEX IF NOT EXISTS 取 ShareLock,挡住所有并发写),
  而 `_conn()` 非 autocommit,锁持到提交,等于把整张表串行化。
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings

_bootstrap_lock = threading.Lock()
_bootstrapped = False


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def _ensure_bootstrapped() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    with _bootstrap_lock:
        if _bootstrapped:
            return
        ensure_qbank_ready()


def ensure_qbank_ready() -> None:
    """启动自举:建题库两表(lifespan 调用)。"""
    global _bootstrapped
    with _conn() as conn:
        ensure_qbank_tables(conn)
    _bootstrapped = True


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
    # (resume_id, cycle_id) 是 UNIQUE(resume_id, cycle_id, qbank_version) 的严格前缀
    conn.execute("DROP INDEX IF EXISTS idx_qbank_resume")
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

    只收 evaluation_qbank/v2 信封(直接替换,不兼容旧形状)——
    旧结构数据在本地开发库直接清(TRUNCATE interview_qbank),不迁移。"""
    if envelope.get("schema_name") != "evaluation_qbank/v2":
        raise ValueError(
            "envelope 非 evaluation_qbank/v2:"
            "本地开发库请清空 interview_qbank 旧结构数据后重跑"
        )
    _ensure_bootstrapped()
    # MAX+1 并发窗口:撞唯一键重读重试(同 evaluation.save_scorecard 先例)
    for attempt in range(2):
        try:
            with _conn() as conn:
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
    _ensure_bootstrapped()
    with _conn() as conn:
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
    """记一道勾选题(question_ref 必须是 resolve_picks 解析出的权威
    条目,含 ref_id 与定位索引)。返回 pick id。"""
    ids = record_picks(
        resume_id=resume_id,
        cycle_id=cycle_id,
        interviewer_user_id=interviewer_user_id,
        question_refs=[question_ref],
        schedule_id=schedule_id,
    )
    return ids[0] if ids else 0


def record_picks(
    *,
    resume_id: int,
    cycle_id: int,
    interviewer_user_id: int,
    question_refs: list[dict[str, Any]],
    schedule_id: int | None = None,
) -> list[int]:
    """整批勾选**单事务**写入,返回 pick id 列表(与入参同序)。

    逐条自开事务时,第 N 条失败会把前 N-1 条留在库里,而接口只会返 500;
    面试官重试即在 qbank_pick_log(无唯一约束)里留下重复勾选,反哺出题的
    证据就被污染了。整批同生共死,重试才是干净的。
    """
    if not question_refs:
        return []
    _ensure_bootstrapped()
    ids: list[int] = []
    with _conn() as conn:
        for question_ref in question_refs:
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
            ids.append(int(row["id"]) if row else 0)
    return ids


def list_picks(resume_id: int, cycle_id: int) -> list[dict[str, Any]]:
    """某候选的勾选记录(时间倒序)。"""
    _ensure_bootstrapped()
    with _conn() as conn:
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


def list_cycle_picks(cycle_id: int) -> list[dict[str, Any]]:
    """一个周期的全部勾选记录(时间正序);question_ref 已解成 dict。"""
    _ensure_bootstrapped()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, resume_id, cycle_id, interviewer_user_id, question_ref, picked_at "
            "FROM qbank_pick_log WHERE cycle_id = %s ORDER BY picked_at, id",
            (cycle_id,),
        ).fetchall()
    picks = []
    for row in rows:
        item = dict(row)
        if isinstance(item["question_ref"], str):
            item["question_ref"] = json.loads(item["question_ref"])
        picks.append(item)
    return picks


def distinct_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一位面试官对同一道题的重复勾选只留第一条。

    勾选表没有唯一约束,重试会留下重复行;统计选题偏好时一次选择只该算一次。
    没有 ref_id 的旧记录按题文认。
    """
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for pick in picks:
        ref = pick.get("question_ref") or {}
        identity = (
            pick.get("resume_id"),
            pick.get("interviewer_user_id"),
            ref.get("ref_id") or ref.get("question"),
        )
        if identity not in seen:
            seen.add(identity)
            unique.append(pick)
    return unique


def summarize_picks(picks: list[dict[str, Any]]) -> dict[str, Any]:
    """选题记录 → 选题偏好的聚合计数。纯函数;输出里**没有题文,也没有任何人的 id**。

    四个切面各自按被选次数降序:题类(by_category)、题在题组里的角色(by_role)、
    出自哪类证据线(by_group_kind)、被选时带着的审题结论(by_review_verdict,
    没审过的记 unreviewed)。每一行有 picks(被选次数)、resumes(涉及几份简历)、
    interviewers(几位面试官选过)。
    """
    unique = distinct_picks(picks)

    def _slice(name: str, value_of: Any) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for pick in unique:
            buckets.setdefault(str(value_of(pick.get("question_ref") or {})), []).append(pick)
        rows = [
            {
                name: value,
                "picks": len(members),
                "resumes": len({m.get("resume_id") for m in members}),
                "interviewers": len({m.get("interviewer_user_id") for m in members}),
            }
            for value, members in buckets.items()
        ]
        return sorted(rows, key=lambda row: (-row["picks"], row[name]))

    return {
        "total_picks": len(unique),
        "resumes": len({pick.get("resume_id") for pick in unique}),
        "interviewers": len({pick.get("interviewer_user_id") for pick in unique}),
        "by_category": _slice("category", lambda ref: ref.get("category") or ""),
        "by_role": _slice("role", lambda ref: ref.get("role") or ""),
        "by_group_kind": _slice("group_kind", lambda ref: ref.get("group_kind") or ""),
        "by_review_verdict": _slice(
            "verdict", lambda ref: (ref.get("review") or {}).get("verdict") or "unreviewed"
        ),
    }


def pick_type_summary(cycle_id: int) -> dict[str, Any]:
    """一个周期里面试官选了哪些类型的题、各多少。只读,只出聚合计数。

    返回 summarize_picks 的结果,外加 cycle_id 与 exemplars:该周期归纳出的题型
    及其计数(题型文字只有已通过人审的才给,草稿与停用的为 None)。题型表读不到
    时 exemplars 为空列表,其余照常返回。
    """
    from official_agent.state import qbank_exemplar

    summary = {"cycle_id": cycle_id, **summarize_picks(list_cycle_picks(cycle_id))}
    try:
        exemplars = qbank_exemplar.list_exemplars(cycle_id=cycle_id)
    except Exception:  # noqa: BLE001 — 题型表是可选的,读不到不该挡住选题统计
        exemplars = []
    summary["exemplars"] = [
        {
            "exemplar_id": row["exemplar_id"],
            "category": row["category"],
            "status": row["status"],
            "pattern": row["pattern"] if row["status"] == "approved" else None,
            "picks": row["pick_count"],
            "resumes": row["resume_count"],
            "interviewers": row["interviewer_count"],
        }
        for row in exemplars
    ]
    return summary


def question_key(
    role: str,
    *,
    chain_index: int = 0,
    layer_index: int = 0,
    reserve_index: int = 0,
    question_index: int = 0,
) -> str:
    """一道题在它所在题组里的编号:审题意见与相似度标注靠它对回题目。

    组内定位只有四种形状,与可挑题视图(flatten_v2_pickable)的 role 与索引
    字段一一对应。它不含题文,所以同一份信封里是稳定的;跨版本的稳定引用用
    ref_id。
    """
    if role == "entry":
        return "entry"
    if role == "chain":
        return f"chain:{chain_index}:{layer_index}"
    if role == "reserve":
        return f"reserve:{reserve_index}"
    return f"question:{question_index}"


def entry_key(entry: dict[str, Any]) -> str:
    """可挑题视图里的一条 → 它在组内的编号。"""
    return question_key(
        str(entry.get("role", "")),
        chain_index=int(entry.get("chain_index", 0)),
        layer_index=int(entry.get("layer_index", 0)),
        reserve_index=int(entry.get("reserve_index", 0)),
        question_index=int(entry.get("question_index", 0)),
    )


def _ref_id(
    ref: dict[str, Any],
    resume_id: int,
    cycle_id: int,
    qbank_version: int,
) -> str:
    """稳定题引用 id:sha256 前 16 位,输入含 resume/cycle/题库版本
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
    """v2 信封 → 可挑题扁平视图:UI 挑题不用懂组内嵌套。

    每题带定位引用 question_ref(group_kind/role/category/chain_index/
    layer_index/question),record_pick 原样落 qbank_pick_log。
    传入 resume_id/cycle_id/qbank_version 时每题附加稳定 ref_id,
    pick 时服务端据此权威绑定(见 resolve_picks),杜绝按题文反查串源。

    信封里有审题意见或相似度标注的题,在这里并到题上(键 review / similarity);
    没有标注的题不带这两个键,形状与没有这两项功能时完全一样。"""
    out: list[dict[str, Any]] = []

    def _ref(**kw: Any) -> dict[str, Any]:
        ref = dict(kw)
        ref["ref_id"] = _ref_id(ref, resume_id, cycle_id, qbank_version)
        return ref

    for gi, g in enumerate(envelope.get("groups", [])):
        kind_raw = g.get("group", "")
        kind = kind_raw if isinstance(kind_raw, str) else "repo"
        # repo v2 组:题目在 qbank_v2.group{entry/chains/reserves}
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
                            answer_reference=layer.get("answer_reference"),
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
    _attach_annotations(out, envelope)
    return out


def _attach_annotations(entries: list[dict[str, Any]], envelope: dict[str, Any]) -> None:
    """把信封里挂在题组旁边的标注并到可挑题条目上(就地修改 entries)。

    - review:{verdict, reasons},来自该题所在证据线的审题结果;拦截模式下被打回
      重写过的题多一个 revised=true;
    - similarity:{duplicate_of_ref_id, generic_resumes},来自信封顶层的相似度
      标注;duplicate_of_ref_id 指向本包里问同一件事、证据更强的那道题。
    标注在信封里按「组下标 + 组内编号」定位,不进题目对象,所以要在这里对回去。
    """
    groups = envelope.get("groups") or []
    reviews: dict[tuple[int, str], dict[str, Any]] = {}
    for group_index, group in enumerate(groups):
        line_review = (group.get("qbank_v2") or {}).get("review") or group.get("review") or {}
        for item in line_review.get("items") or []:
            opinion: dict[str, Any] = {
                "verdict": item.get("verdict"),
                "reasons": list(item.get("reasons") or []),
            }
            if item.get("revised"):
                opinion["revised"] = True  # 只在改写过的题上出现,其余题的形状不变
            reviews[(group_index, str(item.get("key", "")))] = opinion
    similar = {
        (int(item.get("group_index", -1)), str(item.get("key", ""))): item
        for item in (envelope.get("similarity") or {}).get("items") or []
    }
    if not reviews and not similar:
        return
    ref_ids = {(int(e["group_index"]), entry_key(e)): e["ref_id"] for e in entries}
    for entry in entries:
        locator = (int(entry["group_index"]), entry_key(entry))
        if locator in reviews:
            entry["review"] = reviews[locator]
        flagged = similar.get(locator)
        if flagged is None:
            continue
        stronger = flagged.get("duplicate_of") or {}
        entry["similarity"] = {
            "duplicate_of_ref_id": ref_ids.get(
                (int(stronger.get("group_index", -1)), str(stronger.get("key", "")))
            ),
            "generic_resumes": int(flagged.get("generic_resumes") or 0),
        }


def resolve_picks(
    resume_id: int, cycle_id: int, submitted: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把客户端提交的题引用解析为**当前题库**的权威引用。

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
        # 旧形状投影(权威 ref_id 引入之前的 frontend doPick):文本+anchor+证据全对上;
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

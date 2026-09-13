"""B4:autograding 错因(B3 之外的证据线之一,#132)。

- 取「最近且最好一次」评测:先按总分最高,同分取最近(pick_latest_best)
- 非满分才触发错因分析;满分直接略过该线
- 错因归类按失败 test 名关键词:超时/环境/边界/逻辑(默认);归类只服务
  追问措辞,不是定论——面试探「读反馈→归因→修复」的闭环
- evidence = 报告任务名+失败 test 名(不泄源码,#132)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TestFailure:
    task: str
    test_name: str


def pick_latest_best(submissions: list[dict]) -> dict | None:
    """最好一次(总分最高),同分取最近。submissions 需含 total_score/submitted_at。"""

    def _key(s: dict) -> tuple:
        return (s.get("total_score") or 0, str(s.get("submitted_at") or ""))

    if not submissions:
        return None
    return max(submissions, key=_key)


def extract_failures(submission: dict) -> list[TestFailure]:
    """从报告 detail 抽全部失败 test(任务名+test 名;形状防御)。"""
    failures: list[TestFailure] = []
    for task in submission.get("tasks") or []:
        task_name = str(task.get("task_name") or task.get("name") or "")
        for tr in task.get("test_results") or []:
            if tr.get("passed") is False:
                failures.append(TestFailure(task=task_name, test_name=str(tr.get("name") or "")))
    return failures


def classify(failures: list[TestFailure]) -> dict[str, list[tuple[str, str]]]:
    """失败 → 归因桶;桶值保留 (任务名, test 名)——#132:evidence 必须两者都带。

    关键词序:边界先于环境(常见命名 test_edge_error_* 不该落环境桶)。
    """
    buckets: dict[str, list[tuple[str, str]]] = {}
    for f in failures:
        name = f.test_name.lower()
        if "timeout" in name or "超时" in name:
            kind = "timeout"
        elif any(k in name for k in ("edge", "boundary", "边界")):
            kind = "boundary"
        elif any(k in name for k in ("error", "exception", "crash", "env")):
            kind = "environment"
        else:
            kind = "logic"
        buckets.setdefault(kind, []).append((f.task, f.test_name))
    return buckets


def is_full_score(submission: dict) -> bool:
    """满分(全部任务满分)→ 该线略过(#132)。total 缺失视为不触发。"""
    total = submission.get("total_score")
    max_total = submission.get("max_total_score")
    if total is None or max_total in (None, 0):
        return False
    return total >= max_total


async def fetch_latest_submission(github_key: str) -> dict | None:
    """服务账号取该候选(github 归一化键)的最近最好一次评测;无记录 None。"""
    from official_agent.tools.readonly import get_backend_client

    client = await get_backend_client()
    listing = await client.get(f"/api/admin/evaluations/candidates/{github_key}/submissions")
    submissions = listing if isinstance(listing, list) else listing.get("items", [])
    best = pick_latest_best(submissions)
    if best is None or not best.get("id"):
        return None
    detail = await client.get(f"/api/admin/evaluations/submissions/{best['id']}")
    return detail if isinstance(detail, dict) else None

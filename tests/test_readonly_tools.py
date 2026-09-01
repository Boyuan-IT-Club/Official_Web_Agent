"""TOOL-03 只读工具单测:参数映射、路径拼装、客户端过滤、聚合口径、令牌通道。"""

import httpx
import pytest
import respx

from boyuan_agent.config import Settings
from boyuan_agent.tools import readonly
from boyuan_agent.tools.client import BackendClient, BackendError
from boyuan_agent.tools.readonly import (
    get_backend_client,
    get_candidate_card,
    get_my_interview,
    get_open_cycle,
    get_recruit_statistics,
    search_resumes,
    set_backend_client,
)

BASE = "http://backend.test"
LOGIN = f"{BASE}/api/auth/login"


def login_ok() -> httpx.Response:
    return httpx.Response(
        201, json={"code": 200, "message": "ok", "data": {"token": "tok", "user_id": 1}}
    )


def ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "message": "ok", "data": data})


@pytest.fixture
def mock_client() -> BackendClient:
    settings = Settings(
        _env_file=None,
        backend_base_url=BASE,
        backend_service_username="svc",
        backend_service_password="secret",
    )
    client = BackendClient(http=httpx.AsyncClient(base_url=BASE), settings=settings)
    set_backend_client(client)
    yield client
    set_backend_client(None)


@respx.mock
async def test_get_open_cycle(mock_client: BackendClient) -> None:
    respx.post(LOGIN).side_effect = login_ok()
    route = respx.get(f"{BASE}/api/cycles/open")
    route.side_effect = ok([{"cycleId": 2}])

    data = await get_open_cycle()

    assert data == [{"cycleId": 2}]
    assert route.call_count == 1


@respx.mock
async def test_search_resumes_maps_department_to_expected_department(
    mock_client: BackendClient,
) -> None:
    respx.post(LOGIN).side_effect = login_ok()
    route = respx.get(f"{BASE}/api/resumes/search")
    route.side_effect = ok({"content": [], "totalElements": 0, "currentPage": 1})

    await search_resumes(cycle_id=2, department="技术部", name="李四")

    sent = route.calls.last.request.url.params
    assert sent["expectedDepartment"] == "技术部"  # 模型侧 department → 后端参数名
    assert sent["name"] == "李四"
    assert sent["cycleId"] == "2"
    assert "major" not in sent and "status" not in sent  # 未给的过滤不传


@respx.mock
async def test_search_resumes_empty_backend_response(mock_client: BackendClient) -> None:
    """后端 data 为 null 时给模型稳定的空列表结构,而非 None。"""
    respx.post(LOGIN).side_effect = login_ok()
    respx.get(f"{BASE}/api/resumes/search").side_effect = httpx.Response(
        200, json={"code": 200, "message": "ok", "data": None}
    )

    assert await search_resumes() == {"content": [], "totalElements": 0}


@respx.mock
async def test_get_resume_detail_path(mock_client: BackendClient) -> None:
    respx.post(LOGIN).side_effect = login_ok()
    route = respx.get(f"{BASE}/api/resumes/admin/7/2")
    route.side_effect = ok({"fields": []})

    await readonly.get_resume_detail(user_id=7, cycle_id=2)

    assert route.call_count == 1


@respx.mock
async def test_get_my_interview_carries_user_token(mock_client: BackendClient) -> None:
    respx.post(LOGIN).side_effect = login_ok()
    route = respx.get(f"{BASE}/api/interview/schedule/my")
    route.side_effect = ok({"slot": "A"})

    data = await get_my_interview(cycle_id=2, user_token="user-jwt")

    assert data == {"slot": "A"}
    auth = route.calls.last.request.headers["Authorization"]
    assert auth == "Bearer user-jwt"  # 用户本人令牌,不是服务账号 token
    assert route.calls.last.request.url.params["cycleId"] == "2"


@respx.mock
async def test_get_my_interview_requires_token(mock_client: BackendClient) -> None:
    with pytest.raises(BackendError, match="用户本人令牌"):
        await get_my_interview(cycle_id=2, user_token="")


@respx.mock
async def test_get_my_interview_expired_user_token_is_actionable(
    mock_client: BackendClient,
) -> None:
    """用户 token 过期:如实告知重新登录,绝不拿服务账号顶替或静默重试。"""
    respx.post(LOGIN).side_effect = login_ok()
    respx.get(f"{BASE}/api/interview/schedule/my").side_effect = httpx.Response(401)

    with pytest.raises(BackendError, match="重新登录"):
        await get_my_interview(cycle_id=2, user_token="expired")


@respx.mock
async def test_find_available_sessions_filters_date_client_side(
    mock_client: BackendClient,
) -> None:
    respx.post(LOGIN).side_effect = login_ok()
    route = respx.get(f"{BASE}/api/interview/admin/cycles/2/available-sessions")
    route.side_effect = ok(
        [
            {"sessionId": 1, "interviewDate": "2026-09-05", "remaining": 3},
            {"sessionId": 2, "interviewDate": "2026-09-06", "remaining": 1},
        ]
    )

    data = await readonly.find_available_sessions(cycle_id=2, dept_id=5, date="2026-09-05")

    assert route.calls.last.request.url.params["deptId"] == "5"  # deptId 传后端
    assert [s["sessionId"] for s in data] == [1]  # date 客户端过滤


@respx.mock
async def test_list_reschedule_requests_default_pending(mock_client: BackendClient) -> None:
    respx.post(LOGIN).side_effect = login_ok()
    route = respx.get(f"{BASE}/api/interview/reschedule/admin/list")
    route.side_effect = ok([])

    await readonly.list_reschedule_requests(cycle_id=2)
    assert route.calls.last.request.url.params["status"] == "0"

    await readonly.list_reschedule_requests(cycle_id=2, status=None)
    assert "status" not in route.calls.last.request.url.params  # None = 全部


@respx.mock
async def test_get_recruit_statistics_paginates_and_aggregates(
    mock_client: BackendClient,
) -> None:
    """result/list 分页拉全 + decision/部门计数 + summary 缺席不炸。"""
    respx.post(LOGIN).side_effect = login_ok()
    list_route = respx.get(f"{BASE}/api/interview/result/list")
    # 第一页 2 条(total=3),第二页 1 条——验证翻页
    list_route.side_effect = [
        ok(
            {
                "total": 3,
                "interviewResults": [
                    {"decision": 1, "assignedDeptId": 5},
                    {"decision": 1, "assignedDeptId": 5},
                ],
            }
        ),
        ok({"total": 3, "interviewResults": [{"decision": 3, "assignedDeptId": None}]}),
    ]
    # 评价表未开启 → 业务错误 → 统计仍要成功
    respx.get(f"{BASE}/api/interview/evaluation/cycles/2/summary").side_effect = (
        httpx.Response(400, json={"code": 3703, "message": "该周期尚无已分配的面试名单"})
    )

    stats = await get_recruit_statistics(cycle_id=2)

    assert list_route.call_count == 2
    assert stats["totalResults"] == 3
    assert stats["decisionCounts"] == {
        "pending": 0, "passed": 2, "rejected": 0, "toTransfer": 1,
    }
    assert stats["assignedByDeptId"] == {"5": 2}
    assert stats["evaluatedCandidates"] is None  # summary 不可用,不阻塞


@respx.mock
async def test_get_candidate_card_merges_two_endpoints(mock_client: BackendClient) -> None:
    respx.post(LOGIN).side_effect = login_ok()
    base = f"{BASE}/api/interview/evaluation/cycles/2/candidates/9"
    resume_route = respx.get(f"{base}/resume")
    resume_route.side_effect = ok({"name": "张三"})
    dims_route = respx.get(f"{base}/dimensions")
    dims_route.side_effect = ok([{"dim": "基础"}])

    card = await get_candidate_card(cycle_id=2, schedule_id=9, on_behalf_of=42)

    assert card == {"resume": {"name": "张三"}, "dimensions": [{"dim": "基础"}]}
    assert resume_route.calls.last.request.headers["X-On-Behalf-Of"] == "42"
    assert dims_route.calls.last.request.headers["X-On-Behalf-Of"] == "42"


async def test_get_backend_client_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """共享单例:token 缓存与登录锁不分裂。"""
    set_backend_client(None)
    c1 = await get_backend_client()
    c2 = await get_backend_client()
    assert c1 is c2
    set_backend_client(None)

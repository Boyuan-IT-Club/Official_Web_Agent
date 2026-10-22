"""工具返回投影:只留回答要用的字段,姓名与身份号不进模型上下文。"""

from official_agent.tools import projection


def test_sessions_keep_time_place_and_remaining() -> None:
    out = projection.sessions(
        [
            {
                "sessionId": 1,
                "interviewDate": "2026-09-05",
                "startTime": "09:00",
                "endTime": "11:00",
                "deptName": "技术部",
                "location": "202",
                "remaining": 3,
                "capacity": 8,
                "currentOccupied": 5,
                "timeSlotId": 7,
            }
        ]
    )
    assert out == [
        {
            "sessionId": 1,
            "date": "2026-09-05",
            "start": "09:00",
            "end": "11:00",
            "departments": ["技术部"],
            "location": "202",
            "remaining": 3,
            "capacity": 8,
        }
    ]


def test_unassigned_drops_name() -> None:
    out = projection.unassigned(
        [
            {
                "resumeId": 9,
                "userId": 3,
                "name": "张三",
                "firstDeptName": "技术部",
                "reason": "无场次",
            }
        ]
    )
    assert "张三" not in str(out)
    assert out[0]["firstDept"] == "技术部"


def test_reschedule_drops_identity_and_masks_reason() -> None:
    out = projection.reschedule_requests(
        [
            {
                "requestId": 1,
                "name": "李四",
                "studentId": "2025123456",
                "reason": "那天有课,电话 13812345678",
                "preferredSlots": [{"timeSlotId": 2, "label": "周六上午"}],
                "status": 0,
            }
        ]
    )
    flat = str(out)
    assert "李四" not in flat and "2025123456" not in flat and "13812345678" not in flat
    assert out[0]["preferredSlots"] == ["周六上午"]


def test_open_cycles_truncates_long_description() -> None:
    out = projection.open_cycles([{"cycleId": 2, "description": "介" * 2000, "fieldCount": 30}])
    assert len(out[0]["description"]) == 500
    assert "fieldCount" not in out[0]


def test_non_list_payloads_pass_through() -> None:
    assert projection.sessions(None) is None
    assert projection.resume_page(None) == {"total": 0, "items": []}

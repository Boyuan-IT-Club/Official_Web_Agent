"""冒烟测试:包可导入、配置有默认值、写工具未经确认不执行。"""

from official_agent.config import Settings
from official_agent.tools.write import assign_interview


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.backend_base_url.startswith("http")
    assert s.model_light and s.model_strong


async def test_write_tool_without_confirmation_does_not_execute() -> None:
    assert (await assign_interview(resume_id=1, target_session_id=2))["status"] == "cancelled"

"""冒烟测试:包可导入、配置有默认值、写工具无令牌必须拒绝。"""

import pytest

from official_agent.config import Settings
from official_agent.tools.write import ConfirmationRequired, assign_interview


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.backend_base_url.startswith("http")
    assert s.model_light and s.model_strong


async def test_write_tool_requires_confirmation_token() -> None:
    with pytest.raises(ConfirmationRequired):
        await assign_interview(resume_id=1, target_session_id=2, confirmation_token=None)

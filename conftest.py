"""测试全局隔离:凭证文件指到 tmp,防真实 ~/.boyuan-agent 泄漏进测试。

SEC-09 的存储优先级意味着存在真实凭证时,测试行为会随本机状态漂移——
autouse 隔离是正确性要求,不只是卫生。
"""

from pathlib import Path

import pytest

from boyuan_agent import credentials


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(credentials, "_CREDENTIALS_DIR", tmp_path)

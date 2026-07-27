import pytest
from app.config import Settings

@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, scheduler_enabled=False, gate_retry_attempts=2, manual_scan_token="test", admin_bearer_token="test")


from __future__ import annotations

from pathlib import Path

import pytest

from followthrough.config import Settings


@pytest.fixture
def configured_settings(tmp_path: Path) -> tuple[Settings, str, str]:
    secrets_dir = tmp_path / "secrets"
    devices = secrets_dir / "devices"
    devices.mkdir(parents=True)
    dashboard_token = "dashboard-test-token-0123456789"
    device_token = "device-test-token-01234567890123"
    (secrets_dir / "dashboard.token").write_text(dashboard_token)
    (devices / "phone.token").write_text(device_token)
    settings = Settings(
        db_path=tmp_path / "followthrough.db",
        archive_db_path=tmp_path / "archive" / "archive.db",
        reports_dir=tmp_path / "reports",
        jobs_dir=tmp_path / "jobs",
        audio_dir=tmp_path / "archive" / "audio",
        secrets_dir=secrets_dir,
        dashboard_token_file=secrets_dir / "dashboard.token",
        device_tokens_dir=devices,
        require_auth=True,
        public_url="https://example.test",
        kanban_enabled=False,
    )
    return settings, dashboard_token, device_token

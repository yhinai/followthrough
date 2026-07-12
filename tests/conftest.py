from __future__ import annotations

from pathlib import Path

import pytest

from followthrough.config import Settings


@pytest.fixture
def configured_settings(tmp_path: Path) -> tuple[Settings, str, str]:
    # The strings remain only to keep older tests' fixture unpacking stable;
    # Followthrough itself is intentionally tokenless.
    dashboard_token = "dashboard-test-token-0123456789"
    device_token = "device-test-token-01234567890123"
    settings = Settings(
        db_path=tmp_path / "followthrough.db",
        archive_db_path=tmp_path / "archive" / "archive.db",
        reports_dir=tmp_path / "reports",
        jobs_dir=tmp_path / "jobs",
        audio_dir=tmp_path / "archive" / "audio",
        public_url="https://example.test",
        kanban_enabled=True,
    )
    return settings, dashboard_token, device_token

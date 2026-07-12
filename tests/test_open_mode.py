from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from followthrough.app import create_app
from followthrough.config import Settings


def _open_settings(tmp_path: Path) -> Settings:
    secrets_dir = tmp_path / "secrets"
    return Settings(
        db_path=tmp_path / "followthrough.db",
        archive_db_path=tmp_path / "archive" / "archive.db",
        reports_dir=tmp_path / "reports",
        jobs_dir=tmp_path / "jobs",
        audio_dir=tmp_path / "archive" / "audio",
        secrets_dir=secrets_dir,
        dashboard_token_file=secrets_dir / "dashboard.token",
        device_tokens_dir=secrets_dir / "devices",
        archive_key_file=secrets_dir / "archive.key",
        require_auth=False,
        encrypt_archive=False,
        public_url="https://example.test",
        kanban_enabled=False,
    )


def test_open_mode_needs_no_tokens_or_key(tmp_path: Path) -> None:
    app = create_app(_open_settings(tmp_path))
    payload = {
        "event_id": "event-open-0001",
        "device_id": "phone",
        "text": "Lunch was great and the sandwich was perfect.",
        "source": "phone",
        "consent": True,
    }
    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["ok"] is True
        assert health["auth_required"] is False
        assert health["archive_encrypted"] is False

        assert client.post("/api/v1/transcripts", json=payload).status_code == 202
        assert client.get("/api/runs").status_code == 200
        assert client.get("/api/metrics").status_code == 200
        assert client.get("/api/v1/audio/event-open-0001/status").status_code == 200
        assert client.put("/api/v1/audio/event-open-0001/0", content=b"pcm-bytes").status_code == 200

    row = app.state.archive_store.by_event(payload["event_id"])
    assert row is not None
    assert row["transcript_cipher"] == payload["text"].encode()
    assert app.state.vault.decrypt(row["transcript_cipher"], b"transcript:event-open-0001").decode() == payload["text"]


def test_open_mode_ignores_stale_token_files(tmp_path: Path) -> None:
    settings = _open_settings(tmp_path)
    settings.device_tokens_dir.mkdir(parents=True, exist_ok=True)
    settings.dashboard_token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.dashboard_token_file.write_text("stale-dashboard-token")
    (settings.device_tokens_dir / "phone.token").write_text("stale-device-token")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/runs").status_code == 200
        assert (
            client.post(
                "/api/signals",
                json={"text": "hello there", "source": "demo", "consent": True},
            ).status_code
            == 200
        )

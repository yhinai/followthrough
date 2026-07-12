from __future__ import annotations

from fastapi.testclient import TestClient

from followthrough.app import create_app


def test_livekit_session_requires_explicit_consent(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.livekit_url = "wss://livekit.example"
    settings.livekit_api_key = "key"
    settings.livekit_api_secret = "a-secret-long-enough-for-hs256-tests"
    app = create_app(settings)

    with TestClient(app) as client:
        denied = client.post(
            "/api/v1/livekit/session",
            json={"device_id": "memo-samsung-phone", "consent": False},
        )
        accepted = client.post(
            "/api/v1/livekit/session",
            json={
                "device_id": "memo-samsung-phone",
                "consent": True,
                "response_mode": "discord_and_voice",
            },
        )

    assert denied.status_code == 400
    assert accepted.status_code == 200
    assert accepted.json()["server_url"] == "wss://livekit.example"
    assert accepted.json()["room_name"].startswith("followthrough-")


def test_livekit_irrelevant_final_is_archived_without_action(configured_settings) -> None:
    settings, _, _ = configured_settings
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "memo-livekit-ordinary-1",
                "device_id": "memo-samsung-phone",
                "text": "The sandwich at lunch was pretty good.",
                "source": "phone",
                "consent": True,
                "metadata": {"capture": "memo_livekit"},
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "archived"
    assert response.json()["operational_memory"] is False
    assert app.state.archive_store.by_event("memo-livekit-ordinary-1") is not None

from fastapi.testclient import TestClient

from followthrough.app import create_app


def test_owner_activity_feed_reads_recent_transcripts(configured_settings) -> None:
    settings, dashboard_token, device_token = configured_settings
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers={"Authorization": f"Bearer {device_token}"},
            json={
                "event_id": "activity-event-0001",
                "device_id": "phone",
                "source": "phone",
                "text": "Research the Acme repository",
                "consent": True,
            },
        )
        assert response.status_code == 202
        assert client.get("/api/activity").status_code == 401
        activity = client.get(
            "/api/activity",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        ).json()
    assert activity[0]["text"] == "Research the Acme repository"
    assert activity[0]["source"] == "phone"
    assert activity[0]["relevant"] is True

from fastapi.testclient import TestClient

from followthrough.app import create_app


def test_device_reads_sanitized_job_status_by_id(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.kanban_enabled = True
    settings.auto_send = True
    app = create_app(settings)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "device-return-channel-01",
                "device_id": "memo-phone",
                "source": "phone",
                "text": "Research https://github.com/pypa/sampleproject",
                "consent": True,
            },
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]

        status = client.get(f"/api/v1/jobs/{job_id}")
        assert status.status_code == 200
        assert status.json()["job_id"] == job_id
        assert status.json()["state"] == "pending"
        assert "acceptance_json" not in status.json()
        assert "capsule_path" not in status.json()

        assert client.get(f"/api/v1/jobs/no-such-job").status_code == 404


def test_device_poll_stamps_last_polled_at(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.kanban_enabled = True
    app = create_app(settings)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "poll-stamp-01",
                "device_id": "memo-phone",
                "source": "phone",
                "text": "Research https://github.com/pypa/sampleproject",
                "consent": True,
            },
        )
        job_id = accepted.json()["job_id"]
        assert app.state.store.hermes_job(job_id)["last_polled_at"] is None
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200
    assert app.state.store.hermes_job(job_id)["last_polled_at"] is not None

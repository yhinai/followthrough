from fastapi.testclient import TestClient

from followthrough.app import create_app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_device_can_read_only_its_sanitized_job_status(configured_settings) -> None:
    settings, _, device_token = configured_settings
    settings.kanban_enabled = True
    settings.auto_send = True
    other_token = "other-device-token-012345678901234"
    (settings.device_tokens_dir / "other.token").write_text(other_token)
    app = create_app(settings)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "device-return-channel-01",
                "device_id": "memo-phone",
                "source": "phone",
                "text": "Research https://github.com/pypa/sampleproject",
                "consent": True,
                "metadata": {"capture_principal": "forged"},
            },
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]

        status = client.get(f"/api/v1/jobs/{job_id}", headers=_auth(device_token))
        assert status.status_code == 200
        assert status.json()["job_id"] == job_id
        assert status.json()["state"] == "pending"
        assert "acceptance_json" not in status.json()
        assert "capsule_path" not in status.json()
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 401
        assert client.get(f"/api/v1/jobs/{job_id}", headers=_auth(other_token)).status_code == 404


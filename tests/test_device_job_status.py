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

        assert client.get("/api/v1/jobs/no-such-job").status_code == 404


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


def test_completed_web_task_returns_the_computer_use_answer(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.kanban_enabled = True
    app = create_app(settings)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "web-answer-01",
                "device_id": "memo-phone",
                "source": "phone",
                "consent": True,
                "text": "Check the price of the NVIDIA RTX 5080 on Best Buy",
            },
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        session_id = accepted.json()["computer_use_id"]

        # The research worker's guess must never outrank the agent that ran the task.
        app.state.store.update_run(
            app.state.store.hermes_job(job_id)["run_id"],
            summary="Status: unable to verify a live price.",
        )
        app.state.store.update_computer_session(
            session_id, state="completed", latest_answer="RTX 5080 starts at $1,256.99."
        )

        body = client.get(f"/api/v1/jobs/{job_id}").json()
        assert body["summary"] == "RTX 5080 starts at $1,256.99."
        assert body["computer_use"]["state"] == "completed"


def test_running_web_task_does_not_speak_a_stale_worker_summary(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.kanban_enabled = True
    app = create_app(settings)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "web-inflight-01",
                "device_id": "memo-phone",
                "source": "phone",
                "consent": True,
                "text": "Check the price of an RTX 5080 on Newegg",
            },
        )
        job_id = accepted.json()["job_id"]
        session_id = accepted.json()["computer_use_id"]

        # The research worker finishes first with a guess; the agent is still live.
        app.state.store.update_run(
            app.state.store.hermes_job(job_id)["run_id"],
            summary="Status: unable to verify a live price.",
        )
        app.state.store.update_computer_session(session_id, state="running", step_count=7)

        body = client.get(f"/api/v1/jobs/{job_id}").json()
        assert body["state"] == "in_progress"
        assert body["summary"] is None
        assert body["computer_use"]["state"] == "running"


def test_restart_resumes_an_orphaned_computer_use_session(configured_settings) -> None:
    settings, _, _ = configured_settings
    app = create_app(settings)
    store = app.state.store
    session = store.create_computer_session(task="check a price", agent="h/web-surfer-flash")
    store.update_computer_session(session["id"], state="running", h_session_id="h-123")

    assert [row["id"] for row in store.unfinished_computer_sessions()] == [session["id"]]

    resumed: list[str] = []

    async def fake_resume(identifier: str) -> None:
        resumed.append(identifier)

    app.state.h_executor.resume = fake_resume
    with TestClient(app):
        pass
    assert resumed == [session["id"]]

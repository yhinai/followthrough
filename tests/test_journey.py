from fastapi.testclient import TestClient

from followthrough.app import create_app


def test_journey_links_one_event_from_transcript_to_phone(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.h_api_key = ""
    app = create_app(settings)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "journey-event-0001",
                "device_id": "memo-phone",
                "source": "phone",
                "text": "Research https://github.com/pypa/sampleproject",
                "consent": True,
            },
        ).json()
        job_id = accepted["job_id"]
        session = app.state.store.create_computer_session(
            task="Research pypa/sampleproject",
            agent="h/web-surfer-flash",
            source_event_id="journey-event-0001",
        )
        app.state.store.update_computer_session(
            session["id"],
            state="completed",
            step_count=8,
            latest_answer="The repository is Python packaging sample code.",
            agent_view_url="https://platform.hcompany.ai/agents/sessions/test",
            finished_at="2026-07-12T20:00:08+00:00",
        )
        app.state.store.mark_hermes_enqueued(job_id, "t_journey01")
        app.state.store.sync_hermes_job(job_id, "completed", summary="Research complete")
        app.state.store.mark_hermes_notification(job_id, "delivered")
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200

        body = client.get("/api/journey").json()

    assert body["event_id"] == "journey-event-0001"
    assert body["state"] == "completed"
    assert body["decision"]["category"] == "repository"
    assert body["decision"]["confidence"] >= 0.9
    assert body["decision"]["explanation"]
    assert body["job"]["id"] == job_id
    assert body["computer_use"]["step_count"] == 8
    assert body["discord_delivered"] is True
    assert body["phone_returned"] is True
    assert [stage["key"] for stage in body["stages"]] == [
        "heard",
        "relevant",
        "delegated",
        "browsing",
        "verified",
        "discord",
        "phone",
    ]
    assert {stage["state"] for stage in body["stages"]} == {"done"}


def test_empty_journey_is_explicitly_idle(configured_settings) -> None:
    settings, _, _ = configured_settings
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/api/journey").json() == {
            "event_id": None,
            "stages": [],
            "state": "idle",
        }

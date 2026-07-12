from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from followthrough.app import create_app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}



def test_irrelevant_transcript_is_archive_only_and_idempotent(configured_settings) -> None:
    settings, dashboard_token, device_token = configured_settings
    app = create_app(settings)
    payload = {"event_id": "event-irrelevant-0001", "device_id": "phone", "text": "Lunch was great and the sandwich was perfect.", "source": "phone", "consent": True}
    with TestClient(app) as client:
        first = client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload)
        assert first.status_code == 202
        assert first.json()["status"] == "archived"
        assert first.json()["operational_memory"] is False
        second = client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload)
        assert second.status_code == 202
        assert second.json()["created"] is False
        conflict = client.post("/api/v1/transcripts", headers=_auth(device_token), json={**payload, "text": "Different transcript"})
        assert conflict.status_code == 409
        assert client.get("/api/runs", headers=_auth(dashboard_token)).json() == []
    row = app.state.archive_store.by_event(payload["event_id"])
    assert row is not None
    assert row["transcript_bytes"].decode() == payload["text"]


def test_actionable_replay_creates_one_run(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)

    payload = {"event_id": "event-actionable-0001", "device_id": "phone", "text": "Research this GitHub repo https://github.com/NousResearch/hermes-agent", "source": "phone", "consent": True}
    with TestClient(app) as client:
        first = client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload)
        second = client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["status"] == "queued"
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    # Replay short-circuits on the archived event: one run, one durable job.
    assert second.json()["created"] is False
    assert app.state.store.db.execute("SELECT COUNT(*) FROM hermes_jobs").fetchone()[0] == 1
    assert app.state.store.hermes_job_for_run(first.json()["run_id"])["id"]


def test_actionable_web_replay_returns_existing_job_and_computer_receipts(
    configured_settings,
) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    payload = {
        "event_id": "event-web-replay-0001",
        "device_id": "memo-phone",
        "text": "Memo, check the price of gold today",
        "source": "phone",
        "consent": True,
    }

    with TestClient(app) as client:
        first = client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload)
        replay = client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload)

    assert replay.status_code == 202
    assert replay.json()["created"] is False
    assert replay.json()["job_id"] == first.json()["job_id"]
    assert replay.json()["computer_use_id"] == first.json()["computer_use_id"]


def test_phone_fragments_are_centrally_aggregated_before_dispatch(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-fragment-action-0001",
                "device_id": "memo-samsung",
                "text": "Followthrough, please research",
                "source": "phone",
                "consent": True,
            },
        )
        second = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-fragment-subject-0002",
                "device_id": "memo-samsung",
                "text": "the GitHub repository PyPA sampleproject",
                "source": "phone",
                "consent": True,
            },
        )
    assert first.status_code == 202
    assert first.json()["status"] == "waiting_for_context"
    assert second.status_code == 202
    assert second.json()["status"] == "queued"
    assert second.json()["aggregate_event_id"].startswith("adb-omi:aggregate:")
    assert second.json()["original_event_id"] == "memo-fragment-subject-0002"
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    component = app.state.archive_store.by_event("memo-fragment-subject-0002")
    assert component["relevant"] == 0
    assert component["classification"] == "aggregate_component"


def test_natural_search_the_web_command_creates_a_web_job(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    spoken = "Oh, search the web and figure out how much caffeine is in a Red Bull."
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-search-red-bull-0001",
                "device_id": "memo-phone",
                "text": spoken,
                "source": "phone",
                "consent": True,
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["aggregate_event_id"].startswith("adb-omi:aggregate:")
    job = app.state.store.hermes_job_for_run(response.json()["run_id"])
    assert job is not None
    assert job["category"] == "web_task"
    assert job["entity"] == "search the web and figure out how much caffeine is in a Red Bull"


def test_conversational_prefix_cannot_replace_explicit_search_task(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    spoken = "No. No. No. Search the web and find how much caffeine content is in a Red Bull."
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-prefixed-red-bull-0001",
                "device_id": "memo-phone",
                "text": spoken,
                "source": "phone",
                "consent": True,
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    expected = "Search the web and find how much caffeine content is in a Red Bull"
    job = app.state.store.hermes_job_for_run(response.json()["run_id"])
    source_event_id = response.json().get("aggregate_event_id", "memo-prefixed-red-bull-0001")
    session = app.state.store.computer_session_for_event(source_event_id)
    assert job is not None and job["entity"] == expected
    assert session is not None and session["task"] == expected


def test_book_noun_context_does_not_create_an_action(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-book-context-0001",
                "device_id": "memo-phone",
                "text": "This is supposed to be a book to like search to do tasks.",
                "source": "phone",
                "consent": True,
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "archived"
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_time_sensitive_question_creates_live_web_work(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-world-cup-news-0001",
                "device_id": "memo-phone",
                "text": "What is the latest news about the World Cup?",
                "source": "phone",
                "consent": True,
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["classification"]["kind"] == "web_task"
    assert response.json()["relevance"]["information_route"] == "live_research"
    assert response.json()["computer_use_id"]
    assert app.state.store.hermes_job_for_run(response.json()["run_id"])["category"] == "web_task"


def test_stable_question_is_traced_without_web_work(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-stable-caffeine-0001",
                "device_id": "memo-phone",
                "text": "How much caffeine is in a Red Bull?",
                "source": "phone",
                "consent": True,
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "archived"
    assert response.json()["classification"]["kind"] == "stable_answer"
    assert response.json()["relevance"]["information_route"] == "stable_answer"
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert app.state.store.db.execute("SELECT COUNT(*) FROM computer_use_sessions").fetchone()[0] == 0


def test_incomplete_command_waits_then_combines_with_next_segment(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-wait-search-0001",
                "device_id": "memo-phone",
                "text": "perform a web search",
                "source": "phone",
                "consent": True,
            },
        )
        second = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-wait-search-0002",
                "device_id": "memo-phone",
                "text": "for the latest World Cup news",
                "source": "phone",
                "consent": True,
            },
        )

    assert first.status_code == 202
    assert first.json()["status"] == "waiting_for_context"
    assert first.json()["classification"]["kind"] == "waiting_for_context"
    assert second.status_code == 202
    assert second.json()["status"] == "queued"
    assert second.json()["aggregate_event_id"].startswith("adb-omi:aggregate:")
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_memo_activation_does_not_override_incomplete_utterance(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-chunk-search-0001",
                "device_id": "memo-phone",
                "text": "A memo, can you search",
                "source": "phone",
                "consent": True,
            },
        )
        assert first.json()["status"] == "waiting_for_context"
        assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        second = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-chunk-search-0002",
                "device_id": "memo-phone",
                "text": "the web and find caffeine in Red Bull?",
                "source": "phone",
                "consent": True,
            },
        )
        feed = client.get("/api/transcript").json()

    assert second.json()["status"] == "queued"
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert len(feed) == 1
    assert feed[0]["text"] == (
        "A memo, can you search the web and find caffeine in Red Bull?"
    )


def test_email_request_reports_setup_needed_without_creating_work(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={
                "event_id": "memo-email-setup-0001",
                "device_id": "memo-phone",
                "text": "Can you send me email?",
                "source": "phone",
                "consent": True,
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "clarification_setup_needed"
    assert response.json()["classification"]["kind"] == "clarification_setup_needed"
    assert response.json()["relevance"]["evidence"][-1]["rule_id"] == (
        "setup.email_sender_not_configured"
    )
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_retry_of_archived_action_does_not_create_second_aggregate(configured_settings) -> None:
    settings, _, device_token = configured_settings
    settings.kanban_enabled = True
    settings.auto_send = True
    app = create_app(settings)
    payload = {
        "event_id": "memo-retry-action-01",
        "device_id": "memo-phone",
        "text": "Followthrough research the GitHub repository pypa sampleproject",
        "source": "phone",
        "consent": True,
    }
    with TestClient(app) as client:
        first = client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload)
        assert first.status_code == 202
        first_job = first.json()["job_id"]

        # New ambient context arrives after the buffer cleared.
        client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={**payload, "event_id": "memo-ordinary-after-action", "text": "ordinary private context"},
        )
        retry = client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload)

    assert retry.status_code == 202
    assert retry.json()["created"] is False
    assert len(app.state.store.list_hermes_jobs()) == 1
    assert app.state.store.list_hermes_jobs()[0]["id"] == first_job


def test_payload_limits_fail_before_persistence(configured_settings) -> None:
    settings, _, device_token = configured_settings
    settings.max_transcript_bytes = 64
    settings.max_audio_chunk_bytes = 16
    app = create_app(settings)
    with TestClient(app) as client:
        transcript = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={"event_id": "event-oversize-0001", "device_id": "phone", "text": "x" * 65, "source": "phone", "consent": True},
        )
        assert transcript.status_code == 413
        assert app.state.archive_store.by_event("event-oversize-0001") is None
        audio = client.post(
            f"/api/webhooks/omi/audio?token={device_token}&uid=uid-1&sample_rate=16000",
            content=b"x" * 17,
            headers={"Idempotency-Key": "oversize-audio-1", "Content-Length": "17"},
        )
        assert audio.status_code == 413


def test_audio_chunk_is_retry_safe(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    event_id = "event-audio-0000001"
    payload = {"event_id": event_id, "device_id": "phone", "text": "Lunch only.", "source": "phone", "consent": True}
    audio = b"opus-or-pcm-test-bytes"
    digest = hashlib.sha256(audio).hexdigest()
    headers = {**_auth(device_token), "X-Device-Id": "phone", "X-Content-SHA256": digest, "Content-Type": "audio/ogg"}
    with TestClient(app) as client:
        assert client.post("/api/v1/transcripts", headers=_auth(device_token), json=payload).status_code == 202
        first = client.put(f"/api/v1/audio/{event_id}/0", headers=headers, content=audio)
        second = client.put(f"/api/v1/audio/{event_id}/0", headers=headers, content=audio)
        third = client.put(f"/api/v1/audio/{event_id}/2", headers=headers, content=audio)
        conflict = client.put(f"/api/v1/audio/{event_id}/0", headers={**headers, "X-Content-SHA256": hashlib.sha256(b"other").hexdigest()}, content=b"other")
        manifest = client.get(f"/api/v1/audio/{event_id}/status", headers=_auth(device_token))
    assert first.status_code == 200 and first.json()["created"] is True
    assert second.status_code == 200 and second.json()["created"] is False
    assert third.status_code == 200 and third.json()["created"] is True
    assert conflict.status_code == 409
    assert manifest.status_code == 200
    assert manifest.json()["sequences"] == [0, 2]
    assert manifest.json()["missing"] == [1]
    assert manifest.json()["complete"] is False
    archived = app.state.archive_store.by_event(event_id)
    chunk = app.state.archive_store.audio_chunk(archived["id"], 0)
    assert chunk is not None
    path = __import__("pathlib").Path(chunk["path"])
    assert path.read_bytes() == audio
    assert app.state.archive.read_audio(path) == audio

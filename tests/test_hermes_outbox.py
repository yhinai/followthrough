from __future__ import annotations

from fastapi.testclient import TestClient

from followthrough.app import create_app
from followthrough.store import Store


def test_hermes_outbox_is_idempotent_and_drives_run_lifecycle(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    run_id = store.create_run("omi", "repository", "Followthrough signal", "archive-1")
    values = {
        "job_id": "job-1",
        "run_id": run_id,
        "archive_id": "archive-1",
        "event_id": "event-outbox-1",
        "idempotency_key": "followthrough:archive-1:research:v3",
        "capsule_path": str(tmp_path / "jobs" / "job-1.json"),
        "category": "repository",
        "entity": "https://github.com/example/project",
    }
    first, created = store.create_hermes_job(**values)
    replay, replay_created = store.create_hermes_job(**{**values, "job_id": "job-2"})
    assert created is True
    assert replay_created is False
    assert first["id"] == replay["id"] == "job-1"
    assert len(store.pending_hermes_jobs()) == 1

    store.mark_hermes_dispatching("job-1")
    assert store.hermes_job("job-1")["attempts"] == 1
    store.mark_hermes_enqueued("job-1", "task-1")
    assert store.hermes_job_for_run(run_id)["state"] == "enqueued"
    assert store.get_run(run_id)["status"] == "queued"
    assert len(store.unnotified_hermes_jobs()) == 1

    store.mark_hermes_notification("job-1", "subscribed")
    assert store.unnotified_hermes_jobs() == []
    store.sync_hermes_job("job-1", "in_progress")
    assert store.get_run(run_id)["status"] == "running"
    store.sync_hermes_job("job-1", "completed", summary="cited result")
    run = store.get_run(run_id)
    assert run["status"] == "completed"
    assert run["success"] == 1
    assert run["finished_at"] is not None
    assert store.hermes_job_counts() == {"completed": 1}


def test_policy_revision_supersedes_and_audits_parked_task(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    run_id = store.create_run("omi", "repository", "Followthrough signal", "archive-policy")
    store.create_hermes_job(
        job_id="job-policy",
        run_id=run_id,
        archive_id="archive-policy",
        event_id="event-policy",
        idempotency_key="followthrough:archive-policy:research:v2",
        capsule_path=str(tmp_path / "job.json"),
        category="repository",
        entity="https://github.com/example/project",
    )
    store.mark_hermes_enqueued("job-policy", "task-v1")
    store.supersede_hermes_task(
        run_id,
        expected_task_id="task-v1",
        idempotency_key="followthrough:archive-policy:research:v3",
        reason="mandatory runner policy revision",
        replacement_entity="verify the bounded recovery receipt",
    )
    job = store.hermes_job("job-policy")
    assert job["task_id"] is None
    assert job["state"] == "retry"
    assert job["idempotency_key"].endswith(":v3")
    assert job["entity"] == "verify the bounded recovery receipt"
    assert store.kanban_record_reconciled(
        run_id,
        task_id="task-v1",
        state="needs_attention",
        hermes_status="blocked",
        latest_outcome="blocked",
        diagnostics=(),
    ) is False
    job = store.hermes_job("job-policy")
    assert job["task_id"] is None
    assert job["state"] == "retry"
    history = store.db.execute("SELECT * FROM hermes_task_history").fetchone()
    assert history["task_id"] == "task-v1"
    assert history["outcome"] == "superseded"


def test_create_retry_exhaustion_dead_letters_and_stops_selection(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    run_id = store.create_run("phone", "tool", "Followthrough signal", "archive-retry-cap")
    store.create_hermes_job(
        job_id="job-retry-cap",
        run_id=run_id,
        archive_id="archive-retry-cap",
        event_id="event-retry-cap",
        idempotency_key="followthrough:archive-retry-cap:research:v3",
        capsule_path=str(tmp_path / "job.json"),
        category="tool",
        entity="bounded tool",
    )
    for _ in range(5):
        assert len(store.kanban_pending_create(limit=1)) == 1
        store.kanban_record_create_failure(run_id, error="temporary_failure")

    job = store.hermes_job("job-retry-cap")
    assert job["state"] == "dead_letter"
    assert job["attempts"] == 5
    assert store.kanban_pending_create(limit=1) == []
    assert store.get_run(run_id)["status"] == "failed"


def test_mid_create_park_is_not_overwritten_by_created_receipt(tmp_path) -> None:
    from followthrough.controls import ControlPlane

    store = Store(tmp_path / "operations.db")
    controls = ControlPlane(store)
    run_id = store.create_run("phone", "repository", "Followthrough signal", "archive-race")
    store.create_hermes_job(
        job_id="job-race",
        run_id=run_id,
        archive_id="archive-race",
        event_id="event-race",
        idempotency_key="followthrough:archive-race:research:v3",
        capsule_path=str(tmp_path / "job.json"),
        category="repository",
        entity="owner/repo",
    )
    assert store.kanban_pending_create(limit=1)
    controls.park_run(run_id, actor="owner", reason_code="emergency_stop")

    accepted = store.kanban_record_created(
        run_id,
        task_id="task-created-during-stop",
        idempotency_key="followthrough:archive-race:research:v3",
        capsule_path=str(tmp_path / "capsule.json"),
        hermes_status="ready",
    )

    assert accepted is False
    job = store.hermes_job("job-race")
    assert job["state"] == "parked"
    assert job["task_id"] == "task-created-during-stop"


def test_notification_failures_are_bounded(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    run_id = store.create_run("phone", "tool", "Followthrough signal", "archive-notify-cap")
    store.create_hermes_job(
        job_id="job-notify-cap",
        run_id=run_id,
        archive_id="archive-notify-cap",
        event_id="event-notify-cap",
        idempotency_key="followthrough:archive-notify-cap:research:v3",
        capsule_path=str(tmp_path / "job.json"),
        category="tool",
        entity="bounded tool",
        discord_chat_id="123",
        discord_user_id="123",
    )
    store.mark_hermes_enqueued("job-notify-cap", "task-notify-cap")
    store.kanban_record_reconciled(
        run_id,
        task_id="task-notify-cap",
        state="completed",
        hermes_status="done",
        latest_outcome="completed",
        summary="Verified result",
        diagnostics=[],
    )
    for _ in range(5):
        assert len(store.kanban_pending_notifications(limit=1)) == 1
        store.kanban_record_notification_failure(run_id, error="discord_unavailable")

    job = store.hermes_job("job-notify-cap")
    assert job["notification_attempts"] == 5
    assert job["notification_state"] == "failed"
    assert store.kanban_pending_notifications(limit=1) == []


def test_web_task_discord_waits_for_authoritative_h_answer(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    run_id = store.create_run("phone", "web_task", "Followthrough signal", "archive-web")
    job, _ = store.create_hermes_job(
        job_id="job-web-notify",
        run_id=run_id,
        archive_id="archive-web",
        event_id="event-web-notify",
        idempotency_key="followthrough:archive-web:research:v3",
        capsule_path=str(tmp_path / "web.json"),
        category="web_task",
        entity="Check a current price",
        discord_chat_id="12345",
    )
    store.mark_hermes_enqueued(job["id"], "task-web-notify")
    store.sync_hermes_job(job["id"], "completed", summary="Worker finished first")
    session = store.create_computer_session(
        task="Check a current price",
        agent="h/web-surfer-flash",
        source_event_id="event-web-notify",
    )
    store.update_computer_session(session["id"], state="running", step_count=4)

    assert store.kanban_pending_notifications(limit=1) == []

    store.update_computer_session(
        session["id"],
        state="completed",
        step_count=7,
        latest_answer="Authoritative H answer",
        finished_at="2026-07-12T20:00:07+00:00",
    )
    pending = store.kanban_pending_notifications(limit=1)
    assert pending[0]["result_summary"] == "Authoritative H answer"
    assert pending[0]["computer_steps"] == 7


def test_false_positive_can_cancel_only_a_nonrunning_job(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    run_id = store.create_run("omi", "todo", "Followthrough signal", "archive-false")
    store.add_operational_memory("archive-false", run_id, "f" * 64, "todo", "bounded task")
    store.create_hermes_job(
        job_id="job-false",
        run_id=run_id,
        archive_id="archive-false",
        event_id="event-false",
        idempotency_key="followthrough:archive-false:research:v3",
        capsule_path=str(tmp_path / "false.json"),
        category="todo",
        entity="bounded task",
    )
    store.mark_hermes_enqueued("job-false", "task-false")
    store.sync_hermes_job("job-false", "needs_attention")

    assert store.cancel_nonrunning_hermes_job(
        run_id,
        reason="owner_false_positive",
    ) is True
    assert store.hermes_job("job-false")["state"] == "cancelled"
    assert store.get_run(run_id)["status"] == "cancelled"
    assert store.list_operational_memories() == []
    history = store.db.execute("SELECT * FROM hermes_task_history").fetchone()
    assert history["outcome"] == "cancelled"


def test_hermes_outbox_retries_without_storing_raw_transcript(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    run_id = store.create_run("omi", "tool", "Followthrough signal", "archive-2")
    raw = "private raw transcript that must not enter the outbox"
    store.create_hermes_job(
        job_id="job-2",
        run_id=run_id,
        archive_id="archive-2",
        event_id="event-outbox-2",
        idempotency_key="followthrough:archive-2:research:v3",
        capsule_path=str(tmp_path / "jobs" / "job-2.json"),
        category="tool",
        entity="the identified opportunity",
    )
    store.mark_hermes_dispatching("job-2")
    store.mark_hermes_retry("job-2", "temporary CLI failure")
    job = store.hermes_job("job-2")
    assert job["state"] == "retry"
    assert job["attempts"] == 1
    assert raw.encode() not in (tmp_path / "operations.db").read_bytes()


def test_actionable_ingestion_commits_durable_job_before_ack(configured_settings) -> None:
    settings, _, device_token = configured_settings
    settings.kanban_enabled = True
    settings.auto_send = True
    app = create_app(settings)
    raw = "Research https://github.com/astral-sh/uv for this private durable outbox test"
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers={"Authorization": f"Bearer {device_token}"},
            json={"event_id": "event-durable-outbox-01", "device_id": "phone", "text": raw, "source": "phone", "consent": True},
        )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["orchestrator"] == "hermes-kanban"
    jobs = app.state.store.list_hermes_jobs()
    assert len(jobs) == 1
    assert jobs[0]["state"] == "pending"
    assert jobs[0]["entity"] == "https://github.com/astral-sh/uv"
    assert jobs[0]["discord_chat_id"]
    assert raw.encode() not in settings.db_path.read_bytes()
    assert raw.encode() not in settings.archive_db_path.read_bytes()

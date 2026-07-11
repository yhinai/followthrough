from __future__ import annotations

import time

from fastapi.testclient import TestClient

from followthrough.app import create_app
from followthrough.controls import Capability, ControlPlane, GlobalMode
from followthrough.store import Store


def _job(store: Store, tmp_path, *, with_task: bool = True) -> str:
    run_id = store.create_run("omi", "repository", "Followthrough signal", "archive-control")
    store.create_hermes_job(
        job_id="job-control",
        run_id=run_id,
        archive_id="archive-control",
        event_id="event-control",
        idempotency_key="followthrough:archive-control:research:v3",
        capsule_path=str(tmp_path / "capsule.json"),
        category="repository",
        entity="owner/repository",
    )
    if with_task:
        store.mark_hermes_enqueued("job-control", "task-control")
        store.sync_hermes_job("job-control", "in_progress")
    return run_id


def test_global_pause_persists_parks_and_explicitly_resumes(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    controls = ControlPlane(store)
    run_id = _job(store, tmp_path)

    paused = controls.set_global_mode(
        GlobalMode.PAUSED,
        actor="owner:test",
        reason_code="operator_pause",
    )

    assert paused["affected_jobs"] == 1
    assert store.hermes_job_for_run(run_id)["state"] == "park_requested"
    command = controls.pending_task_commands()[0]
    assert command["action"] == "park"
    controls.record_task_command_applied(command["id"])
    assert store.hermes_job_for_run(run_id)["state"] == "parked"
    assert store.get_run(run_id)["status"] == "paused"

    restarted = ControlPlane(Store(tmp_path / "operations.db"))
    assert restarted.status()["global"]["mode"] == "paused"
    running = restarted.set_global_mode(
        GlobalMode.RUNNING,
        actor="owner:test",
        reason_code="operator_resume",
        resume_parked=True,
    )
    assert running["affected_jobs"] == 1
    resume = restarted.pending_task_commands()[0]
    assert resume["action"] == "resume"
    restarted.record_task_command_applied(resume["id"])
    assert restarted.store.hermes_job_for_run(run_id)["state"] == "enqueued"


def test_global_kill_and_capability_switches_fail_closed(tmp_path) -> None:
    controls = ControlPlane(Store(tmp_path / "operations.db"))
    assert controls.authorize(
        Capability.LISTENING,
        idempotency_key="capture-before-kill",
        actor="phone",
    ).allowed
    controls.set_global_mode("paused", actor="owner:test", reason_code="pause_actions")
    assert controls.operation_allowed(Capability.LISTENING)
    assert not controls.operation_allowed(Capability.ACTIONS)

    controls.set_global_mode("killed", actor="owner:test", reason_code="emergency_stop")
    denied = controls.authorize(
        Capability.LISTENING,
        idempotency_key="capture-1",
        actor="phone",
    )
    assert not denied.allowed
    assert denied.reason_code == "global_kill_active"
    replay_after_kill = controls.authorize(
        Capability.LISTENING,
        idempotency_key="capture-before-kill",
        actor="phone",
    )
    assert not replay_after_kill.allowed
    assert replay_after_kill.reason_code == "global_kill_active"

    controls.set_global_mode("running", actor="owner:test", reason_code="controlled_restart")
    controls.set_capability(
        Capability.MESSAGES,
        False,
        actor="owner:test",
        reason_code="silence_notifications",
    )
    assert not controls.operation_allowed(Capability.MESSAGES)
    assert controls.operation_allowed(Capability.ACTIONS)


def test_idempotent_rate_and_cost_budgets(tmp_path) -> None:
    controls = ControlPlane(Store(tmp_path / "operations.db"))
    controls.set_limit(
        Capability.MESSAGES,
        max_events=1,
        window_seconds=3_600,
        max_cost_usd=None,
        actor="owner:test",
        reason_code="one_message_test",
    )
    first = controls.authorize(
        Capability.MESSAGES,
        idempotency_key="message-1",
        actor="orchestrator",
    )
    replay = controls.authorize(
        Capability.MESSAGES,
        idempotency_key="message-1",
        actor="orchestrator",
    )
    denied = controls.authorize(
        Capability.MESSAGES,
        idempotency_key="message-2",
        actor="orchestrator",
    )
    assert first.allowed and replay.allowed and replay.replay
    assert replay.receipt_id == first.receipt_id
    assert not denied.allowed and denied.reason_code == "rate_limit_exceeded"

    controls.set_limit(
        Capability.PURCHASES,
        max_events=5,
        window_seconds=86_400,
        max_cost_usd=10.0,
        actor="owner:test",
        reason_code="purchase_budget_test",
    )
    assert controls.authorize(
        Capability.PURCHASES,
        idempotency_key="purchase-1",
        actor="effector",
        cost_usd=8,
    ).allowed
    purchase = controls.authorize(
        Capability.PURCHASES,
        idempotency_key="purchase-2",
        actor="effector",
        cost_usd=3,
    )
    assert not purchase.allowed and purchase.reason_code == "budget_limit_exceeded"
    assert controls.status()["global"]["mode"] == "paused"
    assert controls.audit_log()[0]["kind"] == "safe_mode_activated"


def test_safe_mode_trigger_parks_active_work(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    controls = ControlPlane(store)
    run_id = _job(store, tmp_path)
    result = controls.trigger_safe_mode(
        "credential_access",
        actor="repository-policy-scanner",
        details={"finding_count": 1},
    )
    assert result["mode"] == "paused"
    assert result["affected_jobs"] == 1
    assert store.hermes_job_for_run(run_id)["state"] == "park_requested"


def test_audit_receipts_are_hash_chained_and_tamper_evident(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    controls = ControlPlane(store)
    controls.set_global_mode("paused", actor="owner:test", reason_code="audit_test")
    controls.set_capability(
        "messages", False, actor="owner:test", reason_code="audit_test"
    )
    assert controls.verify_audit_chain()
    store.db.execute("UPDATE control_audit SET reason_code='tampered' WHERE sequence=1")
    store.db.commit()
    assert not controls.verify_audit_chain()


def test_processing_task_command_is_recovered_after_worker_crash(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    controls = ControlPlane(store)
    _job(store, tmp_path)
    controls.set_global_mode("paused", actor="owner:test", reason_code="crash_recovery")
    first = controls.pending_task_commands()[0]
    assert store.db.execute(
        "SELECT state FROM control_task_commands WHERE id=?", (first["id"],)
    ).fetchone()[0] == "processing"

    recovered = ControlPlane(Store(tmp_path / "operations.db")).pending_task_commands()[0]
    assert recovered["id"] == first["id"]


def test_emergency_kill_supersedes_a_pending_resume(tmp_path) -> None:
    store = Store(tmp_path / "operations.db")
    controls = ControlPlane(store)
    run_id = _job(store, tmp_path)
    controls.set_global_mode("paused", actor="owner:test", reason_code="initial_pause")
    park = controls.pending_task_commands()[0]
    controls.record_task_command_applied(park["id"])
    controls.set_global_mode(
        "running",
        actor="owner:test",
        reason_code="resume_requested",
        resume_parked=True,
    )
    resume = controls.pending_task_commands()[0]
    assert resume["action"] == "resume"

    controls.set_global_mode("killed", actor="owner:test", reason_code="new_emergency")
    assert store.db.execute(
        "SELECT state FROM control_task_commands WHERE id=?", (resume["id"],)
    ).fetchone()[0] == "cancelled"
    controls.record_task_command_applied(resume["id"])
    assert store.hermes_job_for_run(run_id)["state"] == "park_requested"
    pending = controls.pending_task_commands()
    assert len(pending) == 1 and pending[0]["action"] == "park"


def test_emergency_api_persists_inside_rto_and_blocks_new_capture(configured_settings) -> None:
    settings, dashboard_token, device_token = configured_settings
    app = create_app(settings)
    started = time.monotonic()
    with TestClient(app) as client:
        response = client.post(
            "/api/controls/global",
            headers={"Authorization": f"Bearer {dashboard_token}"},
            json={
                "mode": "killed",
                "reason_code": "operator_emergency",
                "actor": "owner:test",
            },
        )
        assert response.status_code == 200
        blocked = client.post(
            "/api/v1/transcripts",
            headers={"Authorization": f"Bearer {device_token}"},
            json={
                "event_id": "event-killed-capture-01",
                "device_id": "phone",
                "text": "Research https://github.com/example/project",
                "source": "phone",
                "consent": True,
            },
        )
        assert blocked.status_code == 503
        assert blocked.json()["detail"] == "global_kill_active"
    assert time.monotonic() - started < settings.emergency_control_rto_seconds
    assert app.state.archive_store.by_event("event-killed-capture-01") is None

    restarted = create_app(settings)
    with TestClient(restarted) as client:
        status = client.get(
            "/api/controls", headers={"Authorization": f"Bearer {dashboard_token}"}
        )
        assert status.json()["global"]["mode"] == "killed"

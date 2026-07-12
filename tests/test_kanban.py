from __future__ import annotations

import json
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from followthrough.controls import ControlPlane
from followthrough.kanban import (
    BOARD,
    HERMES_MODULE,
    HERMES_PYTHON,
    MAX_TIMEOUT_SECONDS,
    CapsuleWriter,
    CardReceipt,
    DurableOrchestrator,
    HermesKanbanClient,
    TaskCapsule,
    map_task_status,
)
from followthrough.effectors.models import EffectKind
from followthrough.store import Store


SECRET_TRANSCRIPT = "private meeting transcript must never enter argv"


class FakeRunner:
    def __init__(self, responses: Sequence[subprocess.CompletedProcess[str]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        assert self.responses, f"unexpected command: {argv}"
        return self.responses.pop(0)


def completed(stdout: str = "", *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_capsule_writer_has_exact_schema_owner_mode_and_no_transcript(tmp_path: Path) -> None:
    capsule = TaskCapsule.from_mapping(
        {
            "run_id": "run-42",
            "archive_id": "archive-9",
            "category": "tool",
            "entity": "example/repository",
            "intent": "Research it",
            "acceptance": ["Cited findings", "Safe next action"],
            "transcript": SECRET_TRANSCRIPT,
            "raw_audio": b"not-for-hermes",
        }
    )

    path = CapsuleWriter(tmp_path / "capsules").write(capsule)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert set(payload) == {
        "run_id",
        "archive_id",
        "category",
        "entity",
        "intent",
        "acceptance",
    }
    assert "transcript" not in payload
    assert "raw_audio" not in payload
    assert SECRET_TRANSCRIPT not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_ensure_board_creates_missing_board_and_verifies_it() -> None:
    runner = FakeRunner(
        [
            completed("\x1b[36mHermes\x1b[0m\n[]\n"),
            completed("created followthrough\n"),
            completed('[{"id":"followthrough","archived":false}]\n'),
        ]
    )
    client = HermesKanbanClient(runner=runner, timeout_seconds=999)

    board = client.ensure_board()

    assert board["id"] == BOARD
    assert runner.calls[1][0] == [
        str(HERMES_PYTHON),
        "-m",
        HERMES_MODULE,
        "kanban",
        "boards",
        "create",
        "followthrough",
        "--name",
        "Followthrough",
        "--description",
        "Durable sanitized work queue for Followthrough",
    ]
    for _argv, kwargs in runner.calls:
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == MAX_TIMEOUT_SECONDS


def test_create_goal_uses_a_safe_exact_argv_and_robust_json(tmp_path: Path) -> None:
    path = CapsuleWriter(tmp_path).write(
        TaskCapsule(
            run_id="run-1",
            archive_id="archive-1",
            category="repo",
            entity="owner/repo",
            intent="evaluate",
            acceptance=("report",),
        )
    )
    runner = FakeRunner(
        [completed('\x1b[32mready\x1b[0m\n{"id":"task-7","status":"ready"}\nbye')]
    )
    client = HermesKanbanClient(runner=runner)

    receipt = client.create_goal_card(
        run_id="run-1", archive_id="archive-1", capsule_path=path
    )

    assert receipt.task_id == "task-7"
    assert receipt.idempotency_key == "followthrough:archive-1:research:v3"
    argv, kwargs = runner.calls[0]
    assert argv[:3] == [str(HERMES_PYTHON), "-m", HERMES_MODULE]
    assert argv[3:] == [
        "kanban",
        "--board",
        "followthrough",
        "create",
        argv[7],
        "--body",
        'Use the followthrough-operator skill. Sanitized signal only: category=repo; entity=owner/repo; intent=evaluate; acceptance=["report"]; runner_evidence=pending. Use only web research and Kanban lifecycle tools. Never request filesystem, terminal, Docker, host git, package installers, browser control, memory, messaging, credentials, or repository execution. A separate deterministic service owns repository acquisition and sandboxing. Return cited findings, clearly mark missing runner evidence, and propose a safe typed next action.',
        "--assignee",
        "followthrough",
        "--workspace",
        "scratch",
        "--tenant",
        "followthrough",
        "--priority",
        "50",
        "--idempotency-key",
        "followthrough:archive-1:research:v3",
        "--max-runtime",
        "20m",
        "--max-retries",
        "3",
        "--goal",
        "--goal-max-turns",
        "6",
        "--created-by",
        "followthrough",
        "--skill",
        "followthrough-operator",
        "--json",
    ]
    assert argv[7].startswith("Research captured signal ")
    assert SECRET_TRANSCRIPT not in "\0".join(argv)
    assert kwargs["shell"] is False
    assert 0 < kwargs["timeout"] <= MAX_TIMEOUT_SECONDS


def test_create_goal_uses_supplied_supersession_idempotency_key(tmp_path: Path) -> None:
    path = CapsuleWriter(tmp_path).write(
        TaskCapsule("run-1", "archive-1", "repo", "owner/repo", "evaluate", ("report",))
    )
    runner = FakeRunner([completed('{"id":"task-8","status":"ready"}')])
    client = HermesKanbanClient(runner=runner)
    key = "followthrough:archive-1:research:v4"

    receipt = client.create_goal_card(
        run_id="run-1",
        archive_id="archive-1",
        capsule_path=path,
        idempotency_key=key,
    )

    argv = runner.calls[0][0]
    assert argv[argv.index("--idempotency-key") + 1] == key
    assert receipt.idempotency_key == key


def test_discord_subscription_is_verified_and_inspection_commands_are_json() -> None:
    runner = FakeRunner(
        [
            completed("subscribed\n"),
            completed(
                '{"subscriptions":[{"platform":"discord","chat_id":"123",'
                '"user_id":"456","notifier_profile":"default"}]}'
            ),
            completed('{"task":{"id":"task-7","status":"blocked"}}'),
            completed('{"runs":[{"id":"r1","outcome":"gave_up"}]}'),
            completed('{"diagnostics":[{"code":"retries_exhausted"}]}'),
        ]
    )
    client = HermesKanbanClient(runner=runner)

    subscription = client.subscribe_discord(task_id="task-7", chat_id="123", user_id="456")
    task = client.show_task("task-7")
    runs = client.task_runs("task-7")
    diagnostics = client.diagnostics("task-7")

    assert subscription["platform"] == "discord"
    assert task["status"] == "blocked"
    assert runs[-1]["outcome"] == "gave_up"
    assert diagnostics == {"diagnostics": [{"code": "retries_exhausted"}]}
    assert runner.calls[0][0][3:] == [
        "kanban",
        "--board",
        "followthrough",
        "notify-subscribe",
        "task-7",
        "--platform",
        "discord",
        "--chat-id",
        "123",
        "--user-id",
        "456",
        "--notifier-profile",
        "default",
    ]
    assert runner.calls[-1][0][3:] == [
        "kanban",
        "--board",
        "followthrough",
        "diagnostics",
        "--task",
        "task-7",
        "--json",
    ]


def test_emergency_park_and_resume_use_reclaim_and_verify_state() -> None:
    runner = FakeRunner(
        [
            completed("reassigned\n"),
            completed('{"task":{"id":"task-7","status":"ready","assignee":null}}'),
            completed("reassigned\n"),
            completed('{"task":{"id":"task-7","status":"ready","assignee":"followthrough"}}'),
        ]
    )
    client = HermesKanbanClient(runner=runner)

    parked = client.park_task("task-7", reason_code="global_pause")
    resumed = client.resume_task("task-7", reason_code="owner_resume")

    assert parked == {"task_id": "task-7", "status": "ready", "assignee": "none"}
    assert resumed == {
        "task_id": "task-7",
        "status": "ready",
        "assignee": "followthrough",
    }
    assert runner.calls[0][0][3:] == [
        "kanban",
        "--board",
        "followthrough",
        "reassign",
        "task-7",
        "none",
        "--reclaim",
        "--reason",
        "global_pause",
    ]
    assert runner.calls[2][0][3:] == [
        "kanban",
        "--board",
        "followthrough",
        "reassign",
        "task-7",
        "followthrough",
        "--reclaim",
        "--reason",
        "owner_resume",
    ]


@pytest.mark.parametrize(
    ("task", "runs", "diagnostics", "expected"),
    [
        ({"status": "done"}, [], [], "completed"),
        ({"status": "running"}, [], [], "in_progress"),
        ({"status": "ready"}, [], [], "queued"),
        ({"status": "ready", "assignee": None}, [], [], "needs_attention"),
        ({"status": "blocked"}, [], [], "needs_attention"),
        ({"status": "blocked"}, [{"outcome": "gave_up"}], [], "dead_letter"),
        (
            {"status": "blocked"},
            [{"outcome": "failed"}],
            [
                {
                    "task_id": "task-1",
                    "title": SECRET_TRANSCRIPT,
                    "diagnostics": [{"kind": "repeated_failures"}],
                }
            ],
            "dead_letter",
        ),
        ({"status": "ready", "archived": True}, [], [], "cancelled"),
        ({"status": "brand_new_state"}, [], [], "needs_attention"),
    ],
)
def test_map_task_status_is_deterministic(
    task: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    diagnostics: Any,
    expected: str,
) -> None:
    assert map_task_status(task, runs, diagnostics) == expected


class FakeStore:
    def __init__(self) -> None:
        self.create_rows: list[Mapping[str, object]] = [
            {
                "run_id": "run-1",
                "archive_id": "archive-1",
                "category": "repository",
                "entity": "owner/repo",
                "intent": "evaluate fit",
                "acceptance": ["cited report"],
                "idempotency_key": "followthrough:archive-1:research:v3",
                "transcript": SECRET_TRANSCRIPT,
            }
        ]
        self.notification_rows: list[Mapping[str, object]] = [
            {
                "run_id": "run-1",
                "task_id": "task-1",
                "discord_chat_id": "123",
                "discord_user_id": "456",
            }
        ]
        self.active_rows: list[Mapping[str, object]] = [
            {"run_id": "run-1", "task_id": "task-1", "event_id": "event-12345678"}
        ]
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.notified: list[tuple[str, dict[str, Any]]] = []
        self.reconciled: list[tuple[str, dict[str, Any]]] = []
        self.failures: list[tuple[str, str, str]] = []

    def kanban_pending_create(self, *, limit: int) -> Sequence[Mapping[str, object]]:
        return self.create_rows[:limit]

    def kanban_record_created(self, run_id: str, **kwargs: Any) -> None:
        self.created.append((run_id, kwargs))

    def kanban_record_create_failure(self, run_id: str, *, error: str) -> None:
        self.failures.append(("create", run_id, error))

    def kanban_pending_notifications(
        self, *, limit: int
    ) -> Sequence[Mapping[str, object]]:
        return self.notification_rows[:limit]

    def kanban_record_notified(
        self, run_id: str, *, subscription: Mapping[str, str]
    ) -> None:
        self.notified.append((run_id, dict(subscription)))

    def kanban_record_notification_failure(self, run_id: str, *, error: str) -> None:
        self.failures.append(("notify", run_id, error))

    def kanban_active(self, *, limit: int) -> Sequence[Mapping[str, object]]:
        return self.active_rows[:limit]

    def kanban_record_reconciled(self, run_id: str, **kwargs: Any) -> None:
        self.reconciled.append((run_id, kwargs))

    def kanban_record_reconcile_failure(self, run_id: str, *, error: str) -> None:
        self.failures.append(("reconcile", run_id, error))


class FakeClient:
    def __init__(self) -> None:
        self.board_calls = 0
        self.created_paths: list[Path] = []

    def ensure_board(self) -> Mapping[str, Any]:
        self.board_calls += 1
        return {"id": BOARD}

    def create_goal_card(
        self,
        *,
        run_id: str,
        archive_id: str,
        capsule_path: str | Path,
        runner_evidence: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CardReceipt:
        self.created_paths.append(Path(capsule_path))
        return CardReceipt(
            task_id="task-1",
            status="ready",
            idempotency_key=f"followthrough:{archive_id}:research:v3",
            payload={"id": "task-1", "status": "ready"},
        )

    def subscribe_discord(
        self, *, task_id: str, chat_id: str, user_id: str | None = None
    ) -> Mapping[str, Any]:
        return {
            "platform": "discord",
            "chat_id": chat_id,
            "user_id": user_id,
            "notifier_profile": "default",
            "display_name": SECRET_TRANSCRIPT,
        }

    def show_task(self, task_id: str) -> dict[str, Any]:
        return {"id": task_id, "status": "blocked", "body": SECRET_TRANSCRIPT}

    def task_runs(self, task_id: str) -> list[dict[str, Any]]:
        return [{"outcome": "gave_up", "output": SECRET_TRANSCRIPT}]

    def diagnostics(self, task_id: str) -> Any:
        return {
            "diagnostics": [
                {"code": "retries_exhausted", "message": SECRET_TRANSCRIPT}
            ]
        }


def test_orchestrator_creates_notifies_and_reconciles_without_raw_text(tmp_path: Path) -> None:
    store = FakeStore()
    client = FakeClient()
    orchestrator = DurableOrchestrator(
        client=client,  # type: ignore[arg-type]
        store=store,
        capsule_writer=CapsuleWriter(tmp_path / "capsules"),
    )

    result = orchestrator.run_once()

    assert result == {"created": 1, "notified": 1, "reconciled": 1, "errors": []}
    assert client.board_calls == 1
    assert store.failures == []
    assert store.created[0][1]["idempotency_key"] == "followthrough:archive-1:research:v3"
    assert store.notified[0][1] == {
        "platform": "discord",
        "chat_id": "123",
        "user_id": "456",
        "notifier_profile": "default",
    }
    assert store.reconciled[0][1]["state"] == "dead_letter"
    assert store.reconciled[0][1]["latest_outcome"] == "gave_up"
    assert store.reconciled[0][1]["diagnostics"] == ("retries_exhausted",)
    capsule_text = client.created_paths[0].read_text(encoding="utf-8")
    assert SECRET_TRANSCRIPT not in capsule_text
    assert SECRET_TRANSCRIPT not in repr(store.created + store.notified + store.reconciled)


class CompletedEffectClient(FakeClient):
    def show_task(self, task_id: str) -> dict[str, Any]:
        return {"id": task_id, "status": "done"}

    def task_runs(self, task_id: str) -> list[dict[str, Any]]:
        return [
            {
                "outcome": "completed",
                "metadata": {
                    "safe_next_action": {
                        "type": "private_task.create",
                        "parameters": {
                            "trigger_event_id": "untrusted-agent-value",
                            "title": "Review repository decision",
                            "description": "Use the verified receipt.",
                            "tags": ["followthrough"],
                        },
                    }
                },
            }
        ]


class FakeEffectService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, str, bool]] = []

    def submit(self, request: Any, *, idempotency_key: str, execute: bool) -> dict[str, Any]:
        self.calls.append((request, idempotency_key, execute))
        return {"state": "completed"}


class FailOnceEffectService(FakeEffectService):
    def submit(self, request: Any, *, idempotency_key: str, execute: bool) -> dict[str, Any]:
        self.calls.append((request, idempotency_key, execute))
        if len(self.calls) == 1:
            raise RuntimeError("temporary provider outage")
        return {"state": "completed"}


def test_completed_card_dispatches_typed_effect_with_event_bound_idempotency(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    store.create_rows = []
    store.notification_rows = []
    effects = FakeEffectService()
    orchestrator = DurableOrchestrator(
        client=CompletedEffectClient(),  # type: ignore[arg-type]
        store=store,
        capsule_writer=CapsuleWriter(tmp_path),
        effect_service=effects,  # type: ignore[arg-type]
    )

    result = orchestrator.run_once()

    assert result["effects"] == 1
    assert len(effects.calls) == 1
    request, idempotency_key, execute = effects.calls[0]
    assert request.kind == EffectKind.PRIVATE_TASK_CREATE
    assert request.trigger_event_id == "event-12345678"
    assert request.title == "Review repository decision"
    assert idempotency_key == (
        "followthrough:event-12345678:effect:private_task.create:v1"
    )
    assert execute is True


def test_transient_effect_failure_retries_before_terminal_reconciliation(tmp_path: Path) -> None:
    store = FakeStore()
    store.create_rows = []
    store.notification_rows = []
    effects = FailOnceEffectService()
    orchestrator = DurableOrchestrator(
        client=CompletedEffectClient(),  # type: ignore[arg-type]
        store=store,
        capsule_writer=CapsuleWriter(tmp_path),
        effect_service=effects,  # type: ignore[arg-type]
    )

    first = orchestrator.run_once()
    assert first["effects"] == 0
    assert store.reconciled == []
    assert first["errors"][0]["phase"] == "reconcile"

    second = orchestrator.run_once()
    assert second["effects"] == 1
    assert len(store.reconciled) == 1
    assert len(effects.calls) == 2
    assert effects.calls[0][1] == effects.calls[1][1]


class CreateRaceStore(FakeStore):
    def kanban_record_created(self, run_id: str, **kwargs: Any) -> bool:
        self.created.append((run_id, kwargs))
        return False


class CreateRaceClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.parked: list[tuple[str, str]] = []

    def park_task(self, task_id: str, *, reason_code: str) -> Mapping[str, Any]:
        self.parked.append((task_id, reason_code))
        return {"task_id": task_id, "status": "ready", "assignee": "none"}


def test_orchestrator_parks_card_when_local_state_changed_during_create(tmp_path: Path) -> None:
    store = CreateRaceStore()
    store.notification_rows = []
    store.active_rows = []
    client = CreateRaceClient()
    result = DurableOrchestrator(
        client=client,  # type: ignore[arg-type]
        store=store,
        capsule_writer=CapsuleWriter(tmp_path),
    ).run_once()

    assert result["created"] == 1
    assert client.parked == [("task-1", "state_changed_during_create")]


class NotificationFailureClient(FakeClient):
    def subscribe_discord(
        self, *, task_id: str, chat_id: str, user_id: str | None = None
    ) -> Mapping[str, Any]:
        raise RuntimeError(SECRET_TRANSCRIPT)


def test_notification_failure_is_sanitized_and_left_for_durable_retry(tmp_path: Path) -> None:
    store = FakeStore()
    store.create_rows = []
    store.active_rows = []
    orchestrator = DurableOrchestrator(
        client=NotificationFailureClient(),  # type: ignore[arg-type]
        store=store,
        capsule_writer=CapsuleWriter(tmp_path),
    )

    result = orchestrator.run_once()

    assert result["notified"] == 0
    assert store.failures == [("notify", "run-1", "runtimeerror")]
    assert SECRET_TRANSCRIPT not in repr(result)
    assert SECRET_TRANSCRIPT not in repr(store.failures)


class ParkingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.parked: list[tuple[str, str]] = []

    def park_task(self, task_id: str, *, reason_code: str) -> Mapping[str, Any]:
        self.parked.append((task_id, reason_code))
        return {"task_id": task_id, "status": "ready", "assignee": "none"}


def test_orchestrator_applies_emergency_park_before_respecting_pause(tmp_path: Path) -> None:
    store = Store(tmp_path / "operations.db")
    controls = ControlPlane(store)
    run_id = store.create_run("omi", "tool", "Followthrough signal", "archive-emergency")
    store.create_hermes_job(
        job_id="job-emergency",
        run_id=run_id,
        archive_id="archive-emergency",
        event_id="event-emergency",
        idempotency_key="followthrough:archive-emergency:research:v3",
        capsule_path=str(tmp_path / "capsule.json"),
        category="tool",
        entity="captured-tool",
    )
    store.mark_hermes_enqueued("job-emergency", "task-emergency")
    store.sync_hermes_job("job-emergency", "in_progress")
    controls.set_global_mode("paused", actor="owner:test", reason_code="orchestrator_test")
    client = ParkingClient()
    worker = DurableOrchestrator(
        client=client,  # type: ignore[arg-type]
        store=store,
        capsule_writer=CapsuleWriter(tmp_path / "capsules"),
        control_plane=controls,
    )

    result = worker.run_once()

    assert result["parked"] == 1
    assert result["created"] == 0
    assert client.parked == [("task-emergency", "global_paused")]
    assert store.hermes_job_for_run(run_id)["state"] == "parked"


def test_latest_summary_strips_ansi_and_control_characters() -> None:
    from followthrough.kanban import _latest_summary, _sanitize_summary

    hostile = "\x1b[31mDANGER\x1b[0m brief\x07 ready\x00\nnext line"
    cleaned = _sanitize_summary(hostile)
    assert "\x1b" not in cleaned
    assert "\x07" not in cleaned
    assert "\x00" not in cleaned
    assert "DANGER brief ready" in cleaned
    assert "next line" in cleaned

    runs = [{"summary": "\x1b[1mResearch complete\x1b[0m"}]
    assert _latest_summary(runs) == "Research complete"
    # A summary that is only control noise sanitizes to nothing, not a blank.
    assert _latest_summary([{"summary": "\x1b[0m\x00"}]) is None

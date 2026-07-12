"""Durable, transcript-free bridge to the Hermes Kanban dispatcher.

The bridge exposes only the bounded fields of a sanitized capsule in the task
body. Ambient transcript/audio content must never be placed in a process
argument, task title, task body, or error record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .controls import ControlPlane
    from .effectors.service import EffectService


BOARD = "followthrough"
WORKER_PROFILE = "followthrough"
HERMES_PYTHON = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
HERMES_MODULE = "hermes_cli.main"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 120.0

_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_OPAQUE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_SAFE_TOKEN = re.compile(r"[^a-z0-9_.:-]+")
_CAPSULE_KEYS = frozenset(
    {"run_id", "archive_id", "category", "entity", "intent", "acceptance"}
)


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
Runner = Callable[..., subprocess.CompletedProcess[str]]


class KanbanCommandError(RuntimeError):
    """A sanitized Hermes command failure that never includes command payloads."""


_PROMPT_DELIMITER = re.compile(r"</?\s*untrusted_transcript\s*>", re.IGNORECASE)


def _clean_text(value: object, *, maximum: int) -> str:
    """Normalize capsule fields without preserving controls or unbounded text.

    Capsule text is derived from speech and is therefore attacker-influenced.
    Defang the fence delimiters so a spoken phrase cannot close the untrusted
    region of a downstream worker prompt and have the rest read as trusted
    instructions.
    """

    if value is None:
        return ""
    cleaned = " ".join(str(value).split())
    cleaned = "".join(character for character in cleaned if character.isprintable())
    cleaned = _PROMPT_DELIMITER.sub("[redacted-delimiter]", cleaned)
    return cleaned[:maximum]


def _opaque_id(value: object, *, name: str) -> str:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be an opaque identifier")
    candidate = str(value)
    if not _OPAQUE_ID.fullmatch(candidate):
        raise ValueError(f"{name} must be an opaque identifier")
    return candidate


def _safe_token(value: object) -> str:
    """Return a bounded diagnostic token, never arbitrary source text."""

    token = _SAFE_TOKEN.sub("_", str(value).strip().lower())[:80]
    return token.strip("_") or "unknown"


def _decode_json(output: str) -> JsonValue:
    """Decode CLI JSON even when Hermes prints an ANSI banner around it."""

    cleaned = _ANSI_ESCAPE.sub("", output).strip()
    if not cleaned:
        raise KanbanCommandError("Hermes returned no JSON")

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise KanbanCommandError("Hermes returned malformed JSON")


def _records(value: JsonValue, *keys: str) -> list[dict[str, Any]]:
    """Normalize supported CLI list envelopes."""

    candidate: Any = value
    if isinstance(candidate, dict):
        for key in keys:
            nested = candidate.get(key)
            if isinstance(nested, list):
                candidate = nested
                break
    if not isinstance(candidate, list):
        raise KanbanCommandError("Hermes returned an unexpected JSON shape")
    return [dict(item) for item in candidate if isinstance(item, Mapping)]


def _object(value: JsonValue, *keys: str) -> dict[str, Any]:
    """Normalize supported CLI object envelopes."""

    if not isinstance(value, dict):
        raise KanbanCommandError("Hermes returned an unexpected JSON shape")
    for key in keys:
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return dict(nested)
    return dict(value)


@dataclass(frozen=True, slots=True)
class TaskCapsule:
    """The complete and intentionally small payload exposed to Hermes."""

    run_id: str
    archive_id: str
    category: str
    entity: str
    intent: str
    acceptance: tuple[str, ...]

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> TaskCapsule:
        raw_acceptance = row.get("acceptance", ())
        if isinstance(raw_acceptance, str):
            acceptance = (raw_acceptance,)
        elif isinstance(raw_acceptance, Sequence):
            acceptance = tuple(str(item) for item in raw_acceptance[:20])
        else:
            acceptance = ()
        return cls(
            run_id=_opaque_id(row.get("run_id"), name="run_id"),
            archive_id=_opaque_id(row.get("archive_id"), name="archive_id"),
            category=_clean_text(row.get("category"), maximum=80),
            entity=_clean_text(row.get("entity"), maximum=240),
            intent=_clean_text(row.get("intent"), maximum=500),
            acceptance=tuple(_clean_text(item, maximum=240) for item in acceptance),
        )

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "run_id": _opaque_id(self.run_id, name="run_id"),
            "archive_id": _opaque_id(self.archive_id, name="archive_id"),
            "category": _clean_text(self.category, maximum=80),
            "entity": _clean_text(self.entity, maximum=240),
            "intent": _clean_text(self.intent, maximum=500),
            "acceptance": [
                _clean_text(item, maximum=240) for item in self.acceptance[:20]
            ],
        }
        if set(payload) != _CAPSULE_KEYS:
            raise AssertionError("capsule schema changed unexpectedly")
        return payload


class CapsuleWriter:
    """Atomically writes sanitized Hermes task capsules with owner-only access."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def write(self, capsule: TaskCapsule) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        digest = hashlib.sha256(capsule.run_id.encode("utf-8")).hexdigest()[:24]
        destination = self.root / f"{digest}.json"
        payload = capsule.as_dict()

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return destination.resolve()


@dataclass(frozen=True, slots=True)
class CardReceipt:
    task_id: str
    status: str
    idempotency_key: str
    payload: Mapping[str, Any]


class HermesKanbanClient:
    """Small synchronous adapter around the stable Hermes Kanban CLI."""

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))

    @property
    def prefix(self) -> tuple[str, ...]:
        return (str(HERMES_PYTHON), "-m", HERMES_MODULE)

    def _run(self, arguments: Sequence[str], *, expect_json: bool) -> JsonValue | str:
        argv = [*self.prefix, *arguments]
        try:
            completed = self._runner(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise KanbanCommandError("Hermes command timed out") from error
        except OSError as error:
            raise KanbanCommandError("Hermes command could not start") from error
        if completed.returncode != 0:
            raise KanbanCommandError(
                f"Hermes command failed with exit code {completed.returncode}"
            )
        if expect_json:
            return _decode_json(completed.stdout)
        return completed.stdout

    def _list_boards(self) -> list[dict[str, Any]]:
        value = self._run(["kanban", "boards", "list", "--json"], expect_json=True)
        return _records(value, "boards", "items", "data")

    def ensure_board(self) -> Mapping[str, Any]:
        for board in self._list_boards():
            if board.get("id") == BOARD or board.get("slug") == BOARD:
                if bool(board.get("archived")):
                    raise KanbanCommandError("followthrough board is archived")
                return board

        self._run(
            [
                "kanban",
                "boards",
                "create",
                BOARD,
                "--name",
                "Followthrough",
                "--description",
                "Durable sanitized work queue for Followthrough",
            ],
            expect_json=False,
        )
        for board in self._list_boards():
            if board.get("id") == BOARD or board.get("slug") == BOARD:
                if bool(board.get("archived")):
                    raise KanbanCommandError("followthrough board is archived")
                return board
        raise KanbanCommandError("followthrough board creation was not observable")

    def create_goal_card(
        self,
        *,
        run_id: str,
        archive_id: str,
        capsule_path: str | Path,
        runner_evidence: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CardReceipt:
        safe_run_id = _opaque_id(run_id, name="run_id")
        safe_archive_id = _opaque_id(archive_id, name="archive_id")
        path = Path(capsule_path).expanduser().resolve(strict=True)
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("capsule must be an owner-only regular file")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("capsule must contain valid JSON") from error
        if not isinstance(loaded, Mapping) or set(loaded) != _CAPSULE_KEYS:
            raise ValueError("capsule schema is invalid")
        capsule = TaskCapsule.from_mapping(loaded)

        short_id = hashlib.sha256(safe_run_id.encode("utf-8")).hexdigest()[:10]
        idempotency_key = _opaque_id(
            idempotency_key or f"followthrough:{safe_archive_id}:research:v3",
            name="idempotency_key",
        )
        acceptance = json.dumps(list(capsule.acceptance), ensure_ascii=True, separators=(",", ":"))
        bounded_evidence = "pending"
        if runner_evidence is not None:
            allowed_evidence = {
                key: runner_evidence.get(key)
                for key in (
                    "status", "commit", "tree", "licenses", "finding_codes", "blocking",
                    "execution_kind", "exit_code", "timed_out", "sandbox_backend",
                    "network_enabled", "receipt_hash",
                )
            }
            bounded_evidence = json.dumps(
                allowed_evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )[:4000]
        body = (
            "Use the followthrough-operator skill. Sanitized signal only: "
            f"category={capsule.category}; entity={capsule.entity}; intent={capsule.intent}; "
            f"acceptance={acceptance}; runner_evidence={bounded_evidence}. "
            "Use only web research and Kanban lifecycle tools. Never request filesystem, terminal, Docker, "
            "host git, package installers, browser control, memory, messaging, credentials, or repository execution. "
            "A separate deterministic service owns repository acquisition and sandboxing. Return cited findings, "
            "clearly mark missing runner evidence, and propose a safe typed next action."
        )
        command = [
            "kanban",
            "--board",
            BOARD,
            "create",
            f"Research captured signal {short_id}",
            "--body",
            body,
            "--assignee",
            "none" if capsule.category == "web_task" else WORKER_PROFILE,
            "--workspace",
            "scratch",
            "--tenant",
            "followthrough",
            "--priority",
            "50",
            "--idempotency-key",
            idempotency_key,
            "--max-runtime",
            "20m",
            "--max-retries",
            "3",
        ]
        if capsule.category != "web_task":
            command.extend(["--goal", "--goal-max-turns", "6"])
        command.extend(["--created-by", "followthrough"])
        if capsule.category != "web_task":
            command.extend(["--skill", "followthrough-operator"])
        command.append("--json")
        value = self._run(command, expect_json=True)
        payload = _object(value, "task", "item", "data")
        raw_task_id = payload.get("id", payload.get("task_id"))
        task_id = _opaque_id(raw_task_id, name="task_id")
        status = _safe_token(payload.get("status", "unknown"))
        return CardReceipt(
            task_id=task_id,
            status=status,
            idempotency_key=idempotency_key,
            payload=payload,
        )

    def show_task(self, task_id: str) -> dict[str, Any]:
        safe_task_id = _opaque_id(task_id, name="task_id")
        value = self._run(
            ["kanban", "--board", BOARD, "show", safe_task_id, "--json"],
            expect_json=True,
        )
        return _object(value, "task", "item", "data")

    def task_runs(self, task_id: str) -> list[dict[str, Any]]:
        safe_task_id = _opaque_id(task_id, name="task_id")
        value = self._run(
            ["kanban", "--board", BOARD, "runs", safe_task_id, "--json"],
            expect_json=True,
        )
        return _records(value, "runs", "items", "data")

    def complete_from_runner(
        self,
        task_id: str,
        *,
        result: str,
        runner: str,
        receipt_url: str | None = None,
    ) -> Mapping[str, Any]:
        """Close a Hermes card from an authoritative typed runner receipt."""

        safe_task_id = _opaque_id(task_id, name="task_id")
        safe_result = _sanitize_summary(result)
        if not safe_result:
            raise ValueError("runner result is empty")
        safe_url = str(receipt_url or "")
        if safe_url and not safe_url.startswith("https://"):
            safe_url = ""
        self._run(
            [
                "kanban", "--board", BOARD, "reassign", safe_task_id, "none",
                "--reclaim", "--reason", "authoritative_runner_completed",
            ],
            expect_json=False,
        )
        metadata = json.dumps(
            {"runner": _safe_token(runner), "receipt_url": safe_url[:500]},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._run(
            [
                "kanban", "--board", BOARD, "complete", safe_task_id,
                "--result", safe_result, "--summary", safe_result,
                "--metadata", metadata,
            ],
            expect_json=False,
        )
        return self.show_task(safe_task_id)

    def diagnostics(self, task_id: str) -> JsonValue:
        safe_task_id = _opaque_id(task_id, name="task_id")
        value = self._run(
            ["kanban", "--board", BOARD, "diagnostics", "--task", safe_task_id, "--json"],
            expect_json=True,
        )
        if not isinstance(value, (dict, list)):
            raise KanbanCommandError("Hermes returned an unexpected diagnostics shape")
        return value

    def park_task(self, task_id: str, *, reason_code: str) -> Mapping[str, Any]:
        safe_task_id = _opaque_id(task_id, name="task_id")
        safe_reason = _safe_token(reason_code)
        self._run(
            [
                "kanban",
                "--board",
                BOARD,
                "reassign",
                safe_task_id,
                "none",
                "--reclaim",
                "--reason",
                safe_reason,
            ],
            expect_json=False,
        )
        task = self.show_task(safe_task_id)
        status = _safe_token(task.get("status", task.get("state", "unknown")))
        assignee = task.get("assignee", task.get("profile", task.get("assigned_profile")))
        if status in {"running", "in_progress"}:
            raise KanbanCommandError("Hermes task remained active after park")
        if assignee not in (None, "", "none"):
            raise KanbanCommandError("Hermes task remained assigned after park")
        return {"task_id": safe_task_id, "status": status, "assignee": "none"}

    def resume_task(self, task_id: str, *, reason_code: str) -> Mapping[str, Any]:
        safe_task_id = _opaque_id(task_id, name="task_id")
        safe_reason = _safe_token(reason_code)
        self._run(
            [
                "kanban",
                "--board",
                BOARD,
                "reassign",
                safe_task_id,
                WORKER_PROFILE,
                "--reclaim",
                "--reason",
                safe_reason,
            ],
            expect_json=False,
        )
        task = self.show_task(safe_task_id)
        status = _safe_token(task.get("status", task.get("state", "unknown")))
        assignee = task.get("assignee", task.get("profile", task.get("assigned_profile")))
        if assignee not in (None, "", WORKER_PROFILE):
            raise KanbanCommandError("Hermes task has unexpected assignee after resume")
        return {"task_id": safe_task_id, "status": status, "assignee": WORKER_PROFILE}


def _latest_outcome(runs: Sequence[Mapping[str, Any]] | None) -> str:
    if not runs:
        return "unknown"
    latest = runs[-1]
    for key in ("outcome", "status", "result"):
        if latest.get(key) is not None:
            return _safe_token(latest[key])
    return "unknown"


def _sanitize_summary(summary: str) -> str:
    """Strip terminal escapes and control characters from LLM-authored text.

    The run summary is model output over untrusted web research and is returned
    verbatim to the submitting device and dashboard. Removing ANSI escapes and
    C0/C1 control characters (keeping tab/newline) prevents injected terminal
    sequences or hidden control bytes from reaching those surfaces.
    """

    without_ansi = _ANSI_ESCAPE.sub("", summary)
    cleaned = "".join(
        character
        for character in without_ansi
        if character in "\t\n" or (0x20 <= ord(character) < 0x7F) or ord(character) >= 0xA0
    )
    return cleaned.strip()[:20_000]


def _latest_summary(runs: Sequence[Mapping[str, Any]] | None) -> str | None:
    if not runs:
        return None
    summary = runs[-1].get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    sanitized = _sanitize_summary(summary)
    return sanitized or None


def _discord_summary(value: object, limit: int = 850) -> str:
    """Bound a result at a readable Markdown boundary, never mid-link/word."""

    clean = _sanitize_summary(str(value or "Completed successfully.")) or "Completed successfully."
    if len(clean) <= limit:
        return clean
    window = clean[:limit]
    boundary = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
    if boundary < limit // 2:
        boundary = window.rfind(" ")
    return f"{window[:max(1, boundary)].rstrip(' .,:;-')}…"


def _diagnostic_codes(diagnostics: JsonValue | None) -> tuple[str, ...]:
    if diagnostics is None:
        return ()
    candidates: list[Any]
    if isinstance(diagnostics, list):
        candidates = diagnostics
    elif isinstance(diagnostics, dict):
        nested = diagnostics.get("diagnostics", diagnostics.get("items"))
        candidates = nested if isinstance(nested, list) else [diagnostics]
    else:
        return ()

    codes: list[str] = []
    for item in candidates:
        if isinstance(item, Mapping):
            nested = item.get("diagnostics")
            if isinstance(nested, list):
                codes.extend(_diagnostic_codes(nested))
            for key in ("code", "kind", "type", "reason", "outcome"):
                if item.get(key) is not None:
                    codes.append(_safe_token(item[key]))
        elif isinstance(item, str):
            codes.append(_safe_token(item))
    return tuple(dict.fromkeys(codes))


def map_task_status(
    task: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]] | None = None,
    diagnostics: JsonValue | None = None,
) -> str:
    """Map Hermes' task/run state into Followthrough's deterministic state set."""

    status = _safe_token(task.get("status", task.get("state", "unknown")))
    outcome = _latest_outcome(runs)
    codes = set(_diagnostic_codes(diagnostics))
    gave_up = outcome == "gave_up" or "gave_up" in codes
    exhausted = bool(codes & {"repeated_failures", "repeated_crashes", "retries_exhausted"})

    if status in {"done", "completed", "complete"}:
        return "completed"
    if bool(task.get("archived")) or status in {"archived", "cancelled", "canceled"}:
        return "cancelled"
    if status in {"gave_up", "dead_letter"} or (status == "blocked" and (gave_up or exhausted)):
        return "dead_letter"
    # Hermes represents a deliberately parked/reclaimed task as ``ready`` with
    # an explicit null assignee.  Treating that state as ordinary queued work
    # would hide a policy stop and make an operator think execution can resume.
    if status == "ready" and "assignee" in task and not task.get("assignee"):
        return "needs_attention"
    if status in {"running", "in_progress", "review"}:
        return "in_progress"
    if status in {"ready", "todo", "triage", "scheduled", "queued", "pending"}:
        return "queued"
    if status in {"blocked", "failed", "error"}:
        return "needs_attention"
    return "needs_attention"


@runtime_checkable
class KanbanStore(Protocol):
    """Durable storage contract consumed by :class:`DurableOrchestrator`.

    Implementations must enforce unique run, archive, task, and idempotency
    identifiers. Pending queries must return only the sanitized fields named by
    each method; they must never return transcript or original-audio content.
    """

    def kanban_pending_create(self, *, limit: int) -> Sequence[Mapping[str, object]]:
        """Return run_id, archive_id, category, entity, intent, and acceptance."""

        ...

    def kanban_record_created(
        self,
        run_id: str,
        *,
        task_id: str,
        idempotency_key: str,
        capsule_path: str,
        hermes_status: str,
    ) -> bool: ...

    def kanban_record_create_failure(self, run_id: str, *, error: str) -> None: ...

    def kanban_pending_notifications(
        self, *, limit: int
    ) -> Sequence[Mapping[str, object]]:
        """Return completed results waiting for durable Discord delivery."""

        ...

    def kanban_record_notified(
        self, run_id: str, *, receipt: Mapping[str, str]
    ) -> None: ...

    def kanban_record_notification_failure(self, run_id: str, *, error: str) -> None: ...

    def kanban_active(self, *, limit: int) -> Sequence[Mapping[str, object]]:
        """Return only run_id and task_id for cards requiring reconciliation."""

        ...

    def kanban_record_reconciled(
        self,
        run_id: str,
        *,
        task_id: str,
        state: str,
        hermes_status: str,
        latest_outcome: str,
        summary: str | None,
        diagnostics: Sequence[str],
    ) -> None: ...

    def kanban_record_reconcile_failure(self, run_id: str, *, error: str) -> None: ...


class DurableOrchestrator:
    """One event-driven, retry-safe pass over the durable Hermes outbox."""

    def __init__(
        self,
        *,
        client: HermesKanbanClient,
        store: KanbanStore,
        capsule_writer: CapsuleWriter,
        repository_evaluator: Any | None = None,
        control_plane: ControlPlane | None = None,
        effect_service: EffectService | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.capsule_writer = capsule_writer
        self.repository_evaluator = repository_evaluator
        self.control_plane = control_plane
        self.effect_service = effect_service
        self._board_ready = False

    @staticmethod
    def _error_code(error: BaseException) -> str:
        return _safe_token(type(error).__name__)

    @staticmethod
    def _run_reference(row: Mapping[str, object]) -> str:
        try:
            return _opaque_id(row.get("run_id"), name="run_id")
        except ValueError:
            digest = hashlib.sha256(str(row.get("run_id")).encode("utf-8")).hexdigest()[:12]
            return f"invalid-{digest}"

    def run_once(
        self,
        *,
        create_limit: int = 10,
        notification_limit: int = 20,
        reconcile_limit: int = 50,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "created": 0,
            "notified": 0,
            "reconciled": 0,
            "errors": [],
        }
        errors: list[dict[str, str]] = result["errors"]  # type: ignore[assignment]
        if self.effect_service is not None:
            result["effects"] = 0

        if self.control_plane is not None:
            from .controls import Capability

            result["parked"] = 0
            result["resumed"] = 0
            commands = self.control_plane.pending_task_commands(limit=20)
            if commands and not self._board_ready:
                self.client.ensure_board()
                self._board_ready = True
            for command in commands:
                try:
                    if command["action"] == "park":
                        self.client.park_task(
                            command["task_id"], reason_code=command["reason_code"]
                        )
                        result["parked"] = int(result["parked"]) + 1
                    elif command["action"] == "resume":
                        self.client.resume_task(
                            command["task_id"], reason_code=command["reason_code"]
                        )
                        result["resumed"] = int(result["resumed"]) + 1
                    else:
                        raise ValueError("unknown task control command")
                    self.control_plane.record_task_command_applied(command["id"])
                except Exception as error:
                    code = self._error_code(error)
                    self.control_plane.record_task_command_failure(command["id"], code)
                    errors.append(
                        {
                            "phase": "control",
                            "run_id": command["run_id"],
                            "error": code,
                        }
                    )

            if not self.control_plane.operation_allowed(
                Capability.ACTIONS
            ) or not self.control_plane.operation_allowed(Capability.SESSIONS):
                return result

        if not self._board_ready:
            self.client.ensure_board()
            self._board_ready = True

        for row in self.store.kanban_pending_create(limit=max(0, create_limit)):
            run_reference = self._run_reference(row)
            try:
                if self.control_plane is not None:
                    from .controls import Capability

                    action = self.control_plane.authorize(
                        Capability.ACTIONS,
                        idempotency_key=f"kanban-action:{run_reference}",
                        actor="orchestrator",
                    )
                    session = self.control_plane.authorize(
                        Capability.SESSIONS,
                        idempotency_key=f"kanban-session:{run_reference}",
                        actor="orchestrator",
                    )
                    if not action.allowed or not session.allowed:
                        self.store.kanban_record_create_failure(
                            run_reference,
                            error=(action.reason_code if not action.allowed else session.reason_code),
                        )
                        continue
                capsule = TaskCapsule.from_mapping(row)
                path = self.capsule_writer.write(capsule)
                runner_evidence = None
                if capsule.category == "repository" and self.repository_evaluator is not None:
                    runner_evidence = self.repository_evaluator.evaluate(capsule.entity).for_card()
                receipt = self.client.create_goal_card(
                    run_id=capsule.run_id,
                    archive_id=capsule.archive_id,
                    capsule_path=path,
                    runner_evidence=runner_evidence,
                    idempotency_key=_opaque_id(
                        row.get("idempotency_key"), name="idempotency_key"
                    ),
                )
                accepted = self.store.kanban_record_created(
                    capsule.run_id,
                    task_id=receipt.task_id,
                    idempotency_key=receipt.idempotency_key,
                    capsule_path=str(path),
                    hermes_status=receipt.status,
                )
                if accepted is False:
                    # An emergency control or cancellation won the race while
                    # Hermes was creating the card. Never revive the local job;
                    # immediately park the newly observable external card.
                    self.client.park_task(
                        receipt.task_id, reason_code="state_changed_during_create"
                    )
                result["created"] = int(result["created"]) + 1
            except Exception as error:  # retry ownership belongs to the durable store
                code = self._error_code(error)
                try:
                    self.store.kanban_record_create_failure(run_reference, error=code)
                except Exception:
                    code = "store_failure"
                errors.append({"phase": "create", "run_id": run_reference, "error": code})

        for row in self.store.kanban_active(limit=max(0, reconcile_limit)):
            run_reference = self._run_reference(row)
            try:
                run_id = _opaque_id(row.get("run_id"), name="run_id")
                task_id = _opaque_id(row.get("task_id"), name="task_id")
                if row.get("category") == "web_task":
                    session_lookup = getattr(self.store, "computer_session_for_event", None)
                    session = (
                        session_lookup(str(row.get("event_id") or ""))
                        if callable(session_lookup)
                        else None
                    )
                    if (
                        session
                        and session.get("state") == "completed"
                        and str(session.get("latest_answer") or "").strip()
                    ):
                        current = self.client.show_task(task_id)
                        current_status = _safe_token(
                            current.get("status", current.get("state", "unknown"))
                        )
                        if current_status not in {"done", "completed", "complete"}:
                            self.client.complete_from_runner(
                                task_id,
                                result=str(session["latest_answer"]),
                                runner="h-company",
                                receipt_url=str(session.get("agent_view_url") or "") or None,
                            )
                task = self.client.show_task(task_id)
                runs = self.client.task_runs(task_id)
                diagnostics = self.client.diagnostics(task_id)
                hermes_status = _safe_token(task.get("status", task.get("state", "unknown")))
                latest_outcome = _latest_outcome(runs)
                summary = _latest_summary(runs)
                state = map_task_status(task, runs, diagnostics)
                if state == "completed" and self.effect_service is not None:
                    # Submit before making the job terminal. If the typed
                    # effector transiently fails, the job remains active and
                    # the next reconciliation retries with the same idempotency
                    # key. A crash after submit is also safe because EffectService
                    # owns idempotent replay.
                    if self._dispatch_effect(row, task_id, runs):
                        result["effects"] = int(result["effects"]) + 1
                self.store.kanban_record_reconciled(
                    run_id,
                    task_id=task_id,
                    state=state,
                    hermes_status=hermes_status,
                    latest_outcome=latest_outcome,
                    summary=summary,
                    diagnostics=_diagnostic_codes(diagnostics),
                )
                result["reconciled"] = int(result["reconciled"]) + 1
            except Exception as error:
                code = self._error_code(error)
                try:
                    self.store.kanban_record_reconcile_failure(run_reference, error=code)
                except Exception:
                    code = "store_failure"
                errors.append(
                    {"phase": "reconcile", "run_id": run_reference, "error": code}
                )

        notification_rows: Sequence[Mapping[str, object]] = ()
        if self.effect_service is not None and (
            self.control_plane is None
            or self.control_plane.operation_allowed(Capability.MESSAGES)
        ):
            notification_rows = self.store.kanban_pending_notifications(
                limit=max(0, notification_limit)
            )
        for row in notification_rows:
            run_reference = self._run_reference(row)
            try:
                if self.control_plane is not None:
                    message = self.control_plane.authorize(
                        Capability.MESSAGES,
                        idempotency_key=f"kanban-result:{run_reference}",
                        actor="orchestrator",
                    )
                    if not message.allowed:
                        continue
                receipt = self._dispatch_result_notification(row)
                self.store.kanban_record_notified(run_reference, receipt=receipt)
                result["notified"] = int(result["notified"]) + 1
            except Exception as error:
                code = self._error_code(error)
                try:
                    self.store.kanban_record_notification_failure(run_reference, error=code)
                except Exception:
                    code = "store_failure"
                errors.append({"phase": "notify", "run_id": run_reference, "error": code})

        return result

    def _dispatch_result_notification(self, row: Mapping[str, object]) -> dict[str, str]:
        if self.effect_service is None:
            raise RuntimeError("Discord result effector is unavailable")
        from .effectors.models import DiscordMessageRequest, EffectKind, EffectState

        event_id = _opaque_id(row.get("event_id"), name="event_id")
        task_id = _opaque_id(row.get("task_id"), name="task_id")
        chat_id = _opaque_id(row.get("discord_chat_id"), name="discord_chat_id")
        entity = str(row.get("entity") or "Completed Followthrough task")[:300]
        summary = _discord_summary(row.get("result_summary"))
        steps = max(0, int(row.get("computer_steps") or 0))
        duration = None
        if row.get("computer_started_at") and row.get("computer_finished_at"):
            duration = max(
                0,
                round(
                    (
                        datetime.fromisoformat(str(row["computer_finished_at"]))
                        - datetime.fromisoformat(str(row["computer_started_at"]))
                    ).total_seconds()
                ),
            )
        proof = ""
        if steps or duration is not None:
            facts = [f"{steps} H steps"] if steps else []
            if duration is not None:
                facts.append(f"{duration}s")
            proof = f"\n\nCompleted: {' · '.join(facts)}"
        replay = str(row.get("computer_replay_url") or "")
        if replay.startswith("https://"):
            proof += f"\nH session: {replay[:500]}"
        request = DiscordMessageRequest(
            kind=EffectKind.DISCORD_MESSAGE_SEND,
            trigger_event_id=event_id,
            target=f"discord:{chat_id}",
            subject="Followthrough · completed",
            body=f"**{entity}**\n\n{summary}{proof}\n\nHermes receipt: `{task_id}`",
            owner_only=True,
        )
        record = self.effect_service.submit(
            request,
            idempotency_key=f"followthrough:{event_id}:result:discord:v2",
            execute=True,
        )
        if record.get("state") != EffectState.COMPLETED.value:
            raise RuntimeError("Discord result delivery did not complete")
        return {"effect_id": str(record.get("id", "unknown")), "state": "delivered"}

    def _dispatch_effect(
        self,
        row: Mapping[str, object],
        task_id: str,
        runs: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Validate and submit a Hermes proposal through the typed effector boundary."""

        if self.effect_service is None or not runs:
            return False
        metadata = runs[-1].get("metadata")
        if not isinstance(metadata, Mapping):
            return False
        suggestion = metadata.get("safe_next_action")
        if not isinstance(suggestion, Mapping):
            return False
        raw_kind = suggestion.get("type")
        parameters = suggestion.get("parameters")
        if not isinstance(raw_kind, str) or not isinstance(parameters, Mapping):
            return False

        from .controls import Capability
        from .effectors.models import EffectKind, PurchaseRequest, parse_request

        try:
            kind = EffectKind(raw_kind)
        except ValueError:
            return False
        event_id = _opaque_id(row.get("event_id") or row.get("run_id"), name="event_id")
        payload = dict(parameters)
        payload["kind"] = kind.value
        payload["trigger_event_id"] = event_id
        request = parse_request(payload)
        idempotency_key = f"followthrough:{event_id}:effect:{kind.value}:v1"

        capability = {
            EffectKind.DISCORD_MESSAGE_SEND: Capability.MESSAGES,
            EffectKind.PURCHASE_CREATE: Capability.PURCHASES,
            EffectKind.DEPLOYMENT_TRIGGER: Capability.DEPLOYMENTS,
        }.get(kind, Capability.ACTIONS)
        if self.control_plane is not None:
            cost = (
                request.total_amount_minor / 100
                if isinstance(request, PurchaseRequest)
                else 0.0
            )
            decision = self.control_plane.authorize(
                capability,
                idempotency_key=idempotency_key,
                actor="effect-orchestrator",
                cost_usd=cost,
            )
            if not decision.allowed:
                return False
        self.effect_service.submit(
            request,
            idempotency_key=idempotency_key,
            execute=True,
        )
        return True

"""Persistent emergency controls, capability budgets, and task parking.

The control plane is deliberately independent of Hermes.  It records the
operator's intent in Followthrough's durable database first; the orchestrator
then applies sanitized park/resume commands to Hermes.  A stopped orchestrator
therefore cannot make a disabled capability available again.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any


class GlobalMode(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    KILLED = "killed"


class Capability(StrEnum):
    LISTENING = "listening"
    ACTIONS = "actions"
    MESSAGES = "messages"
    PURCHASES = "purchases"
    SESSIONS = "sessions"
    DEPLOYMENTS = "deployments"
    REPOSITORY_EXECUTION = "repository_execution"
    ROLLBACK = "rollback"


DEFAULT_LIMITS: dict[Capability, tuple[int | None, int, float | None]] = {
    Capability.LISTENING: (36_000, 3_600, None),
    Capability.ACTIONS: (120, 3_600, None),
    Capability.MESSAGES: (30, 3_600, None),
    Capability.PURCHASES: (10, 86_400, 500.0),
    Capability.SESSIONS: (30, 3_600, None),
    Capability.DEPLOYMENTS: (10, 86_400, None),
    Capability.REPOSITORY_EXECUTION: (30, 3_600, None),
    Capability.ROLLBACK: (100, 86_400, None),
}

SAFE_MODE_TRIGGERS = frozenset(
    {
        "prompt_injection",
        "credential_access",
        "unusual_spending",
        "repeated_failure",
        "policy_drift",
        "operator_safe_mode",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_token(value: object, maximum: int = 120) -> str:
    candidate = "_".join(str(value).strip().lower().split())
    safe = "".join(character for character in candidate if character.isalnum() or character in "_.:-")
    return (safe[:maximum] or "unspecified")


def _safe_details(value: dict[str, Any] | None) -> dict[str, Any]:
    """Keep audit data bounded and structural; never accept ambient prose."""

    result: dict[str, Any] = {}
    for key, item in sorted((value or {}).items())[:20]:
        safe_key = _safe_token(key, 60)
        if isinstance(item, bool) or item is None:
            result[safe_key] = item
        elif isinstance(item, (int, float)):
            result[safe_key] = item
        elif isinstance(item, (list, tuple)):
            result[safe_key] = [_safe_token(entry, 120) for entry in item[:20]]
        else:
            result[safe_key] = _safe_token(item, 160)
    return result


@dataclass(frozen=True, slots=True)
class ControlDecision:
    allowed: bool
    capability: str
    reason_code: str
    receipt_id: str
    replay: bool = False


class ControlPlane:
    """SQLite-backed fail-closed policy boundary shared by API and worker."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.db = store.db
        self.lock = store.lock
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_state (
                  id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL,
                  reason_code TEXT NOT NULL, actor TEXT NOT NULL,
                  generation INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_controls (
                  capability TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                  reason_code TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_limits (
                  capability TEXT PRIMARY KEY, max_events INTEGER,
                  window_seconds INTEGER NOT NULL, max_cost_usd REAL,
                  actor TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_usage (
                  id TEXT PRIMARY KEY, capability TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL, units INTEGER NOT NULL,
                  cost_usd REAL NOT NULL, receipt_id TEXT NOT NULL,
                  created_at TEXT NOT NULL, UNIQUE(capability,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS capability_usage_window
                  ON capability_usage(capability,created_at);
                CREATE TABLE IF NOT EXISTS control_audit (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  receipt_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                  actor TEXT NOT NULL, capability TEXT,
                  reason_code TEXT NOT NULL, details_json TEXT NOT NULL,
                  previous_hash TEXT NOT NULL, receipt_hash TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_task_commands (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, job_id TEXT NOT NULL,
                  task_id TEXT NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL,
                  reason_code TEXT NOT NULL, control_receipt_id TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(run_id,action,control_receipt_id)
                );
                CREATE INDEX IF NOT EXISTS control_task_commands_pending
                  ON control_task_commands(state,created_at);
                """
            )
            timestamp = _now()
            self.db.execute(
                "INSERT OR IGNORE INTO control_state(id,mode,reason_code,actor,generation,updated_at) VALUES(1,'running','initial_state','system',1,?)",
                (timestamp,),
            )
            for capability, (max_events, window, max_cost) in DEFAULT_LIMITS.items():
                self.db.execute(
                    "INSERT OR IGNORE INTO capability_controls(capability,enabled,reason_code,actor,updated_at) VALUES(?,1,'default_enabled','system',?)",
                    (capability.value, timestamp),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO capability_limits(capability,max_events,window_seconds,max_cost_usd,actor,updated_at) VALUES(?,?,?,?,?,?)",
                    (capability.value, max_events, window, max_cost, "system", timestamp),
                )
            self.db.commit()

    @staticmethod
    def capability(value: Capability | str) -> Capability:
        try:
            return value if isinstance(value, Capability) else Capability(value)
        except ValueError as exc:
            raise ValueError("unknown capability") from exc

    def _audit_locked(
        self,
        kind: str,
        actor: str,
        reason_code: str,
        *,
        capability: Capability | str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        # Some audit paths (for example a denied authorization) have not
        # written anything before reading the chain head. Acquire SQLite's
        # cross-process writer reservation first so the API and orchestrator
        # cannot both derive receipts from the same previous hash. Callers that
        # already changed control state are already inside a write transaction.
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        receipt_id = str(uuid.uuid4())
        created_at = _now()
        previous = self.db.execute(
            "SELECT receipt_hash FROM control_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous[0] if previous else "0" * 64
        payload = {
            "receipt_id": receipt_id,
            "kind": _safe_token(kind),
            "actor": _safe_token(actor),
            "capability": self.capability(capability).value if capability else None,
            "reason_code": _safe_token(reason_code),
            "details": _safe_details(details),
            "created_at": created_at,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        receipt_hash = hashlib.sha256((previous_hash + canonical).encode()).hexdigest()
        self.db.execute(
            "INSERT INTO control_audit(receipt_id,kind,actor,capability,reason_code,details_json,previous_hash,receipt_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                payload["kind"],
                payload["actor"],
                payload["capability"],
                payload["reason_code"],
                json.dumps(payload["details"], sort_keys=True, separators=(",", ":")),
                previous_hash,
                receipt_hash,
                created_at,
            ),
        )
        return receipt_id

    def audit(
        self,
        kind: str,
        actor: str,
        reason_code: str,
        *,
        capability: Capability | str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        with self.lock:
            receipt = self._audit_locked(
                kind, actor, reason_code, capability=capability, details=details
            )
            self.db.commit()
        return receipt

    def _queue_park_locked(
        self,
        receipt_id: str,
        reason_code: str,
        *,
        category: str | None = None,
    ) -> int:
        states = (
            "pending",
            "retry",
            "dispatching",
            "enqueued",
            "queued",
            "in_progress",
            "needs_attention",
            "resume_requested",
        )
        placeholders = ",".join("?" for _ in states)
        params: list[Any] = [*states]
        query = f"SELECT * FROM hermes_jobs WHERE state IN ({placeholders})"
        if category:
            query += " AND category=?"
            params.append(category)
        rows = self.db.execute(query, params).fetchall()
        queued = 0
        timestamp = _now()
        for row in rows:
            self.db.execute(
                "UPDATE control_task_commands SET state='cancelled',last_error='superseded_by_emergency_control',updated_at=? WHERE job_id=? AND action='resume' AND state IN ('pending','retry','processing')",
                (timestamp, row["id"]),
            )
            if row["task_id"]:
                command_id = str(uuid.uuid4())
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO control_task_commands(id,run_id,job_id,task_id,action,state,reason_code,control_receipt_id,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                    (
                        command_id,
                        row["run_id"],
                        row["id"],
                        row["task_id"],
                        "park",
                        _safe_token(reason_code),
                        receipt_id,
                        timestamp,
                        timestamp,
                    ),
                ).rowcount
                queued += inserted
                self.db.execute(
                    "UPDATE hermes_jobs SET state='park_requested',last_error=?,updated_at=? WHERE id=?",
                    (_safe_token(reason_code), timestamp, row["id"]),
                )
            else:
                self.db.execute(
                    "UPDATE hermes_jobs SET state='parked',last_error=?,updated_at=? WHERE id=?",
                    (_safe_token(reason_code), timestamp, row["id"]),
                )
                queued += 1
            self.db.execute(
                "UPDATE runs SET status='paused',success=NULL,finished_at=NULL WHERE id=?",
                (row["run_id"],),
            )
        return queued

    def set_global_mode(
        self,
        mode: GlobalMode | str,
        *,
        actor: str,
        reason_code: str,
        resume_parked: bool = False,
    ) -> dict[str, Any]:
        selected = mode if isinstance(mode, GlobalMode) else GlobalMode(mode)
        with self.lock:
            current = self.db.execute("SELECT * FROM control_state WHERE id=1").fetchone()
            self.db.execute(
                "UPDATE control_state SET mode=?,reason_code=?,actor=?,generation=generation+1,updated_at=? WHERE id=1",
                (selected.value, _safe_token(reason_code), _safe_token(actor), _now()),
            )
            receipt = self._audit_locked(
                "global_mode_changed",
                actor,
                reason_code,
                details={"from": current["mode"], "to": selected.value},
            )
            affected = 0
            if selected in {GlobalMode.PAUSED, GlobalMode.KILLED}:
                affected = self._queue_park_locked(receipt, f"global_{selected.value}")
            elif resume_parked:
                affected = self._queue_resume_locked(receipt, "global_operator_resume")
            self.db.commit()
        return {"mode": selected.value, "receipt_id": receipt, "affected_jobs": affected}

    def _activate_safe_mode_locked(
        self,
        trigger: str,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_trigger = _safe_token(trigger)
        if safe_trigger not in SAFE_MODE_TRIGGERS:
            raise ValueError("unknown safe-mode trigger")
        current = self.db.execute("SELECT * FROM control_state WHERE id=1").fetchone()
        if current["mode"] != GlobalMode.KILLED.value:
            self.db.execute(
                "UPDATE control_state SET mode='paused',reason_code=?,actor=?,generation=generation+1,updated_at=? WHERE id=1",
                (safe_trigger, _safe_token(actor), _now()),
            )
        receipt = self._audit_locked(
            "safe_mode_activated",
            actor,
            safe_trigger,
            details={"from": current["mode"], **_safe_details(details)},
        )
        affected = self._queue_park_locked(receipt, f"safe_mode_{safe_trigger}")
        return {"mode": "paused" if current["mode"] != "killed" else "killed", "receipt_id": receipt, "affected_jobs": affected}

    def trigger_safe_mode(
        self,
        trigger: str,
        *,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            result = self._activate_safe_mode_locked(trigger, actor, details)
            self.db.commit()
        return result

    def set_capability(
        self,
        capability: Capability | str,
        enabled: bool,
        *,
        actor: str,
        reason_code: str,
        resume_parked: bool = False,
    ) -> dict[str, Any]:
        selected = self.capability(capability)
        with self.lock:
            self.db.execute(
                "UPDATE capability_controls SET enabled=?,reason_code=?,actor=?,updated_at=? WHERE capability=?",
                (int(enabled), _safe_token(reason_code), _safe_token(actor), _now(), selected.value),
            )
            receipt = self._audit_locked(
                "capability_changed",
                actor,
                reason_code,
                capability=selected,
                details={"enabled": enabled},
            )
            affected = 0
            if not enabled and selected in {
                Capability.ACTIONS,
                Capability.SESSIONS,
                Capability.REPOSITORY_EXECUTION,
            }:
                category = "repository" if selected == Capability.REPOSITORY_EXECUTION else None
                affected = self._queue_park_locked(
                    receipt, f"capability_{selected.value}_disabled", category=category
                )
            elif enabled and resume_parked:
                affected = self._queue_resume_locked(receipt, f"capability_{selected.value}_resume")
            self.db.commit()
        return {
            "capability": selected.value,
            "enabled": enabled,
            "receipt_id": receipt,
            "affected_jobs": affected,
        }

    def set_limit(
        self,
        capability: Capability | str,
        *,
        max_events: int | None,
        window_seconds: int,
        max_cost_usd: float | None,
        actor: str,
        reason_code: str,
    ) -> dict[str, Any]:
        selected = self.capability(capability)
        if max_events is not None and max_events < 0:
            raise ValueError("max_events must be non-negative or null")
        if not 1 <= window_seconds <= 2_592_000:
            raise ValueError("window_seconds must be between 1 and 2592000")
        if max_cost_usd is not None and max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative or null")
        with self.lock:
            self.db.execute(
                "UPDATE capability_limits SET max_events=?,window_seconds=?,max_cost_usd=?,actor=?,updated_at=? WHERE capability=?",
                (max_events, window_seconds, max_cost_usd, _safe_token(actor), _now(), selected.value),
            )
            receipt = self._audit_locked(
                "capability_limit_changed",
                actor,
                reason_code,
                capability=selected,
                details={
                    "max_events": max_events,
                    "window_seconds": window_seconds,
                    "max_cost_usd": max_cost_usd,
                },
            )
            self.db.commit()
        return {
            "capability": selected.value,
            "max_events": max_events,
            "window_seconds": window_seconds,
            "max_cost_usd": max_cost_usd,
            "receipt_id": receipt,
        }

    def _base_denial_locked(self, capability: Capability) -> str | None:
        global_row = self.db.execute("SELECT mode FROM control_state WHERE id=1").fetchone()
        mode = global_row["mode"] if global_row else GlobalMode.KILLED.value
        if mode == GlobalMode.KILLED.value:
            return "global_kill_active"
        if mode == GlobalMode.PAUSED.value and capability != Capability.LISTENING:
            return "global_pause_active"
        row = self.db.execute(
            "SELECT enabled FROM capability_controls WHERE capability=?", (capability.value,)
        ).fetchone()
        if not row or not bool(row["enabled"]):
            return "capability_disabled"
        return None

    def _limit_denial_locked(self, capability: Capability, units: int, cost_usd: float) -> str | None:
        row = self.db.execute(
            "SELECT * FROM capability_limits WHERE capability=?", (capability.value,)
        ).fetchone()
        if not row:
            return "limit_missing"
        cutoff = (datetime.now(UTC) - timedelta(seconds=int(row["window_seconds"]))).isoformat()
        used = self.db.execute(
            "SELECT COALESCE(SUM(units),0) units,COALESCE(SUM(cost_usd),0) cost FROM capability_usage WHERE capability=? AND created_at>=?",
            (capability.value, cutoff),
        ).fetchone()
        if row["max_events"] is not None and int(used["units"]) + units > int(row["max_events"]):
            return "rate_limit_exceeded"
        if row["max_cost_usd"] is not None and float(used["cost"]) + cost_usd > float(row["max_cost_usd"]):
            return "budget_limit_exceeded"
        return None

    def operation_allowed(
        self,
        capability: Capability | str,
        *,
        units: int = 1,
        cost_usd: float = 0.0,
    ) -> bool:
        selected = self.capability(capability)
        if units < 1 or cost_usd < 0:
            return False
        with self.lock:
            return not (
                self._base_denial_locked(selected)
                or self._limit_denial_locked(selected, units, cost_usd)
            )

    def authorize(
        self,
        capability: Capability | str,
        *,
        idempotency_key: str,
        actor: str,
        units: int = 1,
        cost_usd: float = 0.0,
    ) -> ControlDecision:
        selected = self.capability(capability)
        safe_key = _safe_token(idempotency_key, 200)
        if units < 1 or cost_usd < 0:
            raise ValueError("usage must have positive units and non-negative cost")
        with self.lock:
            denial = self._base_denial_locked(selected)
            if denial:
                receipt = self._audit_locked(
                    "capability_denied",
                    actor,
                    denial,
                    capability=selected,
                    details={"idempotency_key": safe_key, "units": units, "cost_usd": cost_usd},
                )
                self.db.commit()
                return ControlDecision(False, selected.value, denial, receipt)
            existing = self.db.execute(
                "SELECT receipt_id FROM capability_usage WHERE capability=? AND idempotency_key=?",
                (selected.value, safe_key),
            ).fetchone()
            if existing:
                return ControlDecision(True, selected.value, "idempotent_replay", existing["receipt_id"], True)
            denial = self._limit_denial_locked(selected, units, cost_usd)
            if denial:
                receipt = self._audit_locked(
                    "capability_denied",
                    actor,
                    denial,
                    capability=selected,
                    details={"idempotency_key": safe_key, "units": units, "cost_usd": cost_usd},
                )
                if selected == Capability.PURCHASES and denial == "budget_limit_exceeded":
                    self._activate_safe_mode_locked(
                        "unusual_spending",
                        "control-plane",
                        {"purchase_idempotency_key": safe_key},
                    )
                self.db.commit()
                return ControlDecision(False, selected.value, denial, receipt)
            receipt = self._audit_locked(
                "capability_authorized",
                actor,
                "within_policy",
                capability=selected,
                details={"idempotency_key": safe_key, "units": units, "cost_usd": cost_usd},
            )
            self.db.execute(
                "INSERT INTO capability_usage(id,capability,idempotency_key,units,cost_usd,receipt_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), selected.value, safe_key, units, cost_usd, receipt, _now()),
            )
            self.db.commit()
        return ControlDecision(True, selected.value, "within_policy", receipt)

    def park_run(self, run_id: str, *, actor: str, reason_code: str) -> dict[str, Any]:
        safe_run = _safe_token(run_id, 200)
        with self.lock:
            receipt = self._audit_locked(
                "task_park_requested", actor, reason_code, capability=Capability.ACTIONS,
                details={"run_id": safe_run},
            )
            affected = self._queue_specific_park_locked(safe_run, receipt, reason_code)
            self.db.commit()
        return {"run_id": safe_run, "receipt_id": receipt, "affected_jobs": affected}

    def _queue_specific_park_locked(self, run_id: str, receipt: str, reason_code: str) -> int:
        row = self.db.execute("SELECT * FROM hermes_jobs WHERE run_id=?", (run_id,)).fetchone()
        if not row or row["state"] in {"completed", "cancelled", "dead_letter", "parked", "park_requested"}:
            return 0
        timestamp = _now()
        self.db.execute(
            "UPDATE control_task_commands SET state='cancelled',last_error='superseded_by_emergency_control',updated_at=? WHERE job_id=? AND action='resume' AND state IN ('pending','retry','processing')",
            (timestamp, row["id"]),
        )
        if row["task_id"]:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO control_task_commands(id,run_id,job_id,task_id,action,state,reason_code,control_receipt_id,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                (str(uuid.uuid4()), run_id, row["id"], row["task_id"], "park", _safe_token(reason_code), receipt, timestamp, timestamp),
            ).rowcount
            self.db.execute(
                "UPDATE hermes_jobs SET state='park_requested',last_error=?,updated_at=? WHERE id=?",
                (_safe_token(reason_code), timestamp, row["id"]),
            )
        else:
            inserted = 1
            self.db.execute(
                "UPDATE hermes_jobs SET state='parked',last_error=?,updated_at=? WHERE id=?",
                (_safe_token(reason_code), timestamp, row["id"]),
            )
        self.db.execute(
            "UPDATE runs SET status='paused',success=NULL,finished_at=NULL WHERE id=?", (run_id,)
        )
        return inserted

    def resume_parked(self, *, actor: str, reason_code: str) -> dict[str, Any]:
        with self.lock:
            receipt = self._audit_locked(
                "task_resume_requested", actor, reason_code, capability=Capability.ACTIONS
            )
            affected = self._queue_resume_locked(receipt, reason_code)
            self.db.commit()
        return {"receipt_id": receipt, "affected_jobs": affected}

    def _queue_resume_locked(self, receipt: str, reason_code: str) -> int:
        if self._base_denial_locked(Capability.ACTIONS) or self._base_denial_locked(Capability.SESSIONS):
            return 0
        rows = self.db.execute("SELECT * FROM hermes_jobs WHERE state='parked'").fetchall()
        timestamp = _now()
        affected = 0
        for row in rows:
            if row["category"] == "repository" and self._base_denial_locked(
                Capability.REPOSITORY_EXECUTION
            ):
                continue
            if row["task_id"]:
                affected += self.db.execute(
                    "INSERT OR IGNORE INTO control_task_commands(id,run_id,job_id,task_id,action,state,reason_code,control_receipt_id,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                    (str(uuid.uuid4()), row["run_id"], row["id"], row["task_id"], "resume", _safe_token(reason_code), receipt, timestamp, timestamp),
                ).rowcount
                self.db.execute(
                    "UPDATE hermes_jobs SET state='resume_requested',updated_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
            else:
                self.db.execute(
                    "UPDATE hermes_jobs SET state='retry',last_error=NULL,updated_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
                self.db.execute("UPDATE runs SET status='queued' WHERE id=?", (row["run_id"],))
                affected += 1
        return affected

    def pending_task_commands(self, *, limit: int = 20) -> list[dict[str, str]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM control_task_commands WHERE state IN ('pending','retry','processing') AND attempts<5 ORDER BY created_at LIMIT ?",
                (max(0, limit),),
            ).fetchall()
            if rows:
                placeholders = ",".join("?" for _ in rows)
                self.db.execute(
                    f"UPDATE control_task_commands SET state='processing',attempts=attempts+1,updated_at=? WHERE id IN ({placeholders})",
                    (_now(), *(row["id"] for row in rows)),
                )
                self.db.commit()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "action": row["action"],
                "reason_code": row["reason_code"],
            }
            for row in rows
        ]

    def record_task_command_applied(self, command_id: str) -> None:
        with self.lock:
            command = self.db.execute(
                "SELECT * FROM control_task_commands WHERE id=?", (command_id,)
            ).fetchone()
            if not command:
                raise ValueError("task command not found")
            if command["state"] != "processing":
                self._audit_locked(
                    "stale_task_command_ignored",
                    "orchestrator",
                    "command_superseded",
                    capability=Capability.ACTIONS,
                    details={"action": command["action"], "run_id": command["run_id"]},
                )
                self.db.commit()
                return
            timestamp = _now()
            state = "parked" if command["action"] == "park" else "enqueued"
            run_status = "paused" if command["action"] == "park" else "queued"
            self.db.execute(
                "UPDATE control_task_commands SET state='applied',last_error=NULL,updated_at=? WHERE id=?",
                (timestamp, command_id),
            )
            self.db.execute(
                "UPDATE hermes_jobs SET state=?,last_error=NULL,updated_at=? WHERE id=?",
                (state, timestamp, command["job_id"]),
            )
            self.db.execute(
                "UPDATE runs SET status=?,success=NULL,finished_at=NULL WHERE id=?",
                (run_status, command["run_id"]),
            )
            self._audit_locked(
                f"task_{command['action']}_applied",
                "orchestrator",
                command["reason_code"],
                capability=Capability.ACTIONS,
                details={"run_id": command["run_id"], "task_id": command["task_id"]},
            )
            self.db.commit()

    def record_task_command_failure(self, command_id: str, error_code: str) -> None:
        with self.lock:
            command = self.db.execute(
                "SELECT * FROM control_task_commands WHERE id=?", (command_id,)
            ).fetchone()
            if not command:
                return
            if command["state"] == "cancelled":
                return
            next_state = "failed" if int(command["attempts"]) >= 5 else "retry"
            self.db.execute(
                "UPDATE control_task_commands SET state=?,last_error=?,updated_at=? WHERE id=?",
                (next_state, _safe_token(error_code), _now(), command_id),
            )
            self._audit_locked(
                "task_command_failed",
                "orchestrator",
                error_code,
                capability=Capability.ACTIONS,
                details={"action": command["action"], "run_id": command["run_id"]},
            )
            if next_state == "failed":
                self._activate_safe_mode_locked(
                    "repeated_failure",
                    "orchestrator",
                    {"action": command["action"], "run_id": command["run_id"]},
                )
            self.db.commit()

    def status(self) -> dict[str, Any]:
        mode = self.db.execute("SELECT * FROM control_state WHERE id=1").fetchone()
        capabilities = [dict(row) for row in self.db.execute(
            "SELECT c.capability,c.enabled,c.reason_code,c.updated_at,l.max_events,l.window_seconds,l.max_cost_usd FROM capability_controls c JOIN capability_limits l USING(capability) ORDER BY c.capability"
        ).fetchall()]
        commands = {
            row["state"]: row["count"]
            for row in self.db.execute(
                "SELECT state,COUNT(*) count FROM control_task_commands GROUP BY state"
            ).fetchall()
        }
        return {
            "global": dict(mode) if mode else {"mode": GlobalMode.KILLED.value},
            "capabilities": capabilities,
            "task_commands": commands,
            "audit_chain_valid": self.verify_audit_chain(),
        }

    def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM control_audit ORDER BY sequence DESC LIMIT ?", (max(0, limit),)
        ).fetchall()
        return [dict(row) | {"details": json.loads(row["details_json"])} for row in rows]

    def verify_audit_chain(self) -> bool:
        previous = "0" * 64
        rows = self.db.execute("SELECT * FROM control_audit ORDER BY sequence").fetchall()
        for row in rows:
            payload = {
                "receipt_id": row["receipt_id"],
                "kind": row["kind"],
                "actor": row["actor"],
                "capability": row["capability"],
                "reason_code": row["reason_code"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256((previous + canonical).encode()).hexdigest()
            if row["previous_hash"] != previous or row["receipt_hash"] != expected:
                return False
            previous = row["receipt_hash"]
        return True


def is_within(path: Path, root: Path) -> bool:
    """Compatibility helper shared with the self-improvement policy."""

    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True

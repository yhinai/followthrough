from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .errors import IdempotencyConflict, InvalidTransition, PolicyDenied
from .models import (
    DriverReceipt,
    EffectRequest,
    EffectState,
    PolicyMode,
    PurchaseRequest,
    canonical_json,
    fingerprint,
    parse_request,
)
from .policy import PolicyDecision


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EffectJournal:
    """A separate, append-audited store for external side effects."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS effects (
              id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL UNIQUE,
              trigger_event_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              request_json TEXT NOT NULL,
              request_fingerprint TEXT NOT NULL,
              state TEXT NOT NULL,
              policy_mode TEXT NOT NULL,
              policy_reason TEXT NOT NULL,
              risk TEXT NOT NULL,
              approved_by TEXT,
              approved_at TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              provider TEXT,
              external_id TEXT,
              receipt_json TEXT,
              reversal_json TEXT,
              spend_mode TEXT,
              spend_currency TEXT,
              spend_amount_minor INTEGER,
              daily_cap_minor INTEGER,
              last_error_code TEXT,
              last_error_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS effect_transitions (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              effect_id TEXT NOT NULL,
              from_state TEXT,
              to_state TEXT NOT NULL,
              reason_code TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(effect_id) REFERENCES effects(id)
            );
            CREATE TRIGGER IF NOT EXISTS effect_transitions_no_update
            BEFORE UPDATE ON effect_transitions BEGIN
              SELECT RAISE(ABORT, 'effect transition log is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS effect_transitions_no_delete
            BEFORE DELETE ON effect_transitions BEGIN
              SELECT RAISE(ABORT, 'effect transition log is append-only');
            END;
            """
        )
        self.db.commit()
        path.chmod(0o600)

    def close(self) -> None:
        self.db.close()

    def register(
        self,
        *,
        request: EffectRequest,
        idempotency_key: str,
        decision: PolicyDecision,
        daily_cap_minor: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if not (8 <= len(idempotency_key) <= 300):
            raise ValueError("idempotency key must be 8-300 characters")
        request_json = canonical_json(request)
        digest = fingerprint(request)
        effect_id = str(uuid.uuid4())
        created_at = _now()
        spend_mode: str | None = None
        spend_currency: str | None = None
        spend_amount_minor: int | None = None
        if isinstance(request, PurchaseRequest):
            spend_mode = request.mode
            spend_currency = request.currency
            spend_amount_minor = request.total_amount_minor
        target_state = {
            PolicyMode.AUTO: EffectState.READY,
            PolicyMode.APPROVAL: EffectState.AWAITING_APPROVAL,
            PolicyMode.DRY_RUN: EffectState.DRY_RUN,
            PolicyMode.DENY: EffectState.DENIED,
        }[decision.mode]
        with self.lock:
            existing = self.db.execute(
                "SELECT * FROM effects WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != digest:
                    raise IdempotencyConflict()
                return self._decode(existing), False
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    """
                    INSERT INTO effects(
                      id,idempotency_key,trigger_event_id,kind,request_json,
                      request_fingerprint,state,policy_mode,policy_reason,risk,
                      spend_mode,spend_currency,spend_amount_minor,daily_cap_minor,
                      created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        effect_id,
                        idempotency_key,
                        request.trigger_event_id,
                        request.kind.value,
                        request_json,
                        digest,
                        EffectState.REGISTERED.value,
                        decision.mode.value,
                        decision.reason_code,
                        decision.risk,
                        spend_mode,
                        spend_currency,
                        spend_amount_minor,
                        daily_cap_minor,
                        created_at,
                        created_at,
                    ),
                )
                self._append_transition(
                    effect_id,
                    None,
                    EffectState.REGISTERED,
                    "registered",
                    {"request_fingerprint": digest},
                )
                self.db.execute(
                    "UPDATE effects SET state=?,updated_at=? WHERE id=?",
                    (target_state.value, created_at, effect_id),
                )
                self._append_transition(
                    effect_id,
                    EffectState.REGISTERED,
                    target_state,
                    decision.reason_code,
                    {"policy_mode": decision.mode.value, "risk": decision.risk},
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.get(effect_id), True

    def get(self, effect_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
        if not row:
            raise KeyError(effect_id)
        return self._decode(row)

    def request(self, effect_id: str) -> EffectRequest:
        return parse_request(self.get(effect_id)["request"])

    def approve(self, effect_id: str, *, principal: str) -> dict[str, Any]:
        if not principal.strip():
            raise ValueError("approval principal is required")
        with self.lock:
            row = self.db.execute("SELECT state FROM effects WHERE id=?", (effect_id,)).fetchone()
            if not row:
                raise KeyError(effect_id)
            current = EffectState(row["state"])
            if current == EffectState.READY:
                return self.get(effect_id)
            if current != EffectState.AWAITING_APPROVAL:
                raise InvalidTransition(current.value, EffectState.READY.value)
            stamp = _now()
            self.db.execute(
                "UPDATE effects SET state=?,approved_by=?,approved_at=?,updated_at=? WHERE id=?",
                (EffectState.READY.value, principal, stamp, stamp, effect_id),
            )
            self._append_transition(
                effect_id,
                current,
                EffectState.READY,
                "owner_approved",
                {"principal": principal},
            )
            self.db.commit()
        return self.get(effect_id)

    def claim_execution(self, effect_id: str) -> dict[str, Any]:
        allowed = {EffectState.READY, EffectState.RETRYABLE_FAILURE}
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
                if not row:
                    raise KeyError(effect_id)
                current = EffectState(row["state"])
                if current == EffectState.COMPLETED:
                    self.db.rollback()
                    return self._decode(row)
                if current == EffectState.DENIED:
                    raise PolicyDenied(row["policy_reason"])
                if current not in allowed:
                    raise InvalidTransition(current.value, EffectState.EXECUTING.value)
                self._assert_daily_cap(row)
                stamp = _now()
                self.db.execute(
                    """
                    UPDATE effects
                    SET state=?,attempt_count=attempt_count+1,last_error_code=NULL,
                        last_error_json=NULL,updated_at=?
                    WHERE id=?
                    """,
                    (EffectState.EXECUTING.value, stamp, effect_id),
                )
                self._append_transition(
                    effect_id,
                    current,
                    EffectState.EXECUTING,
                    "execution_claimed",
                    {},
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.get(effect_id)

    def complete(self, effect_id: str, receipt: DriverReceipt) -> dict[str, Any]:
        return self._finish(
            effect_id,
            target=EffectState.COMPLETED,
            reason="connector_receipt",
            updates={
                "provider": receipt.provider,
                "external_id": receipt.external_id,
                "receipt_json": canonical_json(receipt),
                "reversal_json": canonical_json(receipt.reversal) if receipt.reversal else None,
            },
            metadata={
                "provider": receipt.provider,
                "external_id": receipt.external_id,
                "response_fingerprint": receipt.response_fingerprint,
                "reversible": receipt.reversible,
            },
            expected={EffectState.EXECUTING},
        )

    def resolve_uncertain_applied(
        self,
        effect_id: str,
        *,
        receipt: DriverReceipt,
        principal: str,
    ) -> dict[str, Any]:
        return self._finish(
            effect_id,
            target=EffectState.COMPLETED,
            reason="uncertain_reconciled_applied",
            updates={
                "provider": receipt.provider,
                "external_id": receipt.external_id,
                "receipt_json": canonical_json(receipt),
                "reversal_json": canonical_json(receipt.reversal) if receipt.reversal else None,
            },
            metadata={"principal": principal, "response_fingerprint": receipt.response_fingerprint},
            expected={EffectState.UNCERTAIN},
        )

    def resolve_uncertain_not_applied(self, effect_id: str, *, principal: str) -> dict[str, Any]:
        return self._finish(
            effect_id,
            target=EffectState.RETRYABLE_FAILURE,
            reason="uncertain_reconciled_not_applied",
            updates={"last_error_code": None, "last_error_json": None},
            metadata={"principal": principal},
            expected={EffectState.UNCERTAIN},
        )

    def fail(self, effect_id: str, *, error: dict[str, Any]) -> dict[str, Any]:
        target = EffectState.UNCERTAIN if error.get("uncertain") else EffectState.RETRYABLE_FAILURE
        if not error.get("retryable") and not error.get("uncertain"):
            target = EffectState.FAILED
        return self._finish(
            effect_id,
            target=target,
            reason=str(error.get("code", "connector_failure")),
            updates={
                "last_error_code": str(error.get("code", "connector_failure")),
                "last_error_json": canonical_json(error),
            },
            metadata={
                "code": error.get("code"),
                "retryable": bool(error.get("retryable")),
                "uncertain": bool(error.get("uncertain")),
                "retry_after_seconds": error.get("retry_after_seconds"),
            },
            expected={EffectState.EXECUTING},
        )

    def claim_rollback(self, effect_id: str) -> dict[str, Any]:
        return self._transition(
            effect_id,
            target=EffectState.ROLLING_BACK,
            reason="rollback_claimed",
            expected={EffectState.COMPLETED},
        )

    def recover_inflight(self, *, principal: str) -> list[dict[str, Any]]:
        """Park crash-interrupted writes as uncertain; never resend them automatically."""
        if not principal.strip():
            raise ValueError("recovery principal is required")
        recovered: list[dict[str, Any]] = []
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM effects WHERE state=? ORDER BY created_at",
                (EffectState.EXECUTING.value,),
            ).fetchall()
            for row in rows:
                recovered.append(
                    self._finish(
                        row["id"],
                        target=EffectState.UNCERTAIN,
                        reason="crash_recovery_requires_reconciliation",
                        updates={
                            "last_error_code": "crash_interrupted",
                            "last_error_json": canonical_json(
                                {
                                    "code": "crash_interrupted",
                                    "retryable": False,
                                    "uncertain": True,
                                }
                            ),
                        },
                        metadata={"principal": principal},
                        expected={EffectState.EXECUTING},
                    )
                )
        return recovered

    def complete_rollback(self, effect_id: str, receipt: DriverReceipt) -> dict[str, Any]:
        return self._finish(
            effect_id,
            target=EffectState.ROLLED_BACK,
            reason="rollback_receipt",
            updates={"reversal_json": canonical_json(receipt)},
            metadata={
                "provider": receipt.provider,
                "external_id": receipt.external_id,
                "response_fingerprint": receipt.response_fingerprint,
            },
            expected={EffectState.ROLLING_BACK},
        )

    def fail_rollback(self, effect_id: str, *, error: dict[str, Any]) -> dict[str, Any]:
        return self._finish(
            effect_id,
            target=EffectState.ROLLBACK_FAILED,
            reason=str(error.get("code", "rollback_failed")),
            updates={
                "last_error_code": str(error.get("code", "rollback_failed")),
                "last_error_json": canonical_json(error),
            },
            metadata={"code": error.get("code")},
            expected={EffectState.ROLLING_BACK},
        )

    def history(self, effect_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM effect_transitions WHERE effect_id=? ORDER BY sequence", (effect_id,)
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def list_effects(self, *, states: Iterable[EffectState] | None = None) -> list[dict[str, Any]]:
        if states:
            state_values = [state.value for state in states]
            placeholders = ",".join("?" for _ in state_values)
            rows = self.db.execute(
                f"SELECT * FROM effects WHERE state IN ({placeholders}) ORDER BY created_at",
                state_values,
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM effects ORDER BY created_at").fetchall()
        return [self._decode(row) for row in rows]

    def _assert_daily_cap(self, row: sqlite3.Row) -> None:
        if row["kind"] != "purchase.create" or row["spend_mode"] != "live":
            return
        cap = row["daily_cap_minor"]
        amount = row["spend_amount_minor"]
        if cap is None or amount is None:
            raise PolicyDenied("spend_caps_missing")
        day = datetime.now(UTC).date().isoformat()
        committed = self.db.execute(
            """
            SELECT COALESCE(SUM(spend_amount_minor),0)
            FROM effects
            WHERE kind='purchase.create' AND spend_mode='live' AND spend_currency=?
              AND substr(created_at,1,10)=? AND id != ?
              AND state IN ('executing','completed','uncertain','rollback_failed')
            """,
            (row["spend_currency"], day, row["id"]),
        ).fetchone()[0]
        if committed + amount > cap:
            raise PolicyDenied("daily_spend_cap")

    def _transition(
        self,
        effect_id: str,
        *,
        target: EffectState,
        reason: str,
        expected: set[EffectState],
    ) -> dict[str, Any]:
        return self._finish(
            effect_id,
            target=target,
            reason=reason,
            updates={},
            metadata={},
            expected=expected,
        )

    def _finish(
        self,
        effect_id: str,
        *,
        target: EffectState,
        reason: str,
        updates: dict[str, Any],
        metadata: dict[str, Any],
        expected: set[EffectState],
    ) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT state FROM effects WHERE id=?", (effect_id,)).fetchone()
            if not row:
                raise KeyError(effect_id)
            current = EffectState(row["state"])
            if current not in expected:
                raise InvalidTransition(current.value, target.value)
            stamp = _now()
            columns = {"state": target.value, "updated_at": stamp, **updates}
            assignments = ",".join(f"{name}=?" for name in columns)
            self.db.execute(
                f"UPDATE effects SET {assignments} WHERE id=?",
                (*columns.values(), effect_id),
            )
            self._append_transition(effect_id, current, target, reason, metadata)
            self.db.commit()
        return self.get(effect_id)

    def _append_transition(
        self,
        effect_id: str,
        from_state: EffectState | None,
        to_state: EffectState,
        reason: str,
        metadata: dict[str, Any],
    ) -> None:
        self.db.execute(
            """
            INSERT INTO effect_transitions(
              effect_id,from_state,to_state,reason_code,metadata_json,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                effect_id,
                from_state.value if from_state else None,
                to_state.value,
                reason,
                canonical_json(metadata),
                _now(),
            ),
        )

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        receipt_json = item.pop("receipt_json")
        reversal_json = item.pop("reversal_json")
        error_json = item.pop("last_error_json")
        item["receipt"] = json.loads(receipt_json) if receipt_json else None
        item["reversal"] = json.loads(reversal_json) if reversal_json else None
        item["last_error"] = json.loads(error_json) if error_json else None
        return item

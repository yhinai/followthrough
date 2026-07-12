from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .relevance import Category, CorrectionRecord, Disposition, InterestModel, InterestWeight


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL,
              signal_type TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
              created_at TEXT NOT NULL, finished_at TEXT, latency_ms INTEGER DEFAULT 0,
              success INTEGER, report_url TEXT, voice_url TEXT, summary TEXT
            );
            CREATE TABLE IF NOT EXISTS steps (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, agent TEXT NOT NULL,
              status TEXT NOT NULL, input_summary TEXT NOT NULL, output_json TEXT NOT NULL,
              started_at TEXT NOT NULL, finished_at TEXT, latency_ms INTEGER DEFAULT 0,
              input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
              estimated_cost_usd REAL DEFAULT 0, FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS eval_cases (
              id TEXT PRIMARY KEY, run_id TEXT, input_text TEXT NOT NULL,
              expected TEXT NOT NULL, observed TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
              email TEXT PRIMARY KEY, source TEXT NOT NULL, first_use_at TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roles (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, job TEXT NOT NULL,
              tools_json TEXT NOT NULL, guardrails TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relevance_decisions (
              archive_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
              content_fingerprint TEXT NOT NULL, disposition TEXT NOT NULL,
              owner_status TEXT NOT NULL, categories_json TEXT NOT NULL,
              confidence REAL NOT NULL, reason_code TEXT NOT NULL,
              evidence_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS interest_weights (
              category TEXT PRIMARY KEY, weight REAL NOT NULL,
              source TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relevance_corrections (
              content_fingerprint TEXT PRIMARY KEY, disposition TEXT NOT NULL,
              categories_json TEXT NOT NULL, reason_code TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operational_memories (
              id TEXT PRIMARY KEY, archive_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
              content_fingerprint TEXT NOT NULL, category TEXT NOT NULL,
              entity TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hermes_jobs (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
              archive_id TEXT NOT NULL UNIQUE, event_id TEXT NOT NULL UNIQUE,
              idempotency_key TEXT NOT NULL UNIQUE, task_id TEXT UNIQUE,
              state TEXT NOT NULL, capsule_path TEXT NOT NULL,
              category TEXT NOT NULL, entity TEXT NOT NULL,
              intent TEXT NOT NULL DEFAULT '', acceptance_json TEXT NOT NULL DEFAULT '[]',
              discord_chat_id TEXT, discord_user_id TEXT,
              attempts INTEGER NOT NULL DEFAULT 0,
              notification_state TEXT NOT NULL DEFAULT 'pending',
              notification_json TEXT,
              hermes_status TEXT, latest_outcome TEXT, diagnostics_json TEXT NOT NULL DEFAULT '[]',
              last_error TEXT, last_sync_at TEXT, last_polled_at TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS service_heartbeats (
              name TEXT PRIMARY KEY, status TEXT NOT NULL,
              details_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hermes_task_history (
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL, task_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL, outcome TEXT NOT NULL,
              reason TEXT NOT NULL, archived_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS computer_use_sessions (
              id TEXT PRIMARY KEY, source_event_id TEXT, task TEXT NOT NULL,
              provider TEXT NOT NULL, agent TEXT NOT NULL, h_session_id TEXT UNIQUE,
              state TEXT NOT NULL, step_count INTEGER NOT NULL DEFAULT 0,
              current_action TEXT, latest_answer TEXT, agent_view_url TEXT,
              error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS desktop_action_receipts (
              id TEXT PRIMARY KEY, provider TEXT NOT NULL, computer_id TEXT,
              action TEXT NOT NULL, visual_changed INTEGER, noop INTEGER,
              fingerprint_before TEXT, fingerprint_after TEXT,
              result_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        self._ensure_column("runs", "archive_event_id", "TEXT")
        self._ensure_column("hermes_jobs", "intent", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("hermes_jobs", "acceptance_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("hermes_jobs", "discord_chat_id", "TEXT")
        self._ensure_column("hermes_jobs", "discord_user_id", "TEXT")
        self._ensure_column("hermes_jobs", "notification_json", "TEXT")
        self._ensure_column("hermes_jobs", "hermes_status", "TEXT")
        self._ensure_column("hermes_jobs", "latest_outcome", "TEXT")
        self._ensure_column("hermes_jobs", "diagnostics_json", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("hermes_jobs", "notification_attempts", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("hermes_jobs", "last_polled_at", "TEXT")
        self._ensure_column("hermes_jobs", "workspace_title", "TEXT")
        self._ensure_column("hermes_jobs", "workspace_group", "TEXT")
        self._ensure_column("hermes_jobs", "workspace_deleted", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("computer_use_sessions", "latest_frame_url", "TEXT")
        self.db.commit()

    def create_computer_session(
        self, *, task: str, agent: str, source_event_id: str | None = None
    ) -> dict[str, Any]:
        identifier = str(uuid.uuid4())
        timestamp = now()
        with self.lock:
            self.db.execute(
                "INSERT INTO computer_use_sessions(id,source_event_id,task,provider,agent,state,created_at,updated_at) VALUES(?,?,?,'h-company',?,'starting',?,?)",
                (identifier, source_event_id, task, agent, timestamp, timestamp),
            )
            self.db.commit()
        return self.computer_session(identifier) or {}

    def update_computer_session(self, identifier: str, **values: Any) -> dict[str, Any]:
        allowed = {
            "h_session_id", "state", "step_count", "current_action", "latest_answer",
            "agent_view_url", "error", "finished_at", "latest_frame_url",
        }
        changes = {key: value for key, value in values.items() if key in allowed}
        if changes:
            changes["updated_at"] = now()
            columns = ",".join(f"{key}=?" for key in changes)
            with self.lock:
                self.db.execute(
                    f"UPDATE computer_use_sessions SET {columns} WHERE id=?",
                    (*changes.values(), identifier),
                )
                self.db.commit()
        return self.computer_session(identifier) or {}

    def computer_session(self, identifier: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM computer_use_sessions WHERE id=? OR h_session_id=?",
            (identifier, identifier),
        ).fetchone()
        return dict(row) if row else None

    def computer_session_for_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM computer_use_sessions WHERE source_event_id=? ORDER BY created_at DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        return dict(row) if row else None

    def unfinished_computer_sessions(self) -> list[dict[str, Any]]:
        """Sessions a restart orphaned: still open, and the agent kept working."""
        rows = self.db.execute(
            "SELECT * FROM computer_use_sessions "
            "WHERE state NOT IN ('completed','failed','timed_out','interrupted','configuration_required') "
            "ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_computer_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM computer_use_sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def record_desktop_action(self, receipt: dict[str, Any]) -> dict[str, Any]:
        identifier = str(receipt.get("id") or uuid.uuid4())
        timestamp = str(receipt.get("created_at") or now())
        with self.lock:
            self.db.execute(
                "INSERT INTO desktop_action_receipts(id,provider,computer_id,action,visual_changed,noop,fingerprint_before,fingerprint_after,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    str(receipt.get("provider") or "unknown"),
                    receipt.get("computer_id"),
                    str(receipt.get("action") or "unknown"),
                    None if receipt.get("visual_changed") is None else int(bool(receipt["visual_changed"])),
                    None if receipt.get("noop") is None else int(bool(receipt["noop"])),
                    receipt.get("fingerprint_before"),
                    receipt.get("fingerprint_after"),
                    json.dumps(receipt.get("result") or {}, sort_keys=True, default=str),
                    timestamp,
                ),
            )
            self.db.commit()
        row = self.db.execute("SELECT * FROM desktop_action_receipts WHERE id=?", (identifier,)).fetchone()
        return dict(row) if row else {}

    def list_desktop_actions(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM desktop_action_receipts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def create_run(self, source: str, signal_type: str, title: str, archive_event_id: str | None = None) -> str:
        run_id = str(uuid.uuid4())
        with self.lock:
            self.db.execute("INSERT INTO runs(id,text,source,signal_type,title,status,created_at,archive_event_id) VALUES(?,?,?,?,?,?,?,?)", (run_id, "[complete archive]", source, signal_type, title, "queued", now(), archive_event_id))
            self.db.commit()
        return run_id

    def record_relevance(self, archive_id: str, event_id: str, decision: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO relevance_decisions(archive_id,event_id,content_fingerprint,disposition,owner_status,categories_json,confidence,reason_code,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    archive_id,
                    event_id,
                    decision["content_fingerprint"],
                    decision["disposition"],
                    decision["owner_status"],
                    json.dumps(decision["categories"]),
                    float(decision["confidence"]),
                    decision["reason_code"],
                    json.dumps(decision["evidence"], default=str),
                    now(),
                ),
            )
            self.db.commit()

    def relevance_for_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM relevance_decisions WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["categories"] = json.loads(result.pop("categories_json"))
        result["evidence"] = json.loads(result.pop("evidence_json"))
        return result

    def interest_model(self) -> InterestModel:
        weights = tuple(
            InterestWeight(Category(row["category"]), float(row["weight"]), row["source"])
            for row in self.db.execute("SELECT category,weight,source FROM interest_weights ORDER BY category")
        )
        corrections = tuple(
            CorrectionRecord(
                content_fingerprint=row["content_fingerprint"],
                disposition=Disposition(row["disposition"]),
                categories=tuple(Category(value) for value in json.loads(row["categories_json"])),
                reason_code=row["reason_code"],
            )
            for row in self.db.execute("SELECT * FROM relevance_corrections ORDER BY updated_at")
        )
        return InterestModel(weights=weights, corrections=corrections)

    def set_interest_weight(self, weight: InterestWeight) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO interest_weights(category,weight,source,updated_at) VALUES(?,?,?,?) ON CONFLICT(category) DO UPDATE SET weight=excluded.weight,source=excluded.source,updated_at=excluded.updated_at",
                (weight.category.value, weight.weight, weight.source, now()),
            )
            self.db.commit()

    def add_relevance_correction(self, correction: CorrectionRecord) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO relevance_corrections(content_fingerprint,disposition,categories_json,reason_code,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(content_fingerprint) DO UPDATE SET disposition=excluded.disposition,categories_json=excluded.categories_json,reason_code=excluded.reason_code,updated_at=excluded.updated_at",
                (
                    correction.content_fingerprint,
                    correction.disposition.value,
                    json.dumps([category.value for category in correction.categories]),
                    correction.reason_code,
                    now(),
                ),
            )
            self.db.commit()

    def add_operational_memory(self, archive_id: str, run_id: str, fingerprint: str, category: str, entity: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO operational_memories(id,archive_id,run_id,content_fingerprint,category,entity,created_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), archive_id, run_id, fingerprint, category, entity, now()),
            )
            self.db.commit()

    def list_operational_memories(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM operational_memories ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        ]

    def create_hermes_job(
        self,
        *,
        job_id: str,
        run_id: str,
        archive_id: str,
        event_id: str,
        idempotency_key: str,
        capsule_path: str,
        category: str,
        entity: str,
        intent: str = "Research and evaluate the named item, then return cited findings and a safe next action.",
        acceptance: list[str] | tuple[str, ...] = ("Use primary sources", "Verify claims", "Record blocked effects"),
        discord_chat_id: str | None = None,
        discord_user_id: str | None = None,
        initial_state: str = "pending",
    ) -> tuple[dict[str, Any], bool]:
        timestamp = now()
        with self.lock:
            existing = self.db.execute("SELECT * FROM hermes_jobs WHERE run_id=? OR archive_id=? OR event_id=?", (run_id, archive_id, event_id)).fetchone()
            if existing:
                return dict(existing), False
            self.db.execute(
                "INSERT INTO hermes_jobs(id,run_id,archive_id,event_id,idempotency_key,state,capsule_path,category,entity,intent,acceptance_json,discord_chat_id,discord_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, run_id, archive_id, event_id, idempotency_key, initial_state, capsule_path, category, entity, intent[:500], json.dumps(list(acceptance)[:20]), discord_chat_id, discord_user_id, timestamp, timestamp),
            )
            self.db.commit()
        return self.hermes_job(job_id) or {}, True

    def hermes_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM hermes_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def mark_job_polled(self, job_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE hermes_jobs SET last_polled_at=? WHERE id=?", (now(), job_id))
            self.db.commit()

    def hermes_job_for_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM hermes_jobs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def hermes_job_for_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM hermes_jobs WHERE event_id=? ORDER BY created_at DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        return dict(row) if row else None

    def pending_hermes_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM hermes_jobs WHERE state IN ('pending','retry','dispatching') ORDER BY created_at LIMIT ?", (limit,)).fetchall()]

    def unnotified_hermes_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM hermes_jobs WHERE task_id IS NOT NULL AND notification_state != 'subscribed' AND state NOT IN ('completed','cancelled') ORDER BY updated_at LIMIT ?", (limit,)).fetchall()]

    def active_hermes_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM hermes_jobs WHERE task_id IS NOT NULL AND state IN ('enqueued','queued','in_progress','needs_attention') ORDER BY updated_at LIMIT ?", (limit,)).fetchall()]

    def mark_hermes_dispatching(self, job_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE hermes_jobs SET state='dispatching',attempts=attempts+1,last_error=NULL,updated_at=? WHERE id=?", (now(), job_id))
            self.db.commit()

    def mark_hermes_enqueued(self, job_id: str, task_id: str) -> None:
        timestamp = now()
        with self.lock:
            self.db.execute("UPDATE hermes_jobs SET task_id=?,state='enqueued',last_error=NULL,last_sync_at=?,updated_at=? WHERE id=?", (task_id, timestamp, timestamp, job_id))
            self.db.execute("UPDATE runs SET status='queued' WHERE id=(SELECT run_id FROM hermes_jobs WHERE id=?)", (job_id,))
            self.db.commit()

    def mark_hermes_retry(self, job_id: str, error: str) -> None:
        with self.lock:
            self.db.execute("UPDATE hermes_jobs SET state='retry',last_error=?,updated_at=? WHERE id=?", (error[:500], now(), job_id))
            self.db.commit()

    def mark_hermes_notification(self, job_id: str, state: str, error: str | None = None) -> None:
        with self.lock:
            self.db.execute("UPDATE hermes_jobs SET notification_state=?,last_error=COALESCE(?,last_error),updated_at=? WHERE id=?", (state, error[:500] if error else None, now(), job_id))
            self.db.commit()

    def sync_hermes_job(self, job_id: str, state: str, summary: str | None = None, error: str | None = None) -> None:
        timestamp = now()
        run_status = {
            "queued": "queued",
            "enqueued": "queued",
            "in_progress": "running",
            "completed": "completed",
            "dead_letter": "failed",
            "needs_attention": "needs_attention",
            "cancelled": "cancelled",
        }.get(state, state)
        with self.lock:
            self.db.execute("UPDATE hermes_jobs SET state=?,last_error=?,last_sync_at=?,updated_at=? WHERE id=?", (state, error[:500] if error else None, timestamp, timestamp, job_id))
            job = self.db.execute("SELECT run_id FROM hermes_jobs WHERE id=?", (job_id,)).fetchone()
            if job:
                fields: dict[str, Any] = {"status": run_status}
                if summary:
                    fields["summary"] = summary[:20_000]
                if state == "completed":
                    fields.update({"finished_at": timestamp, "success": 1})
                elif state in {"dead_letter", "cancelled"}:
                    fields.update({"finished_at": timestamp, "success": 0})
                clause = ",".join(f"{key}=?" for key in fields)
                self.db.execute(f"UPDATE runs SET {clause} WHERE id=?", (*fields.values(), job["run_id"]))
            self.db.commit()

    def hermes_job_counts(self) -> dict[str, int]:
        return {row["state"]: row["count"] for row in self.db.execute("SELECT state,COUNT(*) count FROM hermes_jobs GROUP BY state")}

    def list_hermes_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM hermes_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    def list_workspace_items(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id,event_id,category,entity,workspace_title,workspace_group,state,task_id,created_at,updated_at "
            "FROM hermes_jobs WHERE workspace_deleted=0 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        category_groups = {
            "repository": "research", "web_task": "research", "tool": "research",
            "startup": "research", "performance": "research", "todo": "tasks",
            "contact": "tasks", "goal": "tasks", "event": "events",
        }
        return [
            {
                **dict(row),
                "title": row["workspace_title"] or row["entity"],
                "group": row["workspace_group"] or (
                    "backlog" if row["state"] == "backlog" else category_groups.get(row["category"], "backlog")
                ),
            }
            for row in rows
        ]

    def update_workspace_item(
        self, identifier: str, *, title: str, group: str
    ) -> dict[str, Any] | None:
        with self.lock:
            self.db.execute(
                "UPDATE hermes_jobs SET workspace_title=?,workspace_group=?,updated_at=? "
                "WHERE id=? AND workspace_deleted=0",
                (title[:300], group, now(), identifier),
            )
            self.db.commit()
        return next((row for row in self.list_workspace_items() if row["id"] == identifier), None)

    def delete_workspace_item(self, identifier: str) -> bool:
        with self.lock:
            cursor = self.db.execute(
                "UPDATE hermes_jobs SET workspace_deleted=1,updated_at=? "
                "WHERE id=? AND workspace_deleted=0",
                (now(), identifier),
            )
            self.db.commit()
        return cursor.rowcount == 1

    def kanban_pending_create(self, *, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM hermes_jobs WHERE task_id IS NULL AND state IN ('pending','retry','dispatching') AND attempts<5 ORDER BY created_at LIMIT ?", (limit,)).fetchall()
            identifiers = [row["id"] for row in rows]
            if identifiers:
                placeholders = ",".join("?" for _ in identifiers)
                self.db.execute(f"UPDATE hermes_jobs SET state='dispatching',attempts=attempts+1,updated_at=? WHERE id IN ({placeholders})", (now(), *identifiers))
                self.db.commit()
        return [
            {
                "run_id": row["run_id"],
                "archive_id": row["archive_id"],
                "category": row["category"],
                "entity": row["entity"],
                "intent": row["intent"],
                "acceptance": json.loads(row["acceptance_json"]),
                "idempotency_key": row["idempotency_key"],
            }
            for row in rows
        ]

    def kanban_record_created(self, run_id: str, *, task_id: str, idempotency_key: str, capsule_path: str, hermes_status: str) -> bool:
        timestamp = now()
        with self.lock:
            job = self.db.execute("SELECT * FROM hermes_jobs WHERE run_id=?", (run_id,)).fetchone()
            if not job:
                return False
            if job["task_id"] not in (None, task_id):
                return False
            accepted = job["state"] == "dispatching"
            next_state = "enqueued" if accepted else job["state"]
            self.db.execute(
                "UPDATE hermes_jobs SET task_id=?,idempotency_key=?,capsule_path=?,state=?,hermes_status=?,last_error=NULL,last_sync_at=?,updated_at=? WHERE run_id=?",
                (task_id, idempotency_key, capsule_path, next_state, hermes_status, timestamp, timestamp, run_id),
            )
            if accepted:
                self.db.execute("UPDATE runs SET status='queued' WHERE id=?", (run_id,))
            self.db.commit()
        return accepted

    def kanban_record_create_failure(self, run_id: str, *, error: str) -> None:
        with self.lock:
            job = self.db.execute("SELECT attempts,state FROM hermes_jobs WHERE run_id=?", (run_id,)).fetchone()
            if not job or job["state"] != "dispatching":
                return
            terminal = int(job["attempts"]) >= 5
            state = "dead_letter" if terminal else "retry"
            timestamp = now()
            self.db.execute("UPDATE hermes_jobs SET state=?,last_error=?,updated_at=? WHERE run_id=?", (state, error[:500], timestamp, run_id))
            if terminal:
                self.db.execute("UPDATE runs SET status='failed',success=0,finished_at=? WHERE id=?", (timestamp, run_id))
            self.db.commit()

    def kanban_pending_notifications(self, *, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT h.*,COALESCE(NULLIF(c.latest_answer,''),NULLIF(c.error,''),NULLIF(r.summary,''),h.latest_outcome,'Completed') AS result_summary,"
                "c.step_count AS computer_steps,c.created_at AS computer_started_at,"
                "c.finished_at AS computer_finished_at,c.agent_view_url AS computer_replay_url,"
                "c.state AS computer_state "
                "FROM hermes_jobs h LEFT JOIN runs r ON r.id=h.run_id "
                "LEFT JOIN computer_use_sessions c ON c.source_event_id=h.event_id "
                "WHERE h.task_id IS NOT NULL AND h.discord_chat_id IS NOT NULL "
                "AND h.notification_state NOT IN ('delivered','failed') AND h.notification_attempts<5 "
                "AND h.state='completed' "
                "AND (h.category!='web_task' OR (c.state='completed' AND NULLIF(c.latest_answer,'') IS NOT NULL) "
                "OR c.state IN ('failed','timed_out','interrupted','configuration_required')) "
                "ORDER BY h.updated_at LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                placeholders = ",".join("?" for _ in rows)
                self.db.execute(
                    f"UPDATE hermes_jobs SET notification_attempts=notification_attempts+1,updated_at=? WHERE id IN ({placeholders})",
                    (now(), *(row["id"] for row in rows)),
                )
                self.db.commit()
        return [
            {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "discord_chat_id": row["discord_chat_id"],
                "event_id": row["event_id"],
                "entity": row["entity"],
                "result_summary": row["result_summary"],
                "computer_steps": row["computer_steps"],
                "computer_started_at": row["computer_started_at"],
                "computer_finished_at": row["computer_finished_at"],
                "computer_replay_url": row["computer_replay_url"],
                "computer_state": row["computer_state"],
            }
            for row in rows
        ]

    def kanban_record_notified(self, run_id: str, *, receipt: dict[str, str]) -> None:
        with self.lock:
            self.db.execute("UPDATE hermes_jobs SET notification_state='delivered',notification_json=?,last_error=NULL,updated_at=? WHERE run_id=?", (json.dumps(receipt, sort_keys=True), now(), run_id))
            self.db.commit()

    def kanban_record_notification_failure(self, run_id: str, *, error: str) -> None:
        with self.lock:
            job = self.db.execute("SELECT notification_attempts FROM hermes_jobs WHERE run_id=?", (run_id,)).fetchone()
            state = "failed" if job and int(job["notification_attempts"]) >= 5 else "retry"
            self.db.execute("UPDATE hermes_jobs SET notification_state=?,last_error=?,updated_at=? WHERE run_id=?", (state, error[:500], now(), run_id))
            self.db.commit()

    def kanban_active(self, *, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "event_id": row["event_id"],
                "category": row["category"],
            }
            for row in self.db.execute("SELECT run_id,task_id,event_id,category FROM hermes_jobs WHERE task_id IS NOT NULL AND state IN ('enqueued','queued','in_progress','needs_attention') ORDER BY updated_at LIMIT ?", (limit,)).fetchall()
        ]

    def kanban_record_reconciled(self, run_id: str, *, task_id: str, state: str, hermes_status: str, latest_outcome: str, diagnostics: list[str] | tuple[str, ...], summary: str | None = None) -> bool:
        timestamp = now()
        run_status = {
            "queued": "queued",
            "enqueued": "queued",
            "in_progress": "running",
            "completed": "completed",
            "dead_letter": "failed",
            "needs_attention": "needs_attention",
            "cancelled": "cancelled",
        }.get(state, state)
        with self.lock:
            job = self.db.execute(
                "SELECT id,run_id FROM hermes_jobs WHERE run_id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()
            if not job:
                return False
            changed = self.db.execute(
                "UPDATE hermes_jobs SET state=?,hermes_status=?,latest_outcome=?,diagnostics_json=?,last_error=NULL,last_sync_at=?,updated_at=? WHERE id=? AND task_id=?",
                (
                    state,
                    hermes_status,
                    latest_outcome,
                    json.dumps(list(diagnostics)),
                    timestamp,
                    timestamp,
                    job["id"],
                    task_id,
                ),
            )
            if changed.rowcount != 1:
                self.db.rollback()
                return False
            fields: dict[str, Any] = {"status": run_status}
            if summary:
                fields["summary"] = summary[:20_000]
            if state == "completed":
                fields.update({"finished_at": timestamp, "success": 1})
            elif state in {"dead_letter", "cancelled"}:
                fields.update({"finished_at": timestamp, "success": 0})
            clause = ",".join(f"{key}=?" for key in fields)
            self.db.execute(
                f"UPDATE runs SET {clause} WHERE id=?",
                (*fields.values(), run_id),
            )
            self.db.commit()
        return True

    def kanban_record_reconcile_failure(self, run_id: str, *, error: str) -> None:
        with self.lock:
            self.db.execute("UPDATE hermes_jobs SET last_error=?,last_sync_at=?,updated_at=? WHERE run_id=?", (error[:500], now(), now(), run_id))
            self.db.commit()

    def heartbeat(self, name: str, status: str, details: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO service_heartbeats(name,status,details_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET status=excluded.status,details_json=excluded.details_json,updated_at=excluded.updated_at",
                (name, status, json.dumps(details or {}, default=str, sort_keys=True), now()),
            )
            self.db.commit()

    def heartbeat_status(self, name: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM service_heartbeats WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def supersede_hermes_task(
        self,
        run_id: str,
        *,
        expected_task_id: str,
        idempotency_key: str,
        reason: str,
        replacement_entity: str | None = None,
    ) -> None:
        timestamp = now()
        if replacement_entity is not None:
            replacement_entity = " ".join(replacement_entity.split())[:240]
            if not replacement_entity:
                raise ValueError("replacement entity must not be empty")
        with self.lock:
            job = self.db.execute("SELECT * FROM hermes_jobs WHERE run_id=?", (run_id,)).fetchone()
            if not job or job["task_id"] != expected_task_id:
                raise ValueError("Hermes task changed before supersession")
            self.db.execute(
                "INSERT INTO hermes_task_history(id,job_id,task_id,idempotency_key,outcome,reason,archived_at) VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), job["id"], expected_task_id, job["idempotency_key"], "superseded", reason[:500], timestamp),
            )
            self.db.execute(
                "UPDATE hermes_jobs SET task_id=NULL,idempotency_key=?,entity=COALESCE(?,entity),state='retry',notification_state='pending',notification_json=NULL,hermes_status='superseded',latest_outcome='superseded',diagnostics_json='[]',last_error=?,updated_at=? WHERE run_id=?",
                (idempotency_key, replacement_entity, reason[:500], timestamp, run_id),
            )
            self.db.execute("UPDATE runs SET status='queued',success=NULL,finished_at=NULL WHERE id=?", (run_id,))
            self.db.commit()

    def cancel_nonrunning_hermes_job(self, run_id: str, *, reason: str) -> bool:
        """Cancel a false-positive job only when no worker can still be running."""

        timestamp = now()
        with self.lock:
            job = self.db.execute(
                "SELECT * FROM hermes_jobs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not job or job["state"] not in {"pending", "retry", "needs_attention"}:
                return False
            if job["task_id"]:
                self.db.execute(
                    "INSERT INTO hermes_task_history(id,job_id,task_id,idempotency_key,outcome,reason,archived_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        job["id"],
                        job["task_id"],
                        job["idempotency_key"],
                        "cancelled",
                        reason[:500],
                        timestamp,
                    ),
                )
            self.db.execute(
                "UPDATE hermes_jobs SET state='cancelled',last_error=?,last_sync_at=?,updated_at=? WHERE id=?",
                (reason[:500], timestamp, timestamp, job["id"]),
            )
            self.db.execute("DELETE FROM operational_memories WHERE run_id=?", (run_id,))
            self.db.execute(
                "UPDATE runs SET status='cancelled',success=0,finished_at=? WHERE id=?",
                (timestamp, run_id),
            )
            self.db.commit()
        return True

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        clause = ", ".join(f"{key} = ?" for key in fields)
        with self.lock:
            self.db.execute(f"UPDATE runs SET {clause} WHERE id = ?", (*fields.values(), run_id))
            self.db.commit()

    def add_step(self, run_id: str, agent: str, status: str, input_summary: str, output: Any, **meta: Any) -> str:
        step_id = str(uuid.uuid4())
        with self.lock:
            self.db.execute("INSERT INTO steps(id,run_id,agent,status,input_summary,output_json,started_at,finished_at,latency_ms,input_tokens,output_tokens,estimated_cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (step_id, run_id, agent, status, input_summary, json.dumps(output, default=str), meta.get("started_at", now()), meta.get("finished_at"), meta.get("latency_ms", 0), meta.get("input_tokens", 0), meta.get("output_tokens", 0), meta.get("estimated_cost_usd", 0.0)))
            self.db.commit()
        return step_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["steps"] = [dict(s) | {"output": json.loads(s["output_json"])} for s in self.db.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()]
        return out

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    def metrics(self) -> dict[str, Any]:
        r = self.db.execute("SELECT COUNT(*) total, SUM(status='completed') completed, AVG(latency_ms) latency FROM runs").fetchone()
        users = self.db.execute("SELECT COUNT(*) FROM users WHERE first_use_at IS NOT NULL").fetchone()[0]
        return {"operational_total": r[0] or 0, "completed": r[1] or 0, "avg_latency_ms": round(r[2] or 0), "activated_users": users}

    def add_eval(self, run_id: str | None, input_text: str, expected: str, observed: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO eval_cases(id,run_id,input_text,expected,observed,created_at) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), run_id, input_text, expected, observed, now()))
            self.db.commit()

    def signup(self, email: str, source: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO users(email,source,created_at) VALUES(?,?,?)", (email.lower().strip(), source, now()))
            self.db.commit()

    def activate(self, email: str) -> None:
        with self.lock:
            self.db.execute("UPDATE users SET first_use_at=? WHERE email=?", (now(), email.lower().strip()))
            self.db.commit()

    def add_role(self, name: str, job: str, tools: list[str], guardrails: str) -> dict[str, Any]:
        value = {"id": str(uuid.uuid4()), "name": name, "job": job, "tools": tools, "guardrails": guardrails, "created_at": now()}
        with self.lock:
            self.db.execute("INSERT INTO roles(id,name,job,tools_json,guardrails,created_at) VALUES(?,?,?,?,?,?)", (value["id"], name, job, json.dumps(tools), guardrails, value["created_at"]))
            self.db.commit()
        return value

    def roles(self) -> list[dict[str, Any]]:
        return [{**dict(r), "tools": json.loads(r["tools_json"])} for r in self.db.execute("SELECT * FROM roles ORDER BY created_at").fetchall()]

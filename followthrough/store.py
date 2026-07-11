from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
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
            """
        )
        self.db.commit()

    def create_run(self, text: str, source: str, signal_type: str, title: str) -> str:
        run_id = str(uuid.uuid4())
        with self.lock:
            self.db.execute("INSERT INTO runs(id,text,source,signal_type,title,status,created_at) VALUES(?,?,?,?,?,?,?)", (run_id, text, source, signal_type, title, "queued", now()))
            self.db.commit()
        return run_id

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
        r = self.db.execute("SELECT COUNT(*) total, SUM(status='completed') completed, SUM(status='discarded') discarded, AVG(latency_ms) latency FROM runs").fetchone()
        users = self.db.execute("SELECT COUNT(*) FROM users WHERE first_use_at IS NOT NULL").fetchone()[0]
        return {"total": r[0] or 0, "completed": r[1] or 0, "discarded": r[2] or 0, "avg_latency_ms": round(r[3] or 0), "activated_users": users}

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

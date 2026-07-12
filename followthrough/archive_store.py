from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .store import now


class ArchiveStore:
    """Ciphertext-only archive ledger, physically separate from operations."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS archive_events (
              id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, device_id TEXT NOT NULL,
              source TEXT NOT NULL, occurred_at TEXT NOT NULL, received_at TEXT NOT NULL,
              transcript_cipher BLOB NOT NULL, transcript_sha256 TEXT NOT NULL,
              relevant INTEGER NOT NULL, classification TEXT NOT NULL,
              metadata_json TEXT NOT NULL, run_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS audio_chunks (
              id TEXT PRIMARY KEY, archive_id TEXT NOT NULL, sequence INTEGER NOT NULL,
              path TEXT NOT NULL, mime_type TEXT NOT NULL, plaintext_sha256 TEXT NOT NULL,
              plaintext_bytes INTEGER NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(archive_id, sequence), FOREIGN KEY(archive_id) REFERENCES archive_events(id)
            );
            CREATE TABLE IF NOT EXISTS archive_migrations (
              name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capture_streams (
              id TEXT PRIMARY KEY, device_id TEXT NOT NULL, source TEXT NOT NULL,
              next_sequence INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def archive_event(
        self,
        *,
        event_id: str,
        device_id: str,
        source: str,
        occurred_at: str,
        transcript_cipher: bytes,
        transcript_sha256: str,
        relevant: bool,
        classification: str,
        metadata: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        archive_id = str(uuid.uuid4())
        received_at = now()
        with self.lock:
            existing = self.db.execute("SELECT * FROM archive_events WHERE event_id=?", (event_id,)).fetchone()
            if existing:
                if existing["transcript_sha256"] == transcript_sha256:
                    previous = json.loads(existing["metadata_json"])
                    hooks = sorted(set(previous.get("observed_hooks", [])) | set(metadata.get("observed_hooks", [])) | {value for value in (previous.get("hook"), metadata.get("hook")) if value})
                    merged = {**previous, **metadata, "observed_hooks": hooks}
                    # The capture principal is the server-derived authorization
                    # binding for the device job-return channel. A later delivery
                    # of the same content (e.g. a second phone hearing the same
                    # utterance) must never be able to reassign ownership, so the
                    # first-writer principal is preserved.
                    if "capture_principal" in previous:
                        merged["capture_principal"] = previous["capture_principal"]
                    self.db.execute("UPDATE archive_events SET metadata_json=? WHERE id=?", (json.dumps(merged, default=str), existing["id"]))
                    self.db.commit()
                    existing = self.db.execute("SELECT * FROM archive_events WHERE id=?", (existing["id"],)).fetchone()
                return dict(existing), False
            self.db.execute(
                "INSERT INTO archive_events(id,event_id,device_id,source,occurred_at,received_at,transcript_cipher,transcript_sha256,relevant,classification,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (archive_id, event_id, device_id, source, occurred_at, received_at, transcript_cipher, transcript_sha256, int(relevant), classification, json.dumps(metadata, default=str)),
            )
            self.db.commit()
        return self.by_id(archive_id) or {}, True

    def by_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM archive_events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def by_id(self, archive_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM archive_events WHERE id=?", (archive_id,)).fetchone()
        return dict(row) if row else None

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM archive_events WHERE classification != 'audio_only' "
            "ORDER BY received_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def link_run(self, archive_id: str, run_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE archive_events SET run_id=? WHERE id=?", (run_id, archive_id))
            self.db.commit()

    def set_classification(self, archive_id: str, *, relevant: bool, classification: str) -> None:
        with self.lock:
            self.db.execute("UPDATE archive_events SET relevant=?,classification=? WHERE id=?", (int(relevant), classification, archive_id))
            self.db.commit()

    def audio_chunk(self, archive_id: str, sequence: int) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM audio_chunks WHERE archive_id=? AND sequence=?", (archive_id, sequence)).fetchone()
        return dict(row) if row else None

    def add_audio_chunk(self, archive_id: str, sequence: int, path: str, mime_type: str, digest: str, size: int) -> tuple[dict[str, Any], bool]:
        chunk_id = str(uuid.uuid4())
        with self.lock:
            existing = self.db.execute("SELECT * FROM audio_chunks WHERE archive_id=? AND sequence=?", (archive_id, sequence)).fetchone()
            if existing:
                return dict(existing), False
            self.db.execute(
                "INSERT INTO audio_chunks(id,archive_id,sequence,path,mime_type,plaintext_sha256,plaintext_bytes,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (chunk_id, archive_id, sequence, path, mime_type, digest, size, now()),
            )
            self.db.commit()
        return self.audio_chunk(archive_id, sequence) or {}, True

    def allocate_stream_sequence(self, stream_id: str, device_id: str, source: str) -> int:
        with self.lock:
            row = self.db.execute("SELECT next_sequence FROM capture_streams WHERE id=?", (stream_id,)).fetchone()
            sequence = int(row[0]) if row else 0
            self.db.execute(
                "INSERT INTO capture_streams(id,device_id,source,next_sequence,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET next_sequence=excluded.next_sequence,updated_at=excluded.updated_at",
                (stream_id, device_id, source, sequence + 1, now()),
            )
            self.db.commit()
        return sequence

    def audio_manifest(self, archive_id: str) -> dict[str, Any]:
        sequences = [row[0] for row in self.db.execute("SELECT sequence FROM audio_chunks WHERE archive_id=? ORDER BY sequence", (archive_id,)).fetchall()]
        missing: list[int] = []
        if sequences:
            present = set(sequences)
            missing = [number for number in range(sequences[0], sequences[-1] + 1) if number not in present]
        return {"sequences": sequences, "missing": missing, "complete": not missing, "chunks": len(sequences)}

    def metrics(self) -> dict[str, int]:
        row = self.db.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(relevant=0),0) discarded FROM archive_events WHERE classification != 'audio_only'"
        ).fetchone()
        audio = self.db.execute(
            "SELECT COUNT(*) chunks, COALESCE(SUM(plaintext_bytes),0) bytes FROM audio_chunks"
        ).fetchone()
        return {
            "total": int(row["total"]),
            "discarded": int(row["discarded"]),
            "audio_chunks": int(audio["chunks"]),
            "audio_bytes": int(audio["bytes"]),
        }

    def import_legacy(self, source: sqlite3.Connection) -> dict[str, int]:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "archive_events" not in tables:
            return {"events": 0, "audio_chunks": 0}
        events = source.execute("SELECT * FROM archive_events ORDER BY rowid").fetchall()
        chunks = source.execute("SELECT * FROM audio_chunks ORDER BY rowid").fetchall() if "audio_chunks" in tables else []
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for row in events:
                    values = tuple(row)
                    self.db.execute(
                        "INSERT OR IGNORE INTO archive_events(id,event_id,device_id,source,occurred_at,received_at,transcript_cipher,transcript_sha256,relevant,classification,metadata_json,run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                for row in chunks:
                    values = tuple(row)
                    self.db.execute(
                        "INSERT OR IGNORE INTO audio_chunks(id,archive_id,sequence,path,mime_type,plaintext_sha256,plaintext_bytes,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        values,
                    )
                missing_events = [row["id"] for row in events if not self.db.execute("SELECT 1 FROM archive_events WHERE id=? AND transcript_sha256=?", (row["id"], row["transcript_sha256"])).fetchone()]
                missing_chunks = [row["id"] for row in chunks if not self.db.execute("SELECT 1 FROM audio_chunks WHERE id=? AND plaintext_sha256=?", (row["id"], row["plaintext_sha256"])).fetchone()]
                if missing_events or missing_chunks:
                    raise RuntimeError("legacy archive verification failed")
                self.db.execute(
                    "INSERT OR REPLACE INTO archive_migrations(name,applied_at,details_json) VALUES(?,?,?)",
                    ("operations-db-split-v1", now(), json.dumps({"events": len(events), "audio_chunks": len(chunks)})),
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return {"events": len(events), "audio_chunks": len(chunks)}

    def integrity_check(self) -> bool:
        return self.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


GENESIS_HASH = "0" * 64
MAX_LEDGER_LINE_BYTES = 4 * 1024 * 1024
DEFAULT_SERVICES = (
    "followthrough.service",
    "followthrough-orchestrator.service",
    "hermes-gateway.service",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def opaque(value: object) -> str:
    """Return a stable receipt-safe identifier without emitting an event or job key."""

    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:16]


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


class LedgerIntegrityError(RuntimeError):
    pass


class JsonlLedger:
    """Mode-0600 append-only JSONL receipt chain.

    Existing content is fully verified before another record is appended. This does
    not replace filesystem immutability, but it makes truncation, editing, and record
    reordering detectable without requiring a signing key.
    """

    def __init__(self, path: Path, *, fsync: bool = True) -> None:
        requested = path.expanduser()
        if requested.is_symlink():
            raise LedgerIntegrityError("refusing a symlink checkpoint ledger")
        self.path = requested.resolve(strict=False)
        self.fsync = fsync
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            self.path.parent.chmod(0o700)
        if self.path.exists() and not stat.S_ISREG(self.path.stat().st_mode):
            raise LedgerIntegrityError("checkpoint ledger is not a regular file")
        self.previous_hash = self._verify_existing()
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        self.fd = os.open(self.path, flags, 0o600)
        os.fchmod(self.fd, 0o600)

    def _verify_existing(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS_HASH
        previous = GENESIS_HASH
        with self.path.open("rb") as stream:
            for line_number, raw in enumerate(stream, start=1):
                if len(raw) > MAX_LEDGER_LINE_BYTES:
                    raise LedgerIntegrityError(f"ledger line {line_number} exceeds size limit")
                if not raw.endswith(b"\n"):
                    raise LedgerIntegrityError("checkpoint ledger has a truncated final record")
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise LedgerIntegrityError(
                        f"checkpoint ledger line {line_number} is invalid JSON"
                    ) from exc
                if not isinstance(record, dict):
                    raise LedgerIntegrityError(
                        f"checkpoint ledger line {line_number} is not an object"
                    )
                claimed_hash = record.pop("record_hash", None)
                if record.get("previous_record_hash") != previous:
                    raise LedgerIntegrityError(
                        f"checkpoint ledger chain breaks at line {line_number}"
                    )
                expected = hashlib.sha256(
                    (previous + canonical_json(record)).encode()
                ).hexdigest()
                if claimed_hash != expected:
                    raise LedgerIntegrityError(
                        f"checkpoint ledger hash fails at line {line_number}"
                    )
                previous = expected
        return previous

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        if "record_hash" in record or "previous_record_hash" in record:
            raise ValueError("ledger hash fields are reserved")
        record["previous_record_hash"] = self.previous_hash
        digest = hashlib.sha256(
            (self.previous_hash + canonical_json(record)).encode()
        ).hexdigest()
        record["record_hash"] = digest
        encoded = (canonical_json(record) + "\n").encode()
        if len(encoded) > MAX_LEDGER_LINE_BYTES:
            raise ValueError("checkpoint record exceeds size limit")
        view = memoryview(encoded)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise OSError("could not append checkpoint")
            view = view[written:]
        if self.fsync:
            os.fsync(self.fd)
        self.previous_hash = digest
        return record

    def close(self) -> None:
        if getattr(self, "fd", None) is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> JsonlLedger:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class SoakConfig:
    ops_db: Path
    archive_db: Path
    effects_db: Path
    output: Path
    health_url: str = "http://127.0.0.1:18765/healthz"
    services: tuple[str, ...] = DEFAULT_SERVICES
    health_timeout_seconds: float = 5.0
    command_timeout_seconds: float = 5.0
    min_free_bytes: int = 1_073_741_824
    max_used_percent: float = 95.0
    disk_paths: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.health_timeout_seconds <= 0 or self.command_timeout_seconds <= 0:
            raise ValueError("probe timeouts must be positive")
        if self.min_free_bytes < 0:
            raise ValueError("minimum free bytes cannot be negative")
        if not 0 < self.max_used_percent <= 100:
            raise ValueError("maximum disk usage must be in (0, 100]")


HealthProbe = Callable[[str, float], dict[str, Any]]
ServiceProbe = Callable[[str, float], dict[str, Any]]
DiskUsageProbe = Callable[[Path], Any]


def probe_health(url: str, timeout_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "followthrough-soak/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = response.status
            body = response.read(1_048_577)
        if len(body) > 1_048_576:
            raise ValueError("health response exceeds one MiB")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("health response is not an object")
        selected = {
            key: payload.get(key)
            for key in (
                "ok",
                "service",
                "version",
                "database_ready",
                "archive_ready",
                "auth_ready",
                "hermes_cli_present",
                "job_counts",
                "control_mode",
            )
            if key in payload
        }
        orchestrator = payload.get("orchestrator")
        if isinstance(orchestrator, dict):
            selected["orchestrator"] = {
                key: orchestrator.get(key)
                for key in ("name", "status", "updated_at")
                if key in orchestrator
            }
        ok = status_code == 200 and payload.get("ok") is True
        return {
            "ok": ok,
            "status_code": status_code,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "payload": selected,
        }
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return {
            "ok": False,
            "status_code": None,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "error": type(exc).__name__,
        }


def probe_service(name: str, timeout_seconds: float) -> dict[str, Any]:
    properties = (
        "ActiveState",
        "SubState",
        "MainPID",
        "NRestarts",
        "ExecMainStartTimestampMonotonic",
    )
    try:
        completed = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                name,
                "--no-pager",
                f"--property={','.join(properties)}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "name": name, "error": type(exc).__name__}
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in properties:
            values[key] = value
    main_pid = _as_int(values.get("MainPID"))
    restarts = _as_int(values.get("NRestarts"))
    active_state = values.get("ActiveState", "unknown")
    return {
        "ok": completed.returncode == 0 and active_state == "active" and main_pid > 0,
        "name": name,
        "active_state": active_state,
        "sub_state": values.get("SubState", "unknown"),
        "main_pid": main_pid,
        "n_restarts": restarts,
        "start_monotonic_us": _as_int(values.get("ExecMainStartTimestampMonotonic")),
        "return_code": completed.returncode,
    }


def _connect_read_only(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=False)
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    encoded = urllib.parse.quote(str(resolved), safe="/")
    connection = sqlite3.connect(
        f"file:{encoded}?mode=ro", uri=True, timeout=2, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _counts(
    connection: sqlite3.Connection, tables: set[str], expected: Sequence[str]
) -> dict[str, int]:
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in expected
        if table in tables
    }


def _duplicate_groups(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    *,
    nonempty: bool = False,
) -> dict[str, Any]:
    existing = _columns(connection, table)
    if not set(columns).issubset(existing):
        return {"available": False, "groups": 0, "extra_rows": 0, "examples": []}
    quoted = ",".join(f'"{column}"' for column in columns)
    predicates = [f'"{column}" IS NOT NULL' for column in columns]
    if nonempty:
        predicates.extend(f'"{column}" != \'\'' for column in columns)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    rows = connection.execute(
        f'SELECT {quoted},COUNT(*) AS quantity FROM "{table}"{where} '
        f"GROUP BY {quoted} HAVING COUNT(*) > 1"
    ).fetchall()
    return {
        "available": True,
        "groups": len(rows),
        "extra_rows": sum(int(row["quantity"]) - 1 for row in rows),
        "examples": [
            {
                "key_hash": opaque(tuple(row[column] for column in columns)),
                "quantity": int(row["quantity"]),
            }
            for row in rows[:10]
        ],
    }


def _database_snapshot(
    path: Path, expected_tables: Sequence[str]
) -> tuple[dict[str, Any], sqlite3.Connection | None]:
    try:
        connection = _connect_read_only(path)
        integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        tables = _tables(connection)
        missing_tables = sorted(set(expected_tables) - tables)
        snapshot = {
            "ok": integrity_rows == ["ok"] and not missing_tables,
            "integrity": integrity_rows[:10],
            "counts": _counts(connection, tables, expected_tables),
            "missing_tables": missing_tables,
            "size_bytes": path.stat().st_size,
        }
        return snapshot, connection
    except (OSError, sqlite3.Error) as exc:
        return {
            "ok": False,
            "integrity": [],
            "counts": {},
            "missing_tables": list(expected_tables),
            "error": type(exc).__name__,
        }, None


def _control_receipt_hash(row: Mapping[str, Any]) -> str:
    details = json.loads(str(row["details_json"]))
    if not isinstance(details, dict):
        raise ValueError("control audit details are not an object")
    payload = {
        "receipt_id": row["receipt_id"],
        "kind": row["kind"],
        "actor": row["actor"],
        "capability": row["capability"],
        "reason_code": row["reason_code"],
        "details": details,
        "created_at": row["created_at"],
    }
    return hashlib.sha256(
        (str(row["previous_hash"]) + canonical_json(payload)).encode()
    ).hexdigest()


def verify_control_audit(connection: sqlite3.Connection | None) -> dict[str, Any]:
    if connection is None or "control_audit" not in _tables(connection):
        return {"ok": False, "receipts": 0, "reason": "table_missing"}
    required = {
        "sequence",
        "receipt_id",
        "kind",
        "actor",
        "capability",
        "reason_code",
        "details_json",
        "previous_hash",
        "receipt_hash",
        "created_at",
    }
    if not required.issubset(_columns(connection, "control_audit")):
        return {"ok": False, "receipts": 0, "reason": "columns_missing"}
    rows = connection.execute("SELECT * FROM control_audit ORDER BY sequence").fetchall()
    previous = GENESIS_HASH
    for row in rows:
        try:
            if row["previous_hash"] != previous:
                raise ValueError("previous_hash_mismatch")
            expected = _control_receipt_hash(row)
            if row["receipt_hash"] != expected:
                raise ValueError("receipt_hash_mismatch")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "receipts": len(rows),
                "first_invalid_sequence": row["sequence"],
                "reason": str(exc),
                "head_hash": previous,
            }
        previous = str(row["receipt_hash"])
    return {"ok": True, "receipts": len(rows), "head_hash": previous}


def _row_hashes(values: Iterable[object], limit: int = 10) -> list[str]:
    return [opaque(value) for value in list(values)[:limit]]


def inspect_archive_audio(
    archive: sqlite3.Connection | None,
    ops: sqlite3.Connection | None,
) -> dict[str, Any]:
    empty = {
        "ok": False,
        "audio_sequence_gap_streams": 0,
        "capture_sequence_gap_streams": 0,
        "missing_audio_files": 0,
        "empty_audio_files": 0,
        "orphan_audio_rows": 0,
        "audio_only_without_chunk": 0,
        "relevant_without_run": 0,
        "archive_link_gaps": 0,
        "examples": [],
    }
    if archive is None:
        return empty | {"reason": "archive_unavailable"}
    tables = _tables(archive)
    if not {"archive_events", "audio_chunks"}.issubset(tables):
        return empty | {"reason": "archive_tables_missing"}

    events = archive.execute(
        "SELECT id,event_id,relevant,classification,metadata_json,run_id FROM archive_events"
    ).fetchall()
    chunks = archive.execute(
        "SELECT archive_id,sequence,path FROM audio_chunks ORDER BY archive_id,sequence"
    ).fetchall()
    event_ids = {str(row["id"]) for row in events}
    by_event: dict[str, list[int]] = defaultdict(list)
    missing_files: list[tuple[object, object]] = []
    empty_files: list[tuple[object, object]] = []
    orphan: list[tuple[object, object]] = []
    for row in chunks:
        archive_id = str(row["archive_id"])
        sequence = int(row["sequence"])
        by_event[archive_id].append(sequence)
        if archive_id not in event_ids:
            orphan.append((archive_id, sequence))
        path = Path(str(row["path"])).expanduser()
        try:
            if not path.is_file():
                missing_files.append((archive_id, sequence))
            elif path.stat().st_size == 0:
                empty_files.append((archive_id, sequence))
        except OSError:
            missing_files.append((archive_id, sequence))

    audio_gaps: dict[str, list[int]] = {}
    for archive_id, sequences in by_event.items():
        present = set(sequences)
        if present:
            missing = [value for value in range(0, max(present) + 1) if value not in present]
            if missing:
                audio_gaps[archive_id] = missing

    capture_sequences: dict[str, list[int]] = defaultdict(list)
    malformed_metadata = 0
    for row in events:
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError:
            malformed_metadata += 1
            continue
        if not isinstance(metadata, dict):
            malformed_metadata += 1
            continue
        stream_id = metadata.get("capture_stream_id")
        stream_sequence = metadata.get("stream_sequence")
        if stream_id is not None and isinstance(stream_sequence, int):
            capture_sequences[str(stream_id)].append(stream_sequence)
    capture_next: dict[str, int] = {}
    if "capture_streams" in tables and {
        "id",
        "next_sequence",
    }.issubset(_columns(archive, "capture_streams")):
        capture_next = {
            str(row["id"]): int(row["next_sequence"])
            for row in archive.execute("SELECT id,next_sequence FROM capture_streams")
        }
    capture_gaps: dict[str, list[int]] = {}
    for stream_id in set(capture_sequences) | set(capture_next):
        sequences = capture_sequences.get(stream_id, [])
        present = set(sequences)
        expected_next = capture_next.get(
            stream_id, max(present) + 1 if present else 0
        )
        upper = max(expected_next, max(present) + 1 if present else 0)
        missing = [value for value in range(upper) if value not in present]
        if missing:
            capture_gaps[stream_id] = missing

    audio_only_without = [
        row["id"]
        for row in events
        if row["classification"] == "audio_only" and row["id"] not in by_event
    ]
    relevant_without_run = [
        row["id"] for row in events if bool(row["relevant"]) and not row["run_id"]
    ]

    archive_link_gaps: list[object] = []
    if ops is not None:
        ops_tables = _tables(ops)
        if "runs" in ops_tables and "id" in _columns(ops, "runs"):
            run_ids = {str(row[0]) for row in ops.execute("SELECT id FROM runs")}
            archive_link_gaps.extend(
                ("archive_events.run_id", row["run_id"])
                for row in events
                if row["run_id"] and str(row["run_id"]) not in run_ids
            )
        link_specs = (
            ("runs", "archive_event_id"),
            ("relevance_decisions", "archive_id"),
            ("operational_memories", "archive_id"),
            ("hermes_jobs", "archive_id"),
        )
        for table, column in link_specs:
            if table not in ops_tables or column not in _columns(ops, table):
                continue
            rows = ops.execute(
                f'SELECT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL AND "{column}" != \'\''
            ).fetchall()
            archive_link_gaps.extend(
                (table, row[0]) for row in rows if str(row[0]) not in event_ids
            )

    issue_total = sum(
        (
            len(audio_gaps),
            len(capture_gaps),
            len(missing_files),
            len(empty_files),
            len(orphan),
            len(audio_only_without),
            len(relevant_without_run),
            len(archive_link_gaps),
            malformed_metadata,
        )
    )
    examples = (
        _row_hashes(audio_gaps)
        + _row_hashes(capture_gaps)
        + _row_hashes(missing_files)
        + _row_hashes(archive_link_gaps)
    )[:10]
    return {
        "ok": issue_total == 0,
        "audio_sequence_gap_streams": len(audio_gaps),
        "capture_sequence_gap_streams": len(capture_gaps),
        "missing_audio_files": len(missing_files),
        "empty_audio_files": len(empty_files),
        "orphan_audio_rows": len(orphan),
        "audio_only_without_chunk": len(audio_only_without),
        "relevant_without_run": len(relevant_without_run),
        "archive_link_gaps": len(archive_link_gaps),
        "malformed_metadata": malformed_metadata,
        "examples": examples,
    }


def inspect_effect_links(connection: sqlite3.Connection | None) -> dict[str, Any]:
    if connection is None:
        return {
            "ok": False,
            "orphan_transitions": 0,
            "effects_without_transition": 0,
            "reason": "effects_unavailable",
        }
    tables = _tables(connection)
    if not {"effects", "effect_transitions"}.issubset(tables):
        return {
            "ok": False,
            "orphan_transitions": 0,
            "effects_without_transition": 0,
            "reason": "effects_tables_missing",
        }
    orphan = connection.execute(
        "SELECT COUNT(*) FROM effect_transitions t "
        "LEFT JOIN effects e ON e.id=t.effect_id WHERE e.id IS NULL"
    ).fetchone()[0]
    without = connection.execute(
        "SELECT COUNT(*) FROM effects e "
        "LEFT JOIN effect_transitions t ON t.effect_id=e.id WHERE t.effect_id IS NULL"
    ).fetchone()[0]
    return {
        "ok": int(orphan) == 0 and int(without) == 0,
        "orphan_transitions": int(orphan),
        "effects_without_transition": int(without),
    }


def inspect_databases(config: SoakConfig) -> dict[str, Any]:
    specifications = {
        "operations": (
            config.ops_db,
            (
                "runs",
                "steps",
                "relevance_decisions",
                "operational_memories",
                "hermes_jobs",
                "control_audit",
            ),
        ),
        "archive": (
            config.archive_db,
            ("archive_events", "audio_chunks", "capture_streams"),
        ),
        "effects": (config.effects_db, ("effects", "effect_transitions")),
    }
    snapshots: dict[str, dict[str, Any]] = {}
    connections: dict[str, sqlite3.Connection | None] = {}
    try:
        for name, (path, expected) in specifications.items():
            snapshots[name], connections[name] = _database_snapshot(path, expected)

        duplicate_specs = (
            ("archive_event_id", "archive", "archive_events", ("event_id",), False),
            ("job_idempotency", "operations", "hermes_jobs", ("idempotency_key",), False),
            ("job_event", "operations", "hermes_jobs", ("event_id",), False),
            ("job_run", "operations", "hermes_jobs", ("run_id",), False),
            ("job_task", "operations", "hermes_jobs", ("task_id",), True),
            ("effect_idempotency", "effects", "effects", ("idempotency_key",), False),
            (
                "effect_external_receipt",
                "effects",
                "effects",
                ("provider", "external_id"),
                True,
            ),
            (
                "effect_semantic_request",
                "effects",
                "effects",
                ("trigger_event_id", "kind", "request_fingerprint"),
                False,
            ),
        )
        duplicates: dict[str, Any] = {}
        for label, database, table, columns, nonempty in duplicate_specs:
            connection = connections[database]
            if connection is None or table not in _tables(connection):
                duplicates[label] = {
                    "available": False,
                    "groups": 0,
                    "extra_rows": 0,
                    "examples": [],
                }
            else:
                duplicates[label] = _duplicate_groups(
                    connection, table, columns, nonempty=nonempty
                )
        duplicate_groups = sum(int(value["groups"]) for value in duplicates.values())
        # A repeated semantic request is useful diagnostic evidence but is not
        # itself proof of a duplicated effect: two independently approved
        # actions may intentionally share a payload. Stable idempotency keys and
        # external provider receipts are the fail-closed duplication boundary.
        warning_groups = int(duplicates["effect_semantic_request"]["groups"])
        hard_groups = duplicate_groups - warning_groups
        duplicates["ok"] = hard_groups == 0 and all(
            value.get("available", False)
            for key, value in duplicates.items()
            if key != "ok"
        )
        duplicates["total_groups"] = duplicate_groups
        duplicates["hard_failure_groups"] = hard_groups
        duplicates["warning_groups"] = warning_groups

        return {
            "databases": snapshots,
            "duplicates": duplicates,
            "archive_audio": inspect_archive_audio(
                connections["archive"], connections["operations"]
            ),
            "control_audit": verify_control_audit(connections["operations"]),
            "effect_links": inspect_effect_links(connections["effects"]),
        }
    finally:
        for connection in connections.values():
            if connection is not None:
                connection.close()


def _disk_snapshot(
    paths: Sequence[Path],
    min_free_bytes: int,
    max_used_percent: float,
    disk_usage_probe: DiskUsageProbe,
) -> dict[str, Any]:
    volumes: dict[int | str, dict[str, Any]] = {}
    errors: list[str] = []
    for candidate in paths:
        path = candidate.expanduser().resolve(strict=False)
        existing = path if path.exists() else path.parent
        try:
            device: int | str = existing.stat().st_dev
            if device in volumes:
                continue
            usage = disk_usage_probe(existing)
            total = int(usage.total if hasattr(usage, "total") else usage[0])
            used = int(usage.used if hasattr(usage, "used") else usage[1])
            free = int(usage.free if hasattr(usage, "free") else usage[2])
            used_percent = round((used / total * 100) if total else 100.0, 3)
            volumes[device] = {
                "path": str(existing),
                "total_bytes": total,
                "free_bytes": free,
                "used_percent": used_percent,
                "ok": free >= min_free_bytes and used_percent <= max_used_percent,
            }
        except OSError as exc:
            errors.append(type(exc).__name__)
    values = list(volumes.values())
    return {
        "ok": bool(values) and not errors and all(volume["ok"] for volume in values),
        "volumes": values,
        "errors": errors,
        "thresholds": {
            "min_free_bytes": min_free_bytes,
            "max_used_percent": max_used_percent,
        },
    }


def _flatten_counts(databases: Mapping[str, Any]) -> dict[str, int]:
    flattened: dict[str, int] = {}
    for database, snapshot in databases.items():
        for table, value in snapshot.get("counts", {}).items():
            flattened[f"{database}.{table}"] = int(value)
    return flattened


class SoakSampler:
    def __init__(
        self,
        config: SoakConfig,
        *,
        health_probe: HealthProbe = probe_health,
        service_probe: ServiceProbe = probe_service,
        disk_usage_probe: DiskUsageProbe = shutil.disk_usage,
    ) -> None:
        self.config = config
        self.health_probe = health_probe
        self.service_probe = service_probe
        self.disk_usage_probe = disk_usage_probe
        self.previous_services: dict[str, dict[str, Any]] = {}
        self.previous_counts: dict[str, int] | None = None

    def sample(self, index: int) -> dict[str, Any]:
        captured_at = utc_now()
        health = self.health_probe(
            self.config.health_url, self.config.health_timeout_seconds
        )
        database_result = inspect_databases(self.config)

        services: dict[str, Any] = {}
        for name in self.config.services:
            current = self.service_probe(name, self.config.command_timeout_seconds)
            previous = self.previous_services.get(name)
            pid_changed = bool(
                previous
                and previous.get("main_pid")
                and current.get("main_pid")
                and previous.get("main_pid") != current.get("main_pid")
            )
            restart_delta = 0
            if previous:
                restart_delta = max(
                    _as_int(current.get("n_restarts"))
                    - _as_int(previous.get("n_restarts")),
                    0,
                )
            current = dict(current)
            current["pid_changed"] = pid_changed
            current["restart_delta"] = restart_delta
            current["unexpected_restart"] = pid_changed or restart_delta > 0
            services[name] = current
            self.previous_services[name] = current

        disk_paths = self.config.disk_paths or (
            self.config.ops_db.parent,
            self.config.archive_db.parent,
            self.config.effects_db.parent,
            self.config.output.parent,
        )
        disk = _disk_snapshot(
            disk_paths,
            self.config.min_free_bytes,
            self.config.max_used_percent,
            self.disk_usage_probe,
        )

        counts = _flatten_counts(database_result["databases"])
        regressions: dict[str, dict[str, int]] = {}
        if self.previous_counts is not None:
            for key, previous in self.previous_counts.items():
                current = counts.get(key)
                if current is not None and current < previous:
                    regressions[key] = {"previous": previous, "current": current}
        self.previous_counts = counts

        checks = {
            "service_health": bool(health.get("ok")),
            "database_integrity": all(
                snapshot.get("ok", False)
                for snapshot in database_result["databases"].values()
            ),
            "duplicate_keys": bool(database_result["duplicates"].get("ok")),
            "archive_audio_continuity": bool(database_result["archive_audio"].get("ok")),
            "control_audit_chain": bool(database_result["control_audit"].get("ok")),
            "effect_journal_links": bool(database_result["effect_links"].get("ok")),
            "service_processes": bool(services)
            and all(
                service.get("ok", False) and not service.get("unexpected_restart", False)
                for service in services.values()
            ),
            "disk_pressure": bool(disk.get("ok")),
            "monotonic_counts": not regressions,
        }
        return {
            "kind": "checkpoint",
            "captured_at": captured_at,
            "sample_index": index,
            "ok": all(checks.values()),
            "checks": checks,
            "health": health,
            **database_result,
            "services": services,
            "disk": disk,
            "count_regressions": regressions,
        }


def validate_schedule(
    *, duration_seconds: float, interval_seconds: float, maximum_seconds: float
) -> None:
    values = (duration_seconds, interval_seconds, maximum_seconds)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("duration and interval must be finite")
    if duration_seconds <= 0 or interval_seconds <= 0 or maximum_seconds <= 0:
        raise ValueError("duration, interval, and maximum must be positive")
    if duration_seconds > maximum_seconds:
        raise ValueError("duration exceeds the configured hard maximum")


def summarize_run(
    run_id: str,
    checkpoints: Sequence[Mapping[str, Any]],
    *,
    started_at: str,
    elapsed_seconds: float,
    outcome: str,
) -> dict[str, Any]:
    failures: Counter[str] = Counter()
    pid_change_events = 0
    restart_events = 0
    max_used_percent = 0.0
    max_hard_duplicate_groups = 0
    max_duplicate_warning_groups = 0
    max_archive_audio_issues = 0
    for checkpoint in checkpoints:
        failures.update(
            name for name, passed in checkpoint.get("checks", {}).items() if not passed
        )
        for service in checkpoint.get("services", {}).values():
            pid_change_events += int(bool(service.get("pid_changed")))
            restart_events += int(service.get("restart_delta", 0))
        for volume in checkpoint.get("disk", {}).get("volumes", []):
            max_used_percent = max(max_used_percent, float(volume.get("used_percent", 0)))
        duplicates = checkpoint.get("duplicates", {})
        max_hard_duplicate_groups = max(
            max_hard_duplicate_groups,
            int(duplicates.get("hard_failure_groups", 0)),
        )
        max_duplicate_warning_groups = max(
            max_duplicate_warning_groups,
            int(duplicates.get("warning_groups", 0)),
        )
        archive_audio = checkpoint.get("archive_audio", {})
        archive_issue_keys = (
            "audio_sequence_gap_streams",
            "capture_sequence_gap_streams",
            "missing_audio_files",
            "empty_audio_files",
            "orphan_audio_rows",
            "audio_only_without_chunk",
            "relevant_without_run",
            "archive_link_gaps",
            "malformed_metadata",
        )
        max_archive_audio_issues = max(
            max_archive_audio_issues,
            sum(int(archive_audio.get(key, 0)) for key in archive_issue_keys),
        )

    first_counts = (
        _flatten_counts(checkpoints[0].get("databases", {})) if checkpoints else {}
    )
    last_counts = (
        _flatten_counts(checkpoints[-1].get("databases", {})) if checkpoints else {}
    )
    count_deltas = {
        key: last_counts.get(key, 0) - value for key, value in first_counts.items()
    }
    all_passed = bool(checkpoints) and not failures and outcome == "completed"
    return {
        "kind": "run_summary",
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "outcome": outcome,
        "all_passed": all_passed,
        "sample_count": len(checkpoints),
        "passed_samples": sum(bool(checkpoint.get("ok")) for checkpoint in checkpoints),
        "failed_samples": sum(not bool(checkpoint.get("ok")) for checkpoint in checkpoints),
        "failure_totals": dict(sorted(failures.items())),
        "first_counts": first_counts,
        "last_counts": last_counts,
        "count_deltas": count_deltas,
        "pid_change_events": pid_change_events,
        "service_restart_events": restart_events,
        "max_disk_used_percent": round(max_used_percent, 3),
        "max_hard_duplicate_groups": max_hard_duplicate_groups,
        "max_duplicate_warning_groups": max_duplicate_warning_groups,
        "max_archive_audio_issues": max_archive_audio_issues,
    }


def run_soak(
    config: SoakConfig,
    *,
    once: bool = False,
    duration_seconds: float = 86_400,
    interval_seconds: float = 60,
    maximum_seconds: float = 604_800,
    fsync: bool = True,
    sampler: SoakSampler | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], int]:
    validate_schedule(
        duration_seconds=duration_seconds,
        interval_seconds=interval_seconds,
        maximum_seconds=maximum_seconds,
    )
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    started_monotonic = monotonic()
    deadline = started_monotonic + duration_seconds
    checkpoints: list[dict[str, Any]] = []
    outcome = "completed"
    active_sampler = sampler or SoakSampler(config)

    with JsonlLedger(config.output, fsync=fsync) as ledger:
        ledger.append(
            {
                "kind": "run_start",
                "run_id": run_id,
                "started_at": started_at,
                "mode": "once" if once else "bounded_soak",
                "duration_seconds": duration_seconds,
                "interval_seconds": interval_seconds,
                "maximum_seconds": maximum_seconds,
            }
        )
        try:
            index = 0
            while True:
                checkpoint = active_sampler.sample(index)
                checkpoint["run_id"] = run_id
                checkpoint["elapsed_seconds"] = round(
                    monotonic() - started_monotonic, 3
                )
                ledger.append(checkpoint)
                checkpoints.append(checkpoint)
                index += 1
                now_value = monotonic()
                if once or now_value >= deadline:
                    break
                sleep(min(interval_seconds, max(deadline - now_value, 0)))
        except KeyboardInterrupt:
            outcome = "interrupted"
        except Exception as exc:  # preserve a receipt even when the harness itself fails
            outcome = "harness_error"
            ledger.append(
                {
                    "kind": "harness_error",
                    "run_id": run_id,
                    "captured_at": utc_now(),
                    "error": type(exc).__name__,
                }
            )
        summary = summarize_run(
            run_id,
            checkpoints,
            started_at=started_at,
            elapsed_seconds=monotonic() - started_monotonic,
            outcome=outcome,
        )
        ledger.append(summary)

    if outcome == "interrupted":
        return summary, 130
    return summary, 0 if summary["all_passed"] else 2

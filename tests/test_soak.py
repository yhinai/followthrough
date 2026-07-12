from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from followthrough.soak import (
    GENESIS_HASH,
    JsonlLedger,
    LedgerIntegrityError,
    SoakConfig,
    SoakSampler,
    canonical_json,
    inspect_archive_audio,
    run_soak,
    validate_schedule,
)


def _capture_archive(path: Path, *, sequences: list[int], next_sequence: int) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    database.executescript(
        """
        CREATE TABLE archive_events(
          id TEXT, event_id TEXT, relevant INTEGER, classification TEXT,
          metadata_json TEXT, run_id TEXT
        );
        CREATE TABLE audio_chunks(archive_id TEXT, sequence INTEGER, path TEXT);
        CREATE TABLE capture_streams(id TEXT, next_sequence INTEGER);
        """
    )
    for index, sequence in enumerate(sequences):
        database.execute(
            "INSERT INTO archive_events VALUES(?,?,?,?,?,?)",
            (
                f"archive-{index}",
                f"event-{index}",
                0,
                "audio_only",
                canonical_json({"capture_stream_id": "stream-1", "stream_sequence": sequence}),
                None,
            ),
        )
    database.execute("INSERT INTO capture_streams VALUES(?,?)", ("stream-1", next_sequence))
    database.commit()
    return database


def test_capture_gap_ignores_inflight_allocation_ahead_of_events(tmp_path: Path) -> None:
    # next_sequence is two allocations ahead of the committed events, which is
    # the legitimate window between the two-phase ingest commits and must not be
    # reported as a continuity gap.
    archive = _capture_archive(tmp_path / "a.db", sequences=[0, 1], next_sequence=3)
    result = inspect_archive_audio(archive, None)
    assert result["capture_sequence_gap_streams"] == 0


def test_capture_gap_still_detects_interior_hole(tmp_path: Path) -> None:
    archive = _capture_archive(tmp_path / "b.db", sequences=[0, 2], next_sequence=3)
    result = inspect_archive_audio(archive, None)
    assert result["capture_sequence_gap_streams"] == 1


def create_operations(path: Path, *, duplicate_jobs: bool = False, tamper: bool = False) -> None:
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE runs(id TEXT, archive_event_id TEXT);
        CREATE TABLE steps(id TEXT);
        CREATE TABLE relevance_decisions(id TEXT, archive_id TEXT);
        CREATE TABLE operational_memories(id TEXT, archive_id TEXT);
        CREATE TABLE hermes_jobs(
          id TEXT, idempotency_key TEXT, event_id TEXT, run_id TEXT,
          archive_id TEXT, task_id TEXT
        );
        CREATE TABLE control_audit(
          sequence INTEGER PRIMARY KEY, receipt_id TEXT, kind TEXT, actor TEXT,
          capability TEXT, reason_code TEXT, details_json TEXT,
          previous_hash TEXT, receipt_hash TEXT, created_at TEXT
        );
        """
    )
    database.execute("INSERT INTO runs VALUES('run-1','archive-1')")
    database.execute("INSERT INTO relevance_decisions VALUES('decision-1','archive-1')")
    database.execute("INSERT INTO operational_memories VALUES('memory-1','archive-1')")
    database.execute(
        "INSERT INTO hermes_jobs VALUES(?,?,?,?,?,?)",
        ("job-1", "job-key", "event-1", "run-1", "archive-1", "task-1"),
    )
    if duplicate_jobs:
        database.execute(
            "INSERT INTO hermes_jobs VALUES(?,?,?,?,?,?)",
            ("job-2", "job-key", "event-1", "run-1", "archive-1", "task-1"),
        )
    payload = {
        "receipt_id": "receipt-1",
        "kind": "global_mode",
        "actor": "operator",
        "capability": None,
        "reason_code": "test",
        "details": {"mode": "running"},
        "created_at": "2026-07-11T00:00:00+00:00",
    }
    receipt_hash = hashlib.sha256((GENESIS_HASH + canonical_json(payload)).encode()).hexdigest()
    if tamper:
        receipt_hash = "f" * 64
    database.execute(
        "INSERT INTO control_audit VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            payload["receipt_id"],
            payload["kind"],
            payload["actor"],
            payload["capability"],
            payload["reason_code"],
            canonical_json(payload["details"]),
            GENESIS_HASH,
            receipt_hash,
            payload["created_at"],
        ),
    )
    database.commit()
    database.close()


def create_archive(path: Path, audio_path: Path, *, gaps: bool = False) -> None:
    path.parent.mkdir(parents=True)
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE archive_events(
          id TEXT, event_id TEXT, relevant INTEGER, classification TEXT,
          metadata_json TEXT, run_id TEXT
        );
        CREATE TABLE audio_chunks(archive_id TEXT, sequence INTEGER, path TEXT);
        CREATE TABLE capture_streams(id TEXT, next_sequence INTEGER);
        """
    )
    metadata = {
        "capture_stream_id": "stream-1",
        "stream_sequence": 0,
    }
    database.execute(
        "INSERT INTO archive_events VALUES(?,?,?,?,?,?)",
        ("archive-1", "event-1", 1, "repository", canonical_json(metadata), "run-1"),
    )
    audio_path.write_bytes(b"stored-audio")
    database.execute("INSERT INTO audio_chunks VALUES(?,?,?)", ("archive-1", 0, str(audio_path)))
    if gaps:
        database.execute("INSERT INTO audio_chunks VALUES(?,?,?)", ("archive-1", 2, str(audio_path)))
        database.execute(
            "INSERT INTO archive_events VALUES(?,?,?,?,?,?)",
            (
                "archive-2",
                "event-2",
                0,
                "audio_only",
                canonical_json({"capture_stream_id": "stream-1", "stream_sequence": 2}),
                None,
            ),
        )
    database.execute(
        "INSERT INTO capture_streams VALUES(?,?)",
        ("stream-1", 3 if gaps else 1),
    )
    database.commit()
    database.close()


def create_effects(path: Path, *, duplicate: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE effects(
          id TEXT, idempotency_key TEXT, trigger_event_id TEXT, kind TEXT,
          request_fingerprint TEXT, provider TEXT, external_id TEXT
        );
        CREATE TABLE effect_transitions(effect_id TEXT);
        """
    )
    database.execute(
        "INSERT INTO effects VALUES(?,?,?,?,?,?,?)",
        ("effect-1", "effect-key", "event-1", "private_task.create", "fp-1", "local", "ext-1"),
    )
    database.execute("INSERT INTO effect_transitions VALUES('effect-1')")
    if duplicate:
        database.execute(
            "INSERT INTO effects VALUES(?,?,?,?,?,?,?)",
            ("effect-2", "effect-key", "event-1", "private_task.create", "fp-1", "local", "ext-1"),
        )
        database.execute("INSERT INTO effect_transitions VALUES('effect-2')")
    database.commit()
    database.close()


def fixture_config(tmp_path: Path, **anomalies: bool) -> SoakConfig:
    ops = tmp_path / "operations.db"
    archive = tmp_path / "archive" / "archive.db"
    effects = tmp_path / "effects" / "effects.db"
    create_operations(
        ops,
        duplicate_jobs=anomalies.get("duplicate_jobs", False),
        tamper=anomalies.get("tamper", False),
    )
    create_archive(
        archive,
        tmp_path / "audio.enc",
        gaps=anomalies.get("gaps", False),
    )
    create_effects(effects, duplicate=anomalies.get("duplicate_effects", False))
    return SoakConfig(
        ops_db=ops,
        archive_db=archive,
        effects_db=effects,
        output=tmp_path / "soak" / "checkpoints.jsonl",
        services=("test.service",),
        min_free_bytes=1,
        max_used_percent=99,
        disk_paths=(tmp_path,),
    )


def healthy(_url: str, _timeout: float) -> dict[str, Any]:
    return {"ok": True, "status_code": 200, "latency_ms": 1, "payload": {"ok": True}}


def active_service(_name: str, _timeout: float) -> dict[str, Any]:
    return {
        "ok": True,
        "name": "test.service",
        "active_state": "active",
        "sub_state": "running",
        "main_pid": 42,
        "n_restarts": 0,
    }


def roomy_disk(_path: Path) -> SimpleNamespace:
    return SimpleNamespace(total=1_000_000, used=100_000, free=900_000)


def test_once_happy_path_writes_chained_start_checkpoint_and_summary(tmp_path: Path) -> None:
    config = fixture_config(tmp_path)
    sampler = SoakSampler(
        config,
        health_probe=healthy,
        service_probe=active_service,
        disk_usage_probe=roomy_disk,
    )
    summary, exit_code = run_soak(config, once=True, fsync=False, sampler=sampler)

    assert exit_code == 0
    assert summary["all_passed"] is True
    assert summary["sample_count"] == 1
    records = [json.loads(line) for line in config.output.read_text().splitlines()]
    assert [record["kind"] for record in records] == [
        "run_start",
        "checkpoint",
        "run_summary",
    ]
    assert records[0]["previous_record_hash"] == GENESIS_HASH
    assert records[1]["previous_record_hash"] == records[0]["record_hash"]
    assert records[2]["previous_record_hash"] == records[1]["record_hash"]
    assert config.output.stat().st_mode & 0o777 == 0o600


def test_sampler_fails_closed_on_duplicates_gaps_and_tampered_audit(tmp_path: Path) -> None:
    config = fixture_config(
        tmp_path,
        duplicate_jobs=True,
        duplicate_effects=True,
        gaps=True,
        tamper=True,
    )
    sampler = SoakSampler(
        config,
        health_probe=healthy,
        service_probe=active_service,
        disk_usage_probe=roomy_disk,
    )

    checkpoint = sampler.sample(0)

    assert checkpoint["ok"] is False
    assert checkpoint["duplicates"]["total_groups"] >= 3
    assert checkpoint["archive_audio"]["audio_sequence_gap_streams"] == 1
    assert checkpoint["archive_audio"]["capture_sequence_gap_streams"] == 1
    assert checkpoint["control_audit"]["ok"] is False
    assert checkpoint["checks"]["duplicate_keys"] is False


def test_second_sample_detects_pid_change_and_count_regression(tmp_path: Path) -> None:
    config = fixture_config(tmp_path)
    pids = iter((42, 99))

    def changing_service(_name: str, _timeout: float) -> dict[str, Any]:
        return {
            "ok": True,
            "name": "test.service",
            "active_state": "active",
            "sub_state": "running",
            "main_pid": next(pids),
            "n_restarts": 0,
        }

    sampler = SoakSampler(
        config,
        health_probe=healthy,
        service_probe=changing_service,
        disk_usage_probe=roomy_disk,
    )
    assert sampler.sample(0)["ok"] is True
    database = sqlite3.connect(config.ops_db)
    database.execute("DELETE FROM runs")
    database.commit()
    database.close()

    second = sampler.sample(1)

    assert second["services"]["test.service"]["unexpected_restart"] is True
    assert second["checks"]["service_processes"] is False
    assert second["count_regressions"]["operations.runs"] == {
        "previous": 1,
        "current": 0,
    }


def test_ledger_refuses_tampered_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    with JsonlLedger(path, fsync=False) as ledger:
        ledger.append({"kind": "test", "value": 1})
    record = json.loads(path.read_text())
    record["value"] = 2
    path.write_text(canonical_json(record) + "\n")

    with pytest.raises(LedgerIntegrityError):
        JsonlLedger(path, fsync=False)


@pytest.mark.parametrize(
    ("duration", "interval", "maximum"),
    ((0, 1, 2), (3, 1, 2), (1, float("inf"), 2), (1, -1, 2)),
)
def test_schedule_has_a_hard_finite_bound(
    duration: float, interval: float, maximum: float
) -> None:
    with pytest.raises(ValueError):
        validate_schedule(
            duration_seconds=duration,
            interval_seconds=interval,
            maximum_seconds=maximum,
        )

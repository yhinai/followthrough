from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from followthrough.backup import (
    MANIFEST_NAME,
    BackupError,
    BackupSources,
    BackupVerificationError,
    RestoreTargetError,
    create_backup,
    restore_backup,
    verify_backup,
)


RAW_TRANSCRIPT = b"raw transcript that must never enter the backup"
SECRET_TOKEN = b"secret-token-that-must-never-enter-the-backup"


def _database(path: Path, *, value: bytes, wal: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if wal:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
    connection.execute("INSERT INTO ledger(payload) VALUES(?)", (value,))
    connection.commit()
    return connection


def _sources(tmp_path: Path) -> tuple[BackupSources, list[sqlite3.Connection]]:
    operations = tmp_path / "live" / "followthrough.db"
    archive = tmp_path / "live" / "archive" / "archive.db"
    effects = tmp_path / "live" / "effects" / "effects.db"
    connections = [
        _database(operations, value=b"committed operation", wal=True),
        _database(archive, value=b"\x00archived\xff", wal=True),
        _database(effects, value=b"typed effect receipt", wal=True),
    ]
    audio = tmp_path / "live" / "archive" / "audio"
    (audio / "event-1").mkdir(parents=True)
    (audio / "event-1" / "00000000.audio").write_bytes(b"audio-bytes")
    receipts = tmp_path / "live" / "runner" / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "run-1.json").write_bytes(b'{"result":"sandboxed","exit_code":0}')
    return (
        BackupSources(
            operations_db=operations,
            archive_db=archive,
            effects_db=effects,
            audio_dir=audio,
            runner_receipts_dir=receipts,
        ),
        connections,
    )


def _close(connections: list[sqlite3.Connection]) -> None:
    for connection in connections:
        connection.close()


def test_create_is_consistent_private_and_allowlisted(tmp_path: Path) -> None:
    sources, connections = _sources(tmp_path)
    secret_file = tmp_path / "live" / "device.token"
    secret_file.write_bytes(SECRET_TOKEN)
    destination = tmp_path / "backups" / "backup-1"
    try:
        assert Path(f"{sources.operations_db}-wal").is_file()
        result = create_backup(sources, destination)
    finally:
        _close(connections)

    assert result.databases == 3
    assert result.files == 5
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for item in destination.rglob("*"):
        expected = 0o700 if item.is_dir() else 0o600
        assert stat.S_IMODE(item.stat().st_mode) == expected

    operations_copy = sqlite3.connect(destination / "databases" / "operations.db")
    try:
        assert operations_copy.execute("SELECT payload FROM ledger").fetchone()[0] == b"committed operation"
    finally:
        operations_copy.close()
    assert (
        destination / "files" / "audio" / "event-1" / "00000000.audio"
    ).read_bytes() == b"audio-bytes"
    assert (
        destination / "files" / "runner-receipts" / "run-1.json"
    ).read_bytes() == b'{"result":"sandboxed","exit_code":0}'

    all_bytes = b"".join(path.read_bytes() for path in destination.rglob("*") if path.is_file())
    assert SECRET_TOKEN not in all_bytes
    assert RAW_TRANSCRIPT not in all_bytes
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    assert manifest["content_policy"] == {
        "archive": "complete_archive",
        "audio": "opaque_copy",
        "runner_receipts": "opaque_copy",
        "secrets_included": False,
    }
    assert not any(str(secret_file) in json.dumps(entry) for entry in manifest["entries"])


def test_verify_rejects_content_corruption_and_unsafe_mode(tmp_path: Path) -> None:
    sources, connections = _sources(tmp_path)
    destination = tmp_path / "backup"
    try:
        create_backup(sources, destination)
    finally:
        _close(connections)

    receipt = destination / "files" / "runner-receipts" / "run-1.json"
    receipt.write_bytes(b"tampered")
    with pytest.raises(BackupVerificationError, match="(size|hash) mismatch"):
        verify_backup(destination)

    receipt.write_bytes(b'{"result":"sandboxed","exit_code":0}')
    receipt.chmod(0o644)
    with pytest.raises(BackupVerificationError, match="mode mismatch"):
        verify_backup(destination)


def test_verify_rejects_unmanifested_files(tmp_path: Path) -> None:
    sources, connections = _sources(tmp_path)
    destination = tmp_path / "backup"
    try:
        create_backup(sources, destination)
    finally:
        _close(connections)
    extra = destination / "unexpected.txt"
    extra.write_text("not in manifest")
    extra.chmod(0o600)
    with pytest.raises(BackupVerificationError, match="structure mismatch"):
        verify_backup(destination)


def test_source_symlink_aborts_without_partial_destination(tmp_path: Path) -> None:
    sources, connections = _sources(tmp_path)
    external = tmp_path / "outside.audio"
    external.write_bytes(b"outside")
    (sources.audio_dir / "bad.audio").symlink_to(external)
    destination = tmp_path / "backups" / "backup"
    try:
        with pytest.raises(BackupError, match="symlinks are not allowed"):
            create_backup(sources, destination)
    finally:
        _close(connections)
    assert not destination.exists()
    assert not list(destination.parent.glob(".backup.tmp-*"))


def test_destination_cannot_be_inside_copied_tree(tmp_path: Path) -> None:
    sources, connections = _sources(tmp_path)
    try:
        with pytest.raises(BackupError, match="cannot be inside"):
            create_backup(sources, sources.audio_dir / "backup")
    finally:
        _close(connections)


def test_restore_requires_existing_empty_explicit_target(tmp_path: Path) -> None:
    sources, connections = _sources(tmp_path)
    destination = tmp_path / "backup"
    try:
        create_backup(sources, destination)
    finally:
        _close(connections)

    missing = tmp_path / "missing-target"
    with pytest.raises(RestoreTargetError, match="already exist"):
        restore_backup(destination, missing)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    marker = occupied / "must-survive"
    marker.write_text("untouched")
    with pytest.raises(RestoreTargetError, match="must be empty"):
        restore_backup(destination, occupied)
    assert marker.read_text() == "untouched"


def test_restore_round_trip_reverifies_and_does_not_modify_backup(tmp_path: Path) -> None:
    sources, connections = _sources(tmp_path)
    destination = tmp_path / "backup"
    try:
        original = create_backup(sources, destination)
    finally:
        _close(connections)
    before = (destination / MANIFEST_NAME).read_bytes()
    target = tmp_path / "empty-restore-target"
    target.mkdir(mode=0o700)

    restored = restore_backup(destination, target)

    assert restored == original
    assert (destination / MANIFEST_NAME).read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert verify_backup(target) == original
    connection = sqlite3.connect(target / "databases" / "archive.db")
    try:
        assert connection.execute("SELECT payload FROM ledger").fetchone()[0] == b"\x00archived\xff"
    finally:
        connection.close()


def test_create_refuses_existing_destination_without_touching_it(tmp_path: Path) -> None:
    sources, connections = _sources(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "marker"
    marker.write_text("preserve")
    try:
        with pytest.raises(BackupError, match="already exists"):
            create_backup(sources, destination)
    finally:
        _close(connections)
    assert marker.read_text() == "preserve"

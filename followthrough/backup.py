from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
MANIFEST_DIGEST_NAME = "manifest.sha256"


class BackupError(RuntimeError):
    """Base class for backup safety and integrity failures."""


class BackupVerificationError(BackupError):
    """The backup is incomplete, corrupt, or has unsafe permissions."""


class RestoreTargetError(BackupError):
    """The restore destination is not an explicit empty directory."""


@dataclass(frozen=True)
class BackupSources:
    operations_db: Path
    archive_db: Path
    effects_db: Path
    audio_dir: Path
    runner_receipts_dir: Path


@dataclass(frozen=True)
class VerificationResult:
    backup_id: str
    files: int
    directories: int
    bytes: int
    databases: int

    def as_dict(self) -> dict[str, int | str | bool]:
        return {
            "ok": True,
            "backup_id": self.backup_id,
            "files": self.files,
            "directories": self.directories,
            "bytes": self.bytes,
            "databases": self.databases,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(raw: str) -> Path:
    logical = PurePosixPath(raw)
    if (
        not raw
        or not logical.parts
        or logical.is_absolute()
        or ".." in logical.parts
        or "." in logical.parts
    ):
        raise BackupVerificationError(f"unsafe manifest path: {raw!r}")
    return Path(*logical.parts)


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError(f"{label} does not exist") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise BackupError(f"{label} must be a regular file and not a symlink")


def _require_directory(path: Path, *, label: str) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError(f"{label} does not exist") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise BackupError(f"{label} must be a directory and not a symlink")


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _copy_private_opaque(source: Path, destination: Path) -> None:
    """Copy bytes without parsing them and without following a leaf symlink."""

    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    destination_fd: int | None = None
    try:
        source_details = os.fstat(source_fd)
        if not stat.S_ISREG(source_details.st_mode):
            raise BackupError(f"opaque source is not a regular file: {source.name}")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with (
            os.fdopen(source_fd, "rb", closefd=False) as reader,
            os.fdopen(destination_fd, "wb", closefd=False) as writer,
        ):
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    destination.chmod(0o600)


def _sqlite_integrity(path: Path) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"invalid SQLite snapshot: {path.name}") from exc
    if not result or result[0] != "ok":
        raise BackupVerificationError(f"SQLite integrity check failed for {path.name}")


def _backup_sqlite(source: Path, destination: Path) -> None:
    _require_regular_file(source, label="SQLite source")
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source_uri, uri=True)
        destination_connection = sqlite3.connect(destination)
        destination.chmod(0o600)
        source_connection.backup(destination_connection)
        destination_connection.commit()
        destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        destination_connection.commit()
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise BackupVerificationError(
                f"SQLite snapshot integrity check failed for {source.name}"
            )
    except sqlite3.Error as exc:
        raise BackupError(f"failed to snapshot SQLite database: {source.name}") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    destination.chmod(0o600)
    _fsync_file(destination)


def _directory_entry(path: str, *, category: str) -> dict[str, Any]:
    return {"path": path, "kind": "directory", "mode": "0700", "category": category}


def _file_entry(path: Path, root: Path, *, category: str) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "kind": "file",
        "mode": "0600",
        "category": category,
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _make_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def _copy_tree(
    source_root: Path,
    destination_root: Path,
    backup_root: Path,
    *,
    category: str,
) -> list[dict[str, Any]]:
    _require_directory(source_root, label=category)
    entries: list[dict[str, Any]] = []
    for source in sorted(source_root.rglob("*")):
        relative = source.relative_to(source_root)
        destination = destination_root / relative
        details = source.lstat()
        if stat.S_ISLNK(details.st_mode):
            raise BackupError(f"symlinks are not allowed in {category}: {relative.as_posix()}")
        if stat.S_ISDIR(details.st_mode):
            _make_directory(destination)
            entries.append(
                _directory_entry(destination.relative_to(backup_root).as_posix(), category=category)
            )
            continue
        if not stat.S_ISREG(details.st_mode):
            raise BackupError(
                f"special files are not allowed in {category}: {relative.as_posix()}"
            )
        _copy_private_opaque(source, destination)
        entries.append(_file_entry(destination, backup_root, category=category))
    return entries


def _reject_recursive_destination(destination: Path, sources: Iterable[Path]) -> None:
    candidate = destination.resolve(strict=False)
    for source in sources:
        resolved = source.resolve()
        if candidate == resolved or candidate.is_relative_to(resolved):
            raise BackupError("backup destination cannot be inside an opaque source tree")


def create_backup(sources: BackupSources, destination: Path) -> VerificationResult:
    """Create an atomic, owner-only backup without decoding any source artifact."""

    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError("backup destination already exists")
    _require_regular_file(sources.operations_db, label="operations database")
    _require_regular_file(sources.archive_db, label="archive database")
    _require_regular_file(sources.effects_db, label="effects database")
    _require_directory(sources.audio_dir, label="audio directory")
    _require_directory(sources.runner_receipts_dir, label="runner receipts directory")
    _reject_recursive_destination(
        destination, (sources.audio_dir, sources.runner_receipts_dir)
    )

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    backup_id = str(uuid.uuid4())
    try:
        _make_directory(staging)
        databases_dir = staging / "databases"
        files_dir = staging / "files"
        audio_dir = files_dir / "audio"
        receipts_dir = files_dir / "runner-receipts"
        for directory in (databases_dir, files_dir, audio_dir, receipts_dir):
            _make_directory(directory)

        entries: list[dict[str, Any]] = [
            _directory_entry("databases", category="layout"),
            _directory_entry("files", category="layout"),
            _directory_entry("files/audio", category="audio"),
            _directory_entry("files/runner-receipts", category="runner_receipts"),
        ]
        database_sources = (
            ("operations", sources.operations_db),
            ("archive", sources.archive_db),
            ("effects", sources.effects_db),
        )
        for name, source in database_sources:
            target = databases_dir / f"{name}.db"
            _backup_sqlite(source, target)
            entries.append(_file_entry(target, staging, category=f"database:{name}"))

        entries.extend(
            _copy_tree(
                sources.audio_dir,
                audio_dir,
                staging,
                category="audio",
            )
        )
        entries.extend(
            _copy_tree(
                sources.runner_receipts_dir,
                receipts_dir,
                staging,
                category="runner_receipts",
            )
        )
        manifest = {
            "format": "followthrough-backup",
            "format_version": FORMAT_VERSION,
            "backup_id": backup_id,
            "created_at": _utc_now(),
            "content_policy": {
                "archive": "complete_archive",
                "audio": "opaque_copy",
                "runner_receipts": "opaque_copy",
                "secrets_included": False,
            },
            "entries": sorted(entries, key=lambda item: item["path"]),
        }
        manifest_bytes = (
            json.dumps(manifest, indent=2, sort_keys=True, separators=(",", ": ")).encode()
            + b"\n"
        )
        _write_private(staging / MANIFEST_NAME, manifest_bytes)
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest().encode() + b"\n"
        _write_private(staging / MANIFEST_DIGEST_NAME, manifest_digest)
        for directory in sorted(
            (item for item in staging.rglob("*") if item.is_dir()), reverse=True
        ):
            _fsync_directory(directory)
        _fsync_directory(staging)
        os.replace(staging, destination)
        _fsync_directory(destination.parent)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return verify_backup(destination)


def _load_manifest(backup: Path) -> dict[str, Any]:
    manifest_path = backup / MANIFEST_NAME
    digest_path = backup / MANIFEST_DIGEST_NAME
    _require_regular_file(manifest_path, label="backup manifest")
    _require_regular_file(digest_path, label="backup manifest digest")
    manifest_bytes = manifest_path.read_bytes()
    expected = digest_path.read_text().strip()
    actual = hashlib.sha256(manifest_bytes).hexdigest()
    if expected != actual:
        raise BackupVerificationError("manifest digest mismatch")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupVerificationError("manifest is not valid JSON") from exc
    if manifest.get("format") != "followthrough-backup":
        raise BackupVerificationError("unsupported backup format")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BackupVerificationError("unsupported backup format version")
    if not isinstance(manifest.get("backup_id"), str) or not manifest["backup_id"]:
        raise BackupVerificationError("manifest backup id is missing")
    expected_policy = {
        "archive": "complete_archive",
        "audio": "opaque_copy",
        "runner_receipts": "opaque_copy",
        "secrets_included": False,
    }
    if manifest.get("content_policy") != expected_policy:
        raise BackupVerificationError("backup content policy is missing or unsupported")
    if not isinstance(manifest.get("entries"), list):
        raise BackupVerificationError("manifest entries are missing")
    return manifest


def verify_backup(backup: Path) -> VerificationResult:
    """Verify layout, permissions, hashes, and all SQLite snapshots."""

    backup = backup.expanduser().absolute()
    _require_directory(backup, label="backup")
    if stat.S_IMODE(backup.stat().st_mode) != 0o700:
        raise BackupVerificationError("backup root must be mode 0700")
    manifest = _load_manifest(backup)
    for control_file in (backup / MANIFEST_NAME, backup / MANIFEST_DIGEST_NAME):
        if stat.S_IMODE(control_file.stat().st_mode) != 0o600:
            raise BackupVerificationError(f"{control_file.name} must be mode 0600")

    expected_paths = {MANIFEST_NAME, MANIFEST_DIGEST_NAME}
    seen_paths: set[str] = set()
    files = 0
    directories = 0
    byte_count = 0
    databases = 0
    structure: set[tuple[str, str, str]] = set()
    for raw_entry in manifest["entries"]:
        if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("path"), str):
            raise BackupVerificationError("malformed manifest entry")
        relative = _safe_relative_path(raw_entry["path"])
        logical_path = relative.as_posix()
        if logical_path in seen_paths:
            raise BackupVerificationError(f"duplicate manifest entry: {logical_path}")
        seen_paths.add(logical_path)
        expected_paths.add(logical_path)
        path = backup / relative
        kind = raw_entry.get("kind")
        category = raw_entry.get("category")
        if not isinstance(category, str) or not category:
            raise BackupVerificationError(f"missing category for {logical_path}")
        structure.add((logical_path, str(kind), category))
        expected_mode = raw_entry.get("mode")
        if expected_mode not in {"0600", "0700"}:
            raise BackupVerificationError(f"unsafe manifest mode for {logical_path}")
        try:
            details = path.lstat()
        except FileNotFoundError as exc:
            raise BackupVerificationError(f"missing backup entry: {logical_path}") from exc
        if stat.S_ISLNK(details.st_mode):
            raise BackupVerificationError(f"symlink found in backup: {logical_path}")
        actual_mode = f"{stat.S_IMODE(details.st_mode):04o}"
        if actual_mode != expected_mode:
            raise BackupVerificationError(f"mode mismatch for {logical_path}")
        if kind == "directory":
            if not stat.S_ISDIR(details.st_mode) or expected_mode != "0700":
                raise BackupVerificationError(f"invalid directory entry: {logical_path}")
            directories += 1
            continue
        if kind != "file" or not stat.S_ISREG(details.st_mode) or expected_mode != "0600":
            raise BackupVerificationError(f"invalid file entry: {logical_path}")
        size = path.stat().st_size
        if raw_entry.get("bytes") != size:
            raise BackupVerificationError(f"size mismatch for {logical_path}")
        if raw_entry.get("sha256") != _sha256_file(path):
            raise BackupVerificationError(f"hash mismatch for {logical_path}")
        files += 1
        byte_count += size
        if category.startswith("database:"):
            _sqlite_integrity(path)
            databases += 1

    required_structure = {
        ("databases", "directory", "layout"),
        ("files", "directory", "layout"),
        ("files/audio", "directory", "audio"),
        ("files/runner-receipts", "directory", "runner_receipts"),
        ("databases/operations.db", "file", "database:operations"),
        ("databases/archive.db", "file", "database:archive"),
        ("databases/effects.db", "file", "database:effects"),
    }
    if not required_structure.issubset(structure):
        raise BackupVerificationError("backup is missing a required recovery artifact")
    database_entries = {entry for entry in structure if entry[2].startswith("database:")}
    if database_entries != {entry for entry in required_structure if entry[2].startswith("database:")}:
        raise BackupVerificationError("backup contains an unexpected database artifact")

    actual_paths: set[str] = set()
    for item in backup.rglob("*"):
        relative = item.relative_to(backup).as_posix()
        if item.is_symlink():
            raise BackupVerificationError(f"symlink found in backup: {relative}")
        if item.is_file() or item.is_dir():
            actual_paths.add(relative)
        else:
            raise BackupVerificationError(f"special file found in backup: {relative}")
    if actual_paths != expected_paths:
        unexpected = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        raise BackupVerificationError(
            f"backup structure mismatch (unexpected={unexpected}, missing={missing})"
        )
    if databases != 3:
        raise BackupVerificationError("backup must contain exactly three SQLite snapshots")
    return VerificationResult(
        backup_id=manifest["backup_id"],
        files=files,
        directories=directories,
        bytes=byte_count,
        databases=databases,
    )


def restore_backup(backup: Path, target: Path) -> VerificationResult:
    """Restore into an explicitly supplied, existing, empty directory only."""

    backup = backup.expanduser().absolute()
    target = target.expanduser().absolute()
    result = verify_backup(backup)
    try:
        target_details = target.lstat()
    except FileNotFoundError as exc:
        raise RestoreTargetError("restore target must already exist and be empty") from exc
    if stat.S_ISLNK(target_details.st_mode) or not stat.S_ISDIR(target_details.st_mode):
        raise RestoreTargetError("restore target must be a real directory, not a symlink")
    if any(target.iterdir()):
        raise RestoreTargetError("restore target must be empty; overwrite is never allowed")
    if target == backup or target.is_relative_to(backup) or backup.is_relative_to(target):
        raise RestoreTargetError("restore target must be separate from the backup")

    staging = target.parent / f".{target.name}.restore-{uuid.uuid4().hex}"
    try:
        _make_directory(staging)
        for source in sorted(backup.rglob("*")):
            relative = source.relative_to(backup)
            destination = staging / relative
            details = source.lstat()
            if stat.S_ISDIR(details.st_mode):
                _make_directory(destination)
            elif stat.S_ISREG(details.st_mode):
                _copy_private_opaque(source, destination)
            else:
                raise BackupVerificationError(
                    f"unsafe artifact appeared during restore: {relative.as_posix()}"
                )
        restored = verify_backup(staging)
        os.replace(staging, target)
        _fsync_directory(target.parent)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    if restored.backup_id != result.backup_id:
        raise BackupVerificationError("restored backup identity mismatch")
    return restored

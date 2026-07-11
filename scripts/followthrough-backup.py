#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from followthrough.backup import (
    BackupError,
    BackupSources,
    create_backup,
    restore_backup,
    verify_backup,
)


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Create, verify, or safely stage a Followthrough ciphertext backup."
    )
    commands = result.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create a new atomic backup directory")
    create.add_argument("--destination", type=Path, required=True)
    create.add_argument(
        "--operations-db", type=Path, default=ROOT / "data" / "followthrough.db"
    )
    create.add_argument(
        "--archive-db", type=Path, default=ROOT / "data" / "archive" / "archive.db"
    )
    create.add_argument(
        "--effects-db", type=Path, default=ROOT / "data" / "effects" / "effects.db"
    )
    create.add_argument(
        "--encrypted-audio-dir",
        type=Path,
        default=ROOT / "data" / "archive" / "audio",
    )
    create.add_argument(
        "--runner-receipts-dir",
        type=Path,
        default=ROOT / "data" / "runner" / "receipts",
    )

    verify = commands.add_parser("verify", help="Verify hashes, modes, layout, and SQLite")
    verify.add_argument("backup", type=Path)

    restore = commands.add_parser(
        "restore",
        help="Restore to an existing empty target; live overwrite is never permitted",
    )
    restore.add_argument("backup", type=Path)
    restore.add_argument("--target", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "create":
            sources = BackupSources(
                operations_db=args.operations_db.expanduser(),
                archive_db=args.archive_db.expanduser(),
                effects_db=args.effects_db.expanduser(),
                encrypted_audio_dir=args.encrypted_audio_dir.expanduser(),
                runner_receipts_dir=args.runner_receipts_dir.expanduser(),
            )
            output = create_backup(sources, args.destination)
        elif args.command == "verify":
            output = verify_backup(args.backup)
        elif args.command == "restore":
            output = restore_backup(args.backup, args.target)
        else:
            raise BackupError("unsupported command")
    except (BackupError, OSError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(output.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

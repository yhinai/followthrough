#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from followthrough.soak import DEFAULT_SERVICES, SoakConfig, run_soak  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Read-only Phase 7 acceptance monitor. It samples health, SQLite ledgers, "
            "user-service processes, and disk pressure; it never restarts a service."
        )
    )
    result.add_argument("--once", action="store_true", help="take one immediate checkpoint")
    result.add_argument("--duration-seconds", type=float, default=86_400)
    result.add_argument("--interval-seconds", type=float, default=60)
    result.add_argument(
        "--max-duration-seconds",
        type=float,
        default=604_800,
        help="hard safety bound; duration may not exceed this value",
    )
    result.add_argument("--health-url", default="http://127.0.0.1:18765/healthz")
    result.add_argument("--ops-db", type=Path, default=ROOT / "data" / "followthrough.db")
    result.add_argument(
        "--archive-db", type=Path, default=ROOT / "data" / "archive" / "archive.db"
    )
    result.add_argument(
        "--effects-db", type=Path, default=ROOT / "data" / "effects" / "effects.db"
    )
    default_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    result.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "soak" / f"soak-{default_stamp}.jsonl",
    )
    result.add_argument(
        "--service",
        action="append",
        dest="services",
        help="user service to monitor; repeat to replace the three defaults",
    )
    result.add_argument("--health-timeout-seconds", type=float, default=5)
    result.add_argument("--command-timeout-seconds", type=float, default=5)
    result.add_argument("--min-free-bytes", type=int, default=1_073_741_824)
    result.add_argument("--max-used-percent", type=float, default=95)
    result.add_argument(
        "--no-fsync",
        action="store_true",
        help="skip per-record fsync (faster, weaker crash durability)",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        config = SoakConfig(
            ops_db=args.ops_db,
            archive_db=args.archive_db,
            effects_db=args.effects_db,
            output=args.output,
            health_url=args.health_url,
            services=tuple(args.services or DEFAULT_SERVICES),
            health_timeout_seconds=args.health_timeout_seconds,
            command_timeout_seconds=args.command_timeout_seconds,
            min_free_bytes=args.min_free_bytes,
            max_used_percent=args.max_used_percent,
        )
        summary, exit_code = run_soak(
            config,
            once=args.once,
            duration_seconds=args.duration_seconds,
            interval_seconds=args.interval_seconds,
            maximum_seconds=args.max_duration_seconds,
            fsync=not args.no_fsync,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}))
        return 2
    print(json.dumps(summary, sort_keys=True))
    print(f"checkpoint_file={config.output.expanduser().resolve(strict=False)}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

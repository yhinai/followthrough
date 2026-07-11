#!/usr/bin/env python3
"""Deny-by-default CLI for Followthrough's native repository sandbox."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from followthrough.runner import NativeRepositoryRunner, RunnerError, hash_receipt


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--workspace-root", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "runner")
    sub = value.add_subparsers(dest="action", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--source", required=True)
    run = sub.add_parser("run")
    run.add_argument("--source", required=True)
    run.add_argument("--command-json", required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    runner = NativeRepositoryRunner(args.workspace_root)
    try:
        if args.action == "inspect":
            snapshot = runner.acquire(args.source)
            try:
                report = runner.inspect(snapshot)
                print(json.dumps({"provenance": asdict(snapshot.provenance), "inspection": asdict(report), "blocking": report.blocking}, default=str, sort_keys=True))
                return 3 if report.blocking else 0
            finally:
                runner.cleanup(snapshot)
        command = json.loads(args.command_json)
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("command-json must be a non-empty JSON array of strings")
        receipt = runner.run(args.source, command, allow_suspicious=False, allow_install_hooks=False, network=False)
        print(json.dumps({"receipt": receipt.to_dict(), "receipt_sha256": hash_receipt(receipt)}, default=str, sort_keys=True))
        return 0 if receipt.execution.exit_code == 0 and not receipt.execution.timed_out else 4
    except (RunnerError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:500]}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

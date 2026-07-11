#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from followthrough.effectors.errors import EffectorError
from followthrough.effectors.journal import EffectJournal
from followthrough.effectors.models import DriverReceipt, parse_request
from followthrough.effectors.policy import AutonomyPolicy
from followthrough.effectors.service import EffectService, default_drivers


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Plan and execute typed Followthrough side effects with durable receipts."
    )
    result.add_argument("--db", type=Path, default=ROOT / "data" / "effects" / "effects.db")
    result.add_argument("--policy", type=Path)
    commands = result.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="Register an action; never executes unless explicitly requested")
    plan.add_argument("--request", type=Path, required=True)
    plan.add_argument("--idempotency-key", required=True)
    plan.add_argument("--execute-if-auto", action="store_true")

    approve = commands.add_parser("approve")
    approve.add_argument("effect_id")
    approve.add_argument("--principal", default="local-owner")

    execute = commands.add_parser("execute")
    execute.add_argument("effect_id")

    show = commands.add_parser("show")
    show.add_argument("effect_id")

    history = commands.add_parser("history")
    history.add_argument("effect_id")

    recover = commands.add_parser("recover-inflight")
    recover.add_argument("--principal", default="local-operator-restart")

    rollback = commands.add_parser("rollback")
    rollback.add_argument("effect_id")

    resolve = commands.add_parser("resolve-uncertain")
    resolve.add_argument("effect_id")
    resolve.add_argument("--principal", default="local-owner")
    resolve.add_argument("--applied", action="store_true")
    resolve.add_argument("--receipt", type=Path)
    return result


def require_private_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"input file does not exist: {path}")
    if path.is_symlink():
        raise ValueError(f"input file must not be a symlink: {path}")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"input file must be mode 0600: {path}")


def load_policy(path: Path | None) -> AutonomyPolicy:
    if path is not None:
        require_private_file(path)
        return AutonomyPolicy.model_validate_json(path.read_text())
    private_policy = Path.home() / ".config" / "followthrough" / "effect-policy.json"
    if private_policy.is_file():
        require_private_file(private_policy)
        return AutonomyPolicy.model_validate_json(private_policy.read_text())
    return AutonomyPolicy.safe_default(
        owner_discord_target=os.environ.get("FOLLOWTHROUGH_DISCORD_TARGET")
    )


def service_for(args: argparse.Namespace) -> EffectService:
    workspace = Path.home() / ".hermes" / "user" / "google-workspace"
    google_token = workspace / "google_token.json"
    google_client = workspace / "google_client_secret.json"
    journal = EffectJournal(args.db.expanduser().resolve())
    policy = load_policy(args.policy)
    drivers = default_drivers(
        data_dir=args.db.expanduser().resolve().parent,
        hermes_bin=os.environ.get("FOLLOWTHROUGH_HERMES_BIN", "hermes"),
        google_token_path=google_token if google_token.is_file() else None,
        google_client_secret_path=google_client if google_client.is_file() else None,
    )
    return EffectService(journal, policy, drivers)


def safe_record(record: dict[str, Any]) -> dict[str, Any]:
    request = record.get("request") or {}
    key = str(record.get("idempotency_key") or "")
    return {
        "id": record.get("id"),
        "kind": record.get("kind"),
        "trigger_event_id": record.get("trigger_event_id"),
        "state": record.get("state"),
        "policy_mode": record.get("policy_mode"),
        "policy_reason": record.get("policy_reason"),
        "risk": record.get("risk"),
        "attempt_count": record.get("attempt_count"),
        "provider": record.get("provider"),
        "external_id": record.get("external_id"),
        "receipt": record.get("receipt"),
        "last_error": record.get("last_error"),
        "request_fingerprint": record.get("request_fingerprint"),
        "idempotency_fingerprint": hashlib.sha256(key.encode()).hexdigest() if key else None,
        "request_type": request.get("kind"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def main() -> int:
    args = parser().parse_args()
    service = service_for(args)
    try:
        if args.command == "plan":
            require_private_file(args.request)
            action = parse_request(args.request.read_text())
            output: Any = service.submit(
                action,
                idempotency_key=args.idempotency_key,
                execute=args.execute_if_auto,
            )
            output = safe_record(output)
        elif args.command == "approve":
            output = safe_record(service.approve(args.effect_id, principal=args.principal))
        elif args.command == "execute":
            output = safe_record(service.execute(args.effect_id))
        elif args.command == "show":
            output = safe_record(service.journal.get(args.effect_id))
        elif args.command == "history":
            output = service.journal.history(args.effect_id)
        elif args.command == "recover-inflight":
            output = [
                safe_record(item)
                for item in service.journal.recover_inflight(principal=args.principal)
            ]
        elif args.command == "rollback":
            output = safe_record(service.rollback(args.effect_id))
        elif args.command == "resolve-uncertain":
            receipt = None
            if args.receipt:
                require_private_file(args.receipt)
                receipt = DriverReceipt.model_validate_json(args.receipt.read_text())
            output = safe_record(
                service.resolve_uncertain(
                    args.effect_id,
                    applied=args.applied,
                    principal=args.principal,
                    receipt=receipt,
                )
            )
        else:
            raise ValueError("unsupported command")
    except EffectorError as exc:
        print(json.dumps({"ok": False, "error": exc.safe_details()}, sort_keys=True))
        return 2
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps({"ok": True, "result": output}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

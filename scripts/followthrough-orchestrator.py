#!/usr/bin/env python3
"""Durable Followthrough outbox worker backed by Hermes Kanban."""

from __future__ import annotations

import argparse
import fcntl
import json
import signal
import threading

from followthrough.config import Settings
from followthrough.controls import ControlPlane
from followthrough.effectors.journal import EffectJournal
from followthrough.effectors.policy import AutonomyPolicy
from followthrough.effectors.service import EffectService, default_drivers
from followthrough.kanban import CapsuleWriter, DurableOrchestrator, HermesKanbanClient
from followthrough.repository_pipeline import RepositoryEvaluator
from followthrough.runner import NativeRepositoryRunner
from followthrough.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    lock_path = settings.jobs_dir / "orchestrator.lock"
    lock_file = lock_path.open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"ok": False, "error": "orchestrator_already_running"}))
        return 2

    store = Store(settings.db_path)
    controls = ControlPlane(store)
    if settings.effect_policy_file.is_file():
        if settings.effect_policy_file.is_symlink() or settings.effect_policy_file.stat().st_mode & 0o077:
            raise RuntimeError("effect policy must be a non-symlink mode 0600 file")
        effect_policy = AutonomyPolicy.model_validate_json(
            settings.effect_policy_file.read_text(encoding="utf-8")
        )
    else:
        effect_policy = AutonomyPolicy.safe_default(
            owner_discord_target=settings.discord_target
        )
    effect_service = EffectService(
        EffectJournal(settings.effects_dir / "effects.db"),
        effect_policy,
        default_drivers(
            data_dir=settings.effects_dir,
            hermes_bin=settings.hermes_bin,
            google_token_path=(
                settings.google_token_file if settings.google_token_file.is_file() else None
            ),
            google_client_secret_path=(
                settings.google_client_secret_file
                if settings.google_client_secret_file.is_file()
                else None
            ),
        ),
    )
    worker = DurableOrchestrator(
        client=HermesKanbanClient(timeout_seconds=settings.kanban_cli_timeout_seconds),
        store=store,
        capsule_writer=CapsuleWriter(settings.jobs_dir),
        repository_evaluator=RepositoryEvaluator(
            runner=NativeRepositoryRunner(settings.runner_dir),
            receipts_dir=settings.runner_receipts_dir,
        ),
        control_plane=controls,
        effect_service=effect_service,
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop.is_set():
        try:
            result = worker.run_once()
            safe_result = {key: value for key, value in result.items() if key != "errors"}
            safe_result["error_count"] = len(result.get("errors", []))
            store.heartbeat("orchestrator", "ok", safe_result)
            if args.once:
                print(json.dumps({"ok": True, **safe_result}, sort_keys=True))
                return 0
        except Exception as exc:
            store.heartbeat("orchestrator", "error", {"error": type(exc).__name__})
            if args.once:
                print(json.dumps({"ok": False, "error": type(exc).__name__}, sort_keys=True))
                return 1
        stop.wait(max(1.0, settings.kanban_poll_seconds))
    store.heartbeat("orchestrator", "stopped", {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

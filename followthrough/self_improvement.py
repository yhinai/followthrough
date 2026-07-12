"""Candidate-only self-improvement with deterministic, pinned promotion gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .controls import Capability, ControlPlane, is_within


PROTECTED_TARGET_PARTS = frozenset(
    {
        "evaluator",
        "evaluators",
        "gate",
        "gates",
        "verifier",
        "verify",
        "controls",
        "emergency",
        "kill",
        "policy",
        "self_improvement",
    }
)
UNSAFE_PATTERNS = (
    re.compile(r"(?i)skip\s+(?:all\s+)?(?:tests?|evals?|verification)"),
    re.compile(r"(?i)(?:auto|silent)[-_ ]?approv"),
    re.compile(r"(?i)disable\s+(?:the\s+)?(?:gate|verifier|evaluator|kill|control)"),
    re.compile(r"(?i)curl\b.{0,120}\|\s*(?:ba)?sh\b"),
    re.compile(r"(?i)chmod\s+777"),
    re.compile(r"(?i)(?:print|send|upload|exfiltrate).{0,80}(?:secret|token|credential|\.env)"),
    re.compile(r"(?i)ignore\s+(?:all\s+)?previous\s+instructions"),
)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(value: object, maximum: int = 160) -> str:
    candidate = "_".join(str(value).strip().lower().split())
    safe = "".join(character for character in candidate if character.isalnum() or character in "_.:-/")
    return (safe[:maximum] or "unspecified")


def _relative_target(value: str) -> Path:
    target = Path(value)
    if target.is_absolute() or not target.parts or ".." in target.parts:
        raise ValueError("target must be a relative path")
    normalized = Path(*(_safe(part, 80) for part in target.parts))
    for part in normalized.parts:
        # Guard against both bare directory names ("controls") and real file
        # names ("controls.py", "controls.py.bak"): any dot-delimited token of a
        # path component must not name a protected module, otherwise
        # ``followthrough/controls.py`` would slip through the parts check.
        tokens = {part.lower(), *part.lower().split(".")}
        if tokens & PROTECTED_TARGET_PARTS:
            raise ValueError("candidate may not target an evaluator, gate, verifier, or control")
    return normalized


def evaluator_fingerprint() -> str:
    return _digest(Path(__file__).read_bytes())


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    case_id: str
    baseline_passed: bool
    candidate_passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": _safe(self.case_id, 120),
            "baseline_passed": bool(self.baseline_passed),
            "candidate_passed": bool(self.candidate_passed),
        }


class ImprovementManager:
    """Produces reviewable candidates and refuses implicit live installation."""

    def __init__(
        self,
        store: Any,
        root: str | Path,
        controls: ControlPlane,
        *,
        evidence_roots: Iterable[str | Path] = (),
    ) -> None:
        self.store = store
        self.db = store.db
        self.lock = store.lock
        self.controls = controls
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.evidence_roots = tuple(
            Path(path).expanduser().resolve() for path in evidence_roots
        ) + (self.root / "evidence",)
        for path in (self.root / "candidates", self.root / "reports", self.root / "promoted", self.root / "receipts", self.root / "evidence"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS improvement_policy (
                  id INTEGER PRIMARY KEY CHECK(id=1), live_enabled INTEGER NOT NULL,
                  allowed_roots_json TEXT NOT NULL, required_approver_prefix TEXT NOT NULL,
                  evaluator_fingerprint TEXT NOT NULL, version INTEGER NOT NULL,
                  actor TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS improvement_proposals (
                  id TEXT PRIMARY KEY, target TEXT NOT NULL, candidate_path TEXT NOT NULL,
                  candidate_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL,
                  status TEXT NOT NULL, created_by TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS improvement_evaluations (
                  id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
                  evaluator_fingerprint TEXT NOT NULL, gates_json TEXT NOT NULL,
                  held_in_json TEXT NOT NULL, held_out_json TEXT NOT NULL,
                  report_path TEXT NOT NULL, report_sha256 TEXT NOT NULL,
                  passed INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS improvement_promotions (
                  id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
                  mode TEXT NOT NULL, destination_path TEXT NOT NULL,
                  destination_sha256 TEXT NOT NULL, approved_by TEXT NOT NULL,
                  approval_reference TEXT NOT NULL, policy_version INTEGER NOT NULL,
                  backup_path TEXT, receipt_path TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            self.db.execute(
                "INSERT OR IGNORE INTO improvement_policy(id,live_enabled,allowed_roots_json,required_approver_prefix,evaluator_fingerprint,version,actor,updated_at) VALUES(1,0,'[]','owner:',?,1,'system',?)",
                (evaluator_fingerprint(), _now()),
            )
            self.db.commit()

    @staticmethod
    def _write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, mode)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise

    def propose(
        self,
        *,
        target: str,
        content: str,
        evidence: list[dict[str, str]],
        created_by: str,
    ) -> dict[str, Any]:
        relative = _relative_target(target)
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > 1_048_576:
            raise ValueError("candidate must contain between 1 and 1048576 bytes")
        proposal_id = str(uuid.uuid4())
        candidate_path = self.root / "candidates" / proposal_id / "candidate"
        self._write_atomic(candidate_path, encoded)
        normalized_evidence = [
            {"path": str(Path(item["path"]).expanduser().resolve()), "sha256": item["sha256"].lower()}
            for item in evidence[:50]
            if isinstance(item, dict) and item.get("path") and item.get("sha256")
        ]
        timestamp = _now()
        with self.lock:
            self.db.execute(
                "INSERT INTO improvement_proposals(id,target,candidate_path,candidate_sha256,evidence_json,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,'proposed',?,?,?)",
                (
                    proposal_id,
                    str(relative),
                    str(candidate_path),
                    _digest(encoded),
                    json.dumps(normalized_evidence, sort_keys=True, separators=(",", ":")),
                    _safe(created_by),
                    timestamp,
                    timestamp,
                ),
            )
            self.db.commit()
        receipt = self.controls.audit(
            "improvement_proposed",
            created_by,
            "candidate_only",
            capability=Capability.SELF_IMPROVEMENT,
            details={"proposal_id": proposal_id, "target": str(relative)},
        )
        return {
            "id": proposal_id,
            "target": str(relative),
            "candidate_path": str(candidate_path),
            "candidate_sha256": _digest(encoded),
            "status": "proposed",
            "receipt_id": receipt,
        }

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM improvement_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if not row:
            raise ValueError("proposal not found")
        return dict(row) | {"evidence": json.loads(row["evidence_json"])}

    def _evidence_gate(self, evidence: list[dict[str, str]]) -> bool:
        if not evidence:
            return False
        for item in evidence:
            path = Path(item.get("path", "")).expanduser().resolve()
            expected = item.get("sha256", "").lower()
            if not any(is_within(path, root) for root in self.evidence_roots):
                return False
            if not path.is_file() or len(expected) != 64 or _digest(path.read_bytes()) != expected:
                return False
        return True

    def evaluate(
        self,
        proposal_id: str,
        *,
        evaluator_id: str,
        held_in: Iterable[EvalCaseResult],
        held_out: Iterable[EvalCaseResult],
    ) -> dict[str, Any]:
        proposal = self.proposal(proposal_id)
        path = Path(proposal["candidate_path"])
        content = path.read_bytes() if path.is_file() else b""
        held_in_rows = [case.as_dict() for case in held_in]
        held_out_rows = [case.as_dict() for case in held_out]
        policy = self.policy()
        current_evaluator = evaluator_fingerprint()
        unsafe_matches = [pattern.pattern for pattern in UNSAFE_PATTERNS if pattern.search(content.decode("utf-8", errors="replace"))]
        no_regressions = bool(held_out_rows) and all(
            not row["baseline_passed"] or row["candidate_passed"] for row in held_out_rows
        )
        baseline_total = sum(bool(row["baseline_passed"]) for row in held_out_rows)
        candidate_total = sum(bool(row["candidate_passed"]) for row in held_out_rows)
        gates = {
            "candidate_integrity": bool(content) and _digest(content) == proposal["candidate_sha256"],
            "target_safe": True,
            "unsafe_scan": not unsafe_matches,
            "evidence_verified": self._evidence_gate(proposal["evidence"]),
            "evaluator_pinned": current_evaluator == policy["evaluator_fingerprint"],
            "held_in_passed": bool(held_in_rows) and all(row["candidate_passed"] for row in held_in_rows),
            "held_out_no_regression": no_regressions and candidate_total >= baseline_total,
        }
        passed = all(gates.values())
        report = {
            "proposal_id": proposal_id,
            "candidate_sha256": proposal["candidate_sha256"],
            "evaluator_fingerprint": current_evaluator,
            "evaluator_id": _safe(evaluator_id),
            "gates": gates,
            "unsafe_match_count": len(unsafe_matches),
            "held_in": held_in_rows,
            "held_out": held_out_rows,
            "passed": passed,
            "created_at": _now(),
        }
        report_data = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
        report_path = self.root / "reports" / f"{proposal_id}.json"
        self._write_atomic(report_path, report_data)
        evaluation_id = str(uuid.uuid4())
        with self.lock:
            self.db.execute(
                "INSERT INTO improvement_evaluations(id,proposal_id,evaluator_fingerprint,gates_json,held_in_json,held_out_json,report_path,report_sha256,passed,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET id=excluded.id,evaluator_fingerprint=excluded.evaluator_fingerprint,gates_json=excluded.gates_json,held_in_json=excluded.held_in_json,held_out_json=excluded.held_out_json,report_path=excluded.report_path,report_sha256=excluded.report_sha256,passed=excluded.passed,created_at=excluded.created_at",
                (
                    evaluation_id,
                    proposal_id,
                    current_evaluator,
                    json.dumps(gates, sort_keys=True),
                    json.dumps(held_in_rows, sort_keys=True),
                    json.dumps(held_out_rows, sort_keys=True),
                    str(report_path),
                    _digest(report_data),
                    int(passed),
                    report["created_at"],
                ),
            )
            self.db.execute(
                "UPDATE improvement_proposals SET status=?,updated_at=? WHERE id=?",
                ("evaluation_passed" if passed else "evaluation_failed", _now(), proposal_id),
            )
            self.db.commit()
        if not gates["evaluator_pinned"]:
            self.controls.trigger_safe_mode(
                "policy_drift",
                actor="self-improvement-evaluator",
                details={"proposal_id": proposal_id},
            )
        receipt = self.controls.audit(
            "improvement_evaluated",
            evaluator_id,
            "gates_passed" if passed else "gates_failed",
            capability=Capability.SELF_IMPROVEMENT,
            details={"proposal_id": proposal_id, "passed": passed},
        )
        return report | {"report_path": str(report_path), "receipt_id": receipt}

    def policy(self) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM improvement_policy WHERE id=1").fetchone()
        if not row:
            raise RuntimeError("self-improvement policy missing")
        return dict(row) | {"allowed_roots": json.loads(row["allowed_roots_json"])}

    def configure_live_policy(
        self,
        *,
        live_enabled: bool,
        allowed_roots: list[str],
        required_approver_prefix: str,
        actor: str,
    ) -> dict[str, Any]:
        safe_actor = _safe(actor)
        if not safe_actor.startswith("owner:"):
            raise PermissionError("only an explicit owner actor can change live promotion policy")
        prefix = _safe(required_approver_prefix, 40)
        if not prefix.startswith("owner:"):
            raise ValueError("required approver prefix must identify an owner")
        roots = [str(Path(root).expanduser().resolve()) for root in allowed_roots[:10]]
        with self.lock:
            self.db.execute(
                "UPDATE improvement_policy SET live_enabled=?,allowed_roots_json=?,required_approver_prefix=?,evaluator_fingerprint=?,version=version+1,actor=?,updated_at=? WHERE id=1",
                (int(live_enabled), json.dumps(roots), prefix, evaluator_fingerprint(), safe_actor, _now()),
            )
            self.db.commit()
        receipt = self.controls.audit(
            "improvement_policy_changed",
            actor,
            "explicit_owner_policy",
            capability=Capability.SELF_IMPROVEMENT,
            details={"live_enabled": live_enabled, "allowed_root_count": len(roots)},
        )
        return self.policy() | {"receipt_id": receipt}

    def _verified_evaluation(self, proposal: dict[str, Any]) -> dict[str, Any]:
        evaluation = self.db.execute(
            "SELECT * FROM improvement_evaluations WHERE proposal_id=?", (proposal["id"],)
        ).fetchone()
        if not evaluation or not bool(evaluation["passed"]):
            raise PermissionError("deterministic evaluation gates have not passed")
        candidate_path = Path(proposal["candidate_path"])
        report_path = Path(evaluation["report_path"])
        if not candidate_path.is_file() or _digest(candidate_path.read_bytes()) != proposal["candidate_sha256"]:
            raise PermissionError("candidate changed after evaluation")
        if not report_path.is_file() or _digest(report_path.read_bytes()) != evaluation["report_sha256"]:
            raise PermissionError("evaluation report changed after evaluation")
        policy = self.policy()
        current = evaluator_fingerprint()
        if evaluation["evaluator_fingerprint"] != current or policy["evaluator_fingerprint"] != current:
            raise PermissionError("evaluator is not pinned to the evaluated version")
        if not all(json.loads(evaluation["gates_json"]).values()):
            raise PermissionError("one or more deterministic gates failed")
        return dict(evaluation)

    def promote(
        self,
        proposal_id: str,
        *,
        approved_by: str,
        approval_reference: str,
        live_root: str | Path | None = None,
    ) -> dict[str, Any]:
        existing = self.db.execute(
            "SELECT * FROM improvement_promotions WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if existing:
            return dict(existing) | {"replay": True}
        proposal = self.proposal(proposal_id)
        evaluation = self._verified_evaluation(proposal)
        policy = self.policy()
        relative = _relative_target(proposal["target"])
        live = live_root is not None
        if live:
            safe_approver = _safe(approved_by)
            if not bool(policy["live_enabled"]):
                raise PermissionError("live promotion is disabled by policy")
            if not safe_approver.startswith(policy["required_approver_prefix"]):
                raise PermissionError("explicit owner approval is required")
            if len(approval_reference.strip()) < 8:
                raise PermissionError("an explicit approval reference is required")
            root = Path(live_root).expanduser().resolve()
            allowed = [Path(item).resolve() for item in policy["allowed_roots"]]
            if root not in allowed:
                raise PermissionError("live destination root is not allowlisted")
            destination = (root / relative).resolve()
            if not is_within(destination, root):
                raise PermissionError("live destination escapes allowlisted root")
            mode = "live"
        else:
            destination = self.root / "promoted" / proposal_id / relative
            mode = "staged"
        decision = self.controls.authorize(
            Capability.SELF_IMPROVEMENT,
            idempotency_key=f"promotion:{proposal_id}:{mode}",
            actor=approved_by,
        )
        if not decision.allowed:
            raise PermissionError(decision.reason_code)
        source = Path(proposal["candidate_path"])
        backup_path: Path | None = None
        if live and destination.exists():
            backup_path = self.root / "receipts" / f"{proposal_id}.backup"
            self._write_atomic(backup_path, destination.read_bytes())
        self._write_atomic(destination, source.read_bytes())
        if _digest(destination.read_bytes()) != proposal["candidate_sha256"]:
            if backup_path and backup_path.exists():
                self._write_atomic(destination, backup_path.read_bytes())
            raise RuntimeError("promoted artifact failed post-write verification")
        promotion_id = str(uuid.uuid4())
        receipt_payload = {
            "promotion_id": promotion_id,
            "proposal_id": proposal_id,
            "mode": mode,
            "destination_path": str(destination),
            "destination_sha256": proposal["candidate_sha256"],
            "approved_by": _safe(approved_by),
            "approval_reference": _safe(approval_reference, 160),
            "policy_version": int(policy["version"]),
            "evaluation_report_sha256": evaluation["report_sha256"],
            "created_at": _now(),
        }
        receipt_path = self.root / "receipts" / f"{promotion_id}.json"
        self._write_atomic(
            receipt_path,
            (json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        with self.lock:
            self.db.execute(
                "INSERT INTO improvement_promotions(id,proposal_id,mode,destination_path,destination_sha256,approved_by,approval_reference,policy_version,backup_path,receipt_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    promotion_id,
                    proposal_id,
                    mode,
                    str(destination),
                    proposal["candidate_sha256"],
                    receipt_payload["approved_by"],
                    receipt_payload["approval_reference"],
                    int(policy["version"]),
                    str(backup_path) if backup_path else None,
                    str(receipt_path),
                    receipt_payload["created_at"],
                ),
            )
            self.db.execute(
                "UPDATE improvement_proposals SET status=?,updated_at=? WHERE id=?",
                ("promoted_live" if live else "promoted_staged", _now(), proposal_id),
            )
            self.db.commit()
        self.controls.audit(
            "improvement_promoted",
            approved_by,
            "explicit_live_promotion" if live else "gated_staged_promotion",
            capability=Capability.SELF_IMPROVEMENT,
            details={"proposal_id": proposal_id, "mode": mode, "policy_version": policy["version"]},
        )
        return receipt_payload | {"receipt_path": str(receipt_path), "replay": False}

    def list_proposals(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            dict(row) | {"evidence": json.loads(row["evidence_json"])}
            for row in self.db.execute(
                "SELECT * FROM improvement_proposals ORDER BY created_at DESC LIMIT ?",
                (max(0, limit),),
            ).fetchall()
        ]

    def rollback_live(self, proposal_id: str, *, actor: str, reason_code: str) -> dict[str, Any]:
        promotion = self.db.execute(
            "SELECT * FROM improvement_promotions WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if not promotion or promotion["mode"] != "live":
            raise ValueError("live promotion not found")
        if not _safe(actor).startswith("owner:"):
            raise PermissionError("only an owner can roll back a live promotion")
        destination = Path(promotion["destination_path"])
        backup = Path(promotion["backup_path"]) if promotion["backup_path"] else None
        if backup and backup.is_file():
            self._write_atomic(destination, backup.read_bytes())
            result = "restored_backup"
        else:
            destination.unlink(missing_ok=True)
            result = "removed_new_artifact"
        receipt = self.controls.audit(
            "improvement_rolled_back",
            actor,
            reason_code,
            capability=Capability.SELF_IMPROVEMENT,
            details={"proposal_id": proposal_id, "result": result},
        )
        return {"proposal_id": proposal_id, "result": result, "receipt_id": receipt}

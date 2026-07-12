from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit


class RunnerError(RuntimeError):
    """Base error for repository acquisition or execution failures."""


class SourceValidationError(RunnerError):
    """The repository source is not in the deliberately small allow-list."""


class CloneError(RunnerError):
    """Git could not produce a verified isolated checkout."""


class UnsafeRepositoryError(RunnerError):
    """Static inspection found a high-risk repository and execution was denied."""


class SandboxUnavailableError(RunnerError):
    """The mandatory native isolation backend is not available."""


@dataclass(frozen=True)
class RunnerLimits:
    cpu_quota_percent: int = 50
    memory_max_bytes: int = 512 * 1024 * 1024
    tasks_max: int = 64
    runtime_seconds: float = 30.0
    output_max_bytes: int = 2 * 1024 * 1024
    checkout_max_bytes: int = 256 * 1024 * 1024
    checkout_max_files: int = 20_000

    def __post_init__(self) -> None:
        if not 1 <= self.cpu_quota_percent <= 400:
            raise ValueError("cpu_quota_percent must be between 1 and 400")
        if self.memory_max_bytes < 64 * 1024 * 1024:
            raise ValueError("memory_max_bytes must be at least 64 MiB")
        if self.tasks_max < 4:
            raise ValueError("tasks_max must be at least 4")
        if self.runtime_seconds <= 0:
            raise ValueError("runtime_seconds must be positive")
        if self.output_max_bytes < 1024:
            raise ValueError("output_max_bytes must be at least 1024")


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    path: str
    detail: str


@dataclass(frozen=True)
class Provenance:
    requested_source: str
    normalized_source: str
    remote_origin: str
    commit: str
    tree: str
    acquired_at: str
    shallow: bool


@dataclass(frozen=True)
class RepositorySnapshot:
    run_id: str
    root: Path
    checkout: Path
    provenance: Provenance


@dataclass(frozen=True)
class InspectionReport:
    files_scanned: int
    bytes_scanned: int
    checkout_bytes: int
    license_files: tuple[str, ...]
    detected_licenses: tuple[str, ...]
    findings: tuple[Finding, ...]

    @property
    def blocking(self) -> bool:
        return any(item.severity in {"critical", "high"} for item in self.findings)


@dataclass(frozen=True)
class ExecutionReceipt:
    run_id: str
    command: tuple[str, ...]
    started_at: str
    finished_at: str
    duration_ms: int
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    sandbox_backend: str
    systemd_scope: str | None
    network_enabled: bool
    suspicious_override: bool
    install_hooks_override: bool
    limits: dict[str, int | float]


@dataclass(frozen=True)
class RunReceipt:
    provenance: Provenance
    inspection: InspectionReport
    execution: ExecutionReceipt
    workspace: str
    workspace_removed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Source:
    requested: str
    normalized: str
    is_local: bool


_BLOCKED_INSTALL_COMMANDS = (
    re.compile(r"(?:^|\s)(?:pip|pip3|uv\s+pip)\s+install(?:\s|$)", re.I),
    re.compile(r"(?:^|\s)(?:npm|pnpm|yarn|bun)\s+(?:install|add|ci)(?:\s|$)", re.I),
    re.compile(r"(?:^|\s)(?:poetry|pdm)\s+install(?:\s|$)", re.I),
    re.compile(r"(?:^|\s)python(?:3)?\s+-m\s+pip\s+install(?:\s|$)", re.I),
    re.compile(r"(?:^|\s)python(?:3)?\s+setup\.py\s+(?:install|develop)(?:\s|$)", re.I),
    re.compile(r"(?:^|\s)make\s+install(?:\s|$)", re.I),
)

_TEXT_SUFFIXES = {
    "",
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".go",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".bash",
    ".cs",
    ".fish",
    ".gradle",
    ".groovy",
    ".h",
    ".hpp",
    ".kts",
    ".lua",
    ".php",
    ".pl",
    ".pm",
    ".ps1",
    ".scala",
    ".sh",
    ".sql",
    ".swift",
    ".zsh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

_SUSPICIOUS_PATTERNS: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    ("critical", "DESTRUCTIVE_ROOT_DELETE", re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/(?:\s|$)"), "attempts recursive deletion at filesystem root"),
    ("critical", "DOCKER_SOCKET", re.compile(r"(?:/var/run|/run)/docker\.sock"), "references the host Docker control socket"),
    ("high", "HERMES_HOME_ACCESS", re.compile(r"(?:/home/[^/\s]+/)?\.hermes(?:/|\b)"), "references Hermes private state"),
    ("high", "HOST_HOME_ACCESS", re.compile(r"/home/[A-Za-z0-9_.-]+/"), "references an absolute host home path"),
    ("high", "GPU_DEVICE_ACCESS", re.compile(r"/dev/(?:nvidia|dri|kfd)"), "references a host GPU device"),
    ("high", "DOWNLOAD_PIPE_SHELL", re.compile(r"(?:curl|wget)[^\n|]{0,300}\|\s*(?:ba)?sh\b", re.I), "pipes downloaded content into a shell"),
    ("medium", "PRIVILEGE_ESCALATION", re.compile(r"(?:^|\s)(?:sudo|pkexec)(?:\s|$)"), "requests privilege escalation"),
    ("medium", "SETUID_CHANGE", re.compile(r"chmod\s+(?:u\+s|[0-7]*4[0-7]{3})"), "attempts to set a setuid bit"),
    ("low", "RAW_SOCKET_USE", re.compile(r"socket\.(?:create_connection|socket)\s*\("), "opens a network socket"),
)

_SECRET_NAMES = {
    ".env",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
}


class NativeRepositoryRunner:
    """Clone, inspect, and run a repository without exposing the Spark host.

    Repository code remains native machine code, but executes inside a transient
    systemd user scope and a bubblewrap namespace. There is deliberately no
    unsandboxed execution fallback.
    """

    def __init__(
        self,
        workspace_root: Path | None = None,
        *,
        limits: RunnerLimits | None = None,
        allow_local_sources: bool = False,
        allowed_hosts: frozenset[str] | None = None,
    ) -> None:
        self.workspace_root = (
            workspace_root or Path.home() / ".local" / "share" / "followthrough" / "runner"
        ).expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.workspace_root, 0o700)
        self.limits = limits or RunnerLimits()
        self.allow_local_sources = allow_local_sources
        self.allowed_hosts = allowed_hosts or frozenset(
            {"github.com", "gitlab.com", "codeberg.org"}
        )
        self.git = shutil.which("git")
        self.bwrap = shutil.which("bwrap")
        self.prlimit = shutil.which("prlimit")
        self.systemd_run = shutil.which("systemd-run")
        if not self.git:
            raise SandboxUnavailableError("git is required")

    @property
    def sandbox_available(self) -> bool:
        return bool(self.bwrap and self.prlimit)

    def validate_source(self, source: str) -> str:
        return self._source(source).normalized

    def acquire(self, source: str) -> RepositorySnapshot:
        spec = self._source(source)
        run_id = uuid.uuid4().hex
        root = Path(tempfile.mkdtemp(prefix=f"run-{run_id}-", dir=self.workspace_root))
        os.chmod(root, 0o700)
        checkout = root / "checkout"
        clone_home = root / "clone-home"
        clone_home.mkdir(mode=0o700)
        clone_env = {
            "HOME": str(clone_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "GIT_ASKPASS": "/bin/false",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        protocol = "always" if spec.is_local else "never"
        command = [
            self.git,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "submodule.recurse=false",
            "-c",
            f"protocol.file.allow={protocol}",
            "-c",
            "protocol.ext.allow=never",
            "clone",
            "--depth=1",
            "--no-tags",
            "--single-branch",
            "--no-local",
            spec.normalized,
            str(checkout),
        ]
        try:
            clone = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=min(120.0, max(15.0, self.limits.runtime_seconds * 3)),
                env=clone_env,
            )
            if clone.returncode != 0:
                raise CloneError(self._safe_error("git clone failed", clone.stderr))
            self._git_configure_checkout(checkout, clone_env)
            commit = self._git_read(checkout, clone_env, "rev-parse", "HEAD")
            tree = self._git_read(checkout, clone_env, "rev-parse", "HEAD^{tree}")
            remote = self._git_read(checkout, clone_env, "remote", "get-url", "origin")
            shallow = (checkout / ".git" / "shallow").is_file()
            if not re.fullmatch(r"[0-9a-f]{40,64}", commit) or not re.fullmatch(
                r"[0-9a-f]{40,64}", tree
            ):
                raise CloneError("git returned invalid provenance hashes")
            provenance = Provenance(
                requested_source=source,
                normalized_source=spec.normalized,
                remote_origin=remote,
                commit=commit,
                tree=tree,
                acquired_at=datetime.now(UTC).isoformat(),
                shallow=shallow,
            )
            return RepositorySnapshot(run_id, root, checkout, provenance)
        except Exception:
            self._cleanup_root(root)
            raise

    def inspect(self, snapshot: RepositorySnapshot) -> InspectionReport:
        self._assert_snapshot(snapshot)
        findings: list[Finding] = []
        license_files: list[str] = []
        detected_licenses: set[str] = set()
        files_scanned = 0
        bytes_scanned = 0
        checkout_bytes = self._bounded_tree_size(
            snapshot.checkout, self.limits.checkout_max_bytes
        )
        package_json: list[Path] = []

        if checkout_bytes > self.limits.checkout_max_bytes:
            finding = Finding(
                "critical",
                "CHECKOUT_SIZE_LIMIT",
                ".",
                f"checkout exceeds {self.limits.checkout_max_bytes} bytes",
            )
            return InspectionReport(0, 0, checkout_bytes, (), (), (finding,))

        for path in sorted(snapshot.checkout.rglob("*")):
            relative = path.relative_to(snapshot.checkout)
            if relative.parts and relative.parts[0] == ".git":
                continue
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                findings.append(Finding("high", "UNREADABLE_PATH", str(relative), str(exc)))
                continue
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                if os.path.isabs(target) or ".." in Path(target).parts:
                    findings.append(
                        Finding(
                            "high",
                            "ESCAPING_SYMLINK",
                            str(relative),
                            f"symlink target escapes checkout: {target}",
                        )
                    )
                continue
            if not stat.S_ISREG(mode):
                continue
            size = path.stat().st_size
            files_scanned += 1
            if files_scanned > self.limits.checkout_max_files:
                findings.append(
                    Finding(
                        "critical",
                        "CHECKOUT_FILE_LIMIT",
                        str(relative),
                        f"checkout exceeds {self.limits.checkout_max_files} files",
                    )
                )
                break
            lower_name = path.name.lower()
            if lower_name in _SECRET_NAMES:
                findings.append(
                    Finding("high", "EMBEDDED_SECRET_FILE", str(relative), "sensitive filename")
                )
            if lower_name.startswith(("license", "copying", "notice")):
                license_files.append(str(relative))
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    sample = stream.read(64_000)
                detected_licenses.add(self._license_name(sample))
            if lower_name == "package.json":
                package_json.append(path)
            if not self._looks_textual(path):
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    content = stream.read(1024 * 1024)
            except OSError as exc:
                findings.append(Finding("medium", "READ_ERROR", str(relative), str(exc)))
                continue
            if size > 1024 * 1024:
                findings.append(
                    Finding(
                        "medium",
                        "STATIC_SCAN_TRUNCATED",
                        str(relative),
                        "static scan inspected only the first 1048576 bytes",
                    )
                )
            bytes_scanned += len(content.encode("utf-8", errors="replace"))
            for severity, code, pattern, detail in _SUSPICIOUS_PATTERNS:
                if pattern.search(content):
                    findings.append(Finding(severity, code, str(relative), detail))

        for path in package_json:
            relative = str(path.relative_to(snapshot.checkout))
            try:
                scripts = json.loads(path.read_text()).get("scripts", {})
            except (OSError, json.JSONDecodeError, AttributeError):
                findings.append(
                    Finding("medium", "INVALID_PACKAGE_MANIFEST", relative, "cannot parse package.json")
                )
                continue
            for name in ("preinstall", "install", "postinstall", "prepare"):
                if isinstance(scripts, dict) and scripts.get(name):
                    findings.append(
                        Finding(
                            "high",
                            "PACKAGE_INSTALL_HOOK",
                            relative,
                            f"package.json defines {name}",
                        )
                    )

        if not license_files:
            findings.append(
                Finding("medium", "NO_LICENSE", ".", "no LICENSE, COPYING, or NOTICE file found")
            )
        findings = sorted(findings, key=lambda item: (item.path, item.code, item.detail))
        return InspectionReport(
            files_scanned=files_scanned,
            bytes_scanned=bytes_scanned,
            checkout_bytes=checkout_bytes,
            license_files=tuple(sorted(license_files)),
            detected_licenses=tuple(sorted(detected_licenses)),
            findings=tuple(findings),
        )

    def execute(
        self,
        snapshot: RepositorySnapshot,
        command: Sequence[str],
        inspection: InspectionReport | None = None,
        *,
        allow_suspicious: bool = False,
        allow_install_hooks: bool = False,
        network: bool = False,
    ) -> ExecutionReceipt:
        self._assert_snapshot(snapshot)
        if not self.bwrap or not self.prlimit:
            raise SandboxUnavailableError(
                "bubblewrap and prlimit are mandatory; refusing unsandboxed execution"
            )
        argv = self._validate_command(command, allow_install_hooks=allow_install_hooks)
        report = inspection or self.inspect(snapshot)
        if report.blocking and not allow_suspicious:
            codes = ", ".join(sorted({item.code for item in report.findings if item.severity in {"critical", "high"}}))
            raise UnsafeRepositoryError(f"repository execution denied by static inspection: {codes}")

        bwrap = self._bubblewrap_command(snapshot.checkout, argv, network=network)
        limited = [
            self.prlimit,
            f"--as={self.limits.memory_max_bytes}",
            f"--cpu={max(1, math.ceil(self.limits.runtime_seconds) + 1)}",
            f"--fsize={self.limits.output_max_bytes}",
            "--core=0",
            "--",
            *bwrap,
        ]
        scope_name: str | None = None
        backend = "bubblewrap+rlimits"
        launcher = limited
        if self._systemd_available():
            scope_name = f"followthrough-run-{snapshot.run_id[:20]}.service"
            backend = "systemd-service+bubblewrap"
            launcher = [
                self.systemd_run or "systemd-run",
                "--user",
                "--wait",
                "--pipe",
                "--quiet",
                "--collect",
                "--service-type=exec",
                f"--unit={scope_name.removesuffix('.service')}",
                # Ubuntu confines unprivileged user namespaces through a bwrap
                # AppArmor profile. A transient service created by the user
                # manager can make that profile transition even though the
                # long-lived orchestrator retains NoNewPrivileges=yes.
                "-p",
                "NoNewPrivileges=no",
                "-p",
                f"CPUQuota={self.limits.cpu_quota_percent}%",
                "-p",
                f"MemoryMax={self.limits.memory_max_bytes}",
                "-p",
                f"TasksMax={self.limits.tasks_max}",
                "-p",
                f"RuntimeMaxSec={math.ceil(self.limits.runtime_seconds) + 2}s",
                "--",
                *limited,
            ]

        stdout_path = snapshot.root / "stdout.receipt"
        stderr_path = snapshot.root / "stderr.receipt"
        started_wall = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        timed_out = False
        exit_code: int | None = None
        launcher_env = self._launcher_environment()
        with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            process = subprocess.Popen(
                launcher,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=launcher_env,
                start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=self.limits.runtime_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                if scope_name and scope_name.endswith(".service"):
                    self._stop_systemd_unit(scope_name)
                self._kill_process_group(process)
                exit_code = process.returncode
        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout, stdout_truncated = self._read_receipt(stdout_path)
        stderr, stderr_truncated = self._read_receipt(stderr_path)
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        return ExecutionReceipt(
            run_id=snapshot.run_id,
            command=argv,
            started_at=started_wall,
            finished_at=datetime.now(UTC).isoformat(),
            duration_ms=duration_ms,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            sandbox_backend=backend,
            systemd_scope=scope_name,
            network_enabled=network,
            suspicious_override=allow_suspicious,
            install_hooks_override=allow_install_hooks,
            limits={
                "cpu_quota_percent": self.limits.cpu_quota_percent,
                "memory_max_bytes": self.limits.memory_max_bytes,
                "tasks_max": self.limits.tasks_max,
                "runtime_seconds": self.limits.runtime_seconds,
                "output_max_bytes": self.limits.output_max_bytes,
            },
        )

    def run(
        self,
        source: str,
        command: Sequence[str],
        *,
        allow_suspicious: bool = False,
        allow_install_hooks: bool = False,
        network: bool = False,
        keep_workspace: bool = False,
    ) -> RunReceipt:
        snapshot = self.acquire(source)
        receipt: ExecutionReceipt | None = None
        report: InspectionReport | None = None
        try:
            report = self.inspect(snapshot)
            receipt = self.execute(
                snapshot,
                command,
                report,
                allow_suspicious=allow_suspicious,
                allow_install_hooks=allow_install_hooks,
                network=network,
            )
        finally:
            if not keep_workspace:
                self.cleanup(snapshot)
        if receipt is None or report is None:
            raise RunnerError("repository run ended without a receipt")
        return RunReceipt(
            provenance=snapshot.provenance,
            inspection=report,
            execution=receipt,
            workspace=str(snapshot.root),
            workspace_removed=not snapshot.root.exists(),
        )

    def cleanup(self, snapshot: RepositorySnapshot) -> None:
        self._assert_snapshot(snapshot)
        self._cleanup_root(snapshot.root)

    def _source(self, source: str) -> _Source:
        if not isinstance(source, str) or not source.strip() or source != source.strip():
            raise SourceValidationError("repository source must be a nonempty trimmed string")
        if any(char in source for char in ("\x00", "\n", "\r")):
            raise SourceValidationError("repository source contains control characters")
        candidate = Path(source).expanduser()
        if candidate.is_absolute():
            if not self.allow_local_sources:
                raise SourceValidationError("local repositories are disabled")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir() or not (resolved / ".git").exists():
                raise SourceValidationError("local source must be a Git repository")
            return _Source(source, str(resolved), True)

        parsed = urlsplit(source)
        if parsed.scheme.lower() != "https":
            raise SourceValidationError("only HTTPS Git remotes are allowed")
        if parsed.username or parsed.password:
            raise SourceValidationError("credentials must not be embedded in repository URLs")
        if parsed.query or parsed.fragment:
            raise SourceValidationError("repository URL must not contain query or fragment data")
        try:
            port = parsed.port
        except ValueError as exc:
            raise SourceValidationError("repository URL has an invalid port") from exc
        if port not in (None, 443):
            raise SourceValidationError("repository URL uses a disallowed port")
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in self.allowed_hosts:
            raise SourceValidationError(f"repository host is not allowed: {host or '<missing>'}")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise SourceValidationError("private or local repository addresses are forbidden")
        if "%" in parsed.path:
            raise SourceValidationError("percent-encoded repository paths are forbidden")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or any(part in {".", ".."} for part in parts):
            raise SourceValidationError("repository URL must identify an owner and repository")
        normalized_path = "/" + "/".join(parts)
        normalized = urlunsplit(SplitResult("https", host, normalized_path, "", ""))
        return _Source(source, normalized, False)

    def _git_configure_checkout(self, checkout: Path, env: dict[str, str]) -> None:
        for key, value in (
            ("core.hooksPath", "/dev/null"),
            ("credential.helper", ""),
            ("submodule.recurse", "false"),
            ("protocol.file.allow", "never"),
            ("protocol.ext.allow", "never"),
        ):
            result = subprocess.run(
                [self.git or "git", "-C", str(checkout), "config", "--local", key, value],
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
            )
            if result.returncode != 0:
                raise CloneError(self._safe_error(f"cannot harden Git setting {key}", result.stderr))

    def _git_read(self, checkout: Path, env: dict[str, str], *args: str) -> str:
        result = subprocess.run(
            [self.git or "git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        if result.returncode != 0:
            raise CloneError(self._safe_error(f"git {' '.join(args)} failed", result.stderr))
        return result.stdout.strip()

    def _bubblewrap_command(
        self, checkout: Path, command: tuple[str, ...], *, network: bool
    ) -> list[str]:
        empty = checkout.parent / "empty-host-view"
        empty.mkdir(mode=0o555, exist_ok=True)
        os.chmod(empty, 0o555)
        args = [
            self.bwrap or "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
        ]
        if not network:
            args.append("--unshare-net")
        for path in (Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")):
            if path.exists():
                args.extend(("--ro-bind", str(path), str(path)))
        args.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/tmp/home",
                "--ro-bind",
                str(empty),
                "/run",
                "--ro-bind",
                str(empty),
                "/home",
                "--ro-bind",
                str(empty),
                "/root",
                "--ro-bind",
                str(empty),
                "/var",
                "--ro-bind",
                str(empty),
                "/mnt",
                "--ro-bind",
                str(empty),
                "/media",
                "--ro-bind",
                str(empty),
                "/srv",
                "--bind",
                str(checkout),
                "/workspace",
                "--chdir",
                "/workspace",
                "--clearenv",
                "--setenv",
                "PATH",
                "/usr/local/bin:/usr/bin:/bin",
                "--setenv",
                "HOME",
                "/tmp/home",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "LANG",
                "C.UTF-8",
                "--setenv",
                "LC_ALL",
                "C.UTF-8",
                "--setenv",
                "GIT_CONFIG_GLOBAL",
                "/dev/null",
                "--setenv",
                "GIT_CONFIG_NOSYSTEM",
                "1",
                "--setenv",
                "NPM_CONFIG_IGNORE_SCRIPTS",
                "true",
                "--setenv",
                "YARN_ENABLE_SCRIPTS",
                "false",
                "--setenv",
                "PNPM_CONFIG_IGNORE_SCRIPTS",
                "true",
                "--hostname",
                "followthrough-runner",
                "--remount-ro",
                "/",
                "--",
                "/bin/sh",
                "-c",
                'umask 077; exec "$@"',
                "followthrough-runner",
                *command,
            )
        )
        return args

    def _systemd_available(self) -> bool:
        if not self.systemd_run or not shutil.which("systemctl"):
            return False
        env = self._launcher_environment()
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                env=env,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _stop_systemd_unit(self, unit_name: str) -> None:
        if not shutil.which("systemctl") or not re.fullmatch(
            r"followthrough-run-[0-9a-f]{1,20}\.service", unit_name
        ):
            return
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", unit_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=3,
                env=self._launcher_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return

    def _launcher_environment(self) -> dict[str, str]:
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": "/tmp",
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": address,
        }

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _read_receipt(self, path: Path) -> tuple[str, bool]:
        size = path.stat().st_size
        with path.open("rb") as handle:
            content = handle.read(self.limits.output_max_bytes)
        return content.decode("utf-8", errors="replace"), size > len(content)

    def _validate_command(
        self, command: Sequence[str], *, allow_install_hooks: bool
    ) -> tuple[str, ...]:
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command must be a nonempty argv sequence")
        argv = tuple(str(item) for item in command)
        if any(not item or "\x00" in item or len(item) > 16_384 for item in argv):
            raise ValueError("command contains an empty, NUL, or oversized argument")
        joined = " ".join(argv)
        if not allow_install_hooks and any(pattern.search(joined) for pattern in _BLOCKED_INSTALL_COMMANDS):
            raise UnsafeRepositoryError(
                "package installation and install hooks are disabled by default"
            )
        return argv

    def _assert_snapshot(self, snapshot: RepositorySnapshot) -> None:
        root = snapshot.root.resolve()
        if root.parent != self.workspace_root or not root.name.startswith("run-"):
            raise RunnerError("snapshot is outside the managed runner workspace")
        if not snapshot.checkout.is_dir() or snapshot.checkout.resolve().parent != root:
            raise RunnerError("snapshot checkout is missing or invalid")

    def _cleanup_root(self, root: Path) -> None:
        resolved = root.resolve()
        if resolved.parent != self.workspace_root or not resolved.name.startswith("run-"):
            raise RunnerError("refusing to clean an unmanaged path")

        def onerror(function: Any, path: str, _error: Any) -> None:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            function(path)

        shutil.rmtree(resolved, onerror=onerror)

    @staticmethod
    def _looks_textual(path: Path) -> bool:
        return path.suffix.lower() in _TEXT_SUFFIXES or path.name.lower() in {
            "dockerfile",
            "makefile",
        }

    @staticmethod
    def _bounded_tree_size(root: Path, limit: int) -> int:
        """Measure checkout plus Git metadata, stopping once the limit is exceeded."""

        total = 0
        for directory, _subdirs, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                try:
                    info = os.lstat(Path(directory) / filename)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > limit:
                        return total
        return total

    @staticmethod
    def _license_name(content: str) -> str:
        lowered = content.lower()
        if "mit license" in lowered and "permission is hereby granted" in lowered:
            return "MIT"
        if "apache license" in lowered and "version 2.0" in lowered:
            return "Apache-2.0"
        if "redistribution and use in source and binary forms" in lowered and "disclaimer" in lowered:
            if "neither the name" in lowered:
                return "BSD-3-Clause"
            return "BSD-2-Clause"
        if "gnu general public license" in lowered:
            if "version 3" in lowered:
                return "GPL-3.0"
            if "version 2" in lowered:
                return "GPL-2.0"
            return "GPL"
        if "mozilla public license" in lowered and "2.0" in lowered:
            return "MPL-2.0"
        return "UNKNOWN"

    @staticmethod
    def _safe_error(prefix: str, stderr: str) -> str:
        # Remote URLs can contain private organization names. Receipts need the
        # useful Git error, not an unbounded dump or embedded credential.
        cleaned = re.sub(r"https://[^\s/@]+:[^\s/@]+@", "https://<redacted>@", stderr)
        cleaned = " ".join(cleaned.split())[:1000]
        return f"{prefix}: {cleaned}" if cleaned else prefix


def hash_receipt(receipt: RunReceipt) -> str:
    """Return a stable integrity hash suitable for an append-only audit log."""

    encoded = json.dumps(receipt.to_dict(), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

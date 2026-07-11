from __future__ import annotations

import json
import os
import socket
from pathlib import Path


def attempt(name, operation):
    try:
        operation()
    except (OSError, PermissionError) as exc:
        return name, {"blocked": True, "error": type(exc).__name__}
    return name, {"blocked": False, "error": None}


def network_attempt():
    connection = socket.socket()
    connection.settimeout(0.25)
    try:
        connection.connect(("1.1.1.1", 53))
    finally:
        connection.close()


checks = dict(
    [
        attempt("hermes", lambda: Path("/home/alhinai/.hermes/config.yaml").read_text()),
        attempt("docker", lambda: Path("/var/run/docker.sock").read_bytes()),
        attempt("gpu", lambda: Path("/dev/nvidia0").read_bytes()),
        attempt(
            "host_write",
            lambda: Path("/home/alhinai/followthrough-runner-escape-marker").write_text(
                "escaped"
            ),
        ),
        attempt("home_root_write", lambda: Path("/home/escape").write_text("escaped")),
        attempt("run_root_write", lambda: Path("/run/escape").write_text("escaped")),
        attempt("var_root_write", lambda: Path("/var/escape").write_text("escaped")),
        attempt("network", network_attempt),
    ]
)
Path("workspace-write.txt").write_text("allowed")
checks["workspace"] = {"blocked": not Path("workspace-write.txt").is_file(), "error": None}
checks["environment"] = {
    "blocked": False,
    "home": os.environ.get("HOME"),
    "secret_names": sorted(
        name
        for name in os.environ
        if any(token in name.upper() for token in ("TOKEN", "KEY", "SECRET", "PASSWORD"))
    ),
}
print(json.dumps(checks, sort_keys=True))
raise SystemExit(
    0
    if all(
        checks[name]["blocked"]
        for name in (
            "hermes",
            "docker",
            "gpu",
            "host_write",
            "home_root_write",
            "run_root_write",
            "var_root_write",
            "network",
        )
    )
    else 9
)

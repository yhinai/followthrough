from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import httpx


ANSI = re.compile(r"\x1b\[[0-9;]*m")
WHISPER = re.compile(
    r"(?P<clock>\d\d:\d\d:\d\d(?:\.\d+)?)\s+.*?"
    r"\[OnDeviceWhisper\]\s+Transcribed\s+.*?\s+Text:\s*(?P<text>.+?)\s*$"
)
ACTION = re.compile(
    r"\b(research|investigate|test|evaluate|clone|check|find|look[ -]?up|benchmark|"
    r"remind|schedule|add|send|message|email|deploy|purchase|buy)\b",
    re.I,
)
SUBJECT = re.compile(
    r"\b(github|repo(?:sitory)?|tool|framework|library|sdk|api|startup|company|"
    r"event|calendar|meeting|task|todo|to-do|hermes|agent|automation)\b|"
    r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+",
    re.I,
)


@dataclass(frozen=True)
class Transcript:
    event_id: str
    occurred_at: str
    text: str


class TranscriptAggregator:
    """Join short Whisper segments only when they form an explicit command.

    Individual segments are archived first. This buffer emits a second,
    content-addressed event only when an action verb and an actionable subject
    occur inside the bounded window, preventing ordinary ambient speech from
    reaching Hermes memory or the action runtime.
    """

    def __init__(self, *, window_seconds: float = 45, max_segments: int = 12) -> None:
        self.window_seconds = window_seconds
        self.max_segments = max_segments
        self._segments: deque[tuple[float, Transcript]] = deque()

    def add(self, transcript: Transcript, *, monotonic_at: float | None = None) -> Transcript | None:
        observed = time.monotonic() if monotonic_at is None else monotonic_at
        self._segments.append((observed, transcript))
        cutoff = observed - self.window_seconds
        while self._segments and (
            self._segments[0][0] < cutoff or len(self._segments) > self.max_segments
        ):
            self._segments.popleft()

        text = " ".join(item.text.strip() for _, item in self._segments if item.text.strip())
        if not (ACTION.search(text) and SUBJECT.search(text)):
            return None

        occurred_at = self._segments[0][1].occurred_at
        component_ids = "\0".join(item.event_id for _, item in self._segments)
        digest = hashlib.sha256(f"{component_ids}\0{text}".encode()).hexdigest()
        self._segments.clear()
        return Transcript(
            event_id=f"adb-omi:aggregate:{digest}",
            occurred_at=occurred_at,
            text=text,
        )


def parse_whisper_line(line: str, *, day: datetime | None = None) -> Transcript | None:
    clean = ANSI.sub("", line).strip()
    match = WHISPER.search(clean)
    if not match:
        return None
    text = match.group("text").strip()
    if not text:
        return None
    basis = day or datetime.now().astimezone()
    clock = datetime.strptime(match.group("clock"), "%H:%M:%S.%f").time()
    occurred = datetime.combine(basis.date(), clock, tzinfo=UTC)
    digest = hashlib.sha256(f"{occurred.isoformat()}\0{text}".encode()).hexdigest()
    return Transcript(
        event_id=f"adb-omi:{digest}",
        occurred_at=occurred.isoformat(),
        text=text,
    )


def logcat_lines(adb: str, serial: str) -> Iterator[str]:
    process = subprocess.Popen(
        [adb, "-s", serial, "logcat", "-v", "threadtime", "-T", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    try:
        yield from process.stdout
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


class AdbTranscriptBridge:
    def __init__(
        self,
        *,
        serial: str,
        token_file: Path,
        endpoint: str = "http://127.0.0.1:18765/api/v1/transcripts",
        adb: str = "/usr/bin/adb",
        receipts: Path | None = None,
    ) -> None:
        self.serial = serial
        self.token_file = token_file
        self.endpoint = endpoint
        self.adb = adb
        self.receipts = receipts
        self.aggregator = TranscriptAggregator()

    def _record(self, payload: dict[str, object]) -> None:
        if not self.receipts:
            return
        self.receipts.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        os.chmod(self.receipts, 0o600)

    def deliver(self, transcript: Transcript) -> dict[str, object]:
        token = self.token_file.read_text().strip()
        started = time.monotonic()
        response = httpx.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "event_id": transcript.event_id,
                "device_id": self.serial,
                "source": "phone",
                "occurred_at": transcript.occurred_at,
                "text": transcript.text,
                "consent": True,
                "metadata": {
                    "capture": "omi_on_device_whisper_via_adb",
                    "allow_owner_report": True,
                },
            },
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()
        self._record(
            {
                "at": datetime.now(UTC).isoformat(),
                "event_id": transcript.event_id,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "status_code": response.status_code,
                "result_status": result.get("status"),
                "run_id": result.get("run_id"),
                "job_id": result.get("job_id"),
            }
        )
        return result

    def run_forever(self) -> None:
        while True:
            try:
                subprocess.run(
                    [self.adb, "connect", self.serial],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                for line in logcat_lines(self.adb, self.serial):
                    transcript = parse_whisper_line(line)
                    if transcript:
                        result = self.deliver(transcript)
                        if result.get("status") == "archived":
                            aggregate = self.aggregator.add(transcript)
                            if aggregate:
                                self.deliver(aggregate)
            except Exception as exc:
                self._record(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "error": type(exc).__name__,
                    }
                )
                time.sleep(2)

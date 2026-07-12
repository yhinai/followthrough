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
    r"\b(research|search|investigate|test|evaluate|clone|check|find|look[ -]?up|benchmark|"
    r"remind|schedule|add|send|message|email|deploy|purchase|buy)\b",
    re.I,
)
SUBJECT = re.compile(
    r"\b(github|repo(?:sitory)?|tool|framework|library|sdk|api|startup|company|"
    r"event|calendar|meeting|task|todo|to-do|hermes|agent|automation)\b|"
    r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+",
    re.I,
)
SEARCH_QUERY = re.compile(
    r"(?:^(?:oh[,\s]+)?|\b(?:please|can\s+you|could\s+you|would\s+you)\s+)"
    r"(?:perform\s+(?:a\s+)?web\s+search|search\s+(?:the\s+)?(?:web|internet)|"
    r"search\s+online)\s+"
    r"(?:(?:for|about)\s+|and\s+(?:find|figure\s+out|tell\s+me|check)\s+)?\S+|"
    r"(?:^(?:oh[,\s]+)?|\b(?:please|can\s+you|could\s+you|would\s+you)\s+)search\s+(?:"
    r"(?:(?:the\s+)?(?:web|internet)|online)\s+"
    r"(?:(?:for|about)\s+|and\s+(?:find|figure\s+out|tell\s+me|check)\s+)?\S+|"
    r"(?!the\s+(?:web|internet)[.!?]*$|online[.!?]*$)\S+(?:\s+\S+)*"
    r")",
    re.I,
)
RESEARCH_QUERY = re.compile(
    r"(?:^|[.!?]\s*|\b(?:follow\s*through|memo)\s*[,;:\-]?\s*)"
    r"(?:please\s+)?research\s+(?:(?:this|that|the|a|an|my|our)\s+)?"
    r"(?!(?:this|that|the|a|an|my|our)[.!?]*$)\S+",
    re.I,
)
CHECK_QUERY = re.compile(r"\bcheck\s+it\s+out\s+(?:for\s+|and\s+)?\S+", re.I)
INCOMPLETE_COMMAND = re.compile(
    r"^(?:(?:a\s+)?(?:follow\s*through|memo)\s*[,;:\-]?\s*)?"
    r"(?:(?:please|can\s+you|could\s+you|would\s+you)\s+)?"
    r"(?:research(?:\s+(?:the|a|an|this|that|my|our))?|"
    r"perform\s+(?:a\s+)?web\s+search|search\s+(?:(?:the\s+)?(?:web|internet)|online)|"
    r"search|check\s+it\s+out)\s*[.!?]*$",
    re.I,
)


@dataclass(frozen=True)
class Transcript:
    event_id: str
    occurred_at: str
    text: str
    component_event_ids: tuple[str, ...] = ()


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
        self.waiting_for_context = False

    def add(self, transcript: Transcript, *, monotonic_at: float | None = None) -> Transcript | None:
        observed = time.monotonic() if monotonic_at is None else monotonic_at
        self._segments.append((observed, transcript))
        cutoff = observed - self.window_seconds
        while self._segments and (
            self._segments[0][0] < cutoff or len(self._segments) > self.max_segments
        ):
            self._segments.popleft()

        candidates = [item for _, item in self._segments if item.text.strip()]
        text = " ".join(item.text.strip() for item in candidates)
        self.waiting_for_context = bool(INCOMPLETE_COMMAND.search(text))
        query_complete = SEARCH_QUERY.search(text) or RESEARCH_QUERY.search(text) or CHECK_QUERY.search(
            text
        )
        if not ((ACTION.search(text) and SUBJECT.search(text)) or query_complete):
            return None

        # Dispatch the shortest contiguous suffix that contains both an action
        # and a subject. Ambient speech that happened before the command remains
        # in the complete component archive but never crosses into the
        # operational aggregate sent to Hermes.
        selected = candidates
        for index in range(len(candidates) - 1, -1, -1):
            suffix = candidates[index:]
            suffix_text = " ".join(item.text.strip() for item in suffix)
            suffix_query = (
                SEARCH_QUERY.search(suffix_text)
                or RESEARCH_QUERY.search(suffix_text)
                or CHECK_QUERY.search(suffix_text)
            )
            if (ACTION.search(suffix_text) and SUBJECT.search(suffix_text)) or suffix_query:
                selected = suffix
                text = suffix_text
                break

        occurred_at = selected[0].occurred_at
        component_ids = "\0".join(item.event_id for item in selected)
        digest = hashlib.sha256(f"{component_ids}\0{text}".encode()).hexdigest()
        self._segments.clear()
        self.waiting_for_context = False
        return Transcript(
            event_id=f"adb-omi:aggregate:{digest}",
            occurred_at=occurred_at,
            text=text,
            component_event_ids=tuple(item.event_id for item in selected),
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
    clock_text = match.group("clock")
    # The WHISPER pattern makes fractional seconds optional, so the format must
    # match either shape or strptime raises ValueError and tears down ingestion.
    clock_format = "%H:%M:%S.%f" if "." in clock_text else "%H:%M:%S"
    clock = datetime.strptime(clock_text, clock_format).time()
    # The logcat clock is device-local wall time. Stamp it with the basis
    # timezone and convert to UTC rather than relabeling local time as UTC,
    # which previously skewed every archived timestamp by the UTC offset.
    tz = basis.tzinfo or datetime.now().astimezone().tzinfo
    occurred = datetime.combine(basis.date(), clock, tzinfo=tz).astimezone(UTC)
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
        endpoint: str = "http://127.0.0.1:18765/api/v1/transcripts",
        adb: str = "/usr/bin/adb",
        receipts: Path | None = None,
    ) -> None:
        self.serial = serial
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
        started = time.monotonic()
        response = httpx.post(
            self.endpoint,
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

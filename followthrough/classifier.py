from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Classification:
    actionable: bool
    kind: str
    confidence: float
    reason: str


REPO_RE = re.compile(r"(?:https?://)?github\.com/[\w.-]+/[\w.-]+", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)

ACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("repository", REPO_RE),
    ("follow_up", re.compile(r"\b(follow[ -]?up|reach out|send (?:her|him|them)|email|dm)\b", re.I)),
    ("meeting", re.compile(r"\b(meet|meeting|calendar|schedule|tuesday|wednesday|thursday)\b", re.I)),
    ("company", re.compile(r"\b(startup|company|founder|customer|prospect|lead|buyer|partner)\b", re.I)),
    ("tool", re.compile(r"\b(tool|framework|library|sdk|api|agent|automate|automation|github|repo)\b", re.I)),
    ("task", re.compile(r"\b(todo|to-do|remind|need to|should (?:try|build|research|check))\b", re.I)),
]

NOISE_PATTERNS = [
    re.compile(r"\b(lunch|breakfast|dinner|sandwich|coffee|pizza|weather|traffic)\b", re.I),
    re.compile(r"\b(tastes? (?:good|great)|delicious|hungry|nice day)\b", re.I),
]


def classify(text: str) -> Classification:
    clean = " ".join(text.split())
    if not clean:
        return Classification(False, "empty", 1.0, "No content")

    for kind, pattern in ACTION_PATTERNS:
        if pattern.search(clean):
            confidence = 0.98 if kind == "repository" else 0.88
            if URL_RE.search(clean):
                confidence = min(0.99, confidence + 0.06)
            return Classification(True, kind, confidence, f"Matched {kind} business signal")

    if any(pattern.search(clean) for pattern in NOISE_PATTERNS):
        return Classification(False, "ordinary_life", 0.98, "Ordinary conversation is discarded")

    return Classification(False, "low_signal", 0.82, "No business action or opportunity detected")


from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx


def entity(text: str) -> str:
    url = re.search(r"https?://[^\s]+", text)
    if url:
        return url.group(0).rstrip(".,)")
    repo = re.search(r"\b([A-Za-z][\w.-]*/[A-Za-z][\w.-]+)\b", text)
    if repo:
        return repo.group(1)
    quoted = re.search(r"[\"“'‘]([A-Za-z][\w .-]{2,50})[\"”'’]", text)
    if quoted:
        return quoted.group(1).strip()
    found = re.search(
        r"(?:from|at|about|for|called|named|using|try(?:\s+out)?)\s+([A-Z][\w.-]+(?:\s+[A-Z][\w.-]+)?)",
        text,
    )
    return found.group(1) if found else "the identified opportunity"


_FILLER = re.compile(r"^(?:\s*(?:um+|uh+|so|like|okay|ok|well|you know|yeah|hey)[,\s]+)+", re.I)

_ACTION_MARKERS = {
    "todo": re.compile(
        r"(?i)\b(?:remind\s+me(?:\s+to)?|to[- ]?do|action\s+item|"
        r"(?:i|we)\s+(?:need|have|must)\s+to)\b[:\s-]*([^.!?\n]{1,180})"
    ),
    "contact": re.compile(
        # The clause must name a target (proper noun or the/my/our ...) so a
        # bare verb collision like "message you say" is not promoted.
        r"\b(?i:call|text|message|email|ping|reach\s+out\s+to|follow\s+up\s+with|"
        r"contact|talk\s+to|meet\s+with|introduce\s+me\s+to)\b[:\s-]*"
        r"((?:[A-Z]|(?i:the|my|our)\s)[^.!?\n]{1,179})"
    ),
    "event": re.compile(
        r"(?i)\b(?:schedule|meeting\s+(?:with|about)|calendar|appointment\s+(?:with|for)|"
        r"rsvp\s+(?:to|for)|attend(?:ing)?)\b[:\s-]*([^.!?\n]{1,180})"
    ),
    "web_task": re.compile(
        r"((?i:book|reserve|order|purchase|buy|check\s+the\s+price\s+of|"
        r"fill\s+(?:out|in)|sign\s+(?:me\s+)?up\s+for|apply\s+(?:to|for)|"
        r"find|search)\b[^.!?\n]{1,170})"
    ),
    "goal": re.compile(
        r"(?i)\b(?:goal\s+is(?:\s+to)?|plan(?:ning)?\s+to|aim(?:ing)?\s+to|want\s+to|"
        r"objective\s+is(?:\s+to)?)\b[:\s-]*([^.!?\n]{1,180})"
    ),
}

_BOUNDED_DEFAULT = {
    "todo": "Review and complete the captured commitment",
    "contact": "Follow up on the captured contact",
    "event": "Prepare for the captured event",
    "goal": "Advance the captured goal",
    "web_task": "Complete the captured web task",
}

_CREDENTIAL_PATTERNS = (
    re.compile(
        r"\b(?:api[ _-]?key|access[ _-]?token|token|password|passcode|secret|pin)"
        r"\s*(?:is|=|:)?\s*[^\s,;]+",
        re.I,
    ),
    re.compile(r"\b(?:sk|ghp|github_pat|hk|gsk)_[A-Za-z0-9_-]{10,}\b", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{10,}=*", re.I),
)


def operational_entity(text: str, category: str) -> str:
    """Return the minimum relevant action text permitted outside the archive.

    Repository/tool research normally needs only a URL or named entity. Tasks,
    events, contacts, and goals require a bounded actionable clause; keeping
    that clause is intentional operational memory, while the complete source
    transcript remains in the complete archive.
    """

    named = entity(text)
    # A web task is the whole bounded command, not a single extracted name.
    if named != "the identified opportunity" and category != "web_task":
        return named
    if category not in _ACTION_MARKERS:
        return named
    clean = " ".join(text.split())
    clean = _FILLER.sub("", clean)
    for pattern in _CREDENTIAL_PATTERNS:
        clean = pattern.sub("[redacted credential]", clean)
    marker = _ACTION_MARKERS[category].search(clean)
    if marker and marker.group(1).strip():
        return marker.group(1).strip()[:180]
    # A relevant signal without a bounded action clause stays useful but must
    # not leak the surrounding conversation into operational memory.
    return _BOUNDED_DEFAULT[category]


def linkup(text: str, api_key: str) -> dict[str, Any]:
    query = f"{entity(text)} company, product, docs, competitors, and fit for a founder workflow"
    if not api_key:
        return {"configured": False, "query": query, "answer": "Linkup is awaiting the event key; the research slot is ready."}
    started = time.perf_counter()
    response = httpx.post("https://api.linkup.so/v1/search", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={"q": query, "depth": "standard", "outputType": "sourcedAnswer", "includeInlineCitations": True}, timeout=25)
    response.raise_for_status()
    data = response.json()
    return {"configured": True, "query": query, "answer": data.get("answer") or data.get("content") or str(data), "sources": data.get("sources", []), "latency_ms": int((time.perf_counter() - started) * 1000)}


def elevenlabs(text: str, key: str, voice_id: str, output_dir: Path) -> Path | None:
    if not key or not voice_id:
        return None
    response = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}", headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}, json={"text": text[:1800], "model_id": "eleven_flash_v2_5"}, timeout=35)
    response.raise_for_status()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"brief-{int(time.time())}.mp3"
    path.write_bytes(response.content)
    return path


def convex_event(payload: dict[str, Any], url: str, deploy_key: str) -> bool:
    if not url:
        return False
    headers = {"Content-Type": "application/json"}
    if deploy_key:
        headers["Authorization"] = f"Bearer {deploy_key}"
    try:
        return httpx.post(url.rstrip("/") + "/api/followthrough/event", headers=headers, json=payload, timeout=12).is_success
    except httpx.HTTPError:
        return False

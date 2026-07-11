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
    found = re.search(r"(?:from|at|about|for)\s+([A-Z][\w.-]+(?:\s+[A-Z][\w.-]+)?)", text)
    return found.group(1) if found else text[:100]


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

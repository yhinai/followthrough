from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .bus import bus
from .classifier import classify
from .config import settings
from .crew import Crew
from .models import RoleIn, SignalIn, SignupIn
from .store import Store

settings.reports_dir.mkdir(parents=True, exist_ok=True)
store = Store(settings.db_path)
crew = Crew(store, settings)
app = FastAPI(title="Followthrough", version="0.1.0")
static = Path(__file__).parent / "static"
if static.is_dir():
    app.mount("/static", StaticFiles(directory=static), name="static")


def auth(token: str | None) -> None:
    if settings.webhook_token and not hmac.compare_digest(token or "", settings.webhook_token):
        raise HTTPException(status_code=401, detail="invalid webhook token")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (static / "index.html").read_text()


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {"ok": True, "service": "followthrough", "hermes": True, "cloudflare": True}


@app.post("/api/signup")
async def signup(payload: SignupIn) -> dict[str, bool]:
    store.signup(str(payload.email), payload.source)
    return {"ok": True}


@app.post("/api/roles")
async def add_role(payload: RoleIn) -> dict[str, object]:
    return store.add_role(payload.name, payload.job, payload.tools, payload.guardrails)


@app.get("/api/roles")
async def roles() -> list[dict[str, object]]:
    return store.roles()


async def process_signal(payload: SignalIn) -> dict[str, object]:
    if not payload.consent:
        raise HTTPException(status_code=400, detail="explicit consent is required")
    c = classify(payload.text)
    if not c.actionable:
        run_id = store.create_run(payload.text, payload.source, c.kind, "Discarded ordinary conversation")
        store.update_run(run_id, status="discarded", finished_at=__import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(), success=1)
        store.add_step(run_id, "signal_triage", "discarded", c.reason, {"kind": c.kind})
        await bus.publish("run_discarded", {"run_id": run_id, "kind": c.kind, "reason": c.reason})
        return {"run_id": run_id, "status": "discarded", "classification": c.__dict__, "message": "Noise discarded before persistence."}
    run_id = store.create_run(payload.text, payload.source, c.kind, "Followthrough signal")
    if payload.email:
        store.activate(str(payload.email))
    result = await asyncio.to_thread(crew.process, run_id, payload.text, c)
    await bus.publish("run_completed", result)
    return result


@app.post("/api/signals")
async def signal(payload: SignalIn) -> dict[str, object]:
    return await process_signal(payload)


@app.post("/api/webhooks/omi")
async def omi(request: Request, x_followthrough_token: str | None = Header(default=None)) -> dict[str, object]:
    auth(x_followthrough_token)
    data = await request.json()
    text = data.get("transcript") or data.get("text") or (data.get("memory") or {}).get("text")
    if not text:
        return {"ok": True, "ignored": "no transcript"}
    return await process_signal(SignalIn(text=str(text), source="omi", consent=True))


@app.get("/api/runs")
async def runs() -> list[dict[str, object]]:
    return store.list_runs()


@app.get("/api/runs/{run_id}")
async def run(run_id: str) -> dict[str, object]:
    found = store.get_run(run_id)
    if not found:
        raise HTTPException(status_code=404, detail="run not found")
    return found


@app.get("/api/metrics")
async def metrics() -> dict[str, object]:
    return {**store.metrics(), "integrations": {"hermes": True, "cloudflare": True, "convex": bool(settings.convex_url), "linkup": bool(settings.linkup_api_key), "elevenlabs": bool(settings.elevenlabs_api_key), "dodo": bool(settings.dodo_payments_api_key)}}


@app.get("/api/audio/{filename}")
async def audio(filename: str) -> FileResponse:
    path = settings.db_path.parent / "audio" / filename
    if not path.is_file() or path.parent != (settings.db_path.parent / "audio"):
        raise HTTPException(status_code=404, detail="audio not found")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/reports/{filename}")
async def report(filename: str) -> FileResponse:
    path = settings.reports_dir / filename
    if not path.is_file() or path.parent != settings.reports_dir:
        raise HTTPException(status_code=404, detail="report not found")
    return FileResponse(path, media_type="text/markdown")


@app.get("/api/events")
async def events() -> StreamingResponse:
    return StreamingResponse(bus.stream(), media_type="text/event-stream")

#!/usr/bin/env python3
"""Local desktop API for Followthrough: a free, self-hosted alternative to a
paid remote desktop provider.

Serves the same contract the DesktopRouter's local plane expects
(health/screenshot/click/type/key/scroll/drag) against an X display driven by
xdotool. Bind to loopback only; the token is a shared secret, not a session.
"""

from __future__ import annotations

import base64
import os
import subprocess
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DISPLAY = os.environ.get("FOLLOWTHROUGH_DESKTOP_DISPLAY", ":99")
TOKEN = os.environ.get("ORGO_DESKTOP_API_TOKEN", "").strip()
ENV = {**os.environ, "DISPLAY": DISPLAY}

BUTTONS = {"left": "1", "middle": "2", "right": "3"}
SCROLL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}
# xdotool accepts keysyms and chords; reject anything else so a model cannot
# smuggle shell syntax through the key name.
KEY_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-")


def run(*args: str, timeout: int = 20) -> None:
    result = subprocess.run(args, env=ENV, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"{args[0]} failed: {result.stderr[:200]}")


def authorize(authorization: str | None = Header(default=None)) -> None:
    if not TOKEN:
        raise HTTPException(status_code=503, detail="ORGO_DESKTOP_API_TOKEN is not configured")
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or value.strip() != TOKEN:
        raise HTTPException(status_code=401, detail="desktop authentication required")


app = FastAPI(title="Followthrough Desktop API", docs_url=None, redoc_url=None)


class ClickIn(BaseModel):
    x: int = Field(ge=0, le=10_000)
    y: int = Field(ge=0, le=10_000)
    button: Literal["left", "middle", "right"] = "left"
    double: bool = False


class DragIn(BaseModel):
    start_x: int = Field(ge=0, le=10_000)
    start_y: int = Field(ge=0, le=10_000)
    end_x: int = Field(ge=0, le=10_000)
    end_y: int = Field(ge=0, le=10_000)


class TypeIn(BaseModel):
    text: str = Field(max_length=4_000)
    delay_ms: int = Field(default=12, ge=0, le=1_000)


class KeyIn(BaseModel):
    key: str = Field(min_length=1, max_length=60)


class ScrollIn(BaseModel):
    direction: Literal["up", "down", "left", "right"]
    amount: int = Field(default=3, ge=1, le=25)
    x: int = Field(default=640, ge=0, le=10_000)
    y: int = Field(default=360, ge=0, le=10_000)


@app.get("/health")
def health() -> dict[str, object]:
    probe = subprocess.run(
        ["xdotool", "getdisplaygeometry"], env=ENV, capture_output=True, text=True, timeout=5
    )
    if probe.returncode != 0:
        raise HTTPException(status_code=503, detail="X display is unavailable")
    width, _, height = probe.stdout.strip().partition(" ")
    return {
        "status": "ok",
        "provider": "followthrough-local-x11",
        "display": DISPLAY,
        "width": int(width),
        "height": int(height),
    }


@app.get("/screenshot", dependencies=[Depends(authorize)])
def screenshot() -> dict[str, str]:
    capture = subprocess.run(
        ["import", "-display", DISPLAY, "-window", "root", "png:-"],
        env=ENV,
        capture_output=True,
        timeout=30,
    )
    if capture.returncode != 0 or not capture.stdout:
        raise HTTPException(status_code=502, detail="screen capture failed")
    return {"image": base64.b64encode(capture.stdout).decode()}


@app.post("/click", dependencies=[Depends(authorize)])
def click(payload: ClickIn) -> dict[str, object]:
    run("xdotool", "mousemove", "--sync", str(payload.x), str(payload.y))
    button = BUTTONS[payload.button]
    run("xdotool", "click", "--repeat", "2" if payload.double else "1", button)
    return {"ok": True, "x": payload.x, "y": payload.y, "button": payload.button}


@app.post("/drag", dependencies=[Depends(authorize)])
def drag(payload: DragIn) -> dict[str, object]:
    run("xdotool", "mousemove", "--sync", str(payload.start_x), str(payload.start_y))
    run("xdotool", "mousedown", "1")
    run("xdotool", "mousemove", "--sync", str(payload.end_x), str(payload.end_y))
    run("xdotool", "mouseup", "1")
    return {"ok": True}


@app.post("/type", dependencies=[Depends(authorize)])
def type_text(payload: TypeIn) -> dict[str, object]:
    run("xdotool", "type", "--delay", str(payload.delay_ms), "--", payload.text, timeout=60)
    return {"ok": True, "characters": len(payload.text)}


@app.post("/key", dependencies=[Depends(authorize)])
def key(payload: KeyIn) -> dict[str, object]:
    if not set(payload.key) <= KEY_ALLOWED:
        raise HTTPException(status_code=422, detail="unsupported key name")
    run("xdotool", "key", "--", payload.key)
    return {"ok": True, "key": payload.key}


@app.post("/scroll", dependencies=[Depends(authorize)])
def scroll(payload: ScrollIn) -> dict[str, object]:
    run("xdotool", "mousemove", "--sync", str(payload.x), str(payload.y))
    run("xdotool", "click", "--repeat", str(payload.amount), SCROLL_BUTTONS[payload.direction])
    return {"ok": True, "direction": payload.direction, "amount": payload.amount}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8080")), log_level="warning")

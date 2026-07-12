"""Small loopback-only X11 desktop API used by Followthrough on Spark."""

from __future__ import annotations

import base64
import io
import os
import subprocess
import time
from contextlib import contextmanager
from typing import Iterator, Literal

from fastapi import FastAPI, Header, HTTPException
from PIL import ImageGrab
from pydantic import BaseModel, Field
from Xlib import X, XK, display
from Xlib.ext import xtest


DISPLAY = os.getenv("DISPLAY", ":99")
TOKEN = os.getenv("ORGO_DESKTOP_API_TOKEN", "").strip()
app = FastAPI(title="Followthrough Local Desktop", docs_url=None, redoc_url=None)


class Click(BaseModel):
    x: int = Field(ge=0, le=10000)
    y: int = Field(ge=0, le=10000)
    button: Literal["left", "middle", "right"] = "left"
    double: bool = False


class Drag(BaseModel):
    start_x: int = Field(ge=0, le=10000)
    start_y: int = Field(ge=0, le=10000)
    end_x: int = Field(ge=0, le=10000)
    end_y: int = Field(ge=0, le=10000)


class TypeText(BaseModel):
    text: str = Field(max_length=10000)
    delay_ms: int = Field(default=12, ge=0, le=1000)


class Key(BaseModel):
    key: str = Field(min_length=1, max_length=100)


class Scroll(BaseModel):
    direction: Literal["up", "down", "left", "right"]
    amount: int = Field(default=3, ge=1, le=100)
    x: int = Field(default=640, ge=0, le=10000)
    y: int = Field(default=360, ge=0, le=10000)


def _authorize(authorization: str | None) -> None:
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="invalid desktop token")


@contextmanager
def _display() -> Iterator[display.Display]:
    connection = display.Display(DISPLAY)
    try:
        yield connection
    finally:
        connection.close()


def _sync(connection: display.Display) -> None:
    connection.sync()
    time.sleep(0.03)


def _button(connection: display.Display, button: int) -> None:
    xtest.fake_input(connection, X.ButtonPress, button)
    xtest.fake_input(connection, X.ButtonRelease, button)
    _sync(connection)


def _keysym_name(value: str) -> str:
    aliases = {
        "ENTER": "Return", "RETURN": "Return", "ESC": "Escape", "ESCAPE": "Escape",
        "TAB": "Tab", "BACKSPACE": "BackSpace", "DELETE": "Delete", "SPACE": "space",
        "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
        "HOME": "Home", "END": "End", "PAGEUP": "Page_Up", "PAGEDOWN": "Page_Down",
    }
    return aliases.get(value.upper(), value)


def _press_keysym(connection: display.Display, name: str, *, shifted: bool = False) -> None:
    keysym = XK.string_to_keysym(_keysym_name(name))
    if not keysym and len(name) == 1:
        keysym = ord(name)
    keycode = connection.keysym_to_keycode(keysym)
    if not keycode:
        raise HTTPException(status_code=422, detail=f"unsupported key: {name}")
    shift = connection.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
    if shifted:
        xtest.fake_input(connection, X.KeyPress, shift)
    xtest.fake_input(connection, X.KeyPress, keycode)
    xtest.fake_input(connection, X.KeyRelease, keycode)
    if shifted:
        xtest.fake_input(connection, X.KeyRelease, shift)


@app.get("/health")
def health(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    try:
        with _display() as connection:
            geometry = connection.screen().root.get_geometry()
        return {
            "ok": True,
            "provider": "spark-local",
            "display": DISPLAY,
            "width": geometry.width,
            "height": geometry.height,
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"X11 unavailable: {type(exc).__name__}") from exc


@app.get("/screenshot")
def screenshot(authorization: str | None = Header(default=None)) -> dict[str, str]:
    _authorize(authorization)
    try:
        image = ImageGrab.grab(xdisplay=DISPLAY)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return {"image": base64.b64encode(output.getvalue()).decode("ascii")}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"screenshot failed: {type(exc).__name__}") from exc


@app.post("/click")
def click(payload: Click, authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    buttons = {"left": 1, "middle": 2, "right": 3}
    with _display() as connection:
        xtest.fake_input(connection, X.MotionNotify, x=payload.x, y=payload.y)
        _button(connection, buttons[payload.button])
        if payload.double:
            _button(connection, buttons[payload.button])
    return {"ok": True, "x": payload.x, "y": payload.y}


@app.post("/drag")
def drag(payload: Drag, authorization: str | None = Header(default=None)) -> dict[str, bool]:
    _authorize(authorization)
    with _display() as connection:
        xtest.fake_input(connection, X.MotionNotify, x=payload.start_x, y=payload.start_y)
        xtest.fake_input(connection, X.ButtonPress, 1)
        connection.sync()
        steps = 12
        for step in range(1, steps + 1):
            x = payload.start_x + (payload.end_x - payload.start_x) * step // steps
            y = payload.start_y + (payload.end_y - payload.start_y) * step // steps
            xtest.fake_input(connection, X.MotionNotify, x=x, y=y)
            connection.sync()
            time.sleep(0.01)
        xtest.fake_input(connection, X.ButtonRelease, 1)
        _sync(connection)
    return {"ok": True}


@app.post("/type")
def type_text(payload: TypeText, authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    with _display() as connection:
        for character in payload.text:
            if character == "\n":
                _press_keysym(connection, "Return")
            elif character == "\t":
                _press_keysym(connection, "Tab")
            else:
                lower = character.lower()
                _press_keysym(connection, lower, shifted=character.isupper())
            connection.sync()
            if payload.delay_ms:
                time.sleep(payload.delay_ms / 1000)
    return {"ok": True, "characters": len(payload.text)}


@app.post("/key")
def key(payload: Key, authorization: str | None = Header(default=None)) -> dict[str, bool]:
    _authorize(authorization)
    parts = [part.strip() for part in payload.key.replace("+", " ").split() if part.strip()]
    modifiers = {"CTRL": "Control_L", "CONTROL": "Control_L", "ALT": "Alt_L", "SHIFT": "Shift_L", "META": "Super_L", "SUPER": "Super_L"}
    with _display() as connection:
        held: list[int] = []
        for part in parts[:-1]:
            name = modifiers.get(part.upper())
            if not name:
                raise HTTPException(status_code=422, detail=f"unsupported modifier: {part}")
            code = connection.keysym_to_keycode(XK.string_to_keysym(name))
            xtest.fake_input(connection, X.KeyPress, code)
            held.append(code)
        _press_keysym(connection, parts[-1])
        for code in reversed(held):
            xtest.fake_input(connection, X.KeyRelease, code)
        _sync(connection)
    return {"ok": True}


@app.post("/scroll")
def scroll(payload: Scroll, authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    buttons = {"up": 4, "down": 5, "left": 6, "right": 7}
    with _display() as connection:
        xtest.fake_input(connection, X.MotionNotify, x=payload.x, y=payload.y)
        for _ in range(payload.amount):
            _button(connection, buttons[payload.direction])
    return {"ok": True, "amount": payload.amount}


def _desktop_service(command: str) -> dict[str, object]:
    mapped = {"ensure-running": "start", "start": "start", "stop": "stop", "restart": "restart"}
    completed = subprocess.run(
        ["systemctl", "--user", mapped[command], "followthrough-desktop-session.service"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode:
        raise HTTPException(status_code=503, detail=f"desktop lifecycle {command} failed")
    return {"ok": True, "operation": command}


@app.post("/ensure-running")
def ensure_running(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    return _desktop_service("ensure-running")


@app.post("/start")
def start(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    return _desktop_service("start")


@app.post("/stop")
def stop(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    return _desktop_service("stop")


@app.post("/restart")
def restart(authorization: str | None = Header(default=None)) -> dict[str, object]:
    _authorize(authorization)
    return _desktop_service("restart")

from __future__ import annotations

import base64
import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from followthrough.app import create_app
from followthrough.config import Settings
from followthrough.desktop import DesktopRouter, DesktopUnavailable, frame_fingerprint
from followthrough.store import Store


def png(color: str) -> bytes:
    image = Image.new("RGB", (128, 72), color=color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def screenshot(color: str) -> dict[str, str]:
    return {"image": base64.b64encode(png(color)).decode()}


@pytest.mark.asyncio
async def test_local_plane_is_preferred_and_click_verifies_visual_change(tmp_path: Path) -> None:
    clicked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal clicked
        assert request.url.host == "local.test"
        if request.url.path == "/health":
            return httpx.Response(200, json={"service": "orgo-desktop-api"})
        assert request.headers["Authorization"] == "Bearer local-token"
        if request.url.path == "/screenshot":
            return httpx.Response(200, json=screenshot("blue" if clicked else "red"))
        if request.url.path == "/click":
            clicked = True
            return httpx.Response(200, json={"success": True})
        raise AssertionError(request.url)

    settings = Settings(
        db_path=tmp_path / "state.db",
        archive_db_path=tmp_path / "archive.db",
        reports_dir=tmp_path / "reports",
        jobs_dir=tmp_path / "jobs",
        audio_dir=tmp_path / "audio",
        desktop_api_base="https://local.test",
        desktop_api_token="local-token",
        orgo_api_key="remote-token",
        orgo_default_computer_id="remote-id",
    )
    router = DesktopRouter(Store(settings.db_path), settings, httpx.MockTransport(handler))
    doctor = await router.doctor()
    assert doctor["ready"] is True
    assert doctor["provider"] == "spark-local"
    receipt = await router.click(20, 30)
    assert receipt["provider"] == "spark-local"
    assert receipt["visual_changed"] is True
    assert receipt["noop"] is False
    assert router.store.list_desktop_actions()[0]["action"] == "click"


@pytest.mark.asyncio
async def test_remote_default_routing_noop_and_bounded_lifecycle(tmp_path: Path) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.host == "local.test":
            return httpx.Response(503)
        assert request.headers["Authorization"] == "Bearer remote-token"
        if request.url.path.endswith("/screenshot"):
            return httpx.Response(200, json=screenshot("green"))
        return httpx.Response(200, json={"success": True, "status": "running"})

    settings = Settings(
        db_path=tmp_path / "state.db",
        archive_db_path=tmp_path / "archive.db",
        reports_dir=tmp_path / "reports",
        jobs_dir=tmp_path / "jobs",
        audio_dir=tmp_path / "audio",
        desktop_api_base="https://local.test",
        orgo_api_base="https://remote.test/api",
        orgo_api_key="remote-token",
        orgo_default_computer_id="computer-1",
    )
    router = DesktopRouter(Store(settings.db_path), settings, httpx.MockTransport(handler))
    receipt = await router.key("Enter")
    assert receipt["provider"] == "orgo-remote"
    assert receipt["computer_id"] == "computer-1"
    assert receipt["noop"] is True
    with pytest.raises(ValueError, match="confirmed=true"):
        await router.lifecycle("restart")
    lifecycle = await router.lifecycle("restart", confirmed=True)
    assert lifecycle["action"] == "lifecycle.restart"
    assert "/api/computers/computer-1/restart" in paths


@pytest.mark.asyncio
async def test_unconfigured_router_fails_closed(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "state.db",
        archive_db_path=tmp_path / "archive.db",
        reports_dir=tmp_path / "reports",
        jobs_dir=tmp_path / "jobs",
        audio_dir=tmp_path / "audio",
        desktop_api_base="https://local.test",
    )
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    router = DesktopRouter(Store(settings.db_path), settings, transport)
    with pytest.raises(DesktopUnavailable):
        await router.screenshot()
    assert (await router.doctor())["ready"] is False


def test_frame_fingerprint_ignores_top_panel() -> None:
    first = Image.new("RGB", (100, 60), "white")
    second = first.copy()
    for x in range(100):
        for y in range(20):
            second.putpixel((x, y), (0, 0, 0))
    one, two = io.BytesIO(), io.BytesIO()
    first.save(one, format="PNG")
    second.save(two, format="PNG")
    assert frame_fingerprint(one.getvalue(), top_skip=28) == frame_fingerprint(
        two.getvalue(), top_skip=28
    )


def test_desktop_http_surface_reports_unconfigured(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.desktop_api_base = "http://127.0.0.1:1"
    app = create_app(settings)
    with TestClient(app) as client:
        doctor = client.get("/api/desktop/doctor")
        assert doctor.status_code == 200
        assert doctor.json()["ready"] is False
        assert client.get("/api/desktop/screenshot").status_code == 503
        assert client.get("/api/desktop/actions").json() == []



def test_desktop_screenshot_is_served_for_the_dashboard_panel(
    configured_settings, monkeypatch
) -> None:
    """The panel paints from this endpoint. When it stops serving an image the
    viewer goes blank and the desktop only looks dead."""
    settings, _, _ = configured_settings
    image = png("green")

    async def fake_screenshot(self, computer_id=None):
        return image, {
            "provider": "spark-local",
            "computer_id": None,
            "width": 1280,
            "height": 720,
            "fingerprint": "abc",
        }

    monkeypatch.setattr(DesktopRouter, "screenshot", fake_screenshot)
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/desktop/screenshot")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.content == image



def test_operator_input_can_skip_verification(configured_settings, monkeypatch) -> None:
    """Human input is watched by a human, so the dashboard skips the before and
    after screenshots that would otherwise triple every click's latency."""
    settings, _, _ = configured_settings
    shots: list[bool] = []

    async def fake_screenshot(self, computer_id=None):
        shots.append(True)
        return png("green"), {
            "provider": "spark-local", "computer_id": None,
            "width": 1280, "height": 720, "fingerprint": "abc",
        }

    async def fake_request(self, target, method, action, body=None):
        return {"ok": True}

    monkeypatch.setattr(DesktopRouter, "screenshot", fake_screenshot)
    monkeypatch.setattr(DesktopRouter, "_request", fake_request)

    with TestClient(create_app(settings)) as client:
        response = client.post("/api/desktop/click", json={"x": 10, "y": 10, "verify": False})

    assert response.status_code == 200
    assert response.json()["visual_changed"] is None
    assert shots == []

from pathlib import Path

from fastapi.testclient import TestClient

from followthrough.app import create_app


ROOT = Path(__file__).parents[1]
HTML = ROOT / "followthrough/static/index.html"
CSS = ROOT / "followthrough/static/dashboard.css"
JS = ROOT / "followthrough/static/app.js"


def test_dashboard_has_accessible_command_surface():
    html = HTML.read_text()
    js = JS.read_text()
    css = CSS.read_text()

    assert 'aria-live="polite"' in html
    assert 'id="toast"' in html
    assert 'id="pause"' in html
    assert "showToast" in js
    assert ".command-bar" in css


def test_embedded_desktop_viewer_allows_same_origin_framing(configured_settings):
    settings, _, _ = configured_settings
    with TestClient(create_app(settings)) as client:
        response = client.get("/static/desktop-viewer.html")
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]

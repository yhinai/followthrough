from pathlib import Path

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


def test_desktop_viewer_uses_the_reliable_screenshot_stream():
    html = HTML.read_text()
    js = JS.read_text()
    assert 'id="desktopFrame"' in html
    assert "/api/desktop/screenshot?t=" in js
    assert '<iframe id="desktopLive"' not in html
    assert 'id="desktopLive"' not in html

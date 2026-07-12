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


def test_transcript_lives_in_its_own_tab_not_the_main_page():
    html = HTML.read_text()
    js = JS.read_text()
    css = CSS.read_text()

    assert 'id="tabTranscript"' in html
    assert 'id="tabOverview"' in html
    assert 'id="transcriptView"' in html
    assert 'id="transcriptLive"' in html
    assert 'id="transcriptEntries"' in html
    # The word-by-word feed moved off the main page.
    assert 'id="activity"' not in html
    assert 'id="feedFilter"' not in html
    assert ".view-tab" in css
    assert ".transcript-row" in css
    assert "setView" in js


def test_transcript_streams_tokens_newest_first_in_pacific_time():
    js = JS.read_text()

    assert "America/Los_Angeles" in js  # actual Pacific-time stamps per entry
    assert "transcript_partial" in js  # live token stream over SSE
    assert "transcript_archived" in js  # finalized entries arrive without polling
    assert "recognition.interimResults = true" in js
    assert "/api/v1/transcripts/partial" in js
    assert "/api/transcript?" in js
    assert "insertTranscriptEntry" in js  # newest lands on top, order kept strict
    assert 'params.set("before", transcriptCursor.receivedAt)' in js
    assert 'params.set("before_id", transcriptCursor.id)' in js


def test_transcript_stream_recovers_from_lossy_sse_delivery():
    js = JS.read_text()

    # Page-1 merge on every (re)connect, tab open, and tab-visible transition.
    assert "refreshTranscriptHead" in js
    assert 'liveEvents.addEventListener("ready"' in js
    assert "visibilitychange" in js
    # Events arriving during the initial fetch are buffered, not dropped.
    assert "pendingArchived" in js
    # A straggler partial cannot resurrect a finalized utterance's bubble.
    assert "finishedUtterances" in js
    # The live map is bounded and repaints are coalesced.
    assert "LIVE_PARTIAL_CAP" in js
    assert "scheduleLiveRender" in js
    # Bundled final+interim recognition events are walked, not last-only.
    assert "event.resultIndex" in js
    # Brand link and hash edits keep the URL and visible view in sync.
    assert "hashchange" in js


def test_needs_attention_is_not_rendered_as_active_work():
    js = JS.read_text()

    assert '"failed", "needs_attention"' in js
    assert '"failed","needs_attention"' in js


def test_journey_uses_one_server_linked_contract() -> None:
    html = HTML.read_text()
    js = JS.read_text()

    assert 'safeJson("/api/journey"' in js
    assert "function renderJourney(journey)" in js
    assert "activity.find" not in js
    for label in ("Heard", "Relevant", "Delegated", "Browsing", "Verified", "Discord", "Phone"):
        assert f"<li>{label}</li>" in html


def test_workspace_is_a_third_responsive_editable_view() -> None:
    html = HTML.read_text()
    js = JS.read_text()
    css = CSS.read_text()

    assert 'id="tabWorkspace"' in html
    assert 'id="workspaceView"' in html
    for group in ("Research", "Backlog", "Tasks & reminders", "Events & calendar"):
        assert group in html
    assert 'safeJson("/api/workspace"' in js
    assert 'method:"PATCH"' in js
    assert 'method:"DELETE"' in js
    assert ".workspace-board" in css
    assert "@media(max-width:700px)" in css

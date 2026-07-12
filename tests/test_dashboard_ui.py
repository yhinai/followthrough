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
    assert "/api/v1/livekit/session" in js
    assert "setMicrophoneEnabled(true)" in js
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
    # Browser capture uses the same LiveKit agent transport as Memo.
    assert "RoomEvent.TrackSubscribed" in js
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


def test_premium_ui_exposes_phone_origin_truthful_health_and_transcript_filters() -> None:
    html = HTML.read_text()
    js = JS.read_text()
    css = CSS.read_text()

    assert 'class="listening-origin"' in html
    assert 'id="overviewLiveText"' in html
    assert "Memo · Samsung Flip" in html
    assert 'id="connectionState"' in html
    assert 'safeJson("/api/v1/devices"' in js
    assert "function renderDevicePresence" in js
    assert 'memo.last_transcript_activity_at' in js
    assert 'memo ? "Reconnecting" : "Phone not connected"' in js
    assert 'failures.size ? "Partial connection"' in js
    assert "Reconnecting" in js
    assert 'failures.has("/api/metrics") ? "—"' in js
    assert 'id="transcriptSearch"' in html
    assert 'id="transcriptFilter"' in html
    assert "function transcriptClass" in js
    assert "Memo command" in js
    assert ".transcript-toolbar" in css
    assert ".listening-origin" in css
    assert 'class="product-intro"' not in html
    assert "Say it once." not in html


def test_engineering_proof_is_progressively_disclosed_but_remains_live() -> None:
    html = HTML.read_text()
    css = CSS.read_text()

    assert '<details class="panel desktop-panel disclosure-panel">' in html
    assert '<details class="panel workbench disclosure-panel">' in html
    assert '<details class="panel memory-panel disclosure-panel">' in html
    assert 'id="desktopFrame"' in html
    assert "Progressive disclosure" in css
    assert ".disclosure-panel[open]" in css


def test_tabs_use_roving_keyboard_focus() -> None:
    html = HTML.read_text()
    js = JS.read_text()

    assert 'role="tablist"' in html
    assert html.count('role="tab"') == 3
    assert 'setAttribute("aria-selected"' in js
    assert '.tabIndex = transcript ? 0 : -1' in js
    assert 'event.key === "ArrowRight"' in js


def test_browser_microphone_uses_vendored_livekit_with_remote_audio() -> None:
    html = HTML.read_text()
    js = JS.read_text()

    assert '/static/vendor/livekit-client.umd.js' in html
    assert 'id="micStatus"' in html
    assert 'id="remoteAudio"' in html
    assert 'device_id:"dashboard-web"' in js
    assert 'surface:"dashboard"' in js
    assert 'response_mode:"discord_and_voice"' in js
    assert "track.attach()" in js
    assert "setMicrophoneEnabled(false)" in js
    assert "SpeechRecognition" not in js


def test_capture_controls_require_visible_user_consent() -> None:
    html = HTML.read_text()
    js = JS.read_text()

    assert 'id="consent" type="checkbox"' in html
    assert 'id="submit" class="primary" type="button" disabled' in html
    assert 'id="listen" class="secondary livekit-mic" type="button"' in html
    assert "function requireConsent()" in js
    assert '$("#consent").addEventListener("change"' in js

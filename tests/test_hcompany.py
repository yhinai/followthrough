from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from followthrough.config import Settings
from followthrough.hcompany import HCompanyExecutor
from followthrough.store import Store


def configured(tmp_path: Path, *, key: str = "") -> Settings:
    return Settings(
        db_path=tmp_path / "state.db",
        archive_db_path=tmp_path / "archive.db",
        reports_dir=tmp_path / "reports",
        jobs_dir=tmp_path / "jobs",
        audio_dir=tmp_path / "audio",
        h_api_key=key,
        h_api_base="https://h.test/api/v2",
    )


def test_step_count_never_regresses_when_settled_snapshot_is_smaller() -> None:
    from followthrough.hcompany import _step_count

    assert _step_count({"step_count": 2}, 10) == 10
    assert _step_count({"steps": ["one", "two"]}, 10) == 10


@pytest.mark.asyncio
async def test_missing_key_settles_as_configuration_required(tmp_path: Path) -> None:
    settings = configured(tmp_path)
    store = Store(settings.db_path)
    events: list[tuple[str, dict]] = []

    async def publish(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    row = store.create_computer_session(task="Find a flight on the web", agent=settings.h_agent)
    await HCompanyExecutor(store, settings, publish).run(row["id"], row["task"])
    settled = store.computer_session(row["id"])
    assert settled["state"] == "configuration_required"
    assert events[-1][0] == "computer_use_failed"


@pytest.mark.asyncio
async def test_session_progress_and_agent_view_are_persisted(tmp_path: Path) -> None:
    settings = configured(tmp_path, key="h-test-key")
    store = Store(settings.db_path)
    events: list[str] = []
    changes_calls = 0

    async def publish(kind: str, payload: dict) -> None:
        events.append(kind)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal changes_calls
        assert request.headers["Authorization"] == "Bearer h-test-key"
        if request.method == "POST" and request.url.path.endswith("/sessions"):
            return httpx.Response(
                200,
                json={"id": "h-session-1", "status": "pending", "agent_view_url": "https://view.test/1"},
            )
        if request.url.path.endswith("/changes"):
            changes_calls += 1
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "new_events": [{"type": "click", "description": "Opened the result"}],
                    "answer": "The browser task completed.",
                    "step_count": 3,
                },
            )
        if request.method == "GET" and request.url.path.endswith("/sessions/h-session-1"):
            return httpx.Response(
                200,
                json={
                    "id": "h-session-1",
                    "status": "completed",
                    "latest_answer": "The browser task completed.",
                    "agent_view_url": "https://view.test/1",
                    "step_count": 3,
                },
            )
        raise AssertionError(request.url)

    row = store.create_computer_session(task="Find a flight on the web", agent=settings.h_agent)
    executor = HCompanyExecutor(store, settings, publish, httpx.MockTransport(handler))
    await executor.run(row["id"], row["task"])
    settled = store.computer_session(row["id"])
    assert changes_calls == 1
    assert settled["state"] == "completed"
    assert settled["h_session_id"] == "h-session-1"
    assert settled["agent_view_url"] == "https://view.test/1"
    assert settled["step_count"] == 3
    assert settled["latest_answer"] == "The browser task completed."
    assert events == ["computer_use_started", "computer_use_progress", "computer_use_completed"]

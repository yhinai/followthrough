"""Token streaming and the transcript tab's feed API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from followthrough.app import create_app
from followthrough.bus import bus


@pytest.fixture
def bus_events(monkeypatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    original = bus.publish

    async def record(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))
        await original(event_type, payload)

    monkeypatch.setattr(bus, "publish", record)
    return events


def _client(configured_settings) -> TestClient:
    settings, _, _ = configured_settings
    return TestClient(create_app(settings))


def test_partial_streams_to_bus_without_touching_archive(configured_settings, bus_events) -> None:
    with _client(configured_settings) as client:
        response = client.post(
            "/api/v1/transcripts/partial",
            json={
                "utterance_id": "utt-stream-0001",
                "device_id": "dashboard",
                "source": "voice",
                "seq": 3,
                "text": "check the price of",
                "consent": True,
            },
        )
        assert response.status_code == 202
        body = response.json()
        assert body["archived"] is False
        assert body["utterance_id"] == "utt-stream-0001"
        assert client.get("/api/transcript").json() == []
        assert client.get("/api/activity").json() == []
    partials = [payload for kind, payload in bus_events if kind == "transcript_partial"]
    assert len(partials) == 1
    assert partials[0]["text"] == "check the price of"
    assert partials[0]["seq"] == 3
    assert partials[0]["utterance_id"] == "utt-stream-0001"
    assert partials[0]["received_at"]


def test_partial_requires_consent(configured_settings) -> None:
    with _client(configured_settings) as client:
        response = client.post(
            "/api/v1/transcripts/partial",
            json={
                "utterance_id": "utt-stream-0002",
                "device_id": "dashboard",
                "text": "hello",
                "consent": False,
            },
        )
    assert response.status_code == 400


def test_partial_rejects_oversized_hypothesis(configured_settings) -> None:
    with _client(configured_settings) as client:
        response = client.post(
            "/api/v1/transcripts/partial",
            json={
                "utterance_id": "utt-stream-0003",
                "device_id": "dashboard",
                # 40k two-byte characters stay under the field's char cap but
                # exceed the configured byte budget.
                "text": "é" * 40_000,
                "consent": True,
            },
        )
    assert response.status_code == 413


def test_partial_streams_while_paused_but_not_when_killed(configured_settings) -> None:
    # Pause stops actions but deliberately keeps capture alive, matching the
    # archival ingestion path; the kill switch stops listening entirely.
    partial = {
        "utterance_id": "utt-stream-0004",
        "device_id": "dashboard",
        "text": "should this stream?",
        "consent": True,
    }
    with _client(configured_settings) as client:
        paused = client.post(
            "/api/controls/global",
            json={"mode": "paused", "reason_code": "test_pause", "actor": "owner:test"},
        )
        assert paused.status_code == 200
        assert client.post("/api/v1/transcripts/partial", json=partial).status_code == 202
        killed = client.post(
            "/api/controls/global",
            json={"mode": "killed", "reason_code": "test_kill", "actor": "owner:test"},
        )
        assert killed.status_code == 200
        response = client.post("/api/v1/transcripts/partial", json=partial)
    assert response.status_code == 503


def test_finalized_signal_publishes_archived_event_with_utterance_link(
    configured_settings, bus_events
) -> None:
    with _client(configured_settings) as client:
        response = client.post(
            "/api/signals",
            json={
                "text": "we talked about the weather for a while",
                "source": "voice",
                "consent": True,
                "utterance_id": "utt-stream-0005",
            },
        )
        assert response.status_code == 200
    archived = [payload for kind, payload in bus_events if kind == "transcript_archived"]
    assert len(archived) == 1
    assert archived[0]["utterance_id"] == "utt-stream-0005"
    assert archived[0]["text"] == "we talked about the weather for a while"
    assert archived[0]["aggregated"] is False
    assert archived[0]["received_at"]
    # The dashboard inserts SSE payloads directly into the transcript list, so
    # the payload must carry every field transcriptRow() renders and keys on.
    assert archived[0]["event_id"].startswith("web:")
    assert archived[0]["source"] == "voice"
    assert archived[0]["relevant"] is False
    assert archived[0]["classification"]
    assert archived[0]["occurred_at"]


def test_transcript_feed_is_newest_first_with_full_text(configured_settings) -> None:
    long_text = "the quick brown fox jumps over the lazy dog " * 10  # ~440 chars
    with _client(configured_settings) as client:
        for index, text in enumerate(["first thing said", "second thing said", long_text]):
            response = client.post(
                "/api/v1/transcripts",
                json={
                    "event_id": f"transcript-order-{index:04d}",
                    "device_id": "phone",
                    "source": "api",
                    "text": text,
                    "consent": True,
                },
            )
            assert response.status_code == 202
        feed = client.get("/api/transcript").json()
    assert [entry["event_id"] for entry in feed] == [
        "transcript-order-0002",
        "transcript-order-0001",
        "transcript-order-0000",
    ]
    # Full text (ingestion strips edges), unlike /api/activity's 320-char cut.
    assert feed[0]["text"] == long_text.strip()
    assert feed[0]["received_at"] >= feed[-1]["received_at"]
    assert {"source", "occurred_at", "relevant", "classification"} <= feed[0].keys()


def test_transcript_feed_paginates_older_entries_with_before_cursor(configured_settings) -> None:
    with _client(configured_settings) as client:
        for index in range(3):
            client.post(
                "/api/v1/transcripts",
                json={
                    "event_id": f"transcript-page-{index:04d}",
                    "device_id": "phone",
                    "source": "api",
                    "text": f"utterance number {index}",
                    "consent": True,
                },
            )
        first_page = client.get("/api/transcript", params={"limit": 2}).json()
        assert [entry["event_id"] for entry in first_page] == [
            "transcript-page-0002",
            "transcript-page-0001",
        ]
        second_page = client.get(
            "/api/transcript",
            params={
                "limit": 2,
                "before": first_page[-1]["received_at"],
                "before_id": first_page[-1]["archive_id"],
            },
        ).json()
    assert [entry["event_id"] for entry in second_page] == ["transcript-page-0000"]


def test_transcript_feed_excludes_synthetic_aggregates(configured_settings) -> None:
    with _client(configured_settings) as client:
        client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "transcript-component-0001",
                "device_id": "phone",
                "source": "api",
                "text": "remember to check the oven",
                "consent": True,
            },
        )
        client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "adb-omi:aggregate:feedtest0001",
                "device_id": "phone",
                "source": "api",
                "text": "remember to check the oven",
                "consent": True,
                "metadata": {"aggregated": True},
            },
        )
        feed = client.get("/api/transcript").json()
    assert [entry["event_id"] for entry in feed] == ["transcript-component-0001"]


def test_transcript_feed_excludes_audio_only_events(configured_settings) -> None:
    with _client(configured_settings) as client:
        client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "transcript-spoken-0001",
                "device_id": "phone",
                "source": "api",
                "text": "an actual spoken sentence",
                "consent": True,
            },
        )
        audio = client.post(
            "/api/webhooks/omi/audio?uid=uid-audio&sample_rate=16000&timestamp=chunk-1",
            content=b"\x00\x01" * 640,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert audio.status_code == 202
        feed = client.get("/api/transcript").json()
    # Audio-only archive rows have no words; they must not render blank rows.
    assert [entry["event_id"] for entry in feed] == ["transcript-spoken-0001"]


def test_bus_overflow_sheds_partials_but_keeps_durable_events() -> None:
    import asyncio

    from followthrough.bus import EventBus

    async def scenario() -> list[str]:
        bus_instance = EventBus()
        stream = bus_instance.stream()
        assert "ready" in await anext(stream)  # registers the subscriber queue
        queue = next(iter(bus_instance._subscribers))
        for index in range(150):
            await bus_instance.publish("transcript_partial", {"seq": index})
        await bus_instance.publish("transcript_archived", {"event_id": "must-arrive"})
        await bus_instance.publish("transcript_partial", {"seq": 150})
        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait()["type"])
        await stream.aclose()
        return drained

    kinds = asyncio.run(scenario())
    assert kinds.count("transcript_archived") == 1  # durable event survived overflow
    assert len(kinds) == 100  # the queue stayed bounded; excess partials were shed


def test_bus_never_evicts_a_durable_event_for_another_durable_event() -> None:
    import asyncio

    from followthrough.bus import EventBus

    async def scenario() -> list[int]:
        bus_instance = EventBus()
        stream = bus_instance.stream()
        await anext(stream)
        queue = next(iter(bus_instance._subscribers))
        for index in range(101):
            await bus_instance.publish("durable", {"index": index})
        values = [queue.get_nowait()["payload"]["index"] for _ in range(queue.qsize())]
        await stream.aclose()
        return values

    assert asyncio.run(scenario()) == list(range(100))

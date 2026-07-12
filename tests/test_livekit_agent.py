from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import jwt
import pytest

from followthrough.livekit_agent import (
    DEEPGRAM_ENDPOINTING_MS,
    TranscriptBridge,
    _participant_response_mode,
    post_transcript,
)
from followthrough.livekit_tokens import issue_memo_session_token


def test_memo_token_is_room_scoped_audio_only_and_dispatches_agent() -> None:
    issued = issue_memo_session_token(
        server_url="wss://livekit.example",
        api_key="key",
        api_secret="a-secret-long-enough-for-hs256-tests",
        agent_name="followthrough",
        device_id="memo-samsung-phone",
        surface="memo-android",
        response_mode="discord_and_voice",
        speaker_mode="personal",
        ttl_seconds=600,
    )
    claims = jwt.decode(
        issued.participant_token,
        "a-secret-long-enough-for-hs256-tests",
        algorithms=["HS256"],
        audience=None,
        options={"verify_aud": False},
    )

    assert issued.room_name.startswith("followthrough-")
    assert claims["video"]["room"] == issued.room_name
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["canPublishSources"] == ["microphone"]
    assert claims["video"]["canPublishData"] is False
    assert claims["roomConfig"]["agents"][0]["agentName"] == "followthrough"
    metadata = json.loads(claims["metadata"])
    assert metadata["capture_consent"] is True
    assert metadata["speaker_mode"] == "personal"


def test_reconnect_gets_a_fresh_room_and_identity() -> None:
    options = {
        "server_url": "wss://livekit.example",
        "api_key": "key",
        "api_secret": "a-secret-long-enough-for-hs256-tests",
        "agent_name": "followthrough",
        "device_id": "memo-samsung-phone",
        "surface": "memo-android",
        "response_mode": "discord_only",
        "speaker_mode": "meeting",
        "ttl_seconds": 600,
    }
    first = issue_memo_session_token(**options)
    second = issue_memo_session_token(**options)

    assert first.room_name != second.room_name
    assert first.participant_identity != second.participant_identity


def test_deepgram_requires_a_meaningful_pause_before_finalizing() -> None:
    assert DEEPGRAM_ENDPOINTING_MS >= 1000


@pytest.mark.asyncio
async def test_post_transcript_preserves_irrelevant_speech_for_archive() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "archived", "operational_memory": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await post_transcript(
            text="Lunch was good",
            event_id="memo-livekit-event",
            device_id="memo-samsung-phone",
            room_name="followthrough-room",
            client=client,
        )

    assert result == {"status": "archived", "operational_memory": False}
    assert captured["path"] == "/api/v1/transcripts"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert "discard_irrelevant" not in payload["metadata"]
    assert payload["metadata"]["capture"] == "memo_livekit"


@pytest.mark.asyncio
async def test_adjacent_stt_finals_are_coalesced_into_one_utterance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/transcripts":
            delivered.append(json.loads(request.content))
        return httpx.Response(202, json={"status": "queued"})

    monkeypatch.setattr("followthrough.livekit_agent.FINAL_COALESCE_SECONDS", 0.03)
    ctx = SimpleNamespace(room=SimpleNamespace(name="followthrough-room", remote_participants={}))
    bridge = TranscriptBridge(ctx=ctx, session=SimpleNamespace())
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await bridge._queue_final("A memo, can you search", "item-one")
    await asyncio.sleep(0.019)
    await bridge._queue_final(
        "the web and find how much caffeine content is in Red Bull?", "item-two"
    )
    await asyncio.sleep(0.05)

    assert len(delivered) == 1
    assert delivered[0]["text"] == (
        "A memo, can you search the web and find how much caffeine content is in Red Bull?"
    )
    assert bridge.finalized == {"item-one", "item-two"}
    await bridge.close()


@pytest.mark.asyncio
async def test_final_transcript_retries_transient_server_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def no_wait(_: float) -> None:
        return None

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"detail": "temporary"})
        return httpx.Response(202, json={"status": "archived"})

    monkeypatch.setattr("followthrough.livekit_agent.asyncio.sleep", no_wait)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await post_transcript(
            text="Memo, research this tool",
            event_id="memo-livekit-retry",
            device_id="memo-samsung-phone",
            room_name="followthrough-room",
            client=client,
        )

    assert attempts == 2
    assert result["status"] == "archived"


@pytest.mark.asyncio
async def test_failed_final_is_not_marked_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_wait(_: float) -> None:
        return None

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "still unavailable"})

    monkeypatch.setattr("followthrough.livekit_agent.asyncio.sleep", no_wait)
    ctx = SimpleNamespace(room=SimpleNamespace(name="followthrough-room", remote_participants={}))
    bridge = TranscriptBridge(ctx=ctx, session=SimpleNamespace())
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    bridge.pending_finals.append(("Memo, research this tool", "item-retry", None))
    with pytest.raises(httpx.HTTPStatusError):
        await bridge._flush_finals()

    assert bridge.finalized == set()
    await bridge.close()


@pytest.mark.asyncio
async def test_adjacent_finals_from_different_speakers_are_never_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/transcripts":
            delivered.append(json.loads(request.content))
        return httpx.Response(202, json={"status": "archived"})

    monkeypatch.setattr("followthrough.livekit_agent.FINAL_COALESCE_SECONDS", 0.02)
    participant = SimpleNamespace(
        metadata=json.dumps(
            {"device_id": "memo-meeting-phone", "speaker_mode": "meeting"}
        )
    )
    ctx = SimpleNamespace(
        room=SimpleNamespace(
            name="followthrough-meeting", remote_participants={"phone": participant}
        )
    )
    bridge = TranscriptBridge(ctx=ctx, session=SimpleNamespace())
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await bridge._queue_final("Memo, search gold prices", "owner-item", "speaker-0")
    await bridge._queue_final("That sounds useful", "guest-item", "speaker-1")
    await asyncio.sleep(0.04)

    assert [item["text"] for item in delivered] == [
        "Memo, search gold prices",
        "That sounds useful",
    ]
    assert [item["metadata"]["speaker_id"] for item in delivered] == [
        "speaker-0",
        "speaker-1",
    ]
    assert all(item["metadata"]["speaker_mode"] == "meeting" for item in delivered)
    assert all(item["metadata"]["allow_owner_report"] is False for item in delivered)
    await bridge.close()


@pytest.mark.asyncio
async def test_duplicate_final_item_is_delivered_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/transcripts":
            delivered.append(json.loads(request.content))
        return httpx.Response(202, json={"status": "archived"})

    monkeypatch.setattr("followthrough.livekit_agent.FINAL_COALESCE_SECONDS", 0.01)
    ctx = SimpleNamespace(room=SimpleNamespace(name="followthrough-room", remote_participants={}))
    bridge = TranscriptBridge(ctx=ctx, session=SimpleNamespace())
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await bridge._queue_final("Memo, research this", "same-item", "speaker-0")
    await bridge._queue_final("Memo, research this", "same-item", "speaker-0")
    await asyncio.sleep(0.02)

    assert len(delivered) == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_phone_delivery_is_spoken_then_acknowledged() -> None:
    calls: list[tuple[str, str]] = []
    spoken: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "receipt_id": "receipt-one",
                        "state": "completed",
                        "summary": "Gold is 2,400 dollars.",
                    }
                ],
            )
        return httpx.Response(200, json={"state": "acknowledged"})

    async def say(text: str) -> None:
        spoken.append(text)

    participant = SimpleNamespace(
        metadata=json.dumps(
            {
                "device_id": "memo-delivery-phone",
                "response_mode": "discord_and_voice",
            }
        )
    )
    ctx = SimpleNamespace(
        room=SimpleNamespace(name="followthrough-room", remote_participants={"p": participant})
    )
    bridge = TranscriptBridge(ctx=ctx, session=SimpleNamespace(say=say))
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await bridge._drain_phone_deliveries()

    assert spoken == ["Followthrough result. Gold is 2,400 dollars."]
    assert calls == [
        ("GET", "/api/v1/devices/memo-delivery-phone/deliveries"),
        ("POST", "/api/v1/devices/memo-delivery-phone/deliveries/ack"),
    ]
    await bridge.close()


def test_response_mode_defaults_silent_and_enables_voice_explicitly() -> None:
    silent = SimpleNamespace(room=SimpleNamespace(remote_participants={}))
    voiced = SimpleNamespace(
        room=SimpleNamespace(
            remote_participants={
                "phone": SimpleNamespace(
                    metadata=json.dumps({"response_mode": "discord_and_voice"})
                )
            }
        )
    )

    assert _participant_response_mode(silent) == "discord_only"
    assert _participant_response_mode(voiced) == "discord_and_voice"

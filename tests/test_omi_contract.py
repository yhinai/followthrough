from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from followthrough.app import create_app


def test_official_omi_transcript_shapes_and_speaker_aliases(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    object_payload = {
        "session_id": "uid-1",
        "segments": [
            {"id": "seg-owner", "text": "Research github.com/NousResearch/hermes-agent", "speaker": "SPEAKER_00", "speaker_id": 0, "is_user": True},
            {"id": "seg-other", "text": "Research this new vector database SDK", "speaker": "SPEAKER_01", "speakerId": 1, "is_user": False},
            {"id": "seg-unknown", "text": "Benchmark this inference tool", "speaker": "SPEAKER_02", "speakerId": 2},
            {"id": "seg-irrelevant", "text": "Lunch was good", "speaker": "SPEAKER_01", "speakerId": 1, "is_user": False},
        ],
    }
    array_payload = [{"id": "seg-array", "text": "Lunch was good", "speakerId": 0, "is_user": True}]
    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.post(f"/api/webhooks/omi/transcript?token={device_token}&uid=uid-1", headers={"Idempotency-Key": "delivery-1"}, json=object_payload)
        assert time.perf_counter() - started < 0.5
        assert response.status_code == 202
        assert response.json()["accepted"] == 4
        second = client.post(f"/api/webhooks/omi/transcript?token={device_token}&uid=uid-1", headers={"Idempotency-Key": "delivery-1"}, json=object_payload)
        assert second.status_code == 202
        final = client.post(
            f"/api/webhooks/omi/conversation?token={device_token}&uid=uid-1",
            headers={"Idempotency-Key": "conversation-delivery-1"},
            json={"id": "conversation-1", "started_at": "2026-07-11T10:00:00Z", "finished_at": "2026-07-11T10:01:00Z", "segments": object_payload["segments"]},
        )
        assert final.status_code == 202
        assert client.post(f"/api/webhooks/omi/transcript?token={device_token}&uid=uid-1", headers={"Idempotency-Key": "delivery-2"}, json=array_payload).status_code == 202
    assert app.state.archive_store.db.execute("SELECT COUNT(*) FROM archive_events").fetchone()[0] == 5
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
    owner = app.state.archive_store.by_event("omi:segment:uid-1:seg-owner")
    assert owner is not None
    owner_metadata = json.loads(owner["metadata_json"])
    assert owner_metadata["observed_hooks"] == ["conversation", "transcript"]
    assert owner_metadata["conversation_id"] == "conversation-1"
    other = app.state.archive_store.by_event("omi:segment:uid-1:seg-other")
    assert other is not None and other["classification"] == "tool" and other["run_id"]
    other_relevance = json.loads(other["metadata_json"])["relevance"]
    assert other_relevance["owner_status"] == "non_owner"
    assert other_relevance["ambient_authorized"] is True
    assert other_relevance["dispatch_allowed"] is True
    unknown = app.state.archive_store.by_event("omi:segment:uid-1:seg-unknown")
    assert unknown is not None and unknown["classification"] == "tool" and unknown["run_id"]
    unknown_relevance = json.loads(unknown["metadata_json"])["relevance"]
    assert unknown_relevance["owner_status"] == "unknown"
    assert unknown_relevance["ambient_authorized"] is True
    assert unknown_relevance["dispatch_allowed"] is True
    irrelevant = app.state.archive_store.by_event("omi:segment:uid-1:seg-irrelevant")
    assert irrelevant is not None and irrelevant["run_id"] is None
    irrelevant_relevance = json.loads(irrelevant["metadata_json"])["relevance"]
    assert irrelevant_relevance["disposition"] == "ignore"
    assert irrelevant_relevance["dispatch_allowed"] is False


def test_official_omi_raw_pcm_audio_preserves_identical_chunks_without_delivery_id(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    pcm = b"\x00\x01" * 80_000
    url = f"/api/webhooks/omi/audio?token={device_token}&uid=uid-1&sample_rate=16000"
    with TestClient(app) as client:
        official = client.post(url, content=pcm, headers={"Content-Type": "application/octet-stream"})
        official_replay = client.post(url, content=pcm, headers={"Content-Type": "application/octet-stream"})
        distinct = client.post(url, content=b"\x01\x00" * 80_000, headers={"Content-Type": "application/octet-stream"})
        first = client.post(url, content=pcm, headers={"Content-Type": "application/octet-stream", "Idempotency-Key": "audio-delivery-1"})
        second = client.post(url, content=pcm, headers={"Content-Type": "application/octet-stream", "Idempotency-Key": "audio-delivery-1"})
        conflict = client.post(url, content=b"different", headers={"Content-Type": "application/octet-stream", "Idempotency-Key": "audio-delivery-1"})
    assert official.status_code == 202 and official.json()["created"] is True
    assert official_replay.status_code == 202 and official_replay.json()["created"] is True
    assert distinct.status_code == 202 and distinct.json()["created"] is True
    assert official.json()["event_id"] != official_replay.json()["event_id"]
    assert official.json()["event_id"] != distinct.json()["event_id"]
    assert first.status_code == 202 and first.json()["created"] is True
    assert second.status_code == 202 and second.json()["created"] is False
    assert conflict.status_code == 409
    archived = app.state.archive_store.by_event("omi:audio:uid-1:audio-delivery-1")
    chunk = app.state.archive_store.audio_chunk(archived["id"], 0)
    assert chunk is not None
    assert __import__("pathlib").Path(chunk["path"]).read_bytes() == pcm
    metadata = json.loads(archived["metadata_json"])
    assert metadata["idempotency_source"] == "explicit"
    assert metadata["capture_stream_id"].startswith("omi:uid-1:")
    assert metadata["stream_sequence"] == 3
    assert metadata["duration_ms"] == 5000
    assert metadata["alignment"] == "arrival_time_estimate"

    derived = app.state.archive_store.by_event(official.json()["event_id"])
    assert derived is not None
    derived_metadata = json.loads(derived["metadata_json"])
    assert derived_metadata["idempotency_source"] == "official_omi_unique_delivery"
    assert derived_metadata["stream_sequence"] == 0


def test_official_omi_audio_timestamp_restores_retry_idempotency(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    url = (
        f"/api/webhooks/omi/audio?token={device_token}&uid=uid-1&sample_rate=16000"
        "&timestamp=2026-07-11T22%3A00%3A00Z"
    )
    pcm = b"\x00\x01" * 16_000
    with TestClient(app) as client:
        first = client.post(url, content=pcm, headers={"Content-Type": "application/octet-stream"})
        replay = client.post(url, content=pcm, headers={"Content-Type": "application/octet-stream"})
    assert first.status_code == 202 and first.json()["created"] is True
    assert replay.status_code == 202 and replay.json()["created"] is False
    assert first.json()["event_id"] == replay.json()["event_id"]
    archived = app.state.archive_store.by_event(first.json()["event_id"])
    assert json.loads(archived["metadata_json"])["idempotency_source"] == (
        "official_omi_derived"
    )


def test_omi_audio_preserves_compressed_content_type(configured_settings) -> None:
    settings, _, device_token = configured_settings
    app = create_app(settings)
    payload = b"\x00\x00\x00\x18ftypmp42" + b"compressed-audio" * 100
    with TestClient(app) as client:
        response = client.post(
            "/api/webhooks/omi/audio?uid=termux&timestamp=chunk-1",
            content=payload,
            headers={
                "Content-Type": "audio/mp4",
                "Authorization": f"Bearer {device_token}",
            },
        )
    assert response.status_code == 202
    event = app.state.archive_store.by_event(response.json()["event_id"])
    chunk = app.state.archive_store.audio_chunk(event["id"], 0)
    assert chunk["mime_type"] == "audio/mp4"
    metadata = json.loads(event["metadata_json"])
    assert metadata["encoding"] == "audio/mp4"
    assert metadata["duration_ms"] is None

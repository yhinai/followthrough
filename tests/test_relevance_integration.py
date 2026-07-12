from __future__ import annotations
import time
from pathlib import Path

from fastapi.testclient import TestClient

from followthrough.app import create_app
from followthrough.relevance import SpeakerContext, evaluate_relevance
from followthrough.store import now


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _database_bytes(path: Path) -> bytes:
    data = b""
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.is_file():
            data += candidate.read_bytes()
    return data


def _wait_completed(app, run_id: str) -> None:
    for _ in range(50):
        if app.state.store.get_run(run_id)["status"] == "completed":
            return
        time.sleep(0.01)
    raise AssertionError("background run did not complete")


def test_archive_is_physically_separate_and_operational_memory_is_relevance_gated(configured_settings) -> None:
    settings, dashboard_token, device_token = configured_settings
    app = create_app(settings)

    def fake_process(run_id, text, classification, **_):
        app.state.store.update_run(run_id, status="completed", finished_at=now(), success=1)
        return {"run_id": run_id, "status": "completed"}

    app.state.crew.process = fake_process
    raw = "Research the GitHub repository https://github.com/BasedHardware/omi unique-separation-canary"
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={"event_id": "event-separated-0001", "device_id": "phone", "text": raw, "source": "phone", "consent": True},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        _wait_completed(app, run_id)
        memories = client.get("/api/memory/operational", headers=_auth(dashboard_token))
        assert memories.status_code == 200
        assert len(memories.json()) == 1
        assert memories.json()[0]["category"] == "repository"

    assert app.state.archive_store.by_event("event-separated-0001") is not None
    assert app.state.store.db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='archive_events'"
    ).fetchone()[0] == 0
    # The operational store never holds transcript text; the archive stores it
    # as plaintext. Both run in WAL mode, so scan the -wal/-shm sidecars too.
    assert raw.encode() not in _database_bytes(settings.db_path)
    assert raw.encode() in _database_bytes(settings.archive_db_path)


def test_archived_omi_non_owner_correction_restores_ambient_authorization(
    configured_settings,
) -> None:
    settings, dashboard_token, device_token = configured_settings
    app = create_app(settings)

    def fake_process(run_id, text, classification, **_):
        app.state.store.update_run(
            run_id,
            status="completed",
            finished_at=now(),
            success=1,
        )
        return {"run_id": run_id, "status": "completed"}

    app.state.crew.process = fake_process
    payload = {
        "segments": [
            {
                "id": "muted-non-owner",
                "text": "Research this new vector database SDK",
                "is_user": False,
            },
            {
                "id": "irrelevant-non-owner",
                "text": "Lunch was delicious and the coffee was great",
                "is_user": False,
            },
        ]
    }
    with TestClient(app) as client:
        muted = client.post(
            "/api/relevance/interests",
            headers=_auth(dashboard_token),
            json={"category": "tool", "weight": -1.0, "source": "explicit-test"},
        )
        assert muted.status_code == 200
        accepted = client.post(
            f"/api/webhooks/omi/transcript?token={device_token}&uid=ambient-device",
            headers={"Idempotency-Key": "ambient-correction-delivery"},
            json=payload,
        )
        assert accepted.status_code == 202
        assert accepted.json()["receipts"][0]["status"] == "archived"
        assert accepted.json()["receipts"][1]["status"] == "archived"
        assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        corrected = client.post(
            "/api/relevance/corrections",
            headers=_auth(dashboard_token),
            json={
                "event_id": "omi:segment:ambient-device:muted-non-owner",
                "disposition": "action",
                "categories": ["tool"],
            },
        )
        assert corrected.status_code == 202
        assert corrected.json()["run_id"]
        assert corrected.json()["relevance"]["owner_status"] == "non_owner"
        assert corrected.json()["relevance"]["ambient_authorized"] is True
        assert corrected.json()["relevance"]["dispatch_allowed"] is True
        _wait_completed(app, corrected.json()["run_id"])

    irrelevant = app.state.archive_store.by_event(
        "omi:segment:ambient-device:irrelevant-non-owner"
    )
    assert irrelevant is not None and irrelevant["run_id"] is None
    assert app.state.store.relevance_for_event(irrelevant["event_id"])[
        "disposition"
    ] == "ignore"
    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert app.state.store.db.execute(
        "SELECT COUNT(*) FROM operational_memories"
    ).fetchone()[0] == 1


def test_native_non_owner_and_unknown_corrections_remain_fail_closed(
    configured_settings,
) -> None:
    settings, dashboard_token, _ = configured_settings
    app = create_app(settings)
    cases = (
        ("native-unknown-correction", SpeakerContext.unknown(), "unknown"),
        (
            "native-non-owner-correction",
            SpeakerContext.native_non_owner("sha256:guest"),
            "non_owner",
        ),
    )

    for event_id, speaker, _ in cases:
        raw = "This ambiguous thing matters"
        relevance = evaluate_relevance(raw, speaker)
        archived, created = app.state.archive_store.archive_event(
            event_id=event_id,
            device_id="native-device",
            source="phone",
            occurred_at=now(),
            transcript_bytes=raw.encode(),
            transcript_sha256=app.state.archive.digest(raw.encode()),
            relevant=False,
            classification=relevance.reason_code,
            metadata={"relevance": relevance.to_dict()},
        )
        assert created is True
        app.state.store.record_relevance(
            archived["id"],
            event_id,
            relevance.to_dict(),
        )

    with TestClient(app) as client:
        for event_id, _, expected_owner_status in cases:
            corrected = client.post(
                "/api/relevance/corrections",
                headers=_auth(dashboard_token),
                json={
                    "event_id": event_id,
                    "disposition": "action",
                    "categories": ["repository"],
                },
            )
            assert corrected.status_code == 202
            assert corrected.json()["run_id"] is None
            assert corrected.json()["relevance"]["owner_status"] == expected_owner_status
            assert corrected.json()["relevance"]["ambient_authorized"] is False
            assert corrected.json()["relevance"]["dispatch_allowed"] is False

    assert app.state.store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert app.state.store.db.execute(
        "SELECT COUNT(*) FROM operational_memories"
    ).fetchone()[0] == 0


def test_interest_mute_and_owner_correction_are_applied(configured_settings) -> None:
    settings, dashboard_token, device_token = configured_settings
    app = create_app(settings)

    def fake_process(run_id, text, classification, **_):
        app.state.store.update_run(run_id, status="completed", finished_at=now(), success=1)
        return {"run_id": run_id, "status": "completed"}

    app.state.crew.process = fake_process
    with TestClient(app) as client:
        muted = client.post(
            "/api/relevance/interests",
            headers=_auth(dashboard_token),
            json={"category": "tool", "weight": -1.0, "source": "explicit-test"},
        )
        assert muted.status_code == 200
        ignored = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={"event_id": "event-muted-tool-01", "device_id": "phone", "text": "Research this new SDK tool", "source": "phone", "consent": True},
        )
        assert ignored.status_code == 202
        assert ignored.json()["classification"]["kind"] == "interest_muted"
        ordinary = client.post(
            "/api/v1/transcripts",
            headers=_auth(device_token),
            json={"event_id": "event-owner-correction-01", "device_id": "phone", "text": "This unusual matter is important", "source": "phone", "consent": True},
        )
        assert ordinary.json()["status"] == "archived"
        corrected = client.post(
            "/api/relevance/corrections",
            headers=_auth(dashboard_token),
            json={"event_id": "event-owner-correction-01", "disposition": "action", "categories": ["goal"]},
        )
        assert corrected.status_code == 202
        assert corrected.json()["relevance"]["dispatch_allowed"] is True
        _wait_completed(app, corrected.json()["run_id"])
    assert app.state.store.db.execute("SELECT COUNT(*) FROM operational_memories").fetchone()[0] == 1

from fastapi.testclient import TestClient

from followthrough.app import create_app


def test_workspace_groups_edits_and_soft_deletes_items(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.h_api_key = ""
    app = create_app(settings)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "workspace-event-0001",
                "device_id": "memo-phone",
                "source": "phone",
                "text": "Memo, remind me to call Maya tomorrow.",
                "consent": True,
            },
        ).json()
        item = client.get("/api/workspace").json()[0]

        assert item["id"] == accepted["job_id"]
        assert item["group"] == "tasks"
        assert item["title"]

        updated = client.patch(
            f"/api/workspace/{item['id']}",
            json={"title": "Call Maya before lunch", "group": "backlog"},
        )
        assert updated.status_code == 200
        assert updated.json()["title"] == "Call Maya before lunch"
        assert updated.json()["group"] == "backlog"

        removed = client.delete(f"/api/workspace/{item['id']}")
        assert removed.status_code == 204
        assert client.get("/api/workspace").json() == []
        assert app.state.store.hermes_job(item["id"]) is not None


def test_passive_ambient_interest_becomes_backlog_not_execution(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.h_api_key = ""
    app = create_app(settings)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "workspace-event-ambient-1",
                "device_id": "memo-phone",
                "source": "phone",
                "text": "That new agent SDK sounds interesting.",
                "consent": True,
            },
        ).json()

        assert accepted["status"] == "backlog"
        item = client.get("/api/workspace").json()[0]
        assert item["state"] == "backlog"
        assert item["group"] == "backlog"
        assert item["task_id"] is None


def test_explicit_memo_command_never_goes_to_backlog(configured_settings) -> None:
    settings, _, _ = configured_settings
    settings.h_api_key = ""
    app = create_app(settings)

    with TestClient(app) as client:
        accepted = client.post(
            "/api/v1/transcripts",
            json={
                "event_id": "workspace-event-command-1",
                "device_id": "memo-phone",
                "source": "phone",
                "text": "Memo, research that new agent SDK.",
                "consent": True,
            },
        ).json()

        assert accepted["status"] == "queued"
        item = client.get("/api/workspace").json()[0]
        assert item["state"] != "backlog"

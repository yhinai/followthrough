from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings
from .store import Store


TERMINAL_STATES = {"completed", "failed", "timed_out", "interrupted"}
EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


def _status(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("status") or value.get("state") or "running")
    return "running"


def _step_count(payload: dict[str, Any], fallback: int) -> int:
    status = payload.get("status")
    candidates = [payload.get("step_count"), payload.get("steps")]
    if isinstance(status, dict):
        candidates.extend((status.get("step_count"), status.get("steps")))
    for value in candidates:
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    return fallback


def _event_summary(event: Any) -> str:
    if not isinstance(event, dict):
        return str(event)[:500]
    kind = str(event.get("type") or event.get("kind") or event.get("event") or "step")
    for key in ("message", "description", "thought", "action", "text", "content"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return f"{kind}: {value.strip()}"[:500]
        if isinstance(value, dict):
            return f"{kind}: {json.dumps(value, sort_keys=True)}"[:500]
    return f"{kind}: {json.dumps(event, sort_keys=True, default=str)}"[:500]


class HCompanyExecutor:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        publish: EventCallback,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.publish = publish
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.settings.h_api_key.strip())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.h_api_key.strip()}",
            "Content-Type": "application/json",
        }

    async def run(self, identifier: str, task: str, start_url: str | None = None) -> None:
        if not self.configured:
            row = self.store.update_computer_session(
                identifier,
                state="configuration_required",
                error="HAI_API_KEY is not configured",
                finished_at=datetime.now(UTC).isoformat(),
            )
            await self.publish("computer_use_failed", row)
            return

        session_id = ""
        cursor = 0
        step_count = 0
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.h_api_base.rstrip("/"),
                headers=self._headers(),
                timeout=httpx.Timeout(30.0, read=40.0),
                transport=self.transport,
            ) as client:
                body: dict[str, Any] = {
                    "agent": self.settings.h_agent,
                    "messages": [{"type": "user_message", "message": task}],
                    "max_steps": self.settings.h_max_steps,
                    "max_time_s": self.settings.h_max_time_seconds,
                }
                if start_url:
                    body["overrides"] = {
                        "agent.environments[kind=web].start_url": start_url
                    }
                response = await client.post("/sessions", json=body)
                response.raise_for_status()
                created = response.json()
                session_id = str(created["id"])
                row = self.store.update_computer_session(
                    identifier,
                    h_session_id=session_id,
                    state=_status(created.get("status", "pending")),
                    agent_view_url=created.get("agent_view_url"),
                    current_action="H Company browser session created",
                )
                await self.publish("computer_use_started", row)

                while True:
                    changes = await client.get(
                        f"/sessions/{session_id}/changes",
                        params={"from_index": cursor, "wait_for_seconds": 15},
                    )
                    if changes.status_code == 204:
                        continue
                    changes.raise_for_status()
                    payload = changes.json()
                    events = payload.get("new_events") or []
                    cursor += len(events)
                    step_count = _step_count(payload, max(step_count, cursor))
                    state = _status(payload.get("status", "running"))
                    current = _event_summary(events[-1]) if events else f"Session {state}"
                    answer = payload.get("answer")
                    row = self.store.update_computer_session(
                        identifier,
                        state=state,
                        step_count=step_count,
                        current_action=current,
                        latest_answer=answer if isinstance(answer, str) else None,
                    )
                    await self.publish("computer_use_progress", row)
                    if state in TERMINAL_STATES:
                        break

                snapshot = await client.get(f"/sessions/{session_id}")
                snapshot.raise_for_status()
                settled = snapshot.json()
                state = _status(settled.get("status", row.get("state")))
                answer = settled.get("latest_answer") or row.get("latest_answer")
                error = settled.get("error")
                if isinstance(settled.get("status"), dict):
                    error = error or settled["status"].get("error")
                row = self.store.update_computer_session(
                    identifier,
                    state=state,
                    step_count=_step_count(settled, step_count),
                    current_action="Completed browser task" if state == "completed" else f"Session {state}",
                    latest_answer=answer if isinstance(answer, str) else None,
                    agent_view_url=settled.get("agent_view_url") or row.get("agent_view_url"),
                    error=str(error)[:500] if error else None,
                    finished_at=datetime.now(UTC).isoformat(),
                )
                await self.publish("computer_use_completed", row)
        except Exception as exc:
            row = self.store.update_computer_session(
                identifier,
                state="failed",
                current_action="H Company session failed",
                error=f"{type(exc).__name__}: {str(exc)[:350]}",
                finished_at=datetime.now(UTC).isoformat(),
            )
            await self.publish("computer_use_failed", row)

    async def cancel(self, identifier: str) -> dict[str, Any]:
        row = self.store.computer_session(identifier)
        if not row:
            raise KeyError(identifier)
        session_id = row.get("h_session_id")
        if session_id and self.configured and row.get("state") not in TERMINAL_STATES:
            async with httpx.AsyncClient(
                base_url=self.settings.h_api_base.rstrip("/"), headers=self._headers(), timeout=20,
                transport=self.transport,
            ) as client:
                response = await client.delete(f"/sessions/{session_id}")
                response.raise_for_status()
        return self.store.update_computer_session(
            row["id"], state="interrupted", finished_at=datetime.now(UTC).isoformat()
        )

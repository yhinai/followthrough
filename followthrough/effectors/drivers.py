from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar, cast
from urllib.parse import quote

import httpx

from .errors import (
    AuthenticationExpired,
    ConnectorFailure,
    PartialFailure,
    RateLimited,
    RollbackUnavailable,
)
from .models import (
    CalendarEventRequest,
    DeploymentRequest,
    DiscordMessageRequest,
    DriverReceipt,
    EffectKind,
    EffectRequest,
    GitHubIssueRequest,
    PrivateTaskRequest,
    PurchaseRequest,
    canonical_json,
    fingerprint,
)


RequestT = TypeVar("RequestT", bound=EffectRequest)


class EffectDriver(Protocol[RequestT]):
    kind: EffectKind

    def execute(self, request: RequestT, idempotency_key: str) -> DriverReceipt: ...

    def rollback(self, receipt: DriverReceipt) -> DriverReceipt: ...


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def __call__(self, argv: list[str], stdin: str | None = None) -> CommandResult: ...


def subprocess_runner(argv: list[str], stdin: str | None = None) -> CommandResult:
    completed = subprocess.run(
        argv,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class HttpResponse(Protocol):
    status_code: int
    headers: Any

    def json(self) -> Any: ...


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 20,
    ) -> HttpResponse: ...


class HttpxTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 20,
    ) -> httpx.Response:
        return httpx.request(method, url, headers=headers, json=json, timeout=timeout)


class PrivateTaskDriver:
    kind = EffectKind.PRIVATE_TASK_CREATE

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS private_tasks (
              id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL UNIQUE,
              trigger_event_id TEXT NOT NULL,
              title TEXT NOT NULL,
              description TEXT NOT NULL,
              due_at TEXT,
              priority TEXT NOT NULL,
              tags_json TEXT NOT NULL,
              state TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        self.db.commit()
        path.chmod(0o600)

    def execute(self, request: PrivateTaskRequest, idempotency_key: str) -> DriverReceipt:
        task_id = "task_" + fingerprint({"key": idempotency_key})[:24]
        stamp = datetime.now(UTC).isoformat()
        with self.lock:
            self.db.execute(
                """
                INSERT INTO private_tasks(
                  id,idempotency_key,trigger_event_id,title,description,due_at,
                  priority,tags_json,state,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    task_id,
                    idempotency_key,
                    request.trigger_event_id,
                    request.title,
                    request.description,
                    request.due_at.isoformat() if request.due_at else None,
                    request.priority,
                    canonical_json(request.tags),
                    "open",
                    stamp,
                    stamp,
                ),
            )
            self.db.commit()
        return DriverReceipt(
            provider="followthrough-private-tasks",
            external_id=task_id,
            status="completed",
            response_fingerprint=fingerprint({"task_id": task_id, "state": "open"}),
            reversible=True,
            reversal={"task_id": task_id, "operation": "cancel"},
            metadata={"state": "open"},
        )

    def rollback(self, receipt: DriverReceipt) -> DriverReceipt:
        task_id = str((receipt.reversal or {}).get("task_id", ""))
        if not task_id:
            raise RollbackUnavailable("followthrough-private-tasks")
        stamp = datetime.now(UTC).isoformat()
        with self.lock:
            updated = self.db.execute(
                "UPDATE private_tasks SET state='cancelled',updated_at=? WHERE id=?",
                (stamp, task_id),
            ).rowcount
            self.db.commit()
        if not updated:
            raise ConnectorFailure("followthrough-private-tasks", "cancel")
        return DriverReceipt(
            provider="followthrough-private-tasks",
            external_id=task_id,
            status="completed",
            response_fingerprint=fingerprint({"task_id": task_id, "state": "cancelled"}),
            reversible=False,
            metadata={"state": "cancelled"},
        )


class GoogleTokenProvider:
    """Loads and refreshes Google OAuth without exposing token material to receipts."""

    def __init__(
        self,
        token_path: Path,
        client_secret_path: Path,
        transport: HttpTransport | None = None,
    ) -> None:
        self.token_path = token_path
        self.client_secret_path = client_secret_path
        self.transport = transport or HttpxTransport()

    def __call__(self) -> str:
        try:
            permissions = (
                self.token_path.stat().st_mode & 0o077,
                self.client_secret_path.stat().st_mode & 0o077,
            )
        except OSError as exc:
            raise AuthenticationExpired("google-calendar") from exc
        if any(permissions):
            raise AuthenticationExpired("google-calendar")
        try:
            token_data = json.loads(self.token_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthenticationExpired("google-calendar") from exc
        expiry_text = token_data.get("expiry")
        expiry = None
        if expiry_text:
            try:
                expiry = datetime.fromisoformat(str(expiry_text).replace("Z", "+00:00"))
            except ValueError:
                expiry = None
        if token_data.get("token") and expiry and expiry > datetime.now(UTC) + timedelta(minutes=2):
            return str(token_data["token"])
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise AuthenticationExpired("google-calendar")
        try:
            client_data = json.loads(self.client_secret_path.read_text())
            client = client_data.get("installed") or client_data.get("web") or client_data
            client_id = client["client_id"]
            client_secret = client["client_secret"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AuthenticationExpired("google-calendar") from exc
        token_uri = str(token_data.get("token_uri") or "https://oauth2.googleapis.com/token")
        if token_uri not in {
            "https://oauth2.googleapis.com/token",
            "https://accounts.google.com/o/oauth2/token",
        }:
            raise AuthenticationExpired("google-calendar")
        response = httpx.post(
            token_uri,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        if response.status_code != 200:
            raise AuthenticationExpired("google-calendar")
        refreshed = response.json()
        token_data["token"] = refreshed["access_token"]
        token_data["expiry"] = (
            datetime.now(UTC) + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
        ).isoformat()
        temporary = self.token_path.with_suffix(self.token_path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(canonical_json(token_data))
        os.replace(temporary, self.token_path)
        self.token_path.chmod(0o600)
        return str(token_data["token"])


class GoogleCalendarDriver:
    kind = EffectKind.CALENDAR_EVENT_UPSERT

    def __init__(
        self,
        token_provider: Callable[[], str],
        transport: HttpTransport | None = None,
        api_base: str = "https://www.googleapis.com/calendar/v3",
    ) -> None:
        self.token_provider = token_provider
        self.transport = transport or HttpxTransport()
        self.api_base = api_base.rstrip("/")

    @staticmethod
    def event_id(idempotency_key: str) -> str:
        return "ft" + fingerprint({"key": idempotency_key})[:40]

    def execute(self, request: CalendarEventRequest, idempotency_key: str) -> DriverReceipt:
        event_id = self.event_id(idempotency_key)
        event = {
            "id": event_id,
            "summary": request.summary,
            "description": request.description,
            "location": request.location,
            "start": {"dateTime": request.start_at.isoformat(), "timeZone": request.timezone},
            "end": {"dateTime": request.end_at.isoformat(), "timeZone": request.timezone},
            "attendees": [{"email": str(email)} for email in request.attendees],
            "extendedProperties": {
                "private": {
                    "followthroughTrigger": fingerprint(request.trigger_event_id)[:32],
                    "followthroughIdempotency": fingerprint(idempotency_key)[:32],
                }
            },
        }
        calendar = quote(request.calendar_id, safe="")
        url = f"{self.api_base}/calendars/{calendar}/events/{event_id}"
        send_updates = "all" if request.notify_attendees else "none"
        headers = self._headers()
        lookup = self.transport.request("GET", url, headers=headers)
        if lookup.status_code == 200:
            response = self.transport.request(
                "PUT",
                f"{url}?sendUpdates={send_updates}",
                headers=headers,
                json=event,
            )
        elif lookup.status_code == 404:
            collection = f"{self.api_base}/calendars/{calendar}/events"
            response = self.transport.request(
                "POST",
                f"{collection}?sendUpdates={send_updates}",
                headers=headers,
                json=event,
            )
            # A concurrent replay may win the deterministic ID insertion.
            if response.status_code == 409:
                response = self.transport.request(
                    "PUT",
                    f"{url}?sendUpdates={send_updates}",
                    headers=headers,
                    json=event,
                )
        else:
            _google_response(lookup, "calendar_lookup")
            raise ConnectorFailure("google-calendar", "calendar_lookup")
        data = _google_response(response, "calendar_upsert")
        return DriverReceipt(
            provider="google-calendar",
            external_id=str(data.get("id") or event_id),
            status="completed",
            response_fingerprint=fingerprint(data),
            reversible=True,
            reversal={"calendar_id": request.calendar_id, "event_id": event_id},
            metadata={"calendar_id": request.calendar_id, "notifications": send_updates},
        )

    def rollback(self, receipt: DriverReceipt) -> DriverReceipt:
        reversal = receipt.reversal or {}
        calendar_id = str(reversal.get("calendar_id", ""))
        event_id = str(reversal.get("event_id", ""))
        if not calendar_id or not event_id:
            raise RollbackUnavailable("google-calendar")
        calendar = quote(calendar_id, safe="")
        url = f"{self.api_base}/calendars/{calendar}/events/{quote(event_id, safe='')}?sendUpdates=none"
        response = self.transport.request("DELETE", url, headers=self._headers())
        if response.status_code not in {204, 404}:
            _google_response(response, "calendar_delete")
        return DriverReceipt(
            provider="google-calendar",
            external_id=event_id,
            status="completed",
            response_fingerprint=fingerprint({"event_id": event_id, "state": "deleted"}),
            reversible=False,
            metadata={"state": "deleted"},
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_provider()}", "Content-Type": "application/json"}


class DiscordDeleter(Protocol):
    def __call__(self, channel_id: str, message_id: str) -> bool: ...


class HermesDiscordDriver:
    kind = EffectKind.DISCORD_MESSAGE_SEND

    def __init__(
        self,
        hermes_bin: str = "hermes",
        runner: CommandRunner = subprocess_runner,
        deleter: DiscordDeleter | None = None,
    ) -> None:
        self.hermes_bin = hermes_bin
        self.runner = runner
        self.deleter = deleter

    def execute(self, request: DiscordMessageRequest, idempotency_key: str) -> DriverReceipt:
        body = f"{request.subject}\n\n{request.body}" if request.subject else request.body
        result = self.runner(
            [self.hermes_bin, "send", "--to", request.target, "--json", "--file", "-"],
            body,
        )
        if result.returncode != 0:
            _command_error(result, "hermes-discord", "send", uncertain=True)
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise PartialFailure("hermes-discord", "parse_send_receipt") from exc
        message_id = _nested_value(data, {"message_id", "id"})
        channel_id = _nested_value(data, {"channel_id", "chat_id"})
        reversible = bool(message_id and channel_id and self.deleter)
        external_id = str(message_id or fingerprint(data)[:32])
        return DriverReceipt(
            provider="hermes-discord",
            external_id=external_id,
            status="completed",
            response_fingerprint=fingerprint(data),
            reversible=reversible,
            reversal=(
                {"channel_id": str(channel_id), "message_id": str(message_id)}
                if reversible
                else None
            ),
            metadata={"target": request.target, "rollback": "delete" if reversible else "unavailable"},
        )

    def rollback(self, receipt: DriverReceipt) -> DriverReceipt:
        reversal = receipt.reversal or {}
        if not self.deleter or not reversal.get("channel_id") or not reversal.get("message_id"):
            raise RollbackUnavailable("hermes-discord")
        if not self.deleter(str(reversal["channel_id"]), str(reversal["message_id"])):
            raise ConnectorFailure("hermes-discord", "delete_message")
        return DriverReceipt(
            provider="hermes-discord",
            external_id=str(reversal["message_id"]),
            status="completed",
            response_fingerprint=fingerprint({**reversal, "state": "deleted"}),
            reversible=False,
            metadata={"state": "deleted"},
        )


class GitHubIssueDriver:
    kind = EffectKind.GITHUB_ISSUE_CREATE

    def __init__(self, gh_bin: str = "gh", runner: CommandRunner = subprocess_runner) -> None:
        self.gh_bin = gh_bin
        self.runner = runner

    def execute(self, request: GitHubIssueRequest, idempotency_key: str) -> DriverReceipt:
        marker = f"<!-- followthrough:{fingerprint(idempotency_key)} -->"
        existing = self._find_existing(request.repository, marker)
        data: dict[str, Any]
        if existing is not None:
            data = existing
        else:
            payload = {
                "title": request.title,
                "body": f"{request.body}\n\n{marker}".strip(),
                "labels": request.labels,
            }
            result = self.runner(
                [
                    self.gh_bin,
                    "api",
                    f"repos/{request.repository}/issues",
                    "--method",
                    "POST",
                    "--input",
                    "-",
                ],
                canonical_json(payload),
            )
            if result.returncode != 0:
                _command_error(result, "github", "create_issue", uncertain=True)
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise PartialFailure("github", "parse_issue_receipt") from exc
        issue_number = int(data["number"])
        return DriverReceipt(
            provider="github",
            external_id=f"{request.repository}#{issue_number}",
            status="completed",
            response_fingerprint=fingerprint(data),
            reversible=True,
            reversal={"repository": request.repository, "issue_number": issue_number},
            metadata={"url": data.get("html_url"), "deduplicated": existing is not None},
        )

    def rollback(self, receipt: DriverReceipt) -> DriverReceipt:
        reversal = receipt.reversal or {}
        repository = str(reversal.get("repository", ""))
        issue_number = reversal.get("issue_number")
        if not repository or not isinstance(issue_number, int):
            raise RollbackUnavailable("github")
        result = self.runner(
            [
                self.gh_bin,
                "api",
                f"repos/{repository}/issues/{issue_number}",
                "--method",
                "PATCH",
                "--input",
                "-",
            ],
            canonical_json({"state": "closed"}),
        )
        if result.returncode != 0:
            _command_error(result, "github", "close_issue", uncertain=True)
        return DriverReceipt(
            provider="github",
            external_id=f"{repository}#{issue_number}",
            status="completed",
            response_fingerprint=fingerprint({"repository": repository, "issue": issue_number, "state": "closed"}),
            reversible=False,
            metadata={"state": "closed"},
        )

    def _find_existing(self, repository: str, marker: str) -> dict[str, Any] | None:
        result = self.runner(
            [
                self.gh_bin,
                "api",
                f"repos/{repository}/issues?state=all&per_page=100",
                "--method",
                "GET",
            ]
        )
        if result.returncode != 0:
            _command_error(result, "github", "list_issues")
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ConnectorFailure("github", "parse_issue_list") from exc
        for item in rows:
            if isinstance(item, dict) and marker in str(item.get("body") or ""):
                return item
        return None


class GitHubDeploymentDriver:
    kind = EffectKind.DEPLOYMENT_TRIGGER

    def __init__(self, gh_bin: str = "gh", runner: CommandRunner = subprocess_runner) -> None:
        self.gh_bin = gh_bin
        self.runner = runner

    def execute(self, request: DeploymentRequest, idempotency_key: str) -> DriverReceipt:
        self._dispatch(request.repository, request.workflow_id, request.ref, request.inputs)
        correlation = fingerprint(
            {
                "repository": request.repository,
                "workflow": request.workflow_id,
                "ref": request.ref,
                "key": idempotency_key,
            }
        )
        reversible = bool(request.rollback_workflow_id)
        return DriverReceipt(
            provider="github-actions",
            external_id=f"workflow_dispatch:{correlation[:32]}",
            status="accepted",
            response_fingerprint=correlation,
            reversible=reversible,
            reversal=(
                {
                    "repository": request.repository,
                    "workflow_id": request.rollback_workflow_id,
                    "ref": request.ref,
                    "inputs": request.rollback_inputs,
                }
                if reversible
                else None
            ),
            metadata={
                "repository": request.repository,
                "environment": request.environment,
                "workflow": request.workflow_id,
                "rollback": "workflow" if reversible else "unavailable",
            },
        )

    def rollback(self, receipt: DriverReceipt) -> DriverReceipt:
        reversal = receipt.reversal or {}
        repository = str(reversal.get("repository", ""))
        workflow_id = str(reversal.get("workflow_id", ""))
        ref = str(reversal.get("ref", ""))
        inputs = reversal.get("inputs") or {}
        if not repository or not workflow_id or not ref or not isinstance(inputs, dict):
            raise RollbackUnavailable("github-actions")
        self._dispatch(repository, workflow_id, ref, cast(dict[str, str], inputs))
        digest = fingerprint({"repository": repository, "workflow": workflow_id, "ref": ref})
        return DriverReceipt(
            provider="github-actions",
            external_id=f"rollback_dispatch:{digest[:32]}",
            status="accepted",
            response_fingerprint=digest,
            reversible=False,
            metadata={"state": "rollback_dispatched"},
        )

    def _dispatch(
        self,
        repository: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str],
    ) -> None:
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        result = self.runner(
            [
                self.gh_bin,
                "api",
                f"repos/{repository}/actions/workflows/{workflow_id}/dispatches",
                "--method",
                "POST",
                "--input",
                "-",
            ],
            canonical_json(payload),
        )
        if result.returncode != 0:
            _command_error(result, "github-actions", "workflow_dispatch", uncertain=True)


class SandboxPurchaseDriver:
    kind = EffectKind.PURCHASE_CREATE

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS sandbox_purchases (
              id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL UNIQUE,
              vendor_id TEXT NOT NULL,
              sku TEXT NOT NULL,
              amount_minor INTEGER NOT NULL,
              currency TEXT NOT NULL,
              state TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        self.db.commit()
        path.chmod(0o600)

    def execute(self, request: PurchaseRequest, idempotency_key: str) -> DriverReceipt:
        if request.mode != "test":
            raise ConnectorFailure("sandbox-purchase", "live_purchase_not_configured")
        purchase_id = "test_purchase_" + fingerprint({"key": idempotency_key})[:24]
        stamp = datetime.now(UTC).isoformat()
        with self.lock:
            self.db.execute(
                """
                INSERT INTO sandbox_purchases(
                  id,idempotency_key,vendor_id,sku,amount_minor,currency,state,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    purchase_id,
                    idempotency_key,
                    request.vendor_id,
                    request.sku,
                    request.total_amount_minor,
                    request.currency,
                    "authorized",
                    stamp,
                    stamp,
                ),
            )
            self.db.commit()
        return DriverReceipt(
            provider="sandbox-purchase",
            external_id=purchase_id,
            status="completed",
            response_fingerprint=fingerprint({"purchase": purchase_id, "state": "authorized"}),
            reversible=True,
            reversal={"purchase_id": purchase_id, "operation": "void"},
            metadata={"mode": "test", "state": "authorized"},
        )

    def rollback(self, receipt: DriverReceipt) -> DriverReceipt:
        purchase_id = str((receipt.reversal or {}).get("purchase_id", ""))
        if not purchase_id:
            raise RollbackUnavailable("sandbox-purchase")
        stamp = datetime.now(UTC).isoformat()
        with self.lock:
            updated = self.db.execute(
                "UPDATE sandbox_purchases SET state='voided',updated_at=? WHERE id=?",
                (stamp, purchase_id),
            ).rowcount
            self.db.commit()
        if not updated:
            raise ConnectorFailure("sandbox-purchase", "void")
        return DriverReceipt(
            provider="sandbox-purchase",
            external_id=purchase_id,
            status="completed",
            response_fingerprint=fingerprint({"purchase": purchase_id, "state": "voided"}),
            reversible=False,
            metadata={"mode": "test", "state": "voided"},
        )


def _google_response(response: HttpResponse, operation: str) -> dict[str, Any]:
    if response.status_code == 401:
        raise AuthenticationExpired("google-calendar")
    if response.status_code == 429:
        value = response.headers.get("Retry-After") if response.headers else None
        raise RateLimited("google-calendar", int(value) if value and str(value).isdigit() else None)
    if response.status_code >= 500:
        raise ConnectorFailure("google-calendar", operation, retryable=True)
    if response.status_code >= 400:
        raise ConnectorFailure("google-calendar", operation)
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _command_error(
    result: CommandResult,
    provider: str,
    operation: str,
    *,
    uncertain: bool = False,
) -> None:
    error = result.stderr.lower()
    if "rate limit" in error or "http 429" in error:
        retry = re.search(r"retry[- ]after\D+(\d+)", error)
        raise RateLimited(provider, int(retry.group(1)) if retry else None)
    if "http 401" in error or "bad credentials" in error or "unauthorized" in error:
        raise AuthenticationExpired(provider)
    if uncertain:
        raise PartialFailure(provider, operation)
    raise ConnectorFailure(provider, operation, retryable="timeout" in error)


def _nested_value(value: Any, keys: set[str]) -> Any | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = _nested_value(item, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _nested_value(item, keys)
            if found is not None:
                return found
    return None

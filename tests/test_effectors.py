from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from followthrough.effectors.drivers import (
    CommandResult,
    GitHubDeploymentDriver,
    GitHubIssueDriver,
    GoogleCalendarDriver,
    GoogleTokenProvider,
    HermesDiscordDriver,
    PrivateTaskDriver,
    SandboxPurchaseDriver,
)
from followthrough.effectors.errors import (
    AuthenticationExpired,
    InvalidTransition,
    PartialFailure,
    RateLimited,
)
from followthrough.effectors.journal import EffectJournal
from followthrough.effectors.models import (
    CalendarEventRequest,
    DeploymentRequest,
    DiscordMessageRequest,
    DriverReceipt,
    EffectKind,
    EffectRequest,
    EffectState,
    GitHubIssueRequest,
    PrivateTaskRequest,
    PurchaseRequest,
    fingerprint,
)
from followthrough.effectors.policy import AutonomyPolicy, EffectRule
from followthrough.effectors.service import EffectService
from followthrough.effectors.models import PolicyMode


NOW = datetime(2026, 7, 11, 15, 0, tzinfo=UTC)


def all_requests() -> list[EffectRequest]:
    return [
        PrivateTaskRequest(trigger_event_id="event-typed-0001", title="Review Omi research"),
        CalendarEventRequest(
            trigger_event_id="event-typed-0002",
            summary="Hermes review",
            start_at=NOW,
            end_at=NOW + timedelta(hours=1),
        ),
        DiscordMessageRequest(
            trigger_event_id="event-typed-0003",
            target="discord:1510104161612730378",
            body="Research completed.",
        ),
        GitHubIssueRequest(
            trigger_event_id="event-typed-0004",
            repository="yhinai/followthrough",
            title="Evaluate tool",
        ),
        DeploymentRequest(
            trigger_event_id="event-typed-0005",
            repository="yhinai/followthrough",
            workflow_id="deploy.yml",
            ref="main",
        ),
        PurchaseRequest(
            trigger_event_id="event-typed-0006",
            vendor_id="approved-vendor",
            sku="sandbox-plan",
            unit_amount_minor=500,
            mode="test",
        ),
    ]


def auto_policy(request: EffectRequest) -> AutonomyPolicy:
    return AutonomyPolicy(
        rules={
            request.kind: EffectRule(
                mode=PolicyMode.AUTO,
                allowed_targets=["discord:1510104161612730378"],
                allowed_repositories=["yhinai/followthrough"],
                allowed_vendors=["approved-vendor"],
                allow_live_purchase=True,
                allow_production=True,
                max_single_amount_minor=2_000,
                max_daily_amount_minor=5_000,
            )
        }
    )


class FakeDriver:
    def __init__(self, kind: EffectKind) -> None:
        self.kind = kind
        self.execute_calls = 0
        self.rollback_calls = 0
        self.error: Exception | None = None

    def execute(self, request: EffectRequest, idempotency_key: str) -> DriverReceipt:
        self.execute_calls += 1
        if self.error:
            raise self.error
        return DriverReceipt(
            provider="fake",
            external_id=f"external-{fingerprint(idempotency_key)[:12]}",
            status="completed",
            response_fingerprint=fingerprint({"kind": request.kind, "key": idempotency_key}),
            reversible=True,
            reversal={"operation": "undo"},
        )

    def rollback(self, receipt: DriverReceipt) -> DriverReceipt:
        self.rollback_calls += 1
        return DriverReceipt(
            provider="fake",
            external_id=receipt.external_id,
            status="completed",
            response_fingerprint=fingerprint({"external_id": receipt.external_id, "undo": True}),
            reversible=False,
        )


@pytest.mark.parametrize("action_request", all_requests(), ids=lambda item: item.kind.value)
def test_every_connector_is_idempotent_and_rollback_audited(
    tmp_path: Path, action_request: EffectRequest
) -> None:
    journal = EffectJournal(tmp_path / f"{action_request.kind.value}.db")
    driver = FakeDriver(action_request.kind)
    service = EffectService(
        journal,
        auto_policy(action_request),
        {action_request.kind: driver},
    )
    key = f"idempotency-{action_request.kind.value}-0001"

    first = service.submit(action_request, idempotency_key=key, execute=True)
    duplicate = service.submit(action_request, idempotency_key=key, execute=True)
    rolled_back = service.rollback(first["id"])

    assert first["state"] == EffectState.COMPLETED.value
    assert duplicate["id"] == first["id"]
    assert driver.execute_calls == 1
    assert driver.rollback_calls == 1
    assert rolled_back["state"] == EffectState.ROLLED_BACK.value
    assert {row["to_state"] for row in journal.history(first["id"])} >= {
        EffectState.EXECUTING.value,
        EffectState.COMPLETED.value,
        EffectState.ROLLED_BACK.value,
    }


@pytest.mark.parametrize(
    ("error_kind", "expected", "error_code"),
    [
        ("auth", EffectState.RETRYABLE_FAILURE, "authentication_expired"),
        ("rate", EffectState.RETRYABLE_FAILURE, "rate_limited"),
        ("partial", EffectState.UNCERTAIN, "partial_failure"),
    ],
)
@pytest.mark.parametrize("action_request", all_requests(), ids=lambda item: item.kind.value)
def test_every_connector_types_auth_rate_limit_and_partial_failures(
    tmp_path: Path,
    action_request: EffectRequest,
    error_kind: str,
    expected: EffectState,
    error_code: str,
) -> None:
    errors = {
        "auth": AuthenticationExpired("fake"),
        "rate": RateLimited("fake", 60),
        "partial": PartialFailure("fake", "write"),
    }
    driver = FakeDriver(action_request.kind)
    driver.error = errors[error_kind]
    journal = EffectJournal(tmp_path / f"{action_request.kind.value}-{error_kind}.db")
    service = EffectService(
        journal,
        auto_policy(action_request),
        {action_request.kind: driver},
    )

    result = service.submit(
        action_request,
        idempotency_key=f"failure-{action_request.kind.value}-{error_kind}-0001",
        execute=True,
    )

    assert result["state"] == expected.value
    assert result["last_error"]["code"] == error_code
    if expected == EffectState.UNCERTAIN:
        with pytest.raises(InvalidTransition):
            service.execute(result["id"])
        assert driver.execute_calls == 1


def test_uncertain_effect_requires_manual_reconciliation_before_retry(tmp_path: Path) -> None:
    request = PrivateTaskRequest(trigger_event_id="event-partial-0001", title="Partial")
    driver = FakeDriver(request.kind)
    driver.error = PartialFailure("fake", "write")
    service = EffectService(
        EffectJournal(tmp_path / "effects.db"),
        auto_policy(request),
        {request.kind: driver},
    )
    uncertain = service.submit(request, idempotency_key="partial-idempotency-0001", execute=True)

    reconciled = service.resolve_uncertain(
        uncertain["id"], applied=False, principal="local-owner"
    )
    driver.error = None
    completed = service.execute(uncertain["id"])

    assert reconciled["state"] == EffectState.RETRYABLE_FAILURE.value
    assert completed["state"] == EffectState.COMPLETED.value
    assert driver.execute_calls == 2


def test_unexpected_driver_exception_is_uncertain_not_retryable(tmp_path: Path) -> None:
    request = PrivateTaskRequest(trigger_event_id="event-unknown-0001", title="Unknown")
    driver = FakeDriver(request.kind)
    driver.error = RuntimeError("provider response was lost")
    service = EffectService(
        EffectJournal(tmp_path / "effects.db"),
        auto_policy(request),
        {request.kind: driver},
    )

    result = service.submit(request, idempotency_key="unknown-idempotency-0001", execute=True)

    assert result["state"] == EffectState.UNCERTAIN.value
    assert result["last_error"]["code"] == "partial_failure"


def test_crash_recovery_parks_inflight_effect_without_resending(tmp_path: Path) -> None:
    request = PrivateTaskRequest(trigger_event_id="event-recovery-0001", title="Recover")
    journal = EffectJournal(tmp_path / "effects" / "effects.db")
    record, _ = journal.register(
        request=request,
        idempotency_key="recovery-idempotency-0001",
        decision=auto_policy(request).decide(request),
    )
    journal.claim_execution(record["id"])

    recovered = journal.recover_inflight(principal="restart-test")

    assert len(recovered) == 1
    assert recovered[0]["state"] == EffectState.UNCERTAIN.value
    assert recovered[0]["last_error"]["code"] == "crash_interrupted"
    assert (tmp_path / "effects").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "effects" / "effects.db").stat().st_mode & 0o777 == 0o600


def test_policy_keeps_external_and_financial_risk_bounded() -> None:
    policy = AutonomyPolicy.maximum_bounded(
        owner_discord_target="discord:1510104161612730378",
        repositories=["yhinai/followthrough"],
        live_purchase_vendors=["approved-vendor"],
        allow_live_purchase=False,
        allow_production=False,
    )
    owner_message = DiscordMessageRequest(
        trigger_event_id="event-policy-0001",
        target="discord:1510104161612730378",
        body="Owner report",
    )
    external_message = owner_message.model_copy(
        update={"target": "discord:123456789012345", "owner_only": False}
    )
    production = DeploymentRequest(
        trigger_event_id="event-policy-0002",
        repository="yhinai/followthrough",
        workflow_id="deploy.yml",
        ref="main",
        environment="production",
    )
    live_purchase = PurchaseRequest(
        trigger_event_id="event-policy-0003",
        vendor_id="approved-vendor",
        sku="service",
        unit_amount_minor=1_000,
        mode="live",
    )

    assert policy.decide(owner_message).mode == PolicyMode.AUTO
    assert policy.decide(external_message).mode == PolicyMode.APPROVAL
    assert policy.decide(production).mode == PolicyMode.APPROVAL
    assert policy.decide(live_purchase).mode == PolicyMode.APPROVAL


def test_live_purchase_daily_cap_is_atomic(tmp_path: Path) -> None:
    policy = AutonomyPolicy.maximum_bounded(
        owner_discord_target="discord:1510104161612730378",
        repositories=[],
        live_purchase_vendors=["approved-vendor"],
        max_single_purchase_minor=800,
        max_daily_purchase_minor=1_000,
        allow_live_purchase=True,
    )
    journal = EffectJournal(tmp_path / "effects.db")
    first = PurchaseRequest(
        trigger_event_id="event-spend-0001",
        vendor_id="approved-vendor",
        sku="service-a",
        unit_amount_minor=600,
        mode="live",
    )
    second = first.model_copy(
        update={"trigger_event_id": "event-spend-0002", "sku": "service-b"}
    )
    driver = FakeDriver(first.kind)
    service = EffectService(journal, policy, {first.kind: driver})

    assert service.submit(first, idempotency_key="spend-idempotency-0001", execute=True)[
        "state"
    ] == EffectState.COMPLETED.value
    planned = service.submit(second, idempotency_key="spend-idempotency-0002")

    with pytest.raises(Exception) as exc_info:
        service.execute(planned["id"])
    assert getattr(exc_info.value, "code", None) == "policy_denied"
    assert driver.execute_calls == 1


def test_private_task_driver_is_retry_safe_and_reversible(tmp_path: Path) -> None:
    driver = PrivateTaskDriver(tmp_path / "tasks.db")
    request = PrivateTaskRequest(trigger_event_id="event-task-0001", title="Ship demo")

    first = driver.execute(request, "task-idempotency-0001")
    second = driver.execute(request, "task-idempotency-0001")
    rollback = driver.rollback(first)

    assert first.external_id == second.external_id
    assert rollback.metadata["state"] == "cancelled"
    assert (tmp_path / "tasks.db").stat().st_mode & 0o777 == 0o600


class FakeResponse:
    def __init__(self, status_code: int, data: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.data = data or {}
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self.data


class RecordingTransport:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def test_calendar_uses_deterministic_insert_and_delete_rollback() -> None:
    transport = RecordingTransport(
        [FakeResponse(404), FakeResponse(200, {"id": "event-id"}), FakeResponse(204)]
    )
    driver = GoogleCalendarDriver(lambda: "test-token", transport=transport)
    request = CalendarEventRequest(
        trigger_event_id="event-calendar-0001",
        summary="Agent review",
        start_at=NOW,
        end_at=NOW + timedelta(minutes=30),
    )

    receipt = driver.execute(request, "calendar-idempotency-0001")
    rollback = driver.rollback(receipt)

    assert transport.calls[0]["method"] == "GET"
    assert GoogleCalendarDriver.event_id("calendar-idempotency-0001") in transport.calls[0]["url"]
    assert transport.calls[1]["method"] == "POST"
    assert "sendUpdates=none" in transport.calls[1]["url"]
    assert transport.calls[2]["method"] == "DELETE"
    assert rollback.metadata["state"] == "deleted"


def test_calendar_replay_updates_existing_deterministic_event() -> None:
    transport = RecordingTransport(
        [FakeResponse(200, {"id": "event-id"}), FakeResponse(200, {"id": "event-id"})]
    )
    driver = GoogleCalendarDriver(lambda: "test-token", transport=transport)
    request = CalendarEventRequest(
        trigger_event_id="event-calendar-replay",
        summary="Updated review",
        start_at=NOW,
        end_at=NOW + timedelta(minutes=30),
    )

    driver.execute(request, "calendar-idempotency-replay")

    assert [call["method"] for call in transport.calls] == ["GET", "PUT"]


def test_google_token_provider_rejects_group_readable_credentials(tmp_path: Path) -> None:
    token = tmp_path / "google_token.json"
    client = tmp_path / "google_client_secret.json"
    token.write_text(json.dumps({"token": "not-a-real-token", "expiry": "2099-01-01T00:00:00Z"}))
    client.write_text(json.dumps({"installed": {"client_id": "test", "client_secret": "test"}}))
    token.chmod(0o644)
    client.chmod(0o600)
    provider = GoogleTokenProvider(token, client)

    with pytest.raises(AuthenticationExpired):
        provider()

    token.chmod(0o600)
    assert provider() == "not-a-real-token"


@pytest.mark.parametrize(("status", "error"), [(401, AuthenticationExpired), (429, RateLimited)])
def test_calendar_auth_and_rate_limit_fail_closed(status: int, error: type[Exception]) -> None:
    response = FakeResponse(status)
    if status == 429:
        response.headers["Retry-After"] = "30"
    driver = GoogleCalendarDriver(lambda: "test-token", transport=RecordingTransport([response]))
    request = CalendarEventRequest(
        trigger_event_id="event-calendar-fail",
        summary="Agent review",
        start_at=NOW,
        end_at=NOW + timedelta(minutes=30),
    )
    with pytest.raises(error):
        driver.execute(request, "calendar-idempotency-fail")


def test_discord_body_is_stdin_not_process_argument() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def runner(argv: list[str], stdin: str | None = None) -> CommandResult:
        calls.append((argv, stdin))
        return CommandResult(
            0,
            json.dumps({"message_id": "message-1", "channel_id": "channel-1"}),
            "",
        )

    driver = HermesDiscordDriver(hermes_bin="hermes", runner=runner, deleter=lambda _c, _m: True)
    request = DiscordMessageRequest(
        trigger_event_id="event-discord-0001",
        target="discord:1510104161612730378",
        body="private owner report",
    )
    receipt = driver.execute(request, "discord-idempotency-0001")
    rollback = driver.rollback(receipt)

    assert "private owner report" not in " ".join(calls[0][0])
    assert calls[0][1] == "private owner report"
    assert rollback.metadata["state"] == "deleted"


def test_github_issue_marker_deduplicates_and_close_is_typed() -> None:
    calls: list[tuple[list[str], str | None]] = []
    issue = {"number": 7, "html_url": "https://github.test/issue/7", "body": ""}

    def runner(argv: list[str], stdin: str | None = None) -> CommandResult:
        calls.append((argv, stdin))
        if "?state=all" in argv[2]:
            return CommandResult(0, "[]", "")
        if argv[2].endswith("/issues"):
            return CommandResult(0, json.dumps(issue), "")
        return CommandResult(0, json.dumps({**issue, "state": "closed"}), "")

    driver = GitHubIssueDriver(gh_bin="gh", runner=runner)
    request = GitHubIssueRequest(
        trigger_event_id="event-github-0001",
        repository="yhinai/followthrough",
        title="Typed issue",
        body="Details",
    )

    receipt = driver.execute(request, "github-idempotency-0001")
    rollback = driver.rollback(receipt)

    assert "followthrough:" in str(calls[1][1])
    assert rollback.metadata["state"] == "closed"


def test_deployment_dispatch_has_explicit_rollback_workflow() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def runner(argv: list[str], stdin: str | None = None) -> CommandResult:
        calls.append((argv, stdin))
        return CommandResult(0, "", "")

    driver = GitHubDeploymentDriver(gh_bin="gh", runner=runner)
    request = DeploymentRequest(
        trigger_event_id="event-deploy-0001",
        repository="yhinai/followthrough",
        workflow_id="deploy.yml",
        rollback_workflow_id="rollback.yml",
        ref="main",
    )

    receipt = driver.execute(request, "deployment-idempotency-0001")
    rollback = driver.rollback(receipt)

    assert "deploy.yml" in calls[0][0][2]
    assert "rollback.yml" in calls[1][0][2]
    assert rollback.metadata["state"] == "rollback_dispatched"


def test_purchase_driver_only_executes_test_mode(tmp_path: Path) -> None:
    driver = SandboxPurchaseDriver(tmp_path / "purchases.db")
    test_request = PurchaseRequest(
        trigger_event_id="event-purchase-0001",
        vendor_id="sandbox",
        sku="test-plan",
        unit_amount_minor=500,
    )
    live_request = test_request.model_copy(update={"mode": "live"})

    receipt = driver.execute(test_request, "purchase-idempotency-0001")
    rollback = driver.rollback(receipt)

    assert receipt.metadata["mode"] == "test"
    assert rollback.metadata["state"] == "voided"
    with pytest.raises(Exception) as exc_info:
        driver.execute(live_request, "purchase-idempotency-live")
    assert getattr(exc_info.value, "code", None) == "connector_failure"


def test_transition_log_rejects_mutation(tmp_path: Path) -> None:
    request = PrivateTaskRequest(trigger_event_id="event-audit-0001", title="Audit")
    journal = EffectJournal(tmp_path / "effects.db")
    record, _ = journal.register(
        request=request,
        idempotency_key="audit-idempotency-0001",
        decision=auto_policy(request).decide(request),
    )
    with pytest.raises(Exception):
        journal.db.execute(
            "UPDATE effect_transitions SET reason_code='rewritten' WHERE effect_id=?",
            (record["id"],),
        )

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from .drivers import (
    EffectDriver,
    GitHubDeploymentDriver,
    GitHubIssueDriver,
    GoogleCalendarDriver,
    GoogleTokenProvider,
    HermesDiscordDriver,
    PrivateTaskDriver,
    SandboxPurchaseDriver,
)
from .errors import ConnectorFailure, EffectorError, PartialFailure
from .journal import EffectJournal
from .models import DriverReceipt, EffectKind, EffectRequest, EffectState, PurchaseRequest
from .policy import AutonomyPolicy


class EffectService:
    def __init__(
        self,
        journal: EffectJournal,
        policy: AutonomyPolicy,
        drivers: dict[EffectKind, EffectDriver[Any]],
    ) -> None:
        self.journal = journal
        self.policy = policy
        self.drivers = drivers

    def submit(
        self,
        request: EffectRequest,
        *,
        idempotency_key: str,
        execute: bool = False,
    ) -> dict[str, Any]:
        decision = self.policy.decide(request)
        rule = self.policy.rules.get(request.kind)
        daily_cap = rule.max_daily_amount_minor if rule and isinstance(request, PurchaseRequest) else None
        record, _ = self.journal.register(
            request=request,
            idempotency_key=idempotency_key,
            decision=decision,
            daily_cap_minor=daily_cap,
        )
        if execute and record["state"] in {
            EffectState.READY.value,
            EffectState.RETRYABLE_FAILURE.value,
        }:
            return self.execute(record["id"])
        return record

    def approve(self, effect_id: str, *, principal: str) -> dict[str, Any]:
        return self.journal.approve(effect_id, principal=principal)

    def execute(self, effect_id: str) -> dict[str, Any]:
        current = self.journal.get(effect_id)
        if current["state"] == EffectState.COMPLETED.value:
            return current
        claimed = self.journal.claim_execution(effect_id)
        if claimed["state"] == EffectState.COMPLETED.value:
            return claimed
        request = self.journal.request(effect_id)
        driver = self.drivers.get(request.kind)
        if not driver:
            error = ConnectorFailure(request.kind.value, "driver_not_configured")
            return self.journal.fail(effect_id, error=error.safe_details())
        try:
            receipt = driver.execute(request, claimed["idempotency_key"])
        except EffectorError as exc:
            return self.journal.fail(effect_id, error=exc.safe_details())
        except Exception:
            error = PartialFailure(request.kind.value, "unexpected_driver_failure")
            return self.journal.fail(effect_id, error=error.safe_details())
        return self.journal.complete(effect_id, receipt)

    def rollback(self, effect_id: str) -> dict[str, Any]:
        current = self.journal.get(effect_id)
        receipt_raw = current.get("receipt")
        if not receipt_raw:
            error = ConnectorFailure(current["kind"], "receipt_missing")
            return current | {"rollback_error": error.safe_details()}
        driver = self.drivers.get(EffectKind(current["kind"]))
        if not driver:
            error = ConnectorFailure(current["kind"], "driver_not_configured")
            return current | {"rollback_error": error.safe_details()}
        self.journal.claim_rollback(effect_id)
        try:
            rollback_receipt = driver.rollback(DriverReceipt.model_validate(receipt_raw))
        except EffectorError as exc:
            return self.journal.fail_rollback(effect_id, error=exc.safe_details())
        except Exception:
            error = ConnectorFailure(current["kind"], "unexpected_rollback_failure")
            return self.journal.fail_rollback(effect_id, error=error.safe_details())
        return self.journal.complete_rollback(effect_id, rollback_receipt)

    def resolve_uncertain(
        self,
        effect_id: str,
        *,
        applied: bool,
        principal: str,
        receipt: DriverReceipt | None = None,
    ) -> dict[str, Any]:
        if applied:
            if receipt is None:
                raise ValueError("an applied outcome requires a provider receipt")
            return self.journal.resolve_uncertain_applied(
                effect_id,
                receipt=receipt,
                principal=principal,
            )
        return self.journal.resolve_uncertain_not_applied(effect_id, principal=principal)


def default_drivers(
    *,
    data_dir: Path,
    hermes_bin: str = "hermes",
    google_token_path: Path | None = None,
    google_client_secret_path: Path | None = None,
) -> dict[EffectKind, EffectDriver[Any]]:
    drivers: dict[EffectKind, EffectDriver[Any]] = {
        EffectKind.PRIVATE_TASK_CREATE: cast(
            EffectDriver[Any], PrivateTaskDriver(data_dir / "private-tasks.db")
        ),
        EffectKind.DISCORD_MESSAGE_SEND: cast(
            EffectDriver[Any], HermesDiscordDriver(hermes_bin=hermes_bin)
        ),
        EffectKind.GITHUB_ISSUE_CREATE: cast(EffectDriver[Any], GitHubIssueDriver()),
        EffectKind.DEPLOYMENT_TRIGGER: cast(EffectDriver[Any], GitHubDeploymentDriver()),
        EffectKind.PURCHASE_CREATE: cast(
            EffectDriver[Any], SandboxPurchaseDriver(data_dir / "sandbox-purchases.db")
        ),
    }
    if google_token_path and google_client_secret_path:
        drivers[EffectKind.CALENDAR_EVENT_UPSERT] = cast(
            EffectDriver[Any],
            GoogleCalendarDriver(
                GoogleTokenProvider(google_token_path, google_client_secret_path)
            ),
        )
    return drivers

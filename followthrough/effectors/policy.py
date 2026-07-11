from __future__ import annotations

from pydantic import BaseModel, Field

from .models import (
    CalendarEventRequest,
    DeploymentRequest,
    DiscordMessageRequest,
    EffectKind,
    EffectRequest,
    GitHubIssueRequest,
    PolicyMode,
    PurchaseRequest,
)


class EffectRule(BaseModel):
    mode: PolicyMode = PolicyMode.APPROVAL
    allowed_targets: list[str] = Field(default_factory=list)
    allowed_repositories: list[str] = Field(default_factory=list)
    allowed_vendors: list[str] = Field(default_factory=list)
    allow_attendee_notifications: bool = False
    allow_mentions: bool = False
    allow_production: bool = False
    allow_live_purchase: bool = False
    max_single_amount_minor: int | None = Field(default=None, ge=1)
    max_daily_amount_minor: int | None = Field(default=None, ge=1)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class PolicyDecision(BaseModel):
    mode: PolicyMode
    reason_code: str
    risk: str


class AutonomyPolicy(BaseModel):
    rules: dict[EffectKind, EffectRule]

    @classmethod
    def safe_default(cls, *, owner_discord_target: str | None = None) -> AutonomyPolicy:
        owner_targets = [owner_discord_target] if owner_discord_target else []
        return cls(
            rules={
                EffectKind.PRIVATE_TASK_CREATE: EffectRule(mode=PolicyMode.AUTO),
                EffectKind.CALENDAR_EVENT_UPSERT: EffectRule(mode=PolicyMode.APPROVAL),
                EffectKind.DISCORD_MESSAGE_SEND: EffectRule(
                    mode=PolicyMode.AUTO,
                    allowed_targets=owner_targets,
                ),
                EffectKind.GITHUB_ISSUE_CREATE: EffectRule(mode=PolicyMode.APPROVAL),
                EffectKind.DEPLOYMENT_TRIGGER: EffectRule(mode=PolicyMode.APPROVAL),
                EffectKind.PURCHASE_CREATE: EffectRule(
                    mode=PolicyMode.DRY_RUN,
                    max_single_amount_minor=2_500,
                    max_daily_amount_minor=10_000,
                ),
            }
        )

    @classmethod
    def maximum_bounded(
        cls,
        *,
        owner_discord_target: str,
        repositories: list[str],
        live_purchase_vendors: list[str] | None = None,
        max_single_purchase_minor: int = 2_500,
        max_daily_purchase_minor: int = 10_000,
        allow_production: bool = False,
        allow_live_purchase: bool = False,
    ) -> AutonomyPolicy:
        return cls(
            rules={
                EffectKind.PRIVATE_TASK_CREATE: EffectRule(mode=PolicyMode.AUTO),
                EffectKind.CALENDAR_EVENT_UPSERT: EffectRule(mode=PolicyMode.AUTO),
                EffectKind.DISCORD_MESSAGE_SEND: EffectRule(
                    mode=PolicyMode.AUTO,
                    allowed_targets=[owner_discord_target],
                ),
                EffectKind.GITHUB_ISSUE_CREATE: EffectRule(
                    mode=PolicyMode.AUTO,
                    allowed_repositories=repositories,
                ),
                EffectKind.DEPLOYMENT_TRIGGER: EffectRule(
                    mode=PolicyMode.AUTO,
                    allowed_repositories=repositories,
                    allow_production=allow_production,
                ),
                EffectKind.PURCHASE_CREATE: EffectRule(
                    mode=PolicyMode.AUTO,
                    allowed_vendors=live_purchase_vendors or [],
                    allow_live_purchase=allow_live_purchase,
                    max_single_amount_minor=max_single_purchase_minor,
                    max_daily_amount_minor=max_daily_purchase_minor,
                ),
            }
        )

    def decide(self, request: EffectRequest) -> PolicyDecision:
        rule = self.rules.get(request.kind, EffectRule(mode=PolicyMode.DENY))
        if rule.mode in {PolicyMode.DENY, PolicyMode.DRY_RUN}:
            return PolicyDecision(mode=rule.mode, reason_code=f"rule_{rule.mode.value}", risk="none")

        mode = rule.mode
        risk = "reversible"
        reason = "typed_rule"

        if isinstance(request, DiscordMessageRequest):
            if request.target not in rule.allowed_targets or not request.owner_only:
                mode, reason, risk = PolicyMode.APPROVAL, "non_owner_message", "external"
            elif request.allow_mentions and not rule.allow_mentions:
                mode, reason, risk = PolicyMode.APPROVAL, "mentions_not_autonomous", "external"
        elif isinstance(request, CalendarEventRequest):
            if request.notify_attendees and not rule.allow_attendee_notifications:
                mode, reason, risk = PolicyMode.APPROVAL, "attendee_notification", "external"
        elif isinstance(request, GitHubIssueRequest):
            if request.repository not in rule.allowed_repositories:
                mode, reason, risk = PolicyMode.APPROVAL, "repository_not_allowlisted", "external"
        elif isinstance(request, DeploymentRequest):
            risk = "irreversible" if request.environment == "production" else "external"
            if request.repository not in rule.allowed_repositories:
                mode, reason = PolicyMode.APPROVAL, "repository_not_allowlisted"
            elif request.environment == "production" and not rule.allow_production:
                mode, reason = PolicyMode.APPROVAL, "production_requires_approval"
        elif isinstance(request, PurchaseRequest):
            risk = "financial"
            if request.currency != rule.currency:
                return PolicyDecision(
                    mode=PolicyMode.DENY,
                    reason_code="currency_not_allowed",
                    risk=risk,
                )
            if rule.max_single_amount_minor is None or rule.max_daily_amount_minor is None:
                return PolicyDecision(mode=PolicyMode.DENY, reason_code="spend_caps_missing", risk=risk)
            if request.total_amount_minor > rule.max_single_amount_minor:
                return PolicyDecision(mode=PolicyMode.DENY, reason_code="single_spend_cap", risk=risk)
            if request.mode == "live":
                if request.vendor_id not in rule.allowed_vendors:
                    mode, reason = PolicyMode.APPROVAL, "vendor_not_allowlisted"
                elif not rule.allow_live_purchase:
                    mode, reason = PolicyMode.APPROVAL, "live_purchase_requires_approval"

        return PolicyDecision(mode=mode, reason_code=reason, risk=risk)

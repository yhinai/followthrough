from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, EmailStr, Field, TypeAdapter, field_validator, model_validator


class EffectKind(StrEnum):
    PRIVATE_TASK_CREATE = "private_task.create"
    CALENDAR_EVENT_UPSERT = "calendar.event.upsert"
    DISCORD_MESSAGE_SEND = "discord.message.send"
    GITHUB_ISSUE_CREATE = "github.issue.create"
    DEPLOYMENT_TRIGGER = "deployment.trigger"
    PURCHASE_CREATE = "purchase.create"


class EffectState(StrEnum):
    REGISTERED = "registered"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    DRY_RUN = "dry_run"
    DENIED = "denied"
    EXECUTING = "executing"
    RETRYABLE_FAILURE = "retryable_failure"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    COMPLETED = "completed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


class PolicyMode(StrEnum):
    AUTO = "auto"
    APPROVAL = "approval"
    DRY_RUN = "dry_run"
    DENY = "deny"


class EffectRequestBase(BaseModel):
    trigger_event_id: str = Field(min_length=8, max_length=300)


class PrivateTaskRequest(EffectRequestBase):
    kind: Literal[EffectKind.PRIVATE_TASK_CREATE] = EffectKind.PRIVATE_TASK_CREATE
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8_000)
    due_at: datetime | None = None
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", item) for item in value):
            raise ValueError("task tags must be short identifiers")
        return sorted(set(value))


class CalendarEventRequest(EffectRequestBase):
    kind: Literal[EffectKind.CALENDAR_EVENT_UPSERT] = EffectKind.CALENDAR_EVENT_UPSERT
    summary: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=8_000)
    start_at: datetime
    end_at: datetime
    timezone: str = Field(default="America/Los_Angeles", pattern=r"^[A-Za-z_+.-]+(?:/[A-Za-z0-9_+.-]+)*$")
    calendar_id: str = Field(default="primary", min_length=1, max_length=300)
    location: str = Field(default="", max_length=1_000)
    attendees: list[EmailStr] = Field(default_factory=list, max_length=50)
    notify_attendees: bool = False

    @model_validator(mode="after")
    def validate_interval(self) -> CalendarEventRequest:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("calendar timestamps must include an explicit UTC offset")
        if self.end_at <= self.start_at:
            raise ValueError("calendar event end must be after start")
        if self.notify_attendees and not self.attendees:
            raise ValueError("attendee notification requires at least one attendee")
        return self


class DiscordMessageRequest(EffectRequestBase):
    kind: Literal[EffectKind.DISCORD_MESSAGE_SEND] = EffectKind.DISCORD_MESSAGE_SEND
    target: str = Field(pattern=r"^discord:(?:[0-9]{5,30}|#[A-Za-z0-9_.-]{1,100})$")
    body: str = Field(min_length=1, max_length=1_900)
    subject: str = Field(default="", max_length=150)
    owner_only: bool = True
    allow_mentions: bool = False

    @model_validator(mode="after")
    def validate_body(self) -> DiscordMessageRequest:
        if re.search(r"(?im)^\s*MEDIA:", self.body):
            raise ValueError("attachments require a separate typed media effector")
        if not self.allow_mentions and re.search(r"@(everyone|here)\b|<@&?\d+>", self.body):
            raise ValueError("Discord mentions require explicit allow_mentions")
        return self


class GitHubIssueRequest(EffectRequestBase):
    kind: Literal[EffectKind.GITHUB_ISSUE_CREATE] = EffectKind.GITHUB_ISSUE_CREATE
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=60_000)
    labels: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 50 for item in value):
            raise ValueError("GitHub labels must be 1-50 characters")
        return list(dict.fromkeys(item.strip() for item in value))


class DeploymentRequest(EffectRequestBase):
    kind: Literal[EffectKind.DEPLOYMENT_TRIGGER] = EffectKind.DEPLOYMENT_TRIGGER
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,150}$")
    ref: str = Field(pattern=r"^[A-Za-z0-9_./-]{1,250}$")
    environment: Literal["preview", "staging", "production"] = "preview"
    inputs: dict[str, str] = Field(default_factory=dict)
    rollback_workflow_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,150}$")
    rollback_inputs: dict[str, str] = Field(default_factory=dict)

    @field_validator("inputs", "rollback_inputs")
    @classmethod
    def validate_inputs(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("deployment inputs are limited to 20 entries")
        for key, item in value.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", key) or len(item) > 1_000:
                raise ValueError("deployment inputs must be bounded scalar values")
        return value


class PurchaseRequest(EffectRequestBase):
    kind: Literal[EffectKind.PURCHASE_CREATE] = EffectKind.PURCHASE_CREATE
    vendor_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    sku: str = Field(pattern=r"^[A-Za-z0-9_./:-]{1,200}$")
    unit_amount_minor: int = Field(gt=0, le=10_000_000)
    quantity: int = Field(default=1, ge=1, le=100)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    mode: Literal["test", "live"] = "test"
    memo: str = Field(default="", max_length=1_000)

    @property
    def total_amount_minor(self) -> int:
        return self.unit_amount_minor * self.quantity


EffectRequest = Annotated[
    PrivateTaskRequest
    | CalendarEventRequest
    | DiscordMessageRequest
    | GitHubIssueRequest
    | DeploymentRequest
    | PurchaseRequest,
    Field(discriminator="kind"),
]
effect_request_adapter = TypeAdapter(EffectRequest)


class DriverReceipt(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=500)
    status: Literal["accepted", "completed"]
    response_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reversible: bool
    reversal: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def parse_request(raw: str | bytes | dict[str, Any]) -> EffectRequest:
    if isinstance(raw, (str, bytes)):
        raw = json.loads(raw)
    return effect_request_adapter.validate_python(raw)

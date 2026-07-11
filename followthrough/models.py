from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

from .controls import GlobalMode
from .relevance import Category, Disposition


class SignupIn(BaseModel):
    email: EmailStr
    source: str = "direct"


class SignalIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    source: Literal["web", "voice", "omi", "api", "demo"] = "web"
    email: EmailStr | None = None
    consent: bool = False
    allow_owner_report: bool = True


class TranscriptEventIn(BaseModel):
    event_id: str = Field(min_length=8, max_length=200)
    device_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=65_536)
    source: Literal["omi", "phone", "wearable", "web", "api", "demo"] = "omi"
    occurred_at: datetime | None = None
    consent: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class AudioChunkReceipt(BaseModel):
    event_id: str
    sequence: int
    plaintext_sha256: str
    plaintext_bytes: int
    created: bool


class RelevanceCorrectionIn(BaseModel):
    event_id: str = Field(min_length=8, max_length=300)
    disposition: Disposition
    categories: list[Category] = Field(default_factory=list, max_length=8)
    reason_code: str = Field(default="owner_correction", min_length=3, max_length=100)


class InterestWeightIn(BaseModel):
    category: Category
    weight: float = Field(ge=-1.0, le=1.0)
    source: str = Field(default="explicit", min_length=2, max_length=50)


class RoleIn(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    job: str = Field(min_length=8, max_length=500)
    tools: list[str] = Field(default_factory=list, max_length=10)
    guardrails: str = Field(min_length=8, max_length=500)


class GlobalControlIn(BaseModel):
    mode: GlobalMode
    reason_code: str = Field(min_length=3, max_length=120)
    actor: str = Field(default="owner:dashboard", min_length=3, max_length=120)
    resume_parked: bool = False


class CapabilityControlIn(BaseModel):
    enabled: bool
    reason_code: str = Field(min_length=3, max_length=120)
    actor: str = Field(default="owner:dashboard", min_length=3, max_length=120)
    resume_parked: bool = False


class CapabilityLimitIn(BaseModel):
    max_events: int | None = Field(default=None, ge=0)
    window_seconds: int = Field(ge=1, le=2_592_000)
    max_cost_usd: float | None = Field(default=None, ge=0)
    reason_code: str = Field(min_length=3, max_length=120)
    actor: str = Field(default="owner:dashboard", min_length=3, max_length=120)


class TaskControlIn(BaseModel):
    reason_code: str = Field(min_length=3, max_length=120)
    actor: str = Field(default="owner:dashboard", min_length=3, max_length=120)


class SafeModeIn(BaseModel):
    trigger: Literal[
        "prompt_injection",
        "credential_access",
        "unusual_spending",
        "repeated_failure",
        "policy_drift",
        "operator_safe_mode",
    ]
    actor: str = Field(default="owner:dashboard", min_length=3, max_length=120)


class ImprovementEvidenceIn(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class ImprovementProposalIn(BaseModel):
    target: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=1_048_576)
    evidence: list[ImprovementEvidenceIn] = Field(min_length=1, max_length=50)
    created_by: str = Field(default="hermes:candidate-generator", min_length=3, max_length=120)


class ImprovementEvalCaseIn(BaseModel):
    case_id: str = Field(min_length=1, max_length=120)
    baseline_passed: bool
    candidate_passed: bool


class ImprovementEvaluationIn(BaseModel):
    evaluator_id: str = Field(default="deterministic:followthrough-v1", min_length=3, max_length=120)
    held_in: list[ImprovementEvalCaseIn] = Field(min_length=1, max_length=1000)
    held_out: list[ImprovementEvalCaseIn] = Field(min_length=1, max_length=1000)


class ImprovementPolicyIn(BaseModel):
    live_enabled: bool = False
    allowed_roots: list[str] = Field(default_factory=list, max_length=10)
    required_approver_prefix: str = Field(default="owner:", min_length=6, max_length=40)
    actor: str = Field(default="owner:dashboard", min_length=6, max_length=120)


class ImprovementPromotionIn(BaseModel):
    approved_by: str = Field(min_length=3, max_length=120)
    approval_reference: str = Field(min_length=8, max_length=160)
    live_root: str | None = Field(default=None, max_length=1000)


class StepView(BaseModel):
    id: str
    run_id: str
    agent: str
    status: str
    input_summary: str
    output: dict[str, Any]
    started_at: str
    finished_at: str | None
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class RunView(BaseModel):
    id: str
    status: str
    source: str
    signal_type: str
    title: str
    created_at: str
    finished_at: str | None
    latency_ms: int
    success: bool | None
    report_url: str | None
    voice_url: str | None
    summary: str | None
    steps: list[StepView] = Field(default_factory=list)

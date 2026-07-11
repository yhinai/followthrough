from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class SignupIn(BaseModel):
    email: EmailStr
    source: str = "direct"


class SignalIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    source: Literal["web", "voice", "omi", "api", "demo"] = "web"
    email: EmailStr | None = None
    consent: bool = False
    allow_owner_report: bool = True


class RoleIn(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    job: str = Field(min_length=8, max_length=500)
    tools: list[str] = Field(default_factory=list, max_length=10)
    guardrails: str = Field(min_length=8, max_length=500)


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


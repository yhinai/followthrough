from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from typing import Any


def manager_plan(signal: str, context: dict[str, Any], binary: str = "hermes", timeout: int = 55) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""You are the Followthrough manager agent running on Hermes. Return only valid JSON.
Signal: {signal}
Context: {json.dumps(context, default=str)}
Available specialists: entity_resolver, linkup_researcher, opportunity_scorer, relationship_writer, crm_operator, qa_policy, briefing.
Plan the smallest complete BizDev job. Return keys: plan (array of {{agent, job, depends_on}}), user_value, policy.
policy must be research_only, draft_for_approval, or safe_send. Only choose safe_send when the signal contains an explicit recipient and explicit permission."""
    usage_path = tempfile.NamedTemporaryFile(prefix="followthrough-usage-", suffix=".json", delete=False).name
    started = time.perf_counter()
    try:
        proc = subprocess.run([binary, "--oneshot", prompt, "--usage-file", usage_path], capture_output=True, text=True, timeout=timeout, env={**os.environ, "HERMES_SOURCE": "followthrough"})
        raw = proc.stdout.strip()
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError:
            plan = {"plan": [{"agent": "linkup_researcher", "job": "research the named opportunity", "depends_on": []}, {"agent": "opportunity_scorer", "job": "score fit and recommend the next action", "depends_on": ["linkup_researcher"]}, {"agent": "relationship_writer", "job": "draft a safe follow-up", "depends_on": ["opportunity_scorer"]}], "user_value": raw[:600] or "Research brief ready", "policy": "draft_for_approval", "fallback": True}
        usage: dict[str, Any] = {"latency_ms": int((time.perf_counter() - started) * 1000), "estimated_cost_usd": 0.0}
        try:
            usage.update(json.loads(open(usage_path).read()))
        except (OSError, json.JSONDecodeError):
            pass
        return plan, usage
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"plan": [{"agent": "entity_resolver", "job": "extract entities", "depends_on": []}, {"agent": "relationship_writer", "job": "draft a safe follow-up", "depends_on": ["entity_resolver"]}], "user_value": "A safe draft is ready", "policy": "draft_for_approval", "fallback": str(exc)}, {"latency_ms": int((time.perf_counter() - started) * 1000), "estimated_cost_usd": 0.0}

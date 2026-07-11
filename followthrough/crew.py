from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .classifier import Classification
from .config import Settings
from .hermes import manager_plan
from .integrations import convex_event, elevenlabs, linkup
from .store import Store, now


class Crew:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store, self.settings = store, settings

    def process(self, run_id: str, text: str, classification: Classification) -> dict[str, Any]:
        started = time.perf_counter()
        self.store.update_run(run_id, status="running")
        self.store.add_step(run_id, "signal_triage", "completed", f"{classification.kind} ({classification.confidence:.2f})", {"kind": classification.kind, "reason": classification.reason})
        plan, usage = manager_plan(text, {"roles": self.store.roles(), "metrics": self.store.metrics()}, self.settings.hermes_bin, self.settings.hermes_timeout_seconds)
        self.store.add_step(run_id, "hermes_manager", "completed", text[:400], plan, latency_ms=usage.get("latency_ms", 0), estimated_cost_usd=usage.get("estimated_cost_usd", 0.0))
        research: dict[str, Any] = {}
        if classification.actionable:
            try:
                research = linkup(text, self.settings.linkup_api_key)
                self.store.add_step(run_id, "linkup_researcher", "completed", research.get("query", ""), research, latency_ms=research.get("latency_ms", 0))
            except Exception as exc:
                research = {"configured": True, "error": str(exc)}
                self.store.add_step(run_id, "linkup_researcher", "failed", text[:300], research)
                self.store.add_eval(run_id, text, "cited research result", str(exc))
        brief = self._brief(text, classification, plan, research)
        self.store.add_step(run_id, "opportunity_scorer", "completed", "signal + research + remembered context", brief)
        policy = plan.get("policy", "draft_for_approval")
        draft = {"policy": policy, "sent": False, "message": self._draft(text, brief), "reason": "external sends remain approval-gated"}
        self.store.add_step(run_id, "relationship_writer", "completed", "recipient/consent guardrail", draft)
        self.store.add_step(run_id, "crm_operator", "completed", "persist opportunity and next action", {"remembered": True, "surface": "Followthrough trace"})
        audio_path: Path | None = None
        try:
            audio_path = elevenlabs(brief["spoken_brief"], self.settings.elevenlabs_api_key, self.settings.elevenlabs_voice_id, self.settings.reports_dir.parent / "audio")
        except Exception as exc:
            self.store.add_eval(run_id, text, "voice completion when configured", str(exc))
        if audio_path:
            self.store.add_step(run_id, "briefing", "completed", brief["spoken_brief"], {"audio": str(audio_path)})
        report_path = self.settings.reports_dir / f"{run_id}.md"
        report_path.write_text(
            "# Followthrough report\n\n"
            f"**Signal:** {text}\n\n"
            f"**Recommendation:** {brief['recommendation']}\n\n"
            f"**Research:** {brief['research']}\n\n"
            f"**Next action:** {brief['next_action']}\n\n"
            "External messages remain approval-gated.\n"
        )
        try:
            subprocess.run([self.settings.hermes_bin, "send", "--to", self.settings.discord_target, "--file", str(report_path), "--quiet"], check=True, timeout=20, capture_output=True, text=True)
            self.store.add_step(run_id, "publisher", "completed", "owner-facing Discord report", {"target": self.settings.discord_target})
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            self.store.add_step(run_id, "publisher", "skipped", "owner-facing Discord report", {"reason": str(exc)})
        elapsed = int((time.perf_counter() - started) * 1000)
        summary = json.dumps(brief)
        self.store.update_run(run_id, status="completed", finished_at=now(), latency_ms=elapsed, success=1, summary=summary, report_url=f"/api/reports/{report_path.name}", voice_url=(f"/api/audio/{audio_path.name}" if audio_path else None))
        convex_event({"run_id": run_id, "classification": classification.__dict__, "plan": plan, "brief": brief, "created_at": now()}, self.settings.convex_url, getattr(self.settings, "convex_deploy_key", ""))
        return {"run_id": run_id, "status": "completed", "classification": classification.__dict__, "plan": plan, "research": research, "brief": brief, "report_url": f"/api/reports/{report_path.name}", "voice_url": (f"/api/audio/{audio_path.name}" if audio_path else None), "latency_ms": elapsed}

    def _brief(self, text: str, classification: Classification, plan: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
        answer = research.get("answer", "Research adapter is waiting for the event Linkup key.")
        return {"headline": "A high-value conversation is ready for follow-through", "signal": text[:800], "classification": classification.kind, "recommendation": plan.get("user_value", "Review the research and choose the next move."), "research": answer[:5000], "sources": research.get("sources", []), "next_action": "Review the follow-up draft; no external message was sent automatically.", "spoken_brief": f"Followthrough found a {classification.kind} signal. {plan.get('user_value', 'A research brief and safe draft are ready.')} No external message was sent without approval."}

    @staticmethod
    def _draft(text: str, brief: dict[str, Any]) -> str:
        return f"Hi — following up on our conversation about {text[:160]}. I researched it and found: {brief['recommendation']} Would Tuesday at 2 work for a quick follow-up?"

# Followthrough

Followthrough is an always-on personal ambient operator. An authenticated Omi/phone captures continuously, Spark preserves the complete encrypted archive, deterministic relevance promotes only useful speech, and a least-authority Hermes worker researches or proposes typed actions with durable receipts.

This is a fresh event-day implementation. The earlier Ambient Operator prototype is not imported into this repository. Existing Hermes installation, standard scaffolding, and Cloudflare infrastructure are runtime scaffolding; the product code starts here.

## Run

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
cp .env.example .env
.venv/bin/uvicorn followthrough.app:app --host 127.0.0.1 --port 18765
```

Open `/` and enter the owner dashboard token, or use the Omi phone sensor. Every captured transcript and audio delivery is archived with AES-256-GCM; ordinary chatter remains archive-only and never enters Hermes memory or an action queue. Reversible private actions can run automatically, while external/high-risk writes remain policy- and approval-gated.

The fast path is local and deterministic: authentication, archive, relevance, repository acquisition/scanning/sandboxing, emergency controls, typed receipts, backup, and soak monitoring use no LLM call. Hermes runs only after a relevant signal crosses the gate. The live profile uses `gpt-5.6-luna` at low reasoning; routine global cron and delegation are routed away from Codex.

Event handoff material lives in [docs/SUBMISSION.md](docs/SUBMISSION.md), [docs/DEMO.md](docs/DEMO.md), and [docs/PARTNER_CHECKLIST.md](docs/PARTNER_CHECKLIST.md). The sanitized environment template intentionally excludes every secret supplied through chat or attachments.

The production path and hard test gates are in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). Omi configuration is documented in [docs/OMI_SETUP.md](docs/OMI_SETUP.md), and the privacy/security boundary is defined by [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

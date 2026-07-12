# Followthrough

Followthrough is an always-on personal ambient operator. Authenticated Memo Android sensors capture continuously, Spark preserves the complete archive with a simple local storage path, deterministic relevance promotes only useful speech, and a least-authority Hermes worker researches or proposes typed actions with durable receipts. The channel is bidirectional: Memo receives a durable job ID, resumes polling after an app restart, and speaks the Hermes result through the phone's built-in loudspeaker.

This is a fresh event-day implementation. The earlier Ambient Operator prototype is not imported into this repository. Existing Hermes installation, standard scaffolding, and Cloudflare infrastructure are runtime scaffolding; the product code starts here.

## Run

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
cp .env.example .env
.venv/bin/uvicorn followthrough.app:app --host 127.0.0.1 --port 18765
```

Open `/` and enter the owner dashboard token, or use the Memo Android sensor. Every captured transcript and audio delivery is stored as plain files and SQLite rows in the local archive; ordinary chatter remains archive-only and never enters Hermes memory or an action queue. Reversible private actions can run automatically, while external/high-risk writes remain policy- and approval-gated.

The fast path is local and deterministic: authentication, archive, relevance, repository acquisition/scanning/sandboxing, emergency controls, typed receipts, backup, and soak monitoring use no LLM call. Hermes runs only after a relevant signal crosses the gate. The live profile uses `gpt-5.6-luna` at low reasoning; routine global cron and delegation are routed away from Codex.

Memo installation and replacement state are documented in [docs/MEMO_SETUP.md](docs/MEMO_SETUP.md). The older Omi guide remains only as rollback/history documentation.

Event handoff material lives in [docs/SUBMISSION.md](docs/SUBMISSION.md), [docs/DEMO.md](docs/DEMO.md), and [docs/PARTNER_CHECKLIST.md](docs/PARTNER_CHECKLIST.md). The sanitized environment template intentionally excludes every secret supplied through chat or attachments.

The verified current state is in [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md). The production path and hard test gates are in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md), the retired Omi path is documented in [docs/OMI_SETUP.md](docs/OMI_SETUP.md), and the privacy/security boundary is defined by [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md).

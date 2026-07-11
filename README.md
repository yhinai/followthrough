# Followthrough

Followthrough is a consent-first ambient BizDev agency for founders. It turns a real conversation into signal triage, dynamic Hermes delegation, live research, a relationship brief, a safe follow-up draft, CRM memory, and a spoken completion report.

This is a fresh event-day implementation. The earlier Ambient Operator prototype is not imported into this repository. Existing Hermes installation, standard scaffolding, and Cloudflare infrastructure are runtime scaffolding; the product code starts here.

## Run

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
cp .env.example .env
.venv/bin/uvicorn followthrough.app:app --host 127.0.0.1 --port 18765
```

Open `/`. Enable the visibly consented listening session or use the live sample. Ordinary chatter is classified and discarded before persistence. External messages are approval-gated.

Partner keys are optional adapters until the event accounts are configured: Linkup powers live cited research, ElevenLabs produces the spoken completion, Convex mirrors authoritative run state, and Dodo is reserved for the live Event Pass checkout.

Event handoff material lives in [docs/SUBMISSION.md](docs/SUBMISSION.md), [docs/DEMO.md](docs/DEMO.md), and [docs/PARTNER_CHECKLIST.md](docs/PARTNER_CHECKLIST.md). The sanitized environment template intentionally excludes every secret supplied through chat or attachments.

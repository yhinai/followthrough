# Followthrough

Followthrough is an always-on personal ambient operator. Memo Android sensors capture continuously, the local archive keeps every transcript and audio chunk as plain files and SQLite rows, deterministic relevance promotes only useful speech, and a Hermes worker researches or proposes typed actions with durable receipts. The channel is bidirectional: Memo receives a durable job ID, resumes polling after an app restart, and speaks the Hermes result through the phone's built-in loudspeaker.

This is a testing / proof-of-concept build: no authentication, no encryption, no backups — just the core flow.

## Run

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/uvicorn followthrough.app:app --port 18765
```

Open `http://localhost:18765/` for the live dashboard, or point the Memo Android app at the server. Ordinary chatter is archived and never enters Hermes memory or an action queue; only relevant signals become durable Hermes jobs. Reversible private actions run automatically, while external/high-risk writes stay policy-gated.

The fast path is local and deterministic — archive, relevance, repository scanning/sandboxing, emergency controls, and typed receipts use no LLM call. Hermes runs only after a relevant signal crosses the gate.

## Layout

| Piece | Where |
|---|---|
| API server + dashboard | `followthrough/app.py`, `followthrough/static/` |
| Relevance gate | `followthrough/relevance.py` |
| Archive (plaintext) | `followthrough/archive.py`, `followthrough/archive_store.py` |
| Hermes orchestration | `followthrough/kanban.py`, `followthrough/runner.py`, `scripts/followthrough-orchestrator.py` |
| Typed external actions | `followthrough/effectors/` ([docs/EFFECTORS.md](docs/EFFECTORS.md)) |
| Emergency controls | `followthrough/controls.py` ([docs/EMERGENCY_CONTROLS.md](docs/EMERGENCY_CONTROLS.md)) |
| Phone bridge | `followthrough/adb_bridge.py`, `scripts/followthrough-adb-bridge.py` |

Memo phone setup is in [docs/MEMO_SETUP.md](docs/MEMO_SETUP.md), the demo script in [docs/DEMO.md](docs/DEMO.md), and the verified system state in [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md).

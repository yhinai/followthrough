# Phase 6 deterministic evidence — 2026-07-11

Implemented:

- Persistent running/paused/killed global mode.
- Eight independent capability switches.
- Idempotent rolling rate and USD budget reservations.
- Durable Hermes park/resume command outbox and verified CLI adapter.
- Hash-chained, transcript-free audit receipts.
- Automatic paused safe mode for spending anomalies, repeated emergency-command
  failures, and evaluator policy drift, with typed scanner/effector triggers.
- Candidate-only self-improvement with pinned evaluator, evidence, unsafe scan,
  held-in/held-out no-regression gate, explicit live policy, receipts, and
  rollback.

Deterministic verification:

```text
pytest tests/test_controls.py tests/test_self_improvement.py tests/test_kanban.py
30 passed
```

Coverage includes persistence across a new Store/ControlPlane instance,
fail-closed global kill, API capture denial inside the configured RTO,
idempotent rate limits, spend limits, audit tamper detection, two-phase task
parking/resumption, held-out regression rejection, protected-evaluator target
rejection, post-evaluation tamper rejection, explicit live owner approval, and
rollback.

The initial implementation subtask did not restart the shared services. The
later installed-service acceptance run closed that boundary with a 6.14-second
pause and a 5.236-second least-authority resume, both inside the 10-second RTO.

## Current post-reset candidate proof

- Proposal `5d5d7efe-c187-474a-b7cf-cc9b420526d7` was generated for bounded ambient-command aggregation.
- Every deterministic gate passed: candidate integrity, safe target, unsafe-content scan, evidence digest, pinned evaluator, held-in cases, and held-out no-regression cases.
- Live promotion remains disabled. The approved artifact was promoted only to the isolated staged directory with status `promoted_staged`; no Hermes skill or runtime file was changed.
- Audit receipts: proposal `29c6f97c-3a18-490f-b6e0-f5f3db1e83dd`; evaluation `32d16fcc-533d-4ae1-9d5f-5d3a2b730c1e`.

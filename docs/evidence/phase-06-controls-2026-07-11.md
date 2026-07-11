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

Boundary: the shared live systemd services were not restarted as part of this
implementation subtask. The deterministic gate passes; the installed-service
park latency still needs a live reload-and-RTO receipt before P6 is marked
fully accepted.

# Phase 7 acceptance and soak monitor

The current final run monitors the deployed two-way Memo build. An intentional service restart invalidates that run; archive its ledger and begin a fresh run only after public health stabilizes. See [CURRENT_STATE.md](CURRENT_STATE.md).

`scripts/followthrough-soak.py` is a read-only acceptance monitor for a live
Followthrough installation. It writes one mode-0600, hash-chained JSONL receipt
file. Each run appends a `run_start`, one or more `checkpoint` records, and a
final `run_summary`; it never rewrites an earlier record.

The monitor samples:

- public `/healthz` readiness and latency;
- read-only SQLite `PRAGMA integrity_check` plus table counts;
- duplicate archive event, Hermes job, and effect idempotency keys;
- duplicate external effect receipts and semantic effect requests (semantic
  repeats are diagnostic warnings; only stable-key or provider-receipt
  collisions fail the checkpoint);
- native audio chunk and Omi capture-stream sequence gaps, orphan rows,
  missing/empty encrypted audio files, and broken archive-to-operations links;
- the full SHA-256 `control_audit` chain;
- effect-to-transition journal links;
- user-service PID, restart counter, active state, and PID changes between
  checkpoints;
- free bytes and percentage used for every distinct monitored filesystem; and
- table-count regressions between checkpoints as a loss signal.

It does not decrypt transcripts or audio, emit raw event/job/effect keys, inject
faults, mutate a database, stop/restart a service, or execute a recovery action.
Only the checkpoint directory/file is created. Run deliberate restart,
corruption, network-loss, backup/restore, and disk-pressure exercises as separate
operator-controlled tests; this monitor only observes their effects.

## One-checkpoint smoke test

From `/home/alhinai/Projects/followthrough`:

```bash
.venv/bin/python scripts/followthrough-soak.py --once \
  --output data/soak/phase-07-smoke.jsonl
```

The command exits `0` only when every check passes. It exits `2` when the
checkpoint is successfully recorded but any acceptance check fails. The final
line printed to stdout identifies the absolute checkpoint file.

## Bounded 24-hour acceptance run

```bash
.venv/bin/python scripts/followthrough-soak.py \
  --duration-seconds 86400 \
  --interval-seconds 60 \
  --max-duration-seconds 90000 \
  --output data/soak/phase-07-24h.jsonl
```

`--max-duration-seconds` is a hard safety bound. A requested duration above it
is rejected before the ledger is opened. `Ctrl-C` appends an interrupted final
summary and exits `130`. Do not use `--no-fsync` for acceptance evidence.

If monitoring a non-default installation, override the inputs explicitly:

```bash
.venv/bin/python scripts/followthrough-soak.py --once \
  --health-url http://127.0.0.1:18765/healthz \
  --ops-db /path/to/followthrough.db \
  --archive-db /path/to/archive.db \
  --effects-db /path/to/effects.db \
  --service followthrough.service \
  --service followthrough-orchestrator.service \
  --service hermes-gateway.service \
  --output /secure/path/phase-07-smoke.jsonl
```

Repeated `--service` values replace the defaults. The defaults require at least
1 GiB free and no more than 95 percent filesystem use; adjust
`--min-free-bytes` and `--max-used-percent` only when the acceptance policy says
to do so.

## Verification

Run the isolated tests and lint checks:

```bash
.venv/bin/pytest -q tests/test_soak.py
.venv/bin/ruff check followthrough/soak.py scripts/followthrough-soak.py tests/test_soak.py
```

Before claiming P7-02, verify the final `run_summary` has `all_passed: true`,
`failed_samples: 0`, no service/PID restart events, no count regression, and no
duplicate or continuity failure across the entire 24-hour window. The monitor
does not make an incomplete run count as a pass.

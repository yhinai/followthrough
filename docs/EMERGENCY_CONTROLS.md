# Emergency controls

Followthrough's emergency boundary is stored in `data/followthrough.db`. It is
not a Hermes prompt and a worker cannot clear it. Every change requires the
dashboard bearer token, produces a hash-chained audit receipt, and survives
process or host restarts.

## Semantics

| Control | Intake/archive | New actions | Existing Hermes tasks |
|---|---|---|---|
| `running` | allowed within rate limits | allowed within policy | normal |
| `paused` | listening remains allowed | denied | durably queued for parking |
| `killed` | denied | denied | durably queued for parking |

The independent capabilities are `listening`, `actions`, `messages`,
`purchases`, `sessions`, `deployments`, `repository_execution`,
`self_improvement`, and `rollback`. Disabling actions or sessions parks every
active job.
Disabling repository execution parks repository jobs. Disabling messages stops
new Discord subscriptions without stopping archive intake.

Parking is two-phase. The API first commits `park_requested` and an owner-only
task command to SQLite. The orchestrator then runs the exact Hermes equivalent
of `reassign TASK none --reclaim`, verifies that the card is no longer running
or assigned, and records `parked`. A failed remote park remains fail-closed and
is retried up to five times. Resumption is always explicit: set the global or
capability state back to running/enabled with `resume_parked=true`, or call the
resume endpoint.

The local policy transition is committed during the API request. The target
remote-task RTO is 10 seconds; the orchestrator's default five-second poll fits
inside it. A live service-reload test is still required whenever the installed
unit or poll interval changes.

## Budget and rate enforcement

Every capability has a rolling event and optional USD limit. Reservations use
an idempotency key, so retries return the original receipt without consuming a
second unit. Defaults are intentionally finite:

| Capability | Default window |
|---|---|
| listening | 36,000/hour |
| actions | 120/hour |
| messages | 30/hour |
| purchases | 10/day and USD 500/day |
| sessions | 30/hour |
| deployments | 10/day |
| repository execution | 30/hour |
| self-improvement | 4/day |
| rollback | 100/day |

Typed effectors must call `ControlPlane.authorize(...)` immediately before the
external side effect with the effect's stable idempotency key and cost. A
denial is a terminal policy result for that attempt, not a prompt to work around
the limit.

## Operator API

All routes below require `Authorization: Bearer $FOLLOWTHROUGH_DASHBOARD_TOKEN`.
Never put the token in shell history; load it from its owner-only file into the
environment.

```text
GET  /api/controls
GET  /api/controls/audit
POST /api/controls/global
POST /api/controls/safe-mode
POST /api/controls/capabilities/{capability}
PUT  /api/controls/limits/{capability}
POST /api/controls/jobs/{run_id}/park
POST /api/controls/jobs/resume
```

Example bodies:

```json
{"mode":"killed","reason_code":"operator_emergency","actor":"owner:dashboard"}
```

```json
{"enabled":false,"reason_code":"disable_messages","actor":"owner:dashboard"}
```

```json
{"max_events":3,"window_seconds":86400,"max_cost_usd":50,"reason_code":"daily_purchase_budget","actor":"owner:dashboard"}
```

## Audit and recovery

`control_audit` is an append-only SHA-256 chain over bounded metadata. It never
stores transcript or audio content. `GET /api/controls` verifies the complete
chain. The chain detects accidental or partial database edits; it is not an
external notarization against an administrator who can rewrite the database
and recompute every hash.

`paused` is also the automatic safe mode. The built-in triggers are prompt
injection, credential access, unusual spending, repeated failure, and policy
drift. Purchase-budget overruns and an emergency command that fails five times
activate it automatically. The repository/prompt scanners and effectors can
invoke the same typed `trigger_safe_mode` boundary. A trigger can escalate
running to paused but can never downgrade an existing global kill.

For recovery:

1. Inspect `/api/controls` and the audit chain.
2. Resolve the incident before changing `killed` or a disabled capability.
3. Enable only the required capability first.
4. Resume parked jobs explicitly and observe the task-command receipt.
5. Keep live self-improvement disabled until its evaluator fingerprint and
   allowlisted destinations have been reviewed.

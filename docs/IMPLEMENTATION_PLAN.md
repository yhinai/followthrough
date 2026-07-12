# Followthrough production implementation plan

The product is an always-listening personal operating system. Memo Android captures audio, Spark keeps the complete private archive, and only relevant segments cross into operational Hermes memory or actions. Omi remains an inactive compatibility path. Phases 0-6 and the two-way channel are implemented; Phase 7 remains open for the uninterrupted soak and one no-repair physical run. See [CURRENT_STATE.md](CURRENT_STATE.md).

Every phase has a hard gate. A phase is complete only after its deterministic tests, security checks, recovery test, and documented live proof pass.

## Phase 0 — baseline and containment

Deliverables:

- Inventory the current FastAPI, systemd, Cloudflare, Hermes, Discord, storage, and self-improvement state.
- Establish the boundary between the complete local archive and relevance-gated operational memory.
- Require authentication on every private API. Only `/healthz` and the static shell remain public.
- Document risks, acceptance criteria, rollback, and operator commands.

Gate:

- Existing unit/eval tests pass.
- Public unauthenticated calls cannot read transcripts, runs, reports, metrics, audio, or events.
- Hermes, Discord, Followthrough, and Cloudflare health checks pass.

## Phase 1 — reliable Memo and compatibility ingestion

Deliverables:

- Device bearer authentication with independently revocable tokens.
- Idempotent transcript event ingestion using stable event IDs.
- Memo native ingestion plus retained Omi payload normalization for rollback compatibility.
- Chunked audio upload with sequence numbers, digest verification, and retry-safe writes.
- Simple local transcript/audio persistence.
- Archive metadata, device/source attribution, timestamps, and integrity digests.
- Migration of existing plaintext run text into the complete archive.

Gate:

- Authentication, replay, duplicate, missing-chunk, digest-mismatch, and oversized-payload tests pass.
- An irrelevant segment exists only in the complete archive and never reaches Hermes or a run trace.
- An actionable segment has one archive record and one operational run despite retries.
- Audio reads to the original bytes and digest mismatches fail closed.

## Phase 2 — speaker identity and relevance boundary

Deliverables:

- Authenticated capture-channel authorization, explicit owner/non-owner/unknown speaker evidence, and fail-closed behavior for untrusted adapters.
- Transcript/audio alignment plus Omi speaker/diarization metadata when the client supplies it. Voice biometrics are deliberately not inferred.
- Deterministic relevance classifier plus explicit interest weights and content-addressed owner corrections; uncertain content stays out of Hermes.
- Separate archive search and operational-memory indexes.
- Explainable relevance decisions and corrections.

Gate:

- Held-out owner/non-owner/unknown and authenticated-ambient tests meet the configured threshold.
- Ordinary-life and sensitive archive data never appear in Hermes prompts, Discord, or action queues.
- False-positive, false-negative, correction, bounded promotion, and operational-memory removal tests pass.

## Phase 3 — durable Hermes execution

Deliverables:

- Durable Kanban card per accepted signal with idempotency and retry limits.
- Dynamic manager/specialist plan, receipts, time/cost accounting, and exception escalation.
- Event-driven Discord: urgent now, meaningful completion now, routine digest, noise silent.
- Dead-letter queue and recovery command.

Gate:

- Kill/restart during a run resumes without duplicate actions.
- Three different signals produce different plans and valid real outputs.
- Discord deduplication, retry, and outage recovery pass.

## Phase 4 — native repository lifecycle

Deliverables:

- URL/entity validation, provenance, license, dependency, secret, and malicious-install checks.
- Native execution under a dedicated unprivileged identity and transient systemd scope.
- Filesystem/process/network/resource policy, snapshot, timeout, receipt, cleanup, and rollback.
- Tool registry with versions, benchmarks, health, and uninstall instructions.

Gate:

- Known-good repository installs/tests successfully.
- Malicious fixture cannot read protected credentials, persist, exceed limits, or escape its workspace.
- Timeout, crash, dependency conflict, rollback, and repeated-run tests pass.

## Phase 5 — personal and external integrations

Deliverables:

- Read models for Hermes memory, projects, GitHub, browser history, calendar, email, Discord, goals, and tasks.
- Typed action adapters for messages, calendar, deployments, and purchases.
- Idempotency keys, receipts, reversibility metadata, spend accounting, and anomaly thresholds.

Gate:

- Each connector has auth-expiry, rate-limit, duplicate, partial-failure, and rollback tests.
- External writes are observable and attributable to the exact triggering event.
- Payment tests use sandbox/test mode until the final explicit live verification.

## Phase 6 — bounded learning and emergency controls

Deliverables:

- Interest model updated from outcomes and corrections.
- Self-improvement produces candidates only; deterministic gates and held-out evals control promotion.
- Independent emergency controls for listening, actions, messages, purchases, sessions, rollback, safe mode, and global kill.
- Automatic safe mode on prompt injection, credential access, unusual spending, repeated failure, or policy drift.

Gate:

- No candidate can weaken or edit its evaluator.
- Held-out regressions block promotion.
- Every emergency command takes effect inside the target recovery-time objective.

## Phase 7 — full acceptance

Deliverables:

- Memo/phone to Cloudflare to Spark to archive to relevance to Hermes to action to authenticated phone result proof, with optional Discord reporting.
- Load, long-duration, restart, network-loss, disk-pressure, corruption, backup, restore, and disaster-recovery tests.
- Operator handbook, data export/deletion, credential rotation, and incident response.

Gate:

- A bounded live verification run has no data loss or duplicate external actions.
- Backup restore reproduces archive metadata, operational state, and audit receipts.
- All mandatory acceptance checks in `docs/ACCEPTANCE_MATRIX.md` pass with evidence paths.

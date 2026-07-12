# Followthrough — extensive implementation and status report

> **Superseding update — 2026-07-12 PDT:** Memo is the primary sensor and the archive now uses direct local transcript/audio storage. The 24-hour soak is no longer a completion gate; bounded monitoring remains optional. [CURRENT_STATE.md](CURRENT_STATE.md) is authoritative.

**Report date:** July 11, 2026, America/Los_Angeles  
**Repository:** `yhinai/followthrough`  
**Live application:** <https://followthrough.alhinai.dev/>  
**Current revision:** `7668472`  
**Runtime host:** Spark  
**Project status:** Operational and substantially implemented; final Phase 7 acceptance remains incomplete.

## 1. Executive summary

Followthrough is an always-listening personal ambient operator. A phone captures audio and transcripts, Spark retains the complete local archive, a deterministic relevance boundary decides what is useful, and only useful signals cross into Hermes operational memory and autonomous action.

The main system is live. Authentication, stored transcript and audio ingestion, relevance filtering, operational-memory separation, durable Hermes jobs, native repository research and testing, event-driven Discord reporting, typed external actions, emergency controls, bounded self-improvement, backup/restore, and a live observability dashboard have been implemented.

The system currently has one unfinished final-acceptance requirement:

1. A fresh physical utterance on the Samsung Flip must be observed traveling through the complete phone-to-Hermes-to-result path. The previously configured wireless ADB endpoint `100.96.0.1:40785` is currently closed.

These are verification gaps, not an indication that the web service is down. The public application and all Spark services are healthy.

## 2. Original product objective

The requested product is a non-intrusive, continuously operating assistant that:

- listens through Omi, a phone microphone, or another authenticated device;
- sends original audio and transcripts to Spark;
- stores the complete archive privately;
- excludes irrelevant conversation from Hermes memory and action history;
- recognizes tools, repositories, startups, events, tasks, optimization ideas, and explicit requests;
- researches relevant subjects autonomously;
- safely clones, scans, tests, evaluates, and reports on repositories;
- creates tasks, edits the calendar, sends owner messages, triggers deployments, and supports bounded purchasing workflows;
- uses Hermes as the central intelligence, skills, memory, and action runtime;
- reports meaningful outcomes through Discord without sending ordinary conversational noise;
- learns through controlled candidates and evaluation rather than uncontrolled self-modification;
- exposes a live dashboard for the demo and operational inspection;
- provides emergency controls, receipts, recovery, backup, and long-duration verification.

## 3. Final architecture

```text
Samsung Flip / Omi / Termux microphone
              |
              | authenticated transcript and audio events
              v
Cloudflare hostname: followthrough.alhinai.dev
              |
              v
FastAPI sensory gateway on Spark
  |-- device authentication and consent checks
  |-- idempotency and delivery receipts
  |-- direct local archive storage
  |-- deterministic relevance and provenance gate
  |-- realtime activity/dashboard API
              |
      +-------+------------------+
      |                          |
      v                          v
Complete local archive          Relevant-only operational state
transcripts + original audio     category + entity + job capsule
      |                          |
      |                          v
      |                    Durable Hermes Kanban
      |                    manager / specialists
      |                          |
      |             +------------+--------------+
      |             |                           |
      |             v                           v
      |       Repository lifecycle        Typed effectors
      |       research / scan / test      task / calendar / Discord /
      |       sandbox / receipt           GitHub / deploy / purchase
      |             |                           |
      +-------------+-------------+-------------+
                                  |
                                  v
                      Reports, receipts, dashboard,
                      Discord completion or escalation
```

### Architectural decisions

- **Hermes owns runtime intelligence.** Codex was used to build and verify the system, but Followthrough does not require Codex for normal operation.
- **The fast path is deterministic.** Authentication, storage, relevance gates, policy, idempotency, repository containment, controls, backups, and soak checks do not require an LLM call.
- **LLM usage is relevance-gated.** Hermes runs only after a signal is accepted. The configured model strategy uses `gpt-5.6-luna` with low reasoning for routine jobs to reduce credit usage.
- **Archive and memory are different trust zones.** Complete raw material is stored in the archive. Hermes receives bounded operational facts only for relevant events.
- **Autonomy is typed and receipted.** External writes are modeled as explicit effect types with policy decisions, idempotency keys, durable state transitions, and rollback metadata.
- **Self-improvement is bounded.** Candidates cannot edit evaluators or controls, and promotion requires pinned deterministic evaluation plus explicit owner policy.

## 4. Deployed runtime

The following user services are installed and active:

| Service | Function | Current state |
|---|---|---|
| `followthrough.service` | FastAPI application and dashboard | active |
| `followthrough-orchestrator.service` | durable Hermes Kanban reconciliation | active |
| `followthrough-adb-bridge.service` | Omi on-device Whisper log bridge | active, waiting for Flip ADB endpoint |
| `followthrough-soak-24h.service` | Phase 7 acceptance monitor | active |
| `hermes-gateway.service` | Hermes runtime gateway | active |

The public `/healthz` endpoint reports:

- application OK;
- operations database ready;
- complete archive ready;
- device and dashboard authentication ready;
- Hermes CLI present;
- orchestrator heartbeat healthy;
- global control mode `running`.

## 5. Phone, Omi, transcript, and audio path

### Implemented

- Samsung Flip Omi Developer Mode was configured with:
  - Conversation Events enabled;
  - Real-time Transcript enabled;
  - Audio Bytes enabled;
  - Followthrough URLs using `followthrough.alhinai.dev`;
  - five-second delivery interval.
- The obsolete `omi.alhinai.dev` ingress and DNS path were removed.
- Omi on-device Whisper transcript output is bridged through authenticated wireless ADB when the device is reachable.
- A Termux/Termux:API audio uploader records five-second M4A chunks concurrently with Omi transcription.
- The uploader uses a private mode-0600 device-token file.
- The current uploader implementation sends its bearer token in the Authorization header rather than in the URL.
- Transcript delivery receipts include event identity, latency, result status, run ID, and job ID without transcript plaintext.
- Audio uploads preserve their actual compressed MIME type.
- Audio events carry capture stream identity, sequence, timestamps, digest, and alignment metadata.

### Verified

- A real phone-produced M4A chunk was stored on Spark, matched its stored SHA-256, and was successfully parsed by `ffprobe`.
- The verified chunk was 8,958 bytes and 5.248 seconds long.
- Omi and Termux were shown to access the microphone concurrently during the working phone session.
- Prior physical Flip capture delivered stored events while the screen was off.
- Transcript and audio retries are idempotent when stable delivery identity is available.
- MIME preservation has a dedicated regression test.

### Split-transcript hardening

On-device Whisper can split a single instruction across several segments. Followthrough now has a bounded aggregator that:

- retains at most 12 segments inside a 45-second window;
- archives each original segment independently;
- emits a content-addressed aggregate only if an explicit action verb and actionable subject co-occur;
- clears its buffer after dispatch;
- does not aggregate ordinary food conversation or passive tool mentions;
- prevents an expired action fragment from combining with a later subject.

This preserves ambient context without promoting normal conversation into Hermes memory.

### Current phone blocker

The Flip responds at the network level, but wireless ADB port `100.96.0.1:40785` returns `No route to host`/closed. The Spark ADB device list currently shows a Pixel 9 Pro, not the Samsung Flip. Wireless debugging must be re-enabled and the current dynamic port supplied before the final physical utterance proof can run.

## 6. Privacy, authentication, and complete archive

### Authentication

- Private APIs reject unauthenticated callers.
- Device and dashboard credentials are stored outside the repository in private files.
- Device tokens are independently revocable.
- Capture consent is required by the native transcript contract.
- Omi webhook compatibility is retained while native uploaders can use Authorization headers.

### Complete archive

- Transcripts and audio use local byte storage.
- Each transcript and audio chunk has a stored SHA-256 integrity digest.
- Audio files use the `.audio` extension with mode-0600 permissions.
- Digest mismatches fail closed.
- Raw transcript content is excluded from the operations database and job capsules.

### Current archive counts

At report generation time:

- archive events: **1,027**;
- relevant archive events: **6**;
- audio chunks: **747**;
- archived audio: **6,591,579 bytes**.

The high archive count with only six relevant events demonstrates the intended boundary: everything can be retained privately, while only a very small useful subset reaches operational processing.

## 7. Relevance and operational memory

The relevance subsystem evaluates:

- authenticated capture provenance;
- owner, non-owner, or unknown speaker status;
- recognized categories and evidence;
- explicit interest weights;
- content-addressed owner corrections;
- ordinary-life/noise patterns;
- confidence and reason codes.

Relevant categories include repositories, tools, startups/companies, research requests, tasks, follow-ups, calendar/events, deployments, and purchases. Ordinary conversation can be stored in the archive but returns an archive-only decision and produces no Hermes job or operational memory.

Current operations state contains six operational-memory rows, corresponding to six relevant events. There are no operational-memory entries for the remaining 1,021 archive events.

Held-out relevance evaluation and provenance tests pass. The acceptance matrix records 28/28 held-out relevance cases plus false-positive regressions.

## 8. Hermes agent execution

### Durable Kanban model

Each relevant signal receives:

- a complete archive link;
- a run ID;
- a content-addressed operational entity;
- a durable Hermes job;
- a bounded job capsule;
- intent and acceptance criteria;
- task history and state transitions;
- report and notification linkage.

The orchestrator reconciles pending, active, completed, escalated, and parked work. Restart testing demonstrated that durable work survives process termination without duplicating effects.

### Manager and specialist behavior

Hermes receives only bounded operational context and dynamically chooses work such as:

- research and primary-source verification;
- repository validation and acquisition;
- security/license/dependency review;
- safe native test execution;
- report synthesis;
- typed-effect planning;
- completion or escalation reporting.

### Current job state

- completed Hermes jobs: **3**;
- jobs needing attention: **3**;
- completed runs: **3**;
- runs needing attention: **3**.

The `needs_attention` state is explicit rather than silently claiming success when the agent lacks proof or authority.

## 9. Native repository research and evaluation

The repository pipeline implements:

- repository and entity validation;
- provenance and URL normalization;
- license inspection;
- dependency and secret scanning;
- malicious-install pattern checks;
- bounded cloning and workspace isolation;
- resource and timeout limits;
- network-disabled test execution where appropriate;
- deterministic receipts, cleanup, and rollback instructions;
- tool-registry metadata.

Verified examples include:

- known-good PyPA `sampleproject` cloned and tested with exit code 0 and network disabled;
- malicious repository fixture denied and contained;
- timeout and rollback paths tested;
- protected credentials remained inaccessible to contained execution.

## 10. External action system

Followthrough implements these typed effectors:

1. `private_task.create`
2. `calendar.event.upsert`
3. `discord.message.send`
4. `github.issue.create`
5. `deployment.trigger`
6. `purchase.create`

Each effect has:

- a trigger event ID;
- validated typed request;
- policy decision and risk classification;
- stable idempotency key and fingerprint;
- durable append-only transitions;
- execution attempt count;
- provider and external ID;
- response fingerprint;
- reversible metadata where supported;
- explicit failed, retryable, uncertain, completed, or rolled-back state.

### Current post-reset live receipts

| Effect | Result |
|---|---|
| Private task | automatically created and completed |
| Owner Discord message | automatically sent; provider receipt recorded |
| Google Calendar | temporary event created, verified, and rolled back |
| Purchase | USD 0.01 sandbox authorization created, verified, and voided; no real funds |
| Deployment | GitHub Actions workflow autonomously dispatched and completed successfully |

The live effect journal currently contains one row for each of those five effect types.

### Deployment proof

- Workflow: `Followthrough deployment verification`
- GitHub Actions run: `29176367068`
- Result: success
- Effector receipt: `a7a89bf3-a19d-40bd-9df5-ceba3d2e903c`
- Verification: the workflow called the real public `/healthz` endpoint and asserted that Followthrough was healthy.

The autonomous policy is bounded to `yhinai/followthrough`. Preview deployment is allowed; production deployment remains policy-gated.

### Purchasing boundary

The full typed purchase workflow, spend limits, atomic daily cap, idempotency, receipts, test driver, and rollback are implemented. Current live verification intentionally uses sandbox/test mode. Real-money purchasing remains restricted by allowlisted vendor and spend policy.

## 11. Discord reporting

Discord is the primary owner communication surface.

Implemented behavior:

- urgent failures or approval needs can notify immediately;
- meaningful completions notify immediately;
- ordinary conversation is silent;
- messages are content-bounded and mention-safe;
- raw archive transcript markers are excluded;
- notification hashes prevent duplicate delivery;
- task subscription and recovery state persist across restarts.

Live completion, blocked-state, and current post-reset owner-notification receipts have been recorded. The most recent current-state Discord receipt is documented in `docs/evidence/phase-05-current-state-2026-07-11.md`.

## 12. Emergency controls and safety

The control plane contains:

- global running, paused, and killed modes;
- independent controls for listening, actions, messages, purchases, sessions, deployments, rollback, and self-improvement;
- rate and cost budgets;
- task park/resume commands;
- hash-chained audit receipts;
- automatic safe mode for prompt injection, credential access, spending anomalies, repeated failures, and policy drift.

Live recovery verification measured:

- pause: **6.14 seconds**;
- least-authority resume: **5.236 seconds**.

Both are within the ten-second recovery-time objective.

The dashboard's earlier Autonomy Supervisor control card was deliberately removed from the public-facing UI at the owner's request. The underlying authenticated control APIs and runtime controls remain implemented.

## 13. Bounded self-improvement

Self-improvement supports:

- candidate generation in an isolated directory;
- evidence files with SHA-256 validation;
- safe relative target validation;
- protected evaluator/control targets;
- unsafe-content scanning;
- pinned evaluator fingerprint;
- held-in and held-out evaluation;
- no-regression gates;
- explicit owner policy for live promotion;
- staged promotion and receipts;
- artifact integrity checks and rollback.

Current post-reset proof:

- proposal: `5d5d7efe-c187-474a-b7cf-cc9b420526d7`;
- purpose: bounded ambient-command aggregation;
- all deterministic gates passed;
- result: `promoted_staged` only;
- no live Hermes skill, evaluator, control, or runtime file was silently modified.

## 14. Dashboard and user experience

The live dashboard uses an Apple-like white visual theme and presents:

- system and listening state;
- recent transcript decisions;
- live research and task history;
- operational memory;
- current Hermes focus;
- task progress and results;
- realtime refresh from authenticated APIs.

The following earlier elements were removed at the owner's request:

- Average latency card;
- Audio chunks card;
- Autonomy Supervisor section and pause/resume/emergency-stop card.

The backend controls were retained; only the unnecessary dashboard presentation was removed.

## 15. Cloudflare and public delivery

- Canonical hostname: `followthrough.alhinai.dev`
- Public application: HTTPS 200
- Public health endpoint: healthy
- Obsolete hostname: `omi.alhinai.dev` removed from the active configuration
- Spark service binds locally, with Cloudflare providing the public route
- Authenticated private APIs remain private through the public edge

## 16. Test and verification status

Fresh verification at report generation time:

```text
155 tests passed
Ruff: all checks passed
git diff --check: passed
```

The single pytest warning is a Starlette deprecation warning about the TestClient/httpx compatibility layer; it is not a failing functional test.

Test coverage includes:

- authentication and private route denial;
- transcript/audio ingestion and replay;
- local storage integrity and tamper failure;
- Omi webhook normalization and compressed MIME preservation;
- ADB Whisper parsing and split-segment aggregation;
- relevance, speaker provenance, corrections, and archive/memory separation;
- durable Hermes outbox and Kanban recovery;
- repository acquisition, containment, timeout, and malicious fixtures;
- all typed effectors, idempotency, partial failures, uncertainty, and rollback;
- emergency controls and budgets;
- self-improvement gates and artifact tampering;
- backup/restore;
- soak-monitor integrity checks.

## 17. Acceptance status by phase

| Phase | Scope | Status |
|---:|---|---|
| 0 | baseline, authentication, containment | pass |
| 1 | reliable transcript/audio ingestion and storage | pass |
| 2 | speaker provenance, relevance, archive/memory boundary | pass |
| 3 | durable Hermes execution and Discord recovery | pass |
| 4 | native repository lifecycle | pass |
| 5 | typed tasks, calendar, messaging, deployment, purchase | pass within configured policy boundary |
| 6 | emergency controls and bounded self-improvement | pass |
| 7 | full physical end-to-end and recovery | partial: backup/restore passes; physical utterance pending |

The authoritative detailed matrix is `docs/ACCEPTANCE_MATRIX.md`.

## 18. Current soak status

At report generation time, the Phase 7 monitor had completed approximately 9,969 seconds (about 2 hours 46 minutes) of its required 24 hours.

Every current checkpoint is green:

- archive audio continuity;
- control audit chain;
- database integrity;
- disk pressure;
- duplicate-key detection;
- effect-journal linkage;
- monotonic counts;
- service health;
- service process identity.

No hard failure was recorded during the bounded monitoring window. Extended monitoring is optional.

## 19. Backup, restore, and credential operations

Implemented:

- private backup artifacts;
- manifest and SHA-256 verification;
- operations, archive, and effect database preservation;
- restore integrity checks;
- device-token rotation and revocation test;
- incident and recovery documentation.

Current acceptance evidence records three restored databases with SQLite integrity `ok` and a temporary device credential that changed from accepted to rejected after revocation.

## 20. Cost and model strategy

The system was deliberately changed to avoid unnecessary Codex credit consumption:

- deterministic code handles routine ingestion, filtering, policy, storage, controls, and monitoring;
- irrelevant conversation invokes no Hermes research job;
- Hermes is the runtime owner;
- routine work uses the lower-cost `gpt-5.6-luna` low-reasoning path;
- Codex is not a required production dependency;
- repository tests and connector logic run natively when an LLM is unnecessary.

This architecture prevents continuous ambient capture from turning into continuous model spending.

## 21. Known limitations and honest boundaries

1. **Physical phone-to-action proof is incomplete.** Audio and transcript legs were separately proven, but a fresh natural Flip utterance has not yet been observed completing a new Hermes research job after the latest aggregation change.
2. **The Flip ADB port is unavailable.** The dynamic wireless-debugging port must be refreshed.
3. **The Termux uploader on the phone still needs its latest secured script redeployed after reconnection.** The repository version uses Authorization headers; the phone cannot be updated while ADB is unavailable.
4. **Extended monitoring is optional.** It can provide additional operational evidence but does not block completion.
5. **Real-money purchase is intentionally untested.** Sandbox purchase behavior is proven; a real transaction requires an allowlisted vendor and live-spend policy.
6. **Production deployment remains gated.** Preview deployment through GitHub Actions is proven.
7. **Some jobs are in `needs_attention`.** They remain visible rather than being misreported as successful.
8. **Stock Omi webhook behavior was not consistently sufficient.** The production fallback combines Omi on-device STT through ADB with Termux audio upload.

## 22. Remaining execution plan

### Step 1 — restore the Flip connection

On the Samsung Flip:

1. Open Developer options.
2. Open Wireless debugging.
3. Turn Wireless debugging on.
4. Read the current `IP address & Port` value.
5. Supply the new endpoint to the Spark operator.

Acceptance:

- `adb connect IP:PORT` succeeds;
- model resolves to the Samsung Flip;
- Omi process and version are visible;
- the bridge service holds a live logcat connection.

### Step 2 — redeploy and verify the phone uploader

- Replace the Termux script with `scripts/termux-followthrough-audio.sh`.
- Preserve the private token file; do not print or copy it into public storage.
- Start the uploader persistently.
- Verify HTTP 202 receipts with audio larger than 1 KiB.
- Confirm new archive rows use `audio/mp4`.
- Read one fresh chunk, verify its digest, and parse it with `ffprobe`.
- Confirm Omi on-device transcription continues concurrently.

### Step 3 — perform the physical end-to-end utterance

Speak clearly near the Flip:

> Followthrough, research and safely test the GitHub repository PyPA sampleproject.

Acceptance evidence must show one continuous lineage:

1. phone/Omi transcript segment receipts;
2. aggregate event if Whisper splits the instruction;
3. relevance decision `dispatch_allowed=true`;
4. operational-memory row;
5. durable Hermes job ID;
6. repository research/scan/test receipt;
7. completed report;
8. owner Discord completion message;
9. dashboard update in realtime;
10. no duplicate effect after replay.

### Step 4 — finish the soak

- Leave all acceptance services running.
- Allow `followthrough-soak-24h.service` to complete naturally.
- Confirm the selected bounded verification window completed without hard failures.
- Confirm zero hard failures.
- Confirm no missing audio sequences attributable to accepted delivery.
- Confirm no duplicate external actions.
- Preserve the mode-0600 receipt and final summary.

### Step 5 — final completion audit

- Run the complete 155-test suite again.
- Run Ruff and `git diff --check`.
- Verify public health and authenticated dashboard APIs.
- Verify every acceptance-matrix row has direct evidence.
- Update P7-01 and P7-02 from pending to pass only after their evidence exists.
- Commit and push final evidence.
- Mark the persistent goal complete only after every requirement is proven.

## 23. Important paths

| Purpose | Path |
|---|---|
| Repository | `/home/alhinai/Projects/followthrough` |
| Main application | `followthrough/app.py` |
| Relevance engine | `followthrough/relevance.py` |
| ADB transcript bridge | `followthrough/adb_bridge.py` |
| Complete archive | `followthrough/archive.py`, `followthrough/archive_store.py` |
| Durable orchestration | `followthrough/kanban.py` |
| Repository pipeline | `followthrough/repository_pipeline.py`, `followthrough/runner.py` |
| Typed effectors | `followthrough/effectors/` |
| Emergency controls | `followthrough/controls.py` |
| Self-improvement | `followthrough/self_improvement.py` |
| Soak monitor | `followthrough/soak.py` |
| Termux audio uploader | `scripts/termux-followthrough-audio.sh` |
| Acceptance matrix | `docs/ACCEPTANCE_MATRIX.md` |
| Implementation plan | `docs/IMPLEMENTATION_PLAN.md` |
| Operator guidance | `docs/OMI_SETUP.md`, `docs/EFFECTORS.md`, `docs/EMERGENCY_CONTROLS.md`, `docs/SOAK.md` |
| Evidence | `docs/evidence/` |
| Operations database | `data/followthrough.db` |
| Archive database | `data/archive/archive.db` |
| Effect journal | `data/effects/effects.db` |
| Soak receipt | `data/soak/phase7-24h.jsonl` |

## 24. Repository and deployment history

Recent milestone revisions:

- `6d0cf05` — fresh Followthrough Hermes agency foundation;
- `ecb23ec` — ambient Hermes operator implementation;
- `5b43cc3` — realtime phone feed and live console;
- `b243ced` — refined console focused on work evidence;
- `05cad9c` — hardened phone audio and split-command ingestion;
- `1654c71` — refreshed live connector and self-improvement evidence;
- `1e98035` — deployment verification workflow;
- `7668472` — autonomous deployment proof.

The current local branch and `origin/main` are aligned at `7668472` before this report is committed.

## 25. Overall assessment

Followthrough is no longer just a concept or dashboard mock-up. It is a running multi-service system with real stored phone audio, real transcript ingestion, a relevance boundary, durable Hermes work, repository containment, typed external effects, live Discord and calendar receipts, an autonomous deployment proof, emergency controls, staged self-improvement, and a continuously checked public endpoint.

The correct project status is **operational, extensively implemented, and not yet fully accepted**. Phase 7 remains open only for the physical no-repair utterance proof.

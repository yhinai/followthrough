# Current state

Verified 2026-07-12 PDT. This document describes the intentionally simple,
tokenless test deployment currently running on Spark.

## Shipped path

```text
Memo Android foreground microphone
  -> private LiveKit Cloud room
  -> always-on Followthrough worker on Spark
  -> ephemeral partials and finalized transcripts
  -> deterministic relevance and aggregation
  -> every finalized transcript preserved in the local archive
  -> ordinary speech excluded from operational memory and actions
  -> relevant-only durable Hermes Kanban job
  -> research / sandbox test / typed effect
  -> typed Discord result delivery
  -> optional LiveKit phone-speaker result
```

Memo is the primary sensor. Omi remains a supported ingestion adapter. For the LiveKit path, every finalized transcript reaches the archive, while only relevant finals enter the operational workflow. Audio recording is disabled.

## Verified runtime

- Public health endpoint, Followthrough, orchestrator, and Cloudflare tunnel are active.
- Public health reports `auth_required: false`; there are no Followthrough token files, token middleware, or dashboard token prompts.
- Followthrough Python suite: 279 tests passed after the LiveKit integration.
- Memo Android clean unit-test and debug-APK build: successful on OpenJDK 17.
- Samsung `SM-F776U1`: foreground microphone publishing, LiveKit room dispatch, Spark worker join, and built-in speaker routing verified.
- Ambient relevance eval: 40/40 realistic cases, with all 20 actionable signals promoted and all 20 ordinary-conversation cases excluded from actions.

## Two-way contract

- `POST /api/v1/transcripts` returns `job_id` for actionable signals.
- `GET /api/v1/jobs/{job_id}` returns only a sanitized status/result.
- The public API is tokenless for this event-day build, but LiveKit room access uses short-lived, audio-only tokens issued after explicit consent.
- The Spark worker owns durable job and Discord delivery state; Memo reconnects its LiveKit room after network loss and reboot.
- Terminal states are `completed`, `dead_letter`, `needs_attention`, and `cancelled`.
- Hermes/H results are persisted, delivered idempotently to Discord, and returned to Memo. Memo speaks them only in `Discord + voice` mode.
- `/api/journey` links one source event to its decision, Hermes job, H Company session, Discord receipt, and phone return state. The dashboard does not infer these relationships by timestamp.
- Saying `Memo, ...` is an explicit owner override and always creates work, even when that category was muted or the same content was previously corrected to ignore. Passive tool, startup, and goal mentions are preserved in Workspace Backlog without automatically starting a worker.
- Web tasks have one browser owner: the typed H Company runner. Hermes holds the durable card and delivery receipt, then the orchestrator closes it from the authoritative H result; Hermes does not launch a duplicate browser-research pass.

## Tools and services

| Layer | Current tool |
|---|---|
| Phone capture and playback | Memo Android foreground service and LiveKit Android SDK |
| Real-time media | LiveKit Cloud transport; Spark-hosted `followthrough.livekit_agent` systemd worker |
| Desktop execution surface | Free Spark X11/Chromium desktop with local typed API, optional remote Orgo fallback, verified action receipts, and embedded live noVNC |
| Public ingress | Cloudflare Tunnel at `followthrough.alhinai.dev` |
| API and dashboard | FastAPI/Uvicorn on Spark |
| Durable state | Separate SQLite operations, archive, effects, and Hermes Kanban ledgers |
| Complete transcript archive | Finalized transcript storage plus manifests and digests; LiveKit audio is not persisted |
| Relevance | Deterministic owner/category/entity gate and transcript aggregation |
| Agent runtime | Hermes Kanban board and least-authority `followthrough` worker profile |
| Repository evaluation | Pinned provenance, policy scan, systemd+bubblewrap sandbox runner, deterministic receipts |
| Owner surfaces | Durable Hermes Discord DM, optional Memo spoken result, live dashboard and `#transcript` word stream |
| Daily organization | Responsive `#workspace` view with Research, Backlog, Tasks & reminders, and Events & calendar; title/group edits and non-destructive removal |
| Verification | Pytest/Ruff, Android Gradle build, public health checks, and optional bounded monitoring |

## Typed workflow verification

Verified live on 2026-07-12 PDT:

- Private task `4c11a9cb-7b70-4adb-8ea0-a1da9df765d2` executed through
  `followthrough-private-tasks` and was rolled back to cancelled.
- Calendar effect `c7f16e5e-1a5f-4dd5-b195-802b16dfa9cb` created a real
  primary-calendar event with attendee notifications disabled and then deleted it.
- The current typed Discord result path delivered messages `86857` and `86858`
  to the configured owner DM, with H answers and Hermes receipts.
- Completed Best Buy run `9323f56f-8507-47b1-aabc-4a581843f321` linked the
  ambient event, Hermes receipt `t_401dfc5f`, and H Company session in one
  public seven-stage journey. Its receipt includes H steps, elapsed time, and
  the session replay URL.
- Sandbox purchase effect `3285dd4f-969c-453d-9eae-fdf96f3e4c31`
  authorized one cent in test mode and was then voided. No real payment moved.
- Deployment effect `fdb8b9de-16f6-400d-b7a7-2f5dd967dad4` reached the
  durable `dry_run` state. The lean repository intentionally has no deploy
  workflow, so no deployment was dispatched.

Each effect has an append-only transition history in `data/effects/effects.db`.
Repository acquisition/testing and Hermes research were separately proven by
completed job `25a6bfb0-c066-4398-a17b-62ed90ddd9b0` and Kanban task
`t_f9028478`.

## Current revisions

- Followthrough: branch `main`; this document is shipped with the lean tokenless runtime revision.
- Memo: branch `main`; the installed Samsung build includes LiveKit reconnect, speaker-policy routing, and tokenless Followthrough configuration.
- GitHub targets: `yhinai/followthrough` and `yhinai/memo`, branch `main`.

## Remaining acceptance work

The product is operational. The physical Samsung has verified continuous audio,
live word streaming, finalized transcript ingestion, H Company browsing,
completed Hermes work, typed Discord delivery, LiveKit reconnection,
built-in-speaker routing, and automatic listening restoration after reboot.

Both phone delivery policies were replayed against the same completed real job:
`Discord only` consumed the result with no speaker playback, while `Discord +
voice` produced the `Verified result audio playing through the built-in speaker`
device receipt. The phone was returned to its intentional muted state afterward.

The free Spark desktop plane is active and restart-enabled. Public doctor and
screenshot checks pass, the noVNC WebSocket negotiates RFB 3.8 through
`followthrough.alhinai.dev`, and key/type/scroll actions produced distinct
before/after frame fingerprints. A confirmation-gated desktop restart was
also followed by a successful readiness and stream check. Orgo remains only
an optional remote fallback and is not required for the demo.

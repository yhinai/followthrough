# Current state

Verified 2026-07-12 PDT. This document describes the intentionally simple,
tokenless test deployment currently running on Spark.

## Shipped path

```text
Memo Android microphone
  -> audio and finalized transcripts
  -> https://followthrough.alhinai.dev
  -> complete local archive on Spark
  -> deterministic relevance and aggregation
  -> relevant-only durable Hermes Kanban job
  -> research / sandbox test / typed effect
  -> sanitized job status
  -> Memo polling and spoken phone-speaker result
```

Memo is the primary sensor. Omi remains a supported ingestion adapter. Ordinary conversation is preserved only in the complete archive and is excluded from operational memory and actions. Transcripts are stored directly in SQLite and audio is stored as local files.

## Verified runtime

- Public health endpoint, Followthrough, orchestrator, and Cloudflare tunnel are active.
- Public health reports `auth_required: false`; there are no Followthrough token files, token middleware, or dashboard token prompts.
- Followthrough Python suite: 167 passed after the H session and local Spark desktop additions.
- Memo Android build: successful on OpenJDK 17.
- Samsung `SM-F776U1`: foreground microphone, transcript/audio delivery, Gemini Live, and built-in speaker routing verified.

## Two-way contract

- `POST /api/v1/transcripts` returns `job_id` for actionable signals.
- `GET /api/v1/jobs/{job_id}` returns only a sanitized status/result.
- The test deployment is intentionally tokenless. Memo needs only the HTTPS endpoint.
- Memo persists pending IDs in Android preferences and resumes polling after restart.
- Terminal states are `completed`, `dead_letter`, `needs_attention`, and `cancelled`.
- Hermes run summaries are persisted to the Followthrough run and returned to Memo; Gemini Live speaks the result.

## Tools and services

| Layer | Current tool |
|---|---|
| Phone capture and playback | Memo Android foreground service, `AudioRecord`, `AudioTrack`, Gemini Live |
| Desktop execution surface | Free Spark X11/Chromium desktop with local typed API, optional remote Orgo fallback, verified action receipts, and embedded live noVNC |
| Public ingress | Cloudflare Tunnel at `followthrough.alhinai.dev` |
| API and dashboard | FastAPI/Uvicorn on Spark |
| Durable state | Separate SQLite operations, archive, effects, and Hermes Kanban ledgers |
| Complete archive | Simple transcript/audio storage plus manifests, digests, and continuity checks |
| Relevance | Deterministic owner/category/entity gate and transcript aggregation |
| Agent runtime | Hermes Kanban board and least-authority `followthrough` worker profile |
| Repository evaluation | Pinned provenance, policy scan, systemd+bubblewrap sandbox runner, deterministic receipts |
| Owner surfaces | Memo spoken result, live web dashboard, optional Hermes Discord DM |
| Verification | Pytest/Ruff, Android Gradle build, public health checks, and optional bounded monitoring |

## Typed workflow verification

Verified live on 2026-07-12 PDT:

- Private task `4c11a9cb-7b70-4adb-8ea0-a1da9df765d2` executed through
  `followthrough-private-tasks` and was rolled back to cancelled.
- Calendar effect `c7f16e5e-1a5f-4dd5-b195-802b16dfa9cb` created a real
  primary-calendar event with attendee notifications disabled and then deleted it.
- Discord transport delivered verification message
  `1525784068417917028` to the configured owner channel.
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
- Memo: branch `main`; the installed Samsung build includes restart-safe result polling, speaker routing, and tokenless Followthrough configuration.
- GitHub targets: `yhinai/followthrough` and `yhinai/memo`, branch `main`.

## Remaining acceptance work

The product is operational. The physical Samsung has verified continuous audio,
finalized transcript ingestion, completed Hermes research, restart-safe result
recovery, and built-in-speaker routing. A fresh spoken actionable phrase remains
the recommended final demo rehearsal; it is not a soak requirement.

The free Spark desktop plane is active and restart-enabled. Public doctor and
screenshot checks pass, the noVNC WebSocket negotiates RFB 3.8 through
`followthrough.alhinai.dev`, and key/type/scroll actions produced distinct
before/after frame fingerprints. A confirmation-gated desktop restart was
also followed by a successful readiness and stream check. Orgo remains only
an optional remote fallback and is not required for the demo.

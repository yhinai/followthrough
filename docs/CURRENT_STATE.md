# Current state

Verified 2026-07-11 PDT. This document supersedes stale runtime statements in older evidence snapshots; historical evidence remains unchanged.

## Shipped path

```text
Memo Android microphone
  -> authenticated audio and finalized transcripts
  -> https://followthrough.alhinai.dev
  -> encrypted complete archive on Spark
  -> deterministic relevance and aggregation
  -> relevant-only durable Hermes Kanban job
  -> research / sandbox test / typed effect
  -> sanitized authenticated job status
  -> Memo polling and spoken phone-speaker result
```

Memo is the primary sensor. Omi, the Termux audio uploader, and the ADB transcript bridge are inactive rollback/history paths. Ordinary conversation is preserved only in the encrypted archive and is excluded from operational memory and actions.

## Verified runtime

- Public health endpoint and Followthrough, orchestrator, Cloudflare tunnel, and soak services are active.
- Followthrough Python suite: 157 passed after the two-way changes.
- Memo Android build: successful on OpenJDK 17.
- Samsung `SM-F776U1`: foreground microphone, transcript/audio delivery, Gemini Live, and built-in speaker routing verified.
- OnePlus `CPH2513`: foreground microphone, transcript/audio delivery, Gemini Live, and built-in speaker routing verified while connected as ADB serial `31e0272e`.

## Two-way contract

- `POST /api/v1/transcripts` returns `job_id` for actionable signals.
- `GET /api/v1/jobs/{job_id}` returns only a sanitized status/result.
- The server binds each event to a server-derived hash of the device credential. Another valid device token receives `404`.
- Memo persists pending IDs in Android preferences and resumes polling after restart.
- Terminal states are `completed`, `dead_letter`, `needs_attention`, and `cancelled`.
- Hermes run summaries are persisted to the Followthrough run and returned to Memo; Gemini Live speaks the result.

## Tools and services

| Layer | Current tool |
|---|---|
| Phone capture and playback | Memo Android foreground service, `AudioRecord`, `AudioTrack`, Gemini Live |
| Public ingress | Cloudflare Tunnel at `followthrough.alhinai.dev` |
| API and dashboard | FastAPI/Uvicorn on Spark |
| Durable state | Separate SQLite operations, encrypted archive, effects, and Hermes Kanban ledgers |
| Private archive | AES-256-GCM transcript storage plus audio manifests and continuity checks |
| Relevance | Deterministic owner/category/entity gate and transcript aggregation |
| Agent runtime | Hermes Kanban board and least-authority `followthrough` worker profile |
| Repository evaluation | Pinned provenance, policy scan, systemd+bubblewrap sandbox runner, deterministic receipts |
| Owner surfaces | Memo spoken result, live web dashboard, optional Hermes Discord DM |
| Verification | Pytest/Ruff, Android Gradle build, public health checks, hash-chained 24-hour soak |

## Current revisions

- Followthrough: `3eb20da` (`Return Hermes summaries to submitting devices`).
- Memo: `8026daa` (`Route Memo voice responses through phone speaker`).
- GitHub targets: `yhinai/followthrough` and `yhinai/memo`, branch `main`.

## Remaining acceptance work

The product is operational but the overall goal is not marked fully accepted until the current 24-hour soak completes cleanly and a fresh physical no-repair voice run identifies its target correctly and completes phone-to-Spark-to-phone without operator correction.

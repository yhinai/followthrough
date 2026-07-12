# Omi integration

Followthrough supports the current stock Omi developer webhooks and a richer native API for a future Omi fork or phone companion.

## Stock Omi webhooks

Copy the per-device secret from the owner-only file on Spark:

`~/.config/followthrough/devices/omi-primary.token`

In Omi → Settings → Developer Mode → Developer Settings, configure:

```text
Real-Time Transcript:
https://followthrough.alhinai.dev/api/webhooks/omi/transcript?token=<PER_DEVICE_SECRET>

Conversation Events:
https://followthrough.alhinai.dev/api/webhooks/omi/conversation?token=<PER_DEVICE_SECRET>

Realtime Audio Bytes:
https://followthrough.alhinai.dev/api/webhooks/omi/audio?token=<PER_DEVICE_SECRET>
Interval: 5 seconds
```

Omi appends `uid`, and the raw-audio hook also appends `sample_rate`. Followthrough accepts the official client's octet-stream delivery even when it omits `Idempotency-Key`, archives PCM16 immediately, and returns `202` before any Hermes work. A supplied timestamp or sequence becomes a stable replay key; otherwise every delivery is preserved uniquely so repeated silence is not collapsed.

The query token is a stock-Omi compatibility compromise because Omi does not support custom webhook headers or signed webhook requests. Uvicorn access logs are disabled. Rotate the device token if the configured URL is disclosed.

## Rich native API

An Omi fork or phone companion should prefer headers and explicit identifiers:

```http
POST /api/v1/transcripts
Authorization: Bearer <device-token>
Content-Type: application/json

{
  "event_id": "stable-device-event-id",
  "device_id": "omi-primary",
  "source": "omi",
  "occurred_at": "2026-07-11T21:00:00Z",
  "text": "Research github.com/example/tool",
  "consent": true,
  "metadata": {}
}
```

```http
PUT /api/v1/audio/<event_id>/<sequence>
Authorization: Bearer <device-token>
X-Device-Id: omi-primary
X-Content-SHA256: <plaintext-sha256>
Content-Type: audio/ogg

<encrypted-in-transit audio bytes>
```

The native contract provides exact transcript/audio correlation, ordered chunks, and digest validation that stock Omi lacks.

## Processing policy

- Transcripts are the low-latency control plane.
- Raw audio is the complete encrypted evidence plane.
- A valid per-device webhook token authorizes the Omi capture channel for ambient
  dispatch. Relevant speech may therefore be queued for Hermes whether Omi reports
  `is_user=true`, `is_user=false`, or no attribution.
- Omi speaker fields and the resulting owner/non-owner/unknown status are retained in
  encrypted archive metadata and decision evidence; ambient authorization does not
  rewrite speaker identity.
- Irrelevant speech remains encrypted archive-only and never enters Hermes memory or
  actions, regardless of speaker attribution.
- Invalid or missing Omi tokens fail closed. The ambient authorization does not apply
  to native unknown or authenticated non-owner contexts.
- Completed-conversation hooks reconcile live segment revisions.
- A later backfill worker will use the Omi Developer API to repair missed completed conversations.

## Background limitations

Android can continue with the screen locked through its foreground microphone service, but force-stop or reboot requires reopening Omi. On Omi 1.0.544 with the Basic cloud quota exhausted, on-device Whisper continued transcribing while the screen was off, but realtime transcript/audio developer hooks did not fire independently; completed-conversation delivery worked after finalization and is currently eventual rather than realtime. Raw PCM16 at 16 kHz mono is approximately 2.76 GB per day, so any future raw stream needs capacity alarms and compressed archival compaction.

## Verified on Spark

- Official transcript-shaped edge request: `202` in 93 ms.
- Official no-header PCM16 edge request: `202` and one encrypted audio chunk.
- Stored audio: AES-256-GCM ciphertext, mode `0600`.
- Unauthenticated private APIs: `401`.
- Samsung `SM-F776U1`, Android 17, Omi 1.0.544: three endpoints persisted, all toggles enabled, five-second interval, local `ggml-tiny.bin` ready, foreground microphone active while Dozing.
- Physical-phone conversation finalization added 35 encrypted events; a screen-off session added 5 more.

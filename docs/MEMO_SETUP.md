# Memo Android sensor

Memo is the primary phone sensor for Followthrough. Omi and the Termux uploader are not part of the active runtime path. Memo provides the complete two-way loop: audio upload, interim and finalized transcript delivery, durable job receipts, restart-safe status polling, Discord result delivery, and optional spoken results through the built-in phone speaker. The Samsung SM-F776U1 is the supported test phone; see [CURRENT_STATE.md](CURRENT_STATE.md).

## Runtime path

```text
Samsung Flip microphone
  -> Memo AmbientCaptureService
  -> PCM16 chunks + interim/finalized Gemini input transcripts
  -> https://followthrough.alhinai.dev
  -> complete local archive
  -> relevance gate
  -> relevant-only Hermes job
  -> durable Discord DM result
  -> tokenless status/result polling
  -> optional phone loudspeaker response
```

## Source and build

- Repository: `https://github.com/yhinai/memo`
- Mac checkout: `/Users/alhinai/Projects/memo`
- Android project: `/Users/alhinai/Projects/memo/android`
- Package: `dev.alhinai.memo`

The endpoint defaults to `https://followthrough.alhinai.dev`. The discreet Settings screen exposes only the endpoint, Gemini key, and `Discord only` / `Discord + voice` response choice. No Followthrough token is needed or stored.

## Verified phone

- Model: Samsung SM-F776U1
- USB serial on Mac: `R3GL40M4XXV`
- Wireless ADB is kept alive by the Mac bridge; use `adb devices` on the Mac to see the current LAN and private addresses.
- Foreground service: `AmbientCaptureService`

## Replacement state

- Omi (`com.friend.ios`) process stopped and microphone permission revoked.
- Termux audio uploader stopped.
- Unused `followthrough-adb-bridge.service` disabled and stopped.
- Memo foreground microphone service active.
- Followthrough accepted real Memo PCM audio plus interim and finalized transcripts.
- Audio continued after the app left the foreground.
- A real spoken gold-price request created an H Company session, completed a Hermes job, returned through restart-safe phone polling, and produced a verified Discord DM.
- `Discord only` completed without phone playback; `Discord + voice` produced built-in-speaker playback.

Omi may remain installed as an inactive rollback option, but Memo is the only active capture path.

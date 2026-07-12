# Memo Android sensor

Memo is the primary phone sensor for Followthrough. Omi and the Termux uploader are not part of the active runtime path. Memo holds a consent-scoped LiveKit room connection and publishes only its microphone track. The Samsung SM-F776U1 is the supported test phone; see [CURRENT_STATE.md](CURRENT_STATE.md).

## Runtime path

```text
Samsung Flip microphone
  -> Memo AmbientCaptureService
  -> private LiveKit Cloud room
  -> Followthrough LiveKit worker on Spark
  -> Deepgram partial/final transcripts
  -> relevance gate
  -> every final stored in the transcript archive
  -> relevant-only operational memory and Hermes job
  -> durable Discord DM result
  -> optional LiveKit phone-loudspeaker response
```

## Source and build

- Repository: `https://github.com/yhinai/memo`
- Mac checkout: `/Users/alhinai/Projects/memo`
- Android project: `/Users/alhinai/Projects/memo/android`
- Package: `dev.alhinai.memo`

The endpoint defaults to `https://followthrough.alhinai.dev`. The discreet Settings screen exposes only the endpoint and `Discord only` / `Discord + voice` response choice. Memo sends explicit consent and its stable device ID to obtain a short-lived, room-scoped token; no LiveKit API secret is stored on the phone.

## Verified phone

- Model: Samsung SM-F776U1
- USB serial on Mac: `R3GL40M4XXV`
- Wireless ADB is kept alive by the Mac bridge; use `adb devices` on the Mac to see the current LAN and private addresses.
- Foreground service: `AmbientCaptureService`

## Replacement state

- Omi (`com.friend.ios`) process stopped and microphone permission revoked.
- Termux audio uploader stopped.
- Unused `followthrough-adb-bridge.service` disabled and stopped.
- Memo foreground microphone service starts whenever the intentional Muted state is cleared.
- Memo published a real microphone track to LiveKit Cloud and the Spark worker joined the same room.
- Audio continued after the app left the foreground.
- A real spoken gold-price request created an H Company session, completed a Hermes job, returned through LiveKit voice when enabled, and produced a verified Discord DM.
- `Discord only` completed without phone playback; `Discord + voice` produced built-in-speaker playback.
- Adjacent STT finals are coalesced briefly on Spark so one spoken request creates one durable signal.
- A generated spoken `Memo, research ...` request traversed LiveKit, Deepgram, the relevance gate, and Hermes as one event. Ordinary speech is preserved in the transcript archive but does not enter operational memory or trigger work.

Omi may remain installed as an inactive rollback option, but Memo is the only active capture path.

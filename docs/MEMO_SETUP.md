# Memo Android sensor

Memo is the primary phone sensor for Followthrough. Omi and the Termux uploader are not part of the active runtime path. Memo provides the complete two-way loop: audio/transcript upload, durable job receipt, restart-safe status polling, and spoken Hermes results through the built-in phone speaker. The Samsung SM-F776U1 is the supported test phone; see [CURRENT_STATE.md](CURRENT_STATE.md).

## Runtime path

```text
Samsung Flip microphone
  -> Memo AlwaysListeningService
  -> five-second PCM16 chunks + finalized Gemini input transcripts
  -> https://followthrough.alhinai.dev
  -> complete local archive
  -> relevance gate
  -> relevant-only Hermes job
  -> tokenless status/result polling
  -> phone loudspeaker response (plus Discord when enabled)
```

## Source and build

- Repository: `https://github.com/yhinai/memo`
- Mac checkout: `/Users/alhinai/Projects/memo`
- Android project: `/Users/alhinai/Projects/memo/samples/CameraAccessAndroid`
- Package: `com.meta.wearable.dat.externalsampleapps.cameraaccess`

The endpoint defaults to `https://followthrough.alhinai.dev`. Endpoint and continuous-archive enablement are available in the Memo Settings screen. No Followthrough token is needed or stored.

## Verified phone

- Model: Samsung SM-F776U1
- USB serial on Mac: `R3GL40M4XXV`
- Wireless ADB is kept alive by the Mac bridge; use `adb devices` on the Mac to see the current LAN and private addresses.
- Foreground service: `AlwaysListeningService`

## Replacement state

- Omi (`com.friend.ios`) process stopped and microphone permission revoked.
- Termux audio uploader stopped.
- Unused `followthrough-adb-bridge.service` disabled and stopped.
- Memo foreground microphone service active.
- Followthrough accepted real Memo PCM audio and finalized transcripts.
- Audio continued after the app left the foreground.

Omi may remain installed as an inactive rollback option, but Memo is the only active capture path.

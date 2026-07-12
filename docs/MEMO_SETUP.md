# Memo Android sensor

Memo is the primary phone sensor for Followthrough. Omi and the Termux uploader are no longer part of the active runtime path. Memo now provides the complete two-way loop: audio/transcript upload, durable job receipt, restart-safe status polling, and spoken Hermes results through the built-in phone speaker. Samsung SM-F776U1 and OnePlus CPH2513 have both been verified; see [CURRENT_STATE.md](CURRENT_STATE.md).

## Runtime path

```text
Samsung Flip microphone
  -> Memo AlwaysListeningService
  -> five-second PCM16 chunks + finalized Gemini input transcripts
  -> https://followthrough.alhinai.dev
  -> encrypted archive
  -> relevance gate
  -> relevant-only Hermes job
  -> authenticated status/result polling
  -> phone loudspeaker response (plus Discord when enabled)
```

## Source and build

- Repository: `https://github.com/yhinai/memo`
- Mac checkout: `/Users/alhinai/Projects/memo`
- Android project: `/Users/alhinai/Projects/memo/samples/CameraAccessAndroid`
- Package: `com.meta.wearable.dat.externalsampleapps.cameraaccess`
- Build token: pass the private device token as Gradle property `followthrough_token` or place it in the ignored `local.properties` file.

The endpoint defaults to `https://followthrough.alhinai.dev`. Endpoint, token, and continuous-archive enablement are also available in the Memo Settings screen.

## Verified phone

- Model: Samsung SM-F776U1
- USB serial on Mac: `R3GL40M4XXV`
- Current network ADB path from Spark: `100.96.0.1:5555`
- Foreground service: `AlwaysListeningService`

## Replacement state

- Omi (`com.friend.ios`) process stopped and microphone permission revoked.
- Termux audio uploader stopped.
- Legacy `followthrough-adb-bridge.service` disabled and stopped.
- Memo foreground microphone service active.
- Followthrough accepted real Memo PCM audio and finalized transcripts.
- Audio continued after the app left the foreground.

Do not uninstall Omi until Memo completes a longer soak; retaining the inactive package preserves a rollback path without allowing it to capture.

# Memo physical voice end-to-end evidence — 2026-07-11

## Physical capture lineage

- Sensor: Memo Android app on Samsung SM-F776U1.
- Audio source: command played through the Mac speaker beside the USB-connected Flip and captured by the Flip microphone.
- Memo delivered continuous authenticated PCM16 audio and finalized Gemini input transcripts.
- Followthrough aggregated the split command as event `adb-omi:aggregate:fb6bca14030a0781176a7f88c379b17c225451ead151e1721b609ade187a35eb`.
- The relevance gate classified it as `repository` and created run `08223bf7-21b4-4863-ac47-ad742cd9095f`.

Gemini omitted the spoken owner name `PyPA` from its finalized streaming transcript, leaving `sample project`. The repository validator refused to guess and kept the job in retry. The operator applied the user's explicit canonical target `https://github.com/pypa/sampleproject`, after which the same physical run continued as Hermes task `t_caf31e74`.

## Repository result

- Task state: completed.
- Commit: `621e4974ca25ce531773def586ba3ed8e736b3fc`.
- Tree: `c7d439931f56fa21023a7a0e615b91f5699c1827`.
- Execution: Python unittest.
- Exit code: 0.
- Network: disabled.
- Sandbox: `systemd-service+bubblewrap`.
- Receipt: `8d346149bd5ea9db99b4ff1e6ba2b1dd9d58ae61011ad336c7ebe82296932ce4`.
- Verified license text: MIT in `LICENSE.txt`; the deterministic runner's license field remained `UNKNOWN` and was preserved as an explicit discrepancy.

## Discord result

- Channel: owner Discord DM `1510104161612730378`.
- Completion message: `1525729546224013373`.

## Follow-up hardening

- Memo now routes Gemini's structured `execute(task=...)` call to Followthrough instead of the obsolete direct Hermes endpoint.
- Structured actions require the `Followthrough` wake phrase inside a fresh 30-second transcript window.
- A physical no-wake message request produced a Gemini tool call but Memo logged `Ignoring ambient tool call without Followthrough wake phrase`; the Followthrough structured-tool-call event count stayed unchanged.
- Passive email conversation no longer matches the contact rule.

The physical path is proven, but P7-01 remains pending until one fresh canonical repository command completes without operator entity repair.

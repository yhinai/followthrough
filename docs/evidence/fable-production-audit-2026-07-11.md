# Fable production audit — 2026-07-11

Claude Code 2.1.207 ran from the Followthrough repository with model `fable`, high effort, and the user-requested bypass-permissions mode. Five internal review workers covered code, live runtime, documentation, and fixes.

## Accepted fixes

1. Bind native audio read/write endpoints to the server-derived device principal.
2. Preserve the first capture principal during idempotent archive metadata merges.
3. Bound audio sequence indices to prevent dense-manifest allocation abuse.
4. Convert malformed Omi timestamps from server errors to HTTP 422.
5. Neutralize transcript fence delimiters before Hermes prompt interpolation.
6. Strip ANSI and control bytes from Hermes summaries returned to devices.
7. Reject protected self-improvement targets by filename stem as well as directory.
8. Remove a two-transaction false positive from capture continuity monitoring while retaining interior-hole detection.
9. Parse optional-fraction logcat clocks and convert device-local time to UTC correctly.
10. Fail closed for device access to legacy audio events without a capture principal; the owner dashboard remains authorized.
11. Scan SQLite WAL/SHM sidecars in plaintext-leak regression assertions.

## Independent verification

- Ruff: clean.
- Pytest: 169 passed, one pre-existing Starlette deprecation warning.
- No tracked secret or secret-bearing data file was added.
- Healthy services were not restarted during the audit.
- The old soak ledger is preserved rather than rewritten; its single pre-fix false checkpoint means it cannot satisfy the zero-failure acceptance gate.

## Deferred structural work

Follow-up implementation completed aggregate prefix privacy, aggregate replay idempotency, cross-process control-audit serialization, and atomic audio file/manifest persistence. The independently rerun suite is now 173 passed.

Follow-up orchestration work now keeps typed effects retryable until terminal reconciliation, parks external cards when emergency controls win a create race, forwards supersession idempotency keys to Hermes, and dead-letters create/notification poison jobs after five attempts. Static inspection now covers common shell/runtime extensions and scans the first MiB of oversized textual files. The independently rerun suite is now 180 passed.

Remaining structural work includes archive-key rotation/permission checks, device-aggregator eviction, and the stock-Omi query-token compatibility risk. These remain explicit plan items rather than being hidden behind the passing suite.

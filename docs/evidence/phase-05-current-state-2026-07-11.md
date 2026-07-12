# Phase 5 current-state connector verification — 2026-07-11

The active post-reset effect journal was exercised through the production typed-effector CLI. Inputs were private mode-0600 files and every operation used a stable idempotency key.

| Connector | Effect ID | Provider receipt | Result |
|---|---|---|---|
| Private task | `47022a27-1dce-48ef-8cba-cc017e23fd9d` | `task_8c9ea810e3ba7546de1cfa0c` | completed automatically |
| Owner Discord | `3e11c79e-ef46-4d22-a5c7-c4819cc8e783` | message `1525684887787536454` | completed automatically |
| Google Calendar | `2b9bc098-f6ea-43cd-a322-7e60ed95ccf9` | event `ft4c5b340a55023dc7f8b197f2a672f62eb52081eb` | created, receipt verified, then rolled back |
| Sandbox purchase | `57157a54-dc78-4d69-ac5b-970d86ddaaa8` | `test_purchase_4374422401cb7b709d643872` | authorized for USD 0.01, then voided |
| Preview deployment | `a7a89bf3-a19d-40bd-9df5-ceba3d2e903c` | GitHub Actions run `29176367068` | autonomously dispatched; public health verification passed |

The append-only transition log and effect rows are in `data/effects/effects.db`. No attendee notifications, Discord mentions, or real funds were used.

The deployment workflow is repository-scoped to `yhinai/followthrough`, manually dispatchable, and verifies the real public `/healthz` endpoint. Production deployment remains outside the autonomous preview policy.

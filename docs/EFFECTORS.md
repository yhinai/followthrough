# Typed external effectors

Memo's authenticated phone return channel is a read surface, not an unrestricted effector. External writes still pass through the typed policies and receipts below. See [CURRENT_STATE.md](CURRENT_STATE.md).

Followthrough never gives a transcript, web page, repository, or Hermes card arbitrary shell access to an external account. An accepted action must first become one of six validated request types:

| Type | Current driver | Native idempotency / retry boundary | Rollback |
|---|---|---|---|
| `private_task.create` | private local SQLite task store | unique action key | cancel task |
| `calendar.event.upsert` | Google Calendar REST | deterministic event ID plus `PUT` | delete without attendee updates |
| `discord.message.send` | `hermes send` with message body on stdin | journal at-most-once; ambiguous delivery stops for reconciliation | delete only when a message/channel receipt and deleter are configured |
| `github.issue.create` | authenticated `gh api` | hidden hashed marker is checked before create | close issue |
| `deployment.trigger` | GitHub Actions `workflow_dispatch` | journal at-most-once; ambiguous dispatch stops for reconciliation | explicit rollback workflow only |
| `purchase.create` | local sandbox purchase provider | unique action key | void sandbox authorization |

The live purchase provider is intentionally not guessed. Add a vendor-specific driver that supports a provider idempotency header and typed void/refund before enabling live purchase mode.

## Durable action protocol

Every request contains `trigger_event_id` and is registered in `data/effects/effects.db` before a connector runs. The request fingerprint and idempotency key are unique. Reusing the key with different content fails closed.

The state path is:

`registered -> ready|awaiting_approval|dry_run|denied -> executing -> completed`

Authentication expiry and rate limiting become `retryable_failure`. A network or response failure after a non-idempotent write becomes `uncertain`; the executor will not resend it until an operator or a connector-specific reconciliation proves whether the first write happened. All transitions are append-only at the SQLite trigger level.

After a process or host restart, explicitly park any `executing` records as `uncertain` before resuming work:

```bash
.venv/bin/python scripts/followthrough-effector.py recover-inflight
```

Receipts name the provider, external object, response fingerprint, triggering event, reversibility, and sanitized rollback metadata. Request bodies, OAuth tokens, Discord tokens, and payment credentials never appear in command arguments or CLI output.

## Policy profiles

`AutonomyPolicy.safe_default()` automatically permits private tasks and an allowlisted owner Discord DM. Calendar writes, GitHub writes, deployments, and financial effects remain approval-gated or dry-run.

`AutonomyPolicy.maximum_bounded()` permits reversible typed actions automatically for explicit Discord targets and GitHub repositories. Production deploys and live purchases remain gated unless their individual flags are enabled. A live purchase additionally requires:

- an allowlisted vendor;
- the configured currency;
- a per-purchase cap;
- an atomic daily cap;
- a real provider driver with idempotency and rollback.

Start from [`config/effect-policy.example.json`](../config/effect-policy.example.json). Replace the owner target and repository placeholders, copy it to `~/.config/followthrough/effect-policy.json`, and set mode `0600`. An empty purchase vendor list intentionally blocks autonomous live spending.

The CLI loads `~/.config/followthrough/effect-policy.json` when `--policy` is omitted. The installed local policy automatically permits private tasks, self-only calendar writes, owner Discord reports, and test purchases. GitHub writes, deployments, attendee notifications, non-owner messages, production, and live spending remain approval-gated until their exact targets and caps are added.

## Hermes invocation

Hermes should write a single typed request to a mode `0600` JSON file, then use the CLI. The CLI only registers by default; `--execute-if-auto` is the explicit switch that allows a policy-approved connector call.

```bash
chmod 600 /path/to/action.json
.venv/bin/python scripts/followthrough-effector.py \
  --policy /path/to/effect-policy.json \
  plan --request /path/to/action.json \
  --idempotency-key 'followthrough:EVENT_ID:ACTION:v1'
```

For an approved action:

```bash
.venv/bin/python scripts/followthrough-effector.py approve EFFECT_ID --principal local-owner
.venv/bin/python scripts/followthrough-effector.py execute EFFECT_ID
```

To inspect or reverse an action:

```bash
.venv/bin/python scripts/followthrough-effector.py show EFFECT_ID
.venv/bin/python scripts/followthrough-effector.py history EFFECT_ID
.venv/bin/python scripts/followthrough-effector.py rollback EFFECT_ID
```

An ambiguous outcome requires an independent check. Only after proving it did not happen may it be moved back to the retryable state:

```bash
.venv/bin/python scripts/followthrough-effector.py resolve-uncertain EFFECT_ID --principal local-owner
```

If it did happen, supply a mode `0600` provider receipt and add `--applied --receipt /path/to/receipt.json`.

## Credential boundaries

- Google Calendar loads the existing OAuth token and client record from `~/.hermes/user/google-workspace` and refreshes it without emitting token material. Both files must be mode `0600`.
- Discord delegates credential loading to `hermes send`; content is passed over stdin, not the process list.
- GitHub delegates authentication to `gh`; bodies and deployment inputs are passed over stdin.
- Purchase credentials are not present in this repository. Sandbox mode is the only installed purchase driver.

The focused suite is `tests/test_effectors.py`. It covers duplicate suppression and rollback across all six types, authentication expiry, rate limiting, partial/uncertain writes, manual reconciliation, daily spend caps, append-only history, and connector-specific request construction.

## Self-hosted desktop plane (free)

`scripts/followthrough-desktop-api.py` serves the local desktop contract
(`/health`, `/screenshot`, `click/type/key/scroll/drag`) against an Xvfb display
driven by xdotool, so no paid remote desktop provider is required. Two user
units run it:

- `followthrough-xdesktop.service` — Xvfb `:99` at 1280x800, openbox, and the
  demo apps (gedit + xterm).
- `followthrough-desktop-api.service` — the loopback control plane on `:8080`,
  authenticated with `ORGO_DESKTOP_API_TOKEN` from the private secrets file.

`DesktopRouter` prefers this local plane whenever it is healthy, so the live
viewer on the dashboard works with no external spend.

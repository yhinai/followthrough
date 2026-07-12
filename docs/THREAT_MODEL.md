# Threat model and autonomy boundary

Current transport is Memo Android, with Omi retained only as an inactive compatibility path. Device job reads are bound to a server-derived credential principal, so another valid device token cannot read a different phone's result. See [CURRENT_STATE.md](CURRENT_STATE.md).

## Assets

- Complete audio and transcript archive.
- Hermes memory, skills, sessions, and connected accounts.
- GitHub, email, calendar, Discord, browser, deployment, and payment credentials.
- Spark host, user files, network, and public Cloudflare endpoint.

## Trust boundaries

1. Omi/phone to Cloudflare is untrusted transport until the device bearer token and payload integrity pass.
2. The complete archive is private data, not agent context.
3. Relevance gating is the only supported path into operational memory.
4. Web content, repositories, email, Discord, and transcripts are untrusted instructions.
5. Unknown native code is never allowed to inherit the Hermes/service credential environment.
6. Self-improvement candidates are untrusted until independent gates pass.

## Principal threats

- Forged or replayed device events.
- Public transcript/report disclosure.
- Prompt injection in conversations, web pages, repositories, or messages.
- Repository install scripts stealing credentials or establishing persistence.
- Duplicate messages, calendar edits, deployments, subscriptions, or purchases after retries.
- Model mistakes presented as successful real-world actions.
- Archive tampering, corruption, or accidental plaintext copies.
- A self-improvement changing its own evaluator or safety policy.
- Compromised Discord emergency commands.

## Mandatory controls

- Independent device and dashboard credentials, constant-time comparison, rotation, and revocation.
- Local archive digests and authenticated device ingress.
- Event and action idempotency keys with uniqueness constraints.
- Append-only audit receipts for every state transition and external side effect.
- Separate archive and operational stores; irrelevant content is never inserted into Hermes memory.
- Dedicated native-run identity, clean environment, resource limits, snapshots, and rollback.
- Typed adapters rather than arbitrary shell for external side effects.
- Candidate-only learning with independent verification and held-out regression checks.
- Fail-closed safe mode and a local kill path that does not depend on Discord or the model.

## Explicit product decisions

- The user requested complete audio/transcript retention and continuous listening.
- Recording indicators, consent obligations, venue policy, and applicable law remain operator responsibilities.
- The user requested broad autonomous authority, but credentials are still compartmentalized so a single malicious repository cannot exercise every authority.
- Payments and irreversible actions require receipts, anomaly detection, and emergency shutdown even when no confirmation is requested.

# Bounded self-improvement

Self-improvement is candidate-first. Mining, reflection, or Hermes may propose
an artifact, but no proposal edits `~/.hermes/skills`, a live prompt, a control,
or an evaluator. Candidates and reports are owner-only files under
`data/self-improvement/`.

## Flow

1. **Propose** a relative target, candidate content, and one or more local
   evidence artifacts with pinned SHA-256 digests.
2. **Evaluate** with the built-in evaluator and named held-in/held-out cases.
3. **Stage-promote** only when every deterministic gate passes.
4. **Live-promote** only after an owner explicitly enables a destination root,
   supplies an owner approval identity, and supplies a unique approval
   reference.
5. **Roll back** a live promotion using its retained backup or remove a newly
   created artifact.

## Deterministic gates

- Candidate hash still matches the proposal.
- Target does not address an evaluator, gate, verifier, emergency control,
  kill switch, or promotion policy.
- Unsafe-pattern scanner finds no test skipping, self-approval, gate disabling,
  credential exfiltration, `curl | sh`, or equivalent policy weakening.
- Every evidence artifact exists under an allowed evidence root and matches its
  pinned digest.
- The evaluator source fingerprint matches the persistent policy fingerprint.
- Every held-in case passes.
- No held-out case that passed at baseline regresses, and aggregate held-out
  passes do not decline.

Changing the evaluator source invalidates promotion until an explicit owner
policy update pins the new fingerprint. Modifying either the candidate or the
evaluation report after evaluation also blocks promotion.

## Live-promotion boundary

Live promotion is disabled by default and has an empty destination allowlist.
The application never calls live promotion from its ingest or orchestrator
loop. Enabling it requires a dashboard-authenticated policy request whose actor
begins with `owner:`. Promotion then requires a second explicit owner approval
and a reference at least eight characters long. This is the only path that may
write an allowlisted live root.

The relevant private routes are:

```text
GET  /api/self-improvement
POST /api/self-improvement/proposals
POST /api/self-improvement/proposals/{id}/evaluate
PUT  /api/self-improvement/policy
POST /api/self-improvement/proposals/{id}/promote
```

Omitting `live_root` makes promotion staged. Supplying it requests live
promotion and invokes all owner-policy checks. Promotion consumes the
`self_improvement` capability budget and writes a separate immutable receipt.

## What is intentionally not automatic

- No generated candidate can change its own evaluator or promotion policy.
- No failed or regressed candidate can be promoted.
- No candidate can add cron jobs, MCP servers, credentials, or auto-approval.
- No Hermes skill is silently installed or reloaded.
- No live target is inferred from the candidate.


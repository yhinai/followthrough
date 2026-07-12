# Followthrough build instructions

This repository is the fresh, event-day build for the GrowthX Hermes Buildathon.

## Product boundary

Followthrough is a consent-first ambient BizDev agency. It turns a founder's real-world
conversation into a qualified opportunity, cited research, a follow-up draft, a CRM record,
and a completion briefing. Every finalized transcript is kept in the local archive;
ordinary conversation is excluded from operational memory and actions.

## Runtime boundary

- Hermes is the live manager and durable action runtime.
- The manager dynamically chooses specialists per signal; never pretend a static pipeline is dynamic.
- Only read-only research and owner-facing reports may execute automatically by default.
- External messages, payments, account mutations, and destructive actions require explicit approval.
- Keep finalized transcript/audio in the complete local archive, but never promote
  ignored conversation into Hermes memory, Workspace, or an action queue.
- Never print or commit secrets.

## Definition of done

- A cold user can consent, submit or speak a signal, and receive a real output.
- Every run has a searchable agent trace, latency, tokens, estimated cost, and a public brief.
- Failures become eval cases.
- Tests pass and the public Cloudflare URL works from a separate client.

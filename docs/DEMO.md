# 90-second H Company demo

The live demo should finish in Discord and optionally on the phone. Followthrough is the product; Memo is its Android capture companion and the only wake phrase. H Company owns browser execution, while Hermes owns the durable plan, receipt, and delivery lifecycle. Current setup details are in [CURRENT_STATE.md](CURRENT_STATE.md).

## Stage script

- **0–10s:** “Followthrough is an ambient research operator. Memo transcribes the day and turns intent into verified computer use.” Show Memo listening.
- **10–22s:** Say: “Memo, check the current RTX 5080 price on Best Buy and send it to Discord.” Show the words settle in the Transcript tab.
- **22–34s:** Show Heard → Relevant → Delegated and the decision “Explicit Memo command.”
- **34–55s:** Show the H Company session working: live frame, current action, steps, and replay link.
- **55–67s:** While it works asynchronously, open Workspace. Show a passive tool mention in Backlog, then edit or remove it.
- **67–80s:** Show Verified → Discord → Phone and open the real DM receipt.
- **80–90s:** Show ordinary lunch chatter in the complete log with “No action,” then close on the event ID, H replay, Hermes receipt, and public URL.

## Context — 20 seconds

“Founders do not lose opportunities because they lack conversations. They lose them because follow-through disappears by the time they leave the room. Followthrough is an ambient BizDev agency powered by Hermes: it turns a consented conversation into researched, traceable next actions.”

## Live demo — remaining 1 minute 40 seconds

1. Open `https://followthrough.alhinai.dev/#transcript` and show words arriving live from Memo.
2. Say: “Memo, check the current RTX 5080 price on Best Buy and tell me when you're done.”
3. Switch to Overview and show Heard → Relevant → Delegated → Browsing → Verified → Discord → Phone. Point out that every stage is linked to the same source event.
4. Show the live H Company session, current action, screenshot, monotonic step count, elapsed time, and replay link.
5. Open the completed typed Discord DM receipt.
6. In `Discord + voice`, let Memo speak the same verified result; in `Discord only`, show that the phone stays silent.
7. Show the edge case: “Lunch was great; the sandwich was perfect.” It is archived without entering Hermes memory or creating work.

The expected product behavior is asynchronous: “You mention something, put
your phone away, and Followthrough handles it in the background.” Do not wait
silently for a long browser run. Start the request early, narrate the linked
journey, and let the Discord/phone delivery land near the end.

## Proof — 1 minute

Show, in this order:

1. The run trace for three completed real tasks and the per-run latency.
2. The Hermes session receipt and dynamic plans.
3. The Discord DM report.
4. The public Cloudflare URL and tunnel dashboard.
5. Convex, Linkup, ElevenLabs, Dodo, and Wispr evidence only when each is genuinely active.
6. Activated-user and payment counts directly from the backing systems, never typed numbers.

## Expected Q&A

**Is this secretly a transcript logger?** It is an explicit always-listening prototype: every finalized transcript is kept in the local Spark archive. Ordinary conversation is excluded from operational memory and actions; only promoted signals reach specialists. External writes remain policy-gated.

**What is autonomous today?** Triage, Hermes planning, specialist delegation, research when configured, scoring, CRM persistence, owner reporting, and exception recording. Third-party outbound remains gated because silent autonomous messaging would be unsafe.

**Was this pre-built?** The idea existed. This repository and product implementation are a fresh event-day build; existing Hermes and Cloudflare services are infrastructure. The lineage is disclosed explicitly.

# Two-way and multi-phone evidence — 2026-07-11

- Followthrough commit `3eb20da` persists Hermes run summaries and exposes sanitized device job results.
- Memo commit `8026daa` persists pending job IDs, resumes polling after restart, feeds terminal results into Gemini Live, and forces the built-in loudspeaker.
- API authorization tests cover unauthenticated rejection and cross-device non-disclosure.
- Followthrough suite after the change: 157 passed.
- Public job `6aa9cb61-ccb8-4e09-8167-8ff8b9ffa1d8` reached `completed` with Hermes task `t_11774713`.
- Samsung SM-F776U1 and OnePlus CPH2513 both reported the built-in speaker as active.
- The OnePlus logged accepted transcript and audio delivery, and Spark stored rows for `memo-OnePlus-CPH2513`.
- The uninterrupted 24-hour soak remains pending.

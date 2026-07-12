# Acceptance matrix

Status values: `pending`, `pass`, `fail`, `blocked`. Evidence must name a test, report, trace, or command output.

| ID | Requirement | Phase | Status | Evidence |
|---|---|---:|---|---|
| P0-01 | Private API denies unauthenticated access | 0 | pass | `tests/test_ingestion.py::test_private_routes_fail_closed`; public edge 401 |
| P0-02 | Hermes, Discord, service, tunnel healthy | 0 | pass | `docs/evidence/phase-01-2026-07-11.md` |
| P0-03 | Baseline unit and relevance evals pass | 0 | pass | 11 pytest cases, Ruff, 3/3 baseline evals |
| P1-01 | Device-authenticated transcript ingest | 1 | pass | public edge 202 in 39 ms |
| P1-02 | Retry creates no duplicate archive/run | 1 | pass | public transcript/audio replay checks; contract tests |
| P1-03 | Transcript encrypted at rest | 1 | pass | AES-GCM round-trip/tamper test; zero plaintext run/eval rows |
| P1-04 | Chunked audio encrypt/decrypt integrity | 1 | pass | encrypted PCM edge check; digest conflict 409; missing-sequence manifest test |
| P1-05 | Irrelevant archive event reaches no Hermes path | 1 | pass | archive-only and non-owner contract tests |
| P2-01 | Authenticated ambient and speaker-provenance gates meet threshold | 2 | pass | `tests/test_relevance.py`; Omi ambient authorization cannot bypass non-Omi provenance |
| P2-02 | Relevance held-out set meets threshold | 2 | pass | 28/28 held-out cases plus generic `to do` false-positive regression |
| P2-03 | Archive and operational memory remain separated | 2 | pass | `tests/test_relevance_integration.py`; raw transcripts absent from operations DB and capsules |
| P3-01 | Durable execution survives restart | 3 | pass | `docs/evidence/phase-03-04-2026-07-11.md`: three orchestrator stop/start cycles; one durable job and audited task lineage persisted |
| P3-02 | Discord delivery is deduplicated and recoverable | 3 | pass | Live completion `1525623027277107382`, blocked `1525622226416566282`; no duplicate hashes or raw markers |
| P4-01 | Known-good native repository lifecycle passes | 4 | pass | Live PyPA sampleproject receipt `5e980b63e973028d729dc5e6a2ab6c13d7a9658a989a011dcdc3abddb8b2a9a4`, exit 0, network disabled |
| P4-02 | Malicious fixture is contained and rolled back | 4 | pass | `tests/test_runner.py::test_malicious_repo_is_denied_then_boundaries_hold_under_red_team_override`; timeout rollback test |
| P5-01 | Every enabled connector passes auth/policy/failure tests | 5 | pass | 40 effector tests plus current post-reset private task, Google Calendar, owner Discord, sandbox purchase, and autonomous GitHub Actions deployment receipts in `docs/evidence/phase-05-current-state-2026-07-11.md`; production deploy and live purchase remain bounded by policy |
| P5-02 | External actions have idempotent receipts | 5 | pass | Current effect journal receipts and reversible calendar/purchase rollback proof in `docs/evidence/phase-05-current-state-2026-07-11.md` |
| P6-01 | Emergency controls meet recovery target | 6 | pass | Live pause 6.14 s and least-authority resume 5.236 s, both under 10 s |
| P6-02 | Self-improvement regression blocks promotion | 6 | pass | `tests/test_self_improvement.py`; pinned evaluator, protected targets, held-out regression and artifact-tamper gates |
| P7-01 | Phone-to-action end-to-end succeeds | 7 | pending | Physical Memo voice produced completed Hermes task `t_caf31e74`, sandbox receipt `8d346149...`, and Discord message `1525729546224013373`; Gemini omitted `PyPA`, so an owner-confirmed entity repair was required. See `docs/evidence/memo-physical-e2e-2026-07-11.md`; one no-repair run remains. |
| P7-02 | 24-hour soak has zero loss/duplicate side effects | 7 | pending | `followthrough-soak-24h.service` active; mode-0600 receipt `data/soak/phase7-24h.jsonl`; early checkpoints have zero hard failures |
| P7-03 | Backup/restore and credential rotation pass | 7 | pass | `data/backups/phase7-20260711T224637Z`; three restored DBs integrity `ok`; temporary device token accepted then revoked 202→401 |

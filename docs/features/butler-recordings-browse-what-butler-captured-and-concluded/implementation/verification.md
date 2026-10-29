# Recordings specification verification

## Binding records

Read every original file in this feature folder before implementation: Q-001–Q-006 (answers), DEC-001–DEC-006 (accepted decisions), F-001–F-003 (resolutions), discussion.md, overview, all requirements and implementation plan. Applied DEC-006 over Q-002’s former video exclusion, DEC-004 over the prior Display visibility policy, and DEC-005’s file-only controls with independent fixed expiry. New owner answers about main and earlier work are recorded in DEC-007. No answered product question was asked again.

## Requirement coverage

| Requirement | Implementation | Automated evidence |
| --- | --- | --- |
| REQ-001: Recordings/day/source/sessions/ranges/content | `recordings.py` and native `RecordingsPane`; bounded session/child pages | `test_sessions_split_on_activity_source_gap_and_day_with_child_paging`; native sidebar/expand/navigation checks |
| REQ-002: source/time/fact fields/transcript | Recordings detail and separate transcript records | `test_media_file_actions_leave_shared_facts_transcripts_and_other_files`; native transcript/fact/basis checks |
| REQ-003: images/key frames/audio/video/unavailable | Observation/media/event-frame joins; generated images and Display history; AVKit and disposable FFmpeg MP4 derivatives | Actual JPEG-to-MP4 regression; generated-history test; native image/detail check |
| REQ-004: selected-file controls and marked meeting audio/video | `Store.preserve/delete`, capture tagging, explicit current-call mark, fixed file expiry | Shared-file isolation; all three sources retained past 24 hours and deleted at 30 days even after preserve; new-call/restart reset tests |
| REQ-005: local and verified-owner access, pause/consent | Access checks at Recordings browse start/end, playback and media actions; Display is independent under DEC-009; MCP content withholding | Owner-denial, symlink escape, expiring browse lease, Display recovery/action, MCP secret-canary and native cache/disconnection tests; existing archive/policy/consent tests |
| REQ-006: gap reasons and non-selectability | Existing durable events mapped to specified labels; gaps split sessions and have no media/actions | Gap-label regression and existing historical-pause fixture; native gap non-selectability |
| REQ-007: 30-day speech transcripts | Owner listening and call-ASR hooks; separate private SQLite table; secure expiry independent of media | Exact expiry boundary; successful ASR retained despite inference failure; local voice capture test |
| REQ-008: local navigation by time | Listening/private/panel route interception before agent dispatch; local navigation state and panel opening | Actual listening path leaves agent queue empty; time/date parsing and denied navigation tests; native day/time navigation |

## Implementation details settled by the existing decisions

Marking takes effect from the explicit mark; it does not recover previously deleted originals or enable paused/disabled sources. The open camera prefix closes at the mark, and marked retention segments also close when strict retention permission changes. Ordinary unmarked archive clips retain their existing behavior. Call detection uses the existing content-free microphone-app detector independently of manual quiet/availability. Calls end after the detector’s existing release delay; marks do not survive a new call or service restart.

All transcript expiry and marked-media expiry are measured from capture start; preserve never extends them. Ordinary manually preserved originals and event frames retain their existing independent policy. Deleting the selected media removes its raw/preserved copies and playback derivative while leaving observations, facts, other media and transcripts intact. Generated images already have durable local storage; the existing visual-delete control deletes a generated artifact without deleting an imported owner original.

DEC-008/Q-007 supersedes the earlier in-memory/hard-close deletion behavior: Display and written conversation history persist locally until explicit Clear, across pause and restart. DEC-009 later removes verification from Display reads, selection and local controls; retained material stays visible through identity/observation gaps. Recordings clears cached text, paths and playback on denied access or service disconnection. Older MCP observation tools retain coverage/status metadata but withhold observation content.

## Validation status

- 16 focused Recordings regressions pass, including actual FFmpeg playback conversion.
- 52 archive/camera/Recordings regressions pass after fixing the unmarked clip regression.
- 264 native UI checks pass, covering the original flow, first-poll cache revocation and side-effect-free frame persistence; synthetic Recordings render inspected.
- Core: 402 tests passed; system1, bridge and private workspace suites passed in the first paired run.
- Full ambient suite: **872 tests passed** on final 0.10.2 source. The paired exact-source gate runs ambient verification, 402 core tests, system1, bridge, private workspace and release builds. `release-verification.json` records each exact command, exit, artifact hash and whether sources stayed unchanged; these fields must pass before deployment. Native CI also requires all 264 UI checks to pass. F-006 clears older observation/gallery caches on the first failed poll; F-007 supports older SDKs; F-008 fixes the small-display resize loop.
- Synthetic 14,110-observation benchmark: 0.123 seconds for the session list, 1,263-byte list response and 100-row child page of 32,276 bytes (all children available through paging).

## Delivery evidence

- Branch: `feat/butler-recordings`, created from fetched `origin/main` at `2d8aea6` after the owner-authorized main migration.
- Pull requests: https://github.com/t-boris/iButler/pull/1 (feature and existing work), https://github.com/t-boris/iButler/pull/2 (installer repair), https://github.com/t-boris/iButler/pull/3 (media days/index) and https://github.com/t-boris/iButler/pull/4 (containment-root performance). All target main and include test plans.
- Ambient version: **0.11.1**; core version: **0.17.1**. Wheel/app versions derive from package `__version__`.
- Local full verification: `implementation/ambient-awareness/evidence/attention-implementation/release-verification.json` and referenced exact-command logs.
- Existing Back Burner/workspace deliverables included under DEC-007 were hash-compared with installed copies: backburner library/CLI/prompt/docs, knowledge_sources, memory and lessons all match.
- Native evidence: `implementation/ambient-awareness/evidence/ui-preview/native-panel-layout.json` and `butler-recordings.png` (synthetic only).
- Deployment evidence: existing `release_attention.py` writes `implementation/ambient-awareness/evidence/attention-implementation/deployment.json`, checking installed/running/bundle versions and UI binary hashes against exact verified artifacts. No manual owner checklist is required by this verification record.

## Final delivery repairs

F-009 handles this Mac’s absent optional Homebrew model-service plist. F-010 includes pending-media-only days in the date picker and is covered by a regression that opens the file through the selected day/session. These repairs shipped in 0.10.1. The exact-source release report and final PR/main CI runs provide the current validation results.

F-011 adds an observation/media index for pending-original browsing. A deterministic full-browser backlog regression exceeded its database-work budget before the index and passes after it. This supplements the smaller synthetic timeline benchmark above.

F-012 resolves containment roots once per request while retaining each file's canonical path validation and both access checks. The 0.10.1 exact-source gate passed all suites (872 ambient tests, 402 core tests, 264 native checks); main CI run 40635608628 passed both jobs. The same 45,000-file synthetic browse fell from 4.07 to 1.996 seconds. Final 0.10.2 uses the same exact-source gate and CI before normal deployment. The final machine-readable delivery receipt, `implementation/ambient-awareness/evidence/recordings/completion.json`, ties the merged PRs and green main CI to the clean main checkout, matching source fingerprints and verified installed/running release. Its referenced release and deployment reports are the authoritative final outcomes.

## Owner follow-up verification

DEC-008 records inclusion of the newer Display/history changes, the Components screenshot repair and the Back Burner execution request. Q-007 retains owner visibility; Q-008 authorizes two daytime catch-up runs. REQ-009–REQ-011 and I-7–I-9 track this scope. Focused management/settings suites pass (31 + 13 tests). Native and full release checks remain required for 0.11.0. Versioned nightly activation changes only `scenarios.backburner.enabled` (12 → 14); evidence and tracked operation receipts are in `evidence/followup/`.

Follow-up focused results: 272 packaged native checks, 31 management tests, 13 settings tests, 35 core Back Burner tests, 7 persistent-history tests and 16 original Recordings tests pass. Actual catch-up receipts are `applied`, not inferred from scheduling. Final source-fingerprint, CI, deployment and clean-tree outcomes are recorded in `evidence/followup/completion.json` and its referenced release reports.

F-015 updates the cross-package Back Burner card validator to construct persistent Display with a real temporary Store. The focused validator passes; the full installed-environment gate is repeated. The initial 12 sensing skips were also rerun against the existing cached models, all passing, and the cache is present for the final full run.

## Latest owner decision: Display without verification

DEC-009 supersedes the earlier Display gate, including Q-007. Display reads, history selection, local actions and Clear no longer use camera/voice verification or recording permission. Persistent history remains until explicit Clear, and the native panel retains rendered material through failed polls. Recordings keeps its independent checks. Focused regressions cover denied gates, no gate calls during materialization, database reopen and local controls. Final 0.11.1 release evidence is under `evidence/display-open/` and the normal release reports.

## Current 0.11.2 verification

Q-001–Q-008, DEC-001–DEC-010, F-001–F-017, discussion and all requirements were checked. DEC-010 records the owner's authorization to include the previously uncommitted Display video fix. REQ-012 covers it. The 16 focused Recordings regressions passed. The native synthetic-video test passed at 60 and 120 seconds and confirmed player identity and retirement. The full ambient verification passed all 21 jobs, including 884 ambient tests, native builds, UI checks, other project suites and the wheel build. The paired release verification, CI, merge and deployment remain in progress.

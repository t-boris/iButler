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
| REQ-005: local and verified-owner access, pause/consent | Access checks at browse start/end, playback and media/Display actions; UI cache clearing; MCP content withholding | Owner-denial, symlink escape, expiring browse lease, Display recovery/action, MCP secret-canary and native cache/disconnection tests; existing archive/policy/consent tests |
| REQ-006: gap reasons and non-selectability | Existing durable events mapped to specified labels; gaps split sessions and have no media/actions | Gap-label regression and existing historical-pause fixture; native gap non-selectability |
| REQ-007: 30-day speech transcripts | Owner listening and call-ASR hooks; separate private SQLite table; secure expiry independent of media | Exact expiry boundary; successful ASR retained despite inference failure; local voice capture test |
| REQ-008: local navigation by time | Listening/private/panel route interception before agent dispatch; local navigation state and panel opening | Actual listening path leaves agent queue empty; time/date parsing and denied navigation tests; native day/time navigation |

## Implementation details settled by the existing decisions

Marking takes effect from the explicit mark; it does not recover previously deleted originals or enable paused/disabled sources. The open camera prefix closes at the mark, and marked retention segments also close when strict retention permission changes. Ordinary unmarked archive clips retain their existing behavior. Call detection uses the existing content-free microphone-app detector independently of manual quiet/availability. Calls end after the detector’s existing release delay; marks do not survive a new call or service restart.

All transcript expiry and marked-media expiry are measured from capture start; preserve never extends them. Ordinary manually preserved originals and event frames retain their existing independent policy. Deleting the selected media removes its raw/preserved copies and playback derivative while leaving observations, facts, other media and transcripts intact. Generated images already have durable local storage; the existing visual-delete control deletes a generated artifact without deleting an imported owner original.

Display history remains in memory and keeps the existing hard-close clearing behavior. Loss of verification hides it immediately at the next local state response and blocks actions; re-verification can restore the still-live history. Recordings clears cached text, paths and playback on denied access or service disconnection. Older MCP observation tools retain coverage/status metadata but withhold observation content.

## Validation status

- 16 focused Recordings regressions pass, including actual FFmpeg playback conversion.
- 52 archive/camera/Recordings regressions pass after fixing the unmarked clip regression.
- 264 native UI checks pass, covering the original flow, first-poll cache revocation and side-effect-free frame persistence; synthetic Recordings render inspected.
- Core: 402 tests passed; system1, bridge and private workspace suites passed in the first paired run.
- Full ambient run: 867 tests passed. A later association fix ensures retained call ASR is shown with the still-pending original even when fact analysis failed; all 14 focused regressions pass on that revision. The prior exact-source paired release gate **passed**: ambient verification, 402 core tests, system1, bridge, private workspace, and release builds all exit 0; source fingerprints were unchanged. F-006 clears the older observation/gallery caches on the first failed state poll; F-007 fixes older-SDK compilation; F-008 fixes the small-display resize loop found by the CI debugger. The same exact-source gate is required for the final native revision. Exact commands, exits and artifact hashes are in `release-verification.json`. Final GitHub results are attached to PR #1; deployment is allowed only after CI passes as well.
- Synthetic 14,110-observation benchmark: 0.123 seconds for the session list, 1,263-byte list response and 100-row child page of 32,276 bytes (all children available through paging).

## Delivery evidence

- Branch: `feat/butler-recordings`, created from fetched `origin/main` at `2d8aea6` after the owner-authorized main migration.
- Pull request: https://github.com/t-boris/iButler/pull/1
- Ambient version: **0.10.1**; core version: **0.17.1**. Wheel/app versions derive from package `__version__`.
- Local full verification: `implementation/ambient-awareness/evidence/attention-implementation/release-verification.json` and referenced exact-command logs.
- Existing Back Burner/workspace deliverables included under DEC-007 were hash-compared with installed copies: backburner library/CLI/prompt/docs, knowledge_sources, memory and lessons all match.
- Native evidence: `implementation/ambient-awareness/evidence/ui-preview/native-panel-layout.json` and `butler-recordings.png` (synthetic only).
- Deployment evidence: existing `release_attention.py` writes `implementation/ambient-awareness/evidence/attention-implementation/deployment.json`, checking installed/running/bundle versions and UI binary hashes against exact verified artifacts. No manual owner checklist is required by this verification record.

## Final delivery repairs

F-009 handles this Mac’s absent optional Homebrew model-service plist. F-010 includes pending-media-only days in the date picker and is covered by a regression that opens the file through the selected day/session. The final patch version is 0.10.1. The exact-source release report and final PR/main CI runs provide the current validation results.

F-011 adds an observation/media index for pending-original browsing. A deterministic full-browser backlog regression exceeded its database-work budget before the index and passes after it. This supplements the smaller synthetic timeline benchmark above.

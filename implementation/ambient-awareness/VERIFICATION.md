# Current verification — 440 tests; public hearing repair installed

## 0.12.2 — narrative summaries and grounded advice (2026-10-30)

Daily summaries now use a bounded, time-distributed sample of inferred observation facts and a local model, rather than a duration table with internal identifiers. Manual and automatic advice use local model output grounded in dated facts from up to 30 days. The model is instructed to report uncertainty and insufficient evidence; disabled topics, cancellation, meeting quiet, autonomous quotas and quiet hours are retained. Automatic generation runs on a background thread and failed or unproductive attempts are throttled. These outputs are model-generated and cannot guarantee that every suggestion is useful or correct. Transcripts and raw media are not inputs to these reports.

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m unittest discover -s tests` passed 975 tests; a subsequent cancellation regression also passed independently. The installed wheel passed 149 focused tests (`test_daily_report test_archive test_management test_feature test_display`); the source focused run including `test_general_dialogue` passed 180 before the extra cancellation check. A synthetic local-model smoke test produced a narrative and a fact-based suggestion. The changed wheel modules match source, the versioned app signature verifies, and installed service and app report 0.12.2. Both built-in schedules report enabled and Telegram disabled. No live summary, live advice or Telegram delivery was triggered for verification. The service restart also restarted the panel, after checking that no video card was selected. Backup: `~/.local/share/butler-ambient/release-backups/narrative-schedules-20261030T120319Z`.

## 0.12.1 — manual Display schedules (2026-10-30)

Manual daily and advice runs use the permission-gated local Display, including during quiet hours and independently of autonomous delivery quotas. Advice uses eligible observations from the last 30 days and labels their time; disabled topics remain excluded. Optional Telegram still requires the owner gate and is suppressed during quiet hours. Automatic delivery is unchanged. Manual failures are visible in schedule status.

The source suite `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m unittest discover -s tests` passed 969 tests before two additional opt-in and single-user regression checks were added. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests .venv/bin/python -m unittest test_management test_feature test_display` passed 115 tests, as did the same three modules against the installed wheel. The new daily and advice behavior tests failed against 0.12.0 and pass against 0.12.1. The wheel was built in `/tmp/butler-manual-0121`, its changed Python files match source, and the versioned app signature verifies. Service and app report 0.12.1; the two schedules report enabled with Telegram disabled. No actual manual delivery or Telegram message was triggered for testing. Backup: `~/.local/share/butler-ambient/release-backups/manual-schedules-20261030T115005Z`. Restarting the service also restarted its panel.

All 369 feature +71 project regression tests, 42 native UI checks and21verification jobs pass: [results](evidence/results.json). Exact source/wheel/native code installed; stable permission parent/configuration/agreements/preferences unchanged: [deployment](evidence/streaming-deployment.json), [integrity](evidence/current-runtime-verification.json). Native UI binary is identical and was left running: [UI refresh](evidence/public-hearing-ui-refresh.json).107 installed voice/control tests pass: [log](evidence/installed-public-hearing-tests.log).

DEC-060/061 repair two public-status failures: a short pause between the assistant vocative and the exact hearing question, and refusal while older private work owns the output. A volatile4s capture-bound alias can join only the next exact hearing question; expiry, input gaps, unrelated/reporting/quoted/extended text and full Pause cannot authorize anything. Older private generation/audio is cancelled with context/pending review preserved; output handoff waits≤0.75s without holding the control lock, rechecking Pause/freshness. The reply is the existing fixed nameless public acknowledgement, with no personal knowledge, private job or owner authority.

Actual Whisper adds outer quotes to known unquoted synthetic speech. The candidate literal guard failed before installation; DEC-061 canonicalizes a single outer ASR frame only, preserving words and inline/nested/reporting content. [Framing evidence](evidence/synthetic-whisper-framing.json), [retained failed crops](evidence/split-hearing-first-crops.json). Real **installed** local resident Whisper now recognizes the0.7s split fixture and the isolated listener selects exactly one public route with no authority/private job/no sound played: [installed offline evidence](evidence/split-hearing-installed-offline.json). This does not test an intended live owner or real audible output. BUG-009 resolved at this controlled routing/formatting scope.

[Current runtime metadata](evidence/current-background-live.json) proves10/10fresh mic/camera and all4modelsready. The UI was foreground for this window; no new background claim is inferred. [Prior background proof](evidence/pre-public-hearing-current-background-live.json) remains scoped to the earlier431-test build; native UI/capture code are unchanged. Storage scope remains verified by the prior lifecycle evidence and current full regressions; the historical storage section below retains its timestamps/counts.

**The full goal/F-004 remain incomplete.** Intended owner dialogue/semantic correctness/audibility, real end-to-end latency, voice-only identity, complete real spoken desktop/MCP control and genuine call classification remain open: [status](evidence/current-completion-status.json), [voice metadata only](evidence/current-voice-state.json). Q013/Q014/Q015/Q016 have no new owner answer; ordinary private reply gating is unchanged. No model/reference/threshold/device/volume/version/shared-gateway change, production playback from recorded fixtures, MarkView browser check or manual owner checklist. Git/main/commits/GitHub/PR/CI waived by DEC005; no merge. Earlier evidence below is historical.

---

# Historical verification — 431 tests; storage scope verified

All 360 feature +71 project regression tests, 42 native UI checks and 21 verification jobs pass: [results](evidence/results.json). The exact source/wheel/native/UI/icon are installed, with stable permission parent/configuration/agreements/preferences unchanged: [deployment](evidence/streaming-deployment.json), [integrity](evidence/current-runtime-verification.json). The installed interpreter passes 98 focused tests: [log](evidence/installed-camera-storage-tests.log).

DEC-056–059 implement a 512MiB temporary analysis spool, atomic facts/provenance before raw cleanup, incremental legacy cleanup, continuous≤10 s camera clips with≤3 timestamped representatives, constrained current-only perception/name association, fair current/recent-tail scheduling, quota recovery headroom and a fresh volatile native preview independent of disk admission. [107 feature records/26-task metadata validation](evidence/spec-metadata-validation.json) passes. REQ003/004/024/025 and I4/I6/I25/I26 are verified in this controlled/installed scope.

[Actual 181 s scene-changing capture](evidence/camera-clips-installed-live.json):181/181 fresh microphone/camera,37/37 fresh private previews,zero capture/analysis/participant errors,spool≤512MiB. Analysis is asynchronous: at window end 2 camera and 21 system-audio inputs older 90 s are still pending; no 90 s completion guarantee is claimed. A [later lifecycle audit](evidence/storage-lifecycle-final.json) at +213 s proves all 380 closed inputs wholly inside that window analyzed/reclaimed (18 camera, 180 microphone, 3 screenshots, 179 system-audio), while continuous ingestion continues and spool falls504,682,131→491,477,893bytes. Existing 1,920,700,558bytes/15,367 legacy originals remain pending under their24h expiry; they are not discarded to meet the new quota. Derived metadata survives raw cleanup, selected copies remain, and unapproved automatic image selection stays off pendingQ015.

[Background evidence](evidence/current-background-live.json):10/10 fresh microphone/camera while UI notforeground,all 4 models ready,no global audio/device/volume change. Both actual local models pass the synthetic blank-plus-fictional-prior counterexample: [grounding](evidence/vision-prior-grounding.json); BUG008 resolved. Earlier saturated/negative results are retained in camera-clips-saturated-before-hysteresis.json, camera-clips-recovery-live.json and the before regression logs.

**The full goal/F004 remain incomplete.** Intended owner dialogue/semantic correctness/audibility, real end-to-end latency, voice-only identity, complete real spoken desktop/MCP control and genuine call classification remain unverified: [current status](evidence/current-completion-status.json), [metadata-only voice audit](evidence/current-voice-state.json). Q013/Q014/Q015/Q016 have no new owner answer. No model/reference/threshold/device/version/shared-gateway change, MarkView browser check or manual owner checklist. Git/main/commits/GitHub/PR/CI are waived by DEC005; no merge performed. All earlier verification below is historical.

---

# Historical verification — 367 tests

All296feature+71regression tests,38native UI checks,21jobs pass: [results](evidence/results.json). Exact source/native/UI installed, stable permission parent/configuration/agreements/preferences unchanged: [deployment](evidence/streaming-deployment.json), [integrity](evidence/current-runtime-verification.json).

DEC052 separates continuous passive capture/hearing from bounded participant checks. Private output waits for exact full-PCM receipts and current permission; stale/skipped/unknown data cannot borrow the former owner lease. Capture-time freshness bounds slow inference. Installed11pipeline+15listener tests pass: [pipeline log](evidence/installed-capture-pipeline-tests.log), [hearing log](evidence/installed-listening-tests.log). Pre-fix failures retained in [baseline](evidence/capture-pipeline-before.log), [first full run](evidence/capture-pipeline-first-full-results.json), [edge cases](evidence/utterance-edges-before.log).

[Actual installed capture](evidence/capture-pipeline-installed-live.json):10/10fresh camera/mic,20segments each camera/mic/system-audio in20seconds,zero check errors/skips,max check0.168s; all4modelsready and4sourcesactive. [Background proof](evidence/capture-pipeline-background-live.json):5/5fresh camera/mic while own UI notforeground. These establish capture/scheduling, not intended owner recognition or an audible response.

**Full goal/F004 remain incomplete.** Fresh owner question/follow-up/reply, actual end-of-speech latency, voice-only identity, subjective audibility, complete real spoken desktop/MCP control and genuine call classification remain open. Q013/Q014 have no new answer. No thresholds/references/devices/global volume/version/shared gateway changed. Older evidence below retains its original scope.

---

# Historical verification — 354 tests

All283feature+71project regression tests,38native UI checks and21jobs pass: [results](evidence/results.json). Exact source/wheel/native artifacts are installed, stable permission parent/configuration/agreements/preferences unchanged: [deployment](evidence/streaming-deployment.json), [current integrity](evidence/current-runtime-verification.json). One updated own native UI is running.

DEC051 removes repeated4096→8192context reloads. Same synthetic command: old21.728s/12.516s, corrected4.069s/4.055s in [source comparison](evidence/command-context-before-after.json); installed corrected3.214s/2.549s in [installed evidence](evidence/installed-inference-live.json). No operation executed. These are planning-only measurements, excluding ASR/TTS/native playback; they do not establish the≤3s end-of-speech target or owner success. Six installed diagnostic tests verify finite numeric/controlled metadata without raw prompt/reply/reasoning.

Installed runtime has fresh microphone/camera10/10, four models ready, four archive sources active, no storage error, no VPIO/ducking, new inference metadata available. Actual/default input/output lastJabra; earlier AirPods mismatch is historical. No new owner answer toQ013/Q014. **Full goal/F004 remain incomplete:** real owner recognition/reply, audibility, end-of-speech latency/follow-ups, voice-only identity, complete actual voice/desktop/MCP coverage and genuine call classification remain open. Older evidence below retains its original scope.

---

# Historical verification — 352 tests

All281feature+71project regression tests,38native UI checks and21jobs pass: [results](evidence/results.json). Exact source/wheel/native artifacts are installed with unchanged stable permission parent/configuration/agreements/preferences: [deployment](evidence/streaming-deployment.json), [current integrity](evidence/current-runtime-verification.json).

DEC047–050 add13native observation/image voice adapters (31registry entries), atomic preservation error cleanup, reliable in-flight dispatcher JSON reads and correct installed-worker import order. Installed-code checks pass14storage/image +8real harmless worker +5knowledge tests. A real readonly shared Butler lookup now returns ready/6hits in2.701s: [metadata](evidence/installed-knowledge-after-import-fix.json). No owner content exported or unrequested provider/project task run. Earlier failures remain in [source failure](evidence/pre-dispatch-snapshot-fix-results.json), [installed worker failure](evidence/installed-work-tasks-before-import-fix.log), [installed memory failure](evidence/installed-knowledge-before-import-fix.json).

Fresh microphone/camera/listener10/10 while Butler is not foreground, four models ready, four archive sources active, no storage error, no VPIO/ducking: [installed live evidence](evidence/voice-controls-background-live.json). Butler input remains Jabra; system input/output are AirPods Pro. Q013/Q014 are still unanswered. **Full goal and F004 remain incomplete:** actual owner recognition/reply, audibility, intended-owner latency, voice-only identity, complete conversational/tool control and genuine call classification are not established by these tests.

Older verification below is historical and retains its original scope.

---

# Historical verification —333tests

Latest exact source/build/runtime:262feature+71project regressions,38nativeUIchecks,21jobs all exit0 in [results.json](evidence/results.json). [Current runtime integrity](evidence/current-runtime-verification.json) confirms installed source/UI and unchanged stable permission parent/config/agreements/preferences. Fork-free numeric/concurrent subprocess tests pass; actual post-warm provider refresh remains responsive ([evidence](evidence/fork-free-post-warm-refresh-live.json)).

Background hearing is independently proven: native foreground metadata available, Butler UI notforeground, fresh microphone/camera/listener5/5samples ([evidence](evidence/background-hearing-live.json)). This does not prove intended owner speech is heard. Current selected Butler inputJabra differs from system input/outputAirPodsPro; actual test fixture played into AirPods was not recognized in Jabra ([negative acoustic result](evidence/fixed-hearing-acoustic-live.json)). Q014 asks the source preference; Q013 identity-policy question remains open. Latest owner again reports no reply; F004/full goal remain open.

Older verification below is historical and retains only its original scope.

---

# Historical verification — 324 tests

Latest exact build:253feature tests +71project regressions,38native UI checks,21jobs all exit0 in [results.json](evidence/results.json). Installed source/native UI/wheel and unchanged stable permission parent/config/references/preferences in [streaming-deployment.json](evidence/streaming-deployment.json).

DEC-041/042 continuous hearing correction: [continuous-hearing-verification.json](evidence/continuous-hearing-verification.json), actual30/30fresh microphone and enabled listener,15recognized phrases,25snapshots showing local text without dialogue identity. This proves continuous hearing survives identity transitions; it does not prove an intended owner question receives a correct audible reply. Q-013 asks the unresolved ordinary-versus-privileged reply policy; F-004/full goal remain open. Historical results below retain their original scope and do not supersede this negative live limitation.

---

# Acceptance verification — Butler Vision and Ambient Awareness

## Current result · 2026-10-23

**312tests pass (241feature+71project regressions),38nativeUIchecks and all21verification jobs exit0.** Exact commands, logs,74source hashes and artifact hashes: [results.json](evidence/results.json). This includes seven native builds, actual AppKit Start mouse-event regression, UI layout/lifecycle, audio/AEC/format checks, wheel and dependency readiness. [streaming-deployment.json](evidence/streaming-deployment.json) proves the verified own-runtime/UI/helpers refresh with stable sensing permission parent, config, enrolled agreements and preferences preserved. [final-runtime-live.json](evidence/final-runtime-live.json) additionally checks installed native hashes, selected baritone and actual device/model state.

The whole goal remains **incomplete**. Actual normal conversational accuracy/reliability, new-model end-of-speech latency, installed voice-only identification, full voice-control reliability, background-audio audibility and genuine automatic muted-call classification remain at the open scopes in F-004/REQ010/013/017/018/021/023. Historical2.350s controlled latency is not the new model’s current end-to-end timing. No fresh successful live conversational reply occurred in the final30s sample; two unnamed utterances were cancelled. The input could include media; do not infer owner intent from counters.

Current requirements001/002/003/004/007/018/019 explicitly apply DEC-035/038. The original participant-paused recording matrix below is historical. The new passport for current behavior is the requirement's Current verification section and these executable modules/evidence:

| Scope | Code/test evidence | Actual local evidence / limit |
| --- | --- | --- |
| Native Start/owner renewal | native/owner-auth.swift; tests/test_owner_setup.py; native-owner-auth-events-test.log | [owner-start-event-regression.json](evidence/owner-start-event-regression.json): old bare-loop fails, fixed AppKit passes; [owner-voice-renewal-live.json](evidence/owner-voice-renewal-live.json): actual saved,15face/30voice refs, buffer empty |
| Russian ASR | tests/test_conversation.py; tests/test_voice_route.py | [owner-report-recorded-asr.json](evidence/owner-report-recorded-asr.json): actual historical microphone report recognized exactly6/6words without hints; not a fresh dialogue reply |
| Name-free controls/streaming | voice_commands.py; tests/test_control_model.py; tests/test_streaming.py | Default local qwen3.5:9b, think:false; separate recipient/typed planner, same0.90address threshold and strict owner-bound confirmation. Synthetic fixtures do not prove live usability |
| Unknown encounters/guest registration | tests/test_participants.py; tests/test_owner_setup.py; tests/test_voice_commands.py | Volatile120s fingerprints/fixed public speech; deliberate guest face+voice registration verified with controlled inputs, no real guest enrollment claimed |
| Shared knowledge/tools and CLI work | tests/test_knowledge.py; tests/test_work_tasks.py | Actual bounded6memory hits,37gatewaytools/12groups,164projects/10routes; actual dispatcher monitor tested with harmless worker, no unrequested provider job/owner-project change |
| Passive archive/analysis | tests/test_archive.py; archive.py; service.py | [continuous-analysis-final-live.json](evidence/continuous-analysis-final-live.json):90.96s,10NEW observations across4sources,0analysiserror samples; [archive-format-live.json](evidence/archive-format-live.json): configured compact640x360video; not24hour endurance |
| Meeting state | tests/test_meeting.py; native/audio.swift | BUG-004 exact system-capture broker exclusion; actual input_apps=[],2own/1system sources ignored,30/30auto available/0quiet. Actual soft-muted/end-call browser cases remain unverified |

Q-011 and Q-012 are answered in the specification, including Q-012's actual completion and owner-reported button failure. DEC-040 records the model/control engineering correction; BUG-002/003/004 have evidence-backed resolutions. Git/main/branch/commits/issues/PR/CI are waived by DEC-005. No merge, version bump, shared gateway restart, global volume change, MarkView browser check or manual owner checklist.

## Historical result and scope

Implementation verified on 2026-10-23: **239 tests passed** (168 feature, 40 news-video, 19 Bamberg, 6 WhatsApp Python and 6 WhatsApp Node), plus 38 native UI lifecycle/selection checks, four native builds, Butler.app packaging, the native AEC/raw-input test, wheel build and doctor. No test was skipped in the full verification run. Exact commands, exit codes, source hashes and artifact hashes are in [results.json](evidence/results.json). Test output is in [feature-tests.log](evidence/feature-tests.log), [news-video-tests.log](evidence/news-video-tests.log), [bamberg-tests.log](evidence/bamberg-tests.log), [whatsapp-python-tests.log](evidence/whatsapp-python-tests.log) and [whatsapp-node-tests.log](evidence/whatsapp-node-tests.log). Builds: [native-build.log](evidence/native-build.log), [wheel-build.log](evidence/wheel-build.log).

The original 67 acceptance criteria and the 25 interaction-extension criteria are mapped below to implementation and executable tests. This is implementation verification using controlled fixtures and synthetic inputs, not a claim that live recognition accuracy, real-device macOS permissions, vision-model throughput or Telegram delivery were tested. Deployment was initially excluded, then explicitly authorized by DEC-007. The [dependency preflight](evidence/doctor.log) now confirms installed local models and dependencies; live deployment evidence and limits are separate below. Owner manual testing remains excluded. The service stays disabled by default and fails closed if local sensing or analysis is unavailable.

## Binding records

Read the entire original feature folder before implementation: Q-001–Q-007 and their answers, DEC-001–DEC-004, F-001/F-002 and their Resolution sections, discussion.md, REQ-001–REQ-008, overview.md, implementation/plan.md and SRC-001. No recorded question was asked again.

The new answers are recorded in DEC-005 (owner: “Skip the github”) and Q-008/DEC-006 (owner: “Use this policy (recommended)”). DEC-005 waives the unavailable Git/GitHub workflow in this non-Git workspace. DEC-006 settles one-second participant checks and three consecutive fresh checks against enrolled local references with active prior agreement. Discussion and traceability links are updated. Existing accepted decisions and resolved findings remain accepted/resolved. Deployment authorization is recorded in DEC-007, live enrollment authorization in DEC-008, and engineering model/sampling settings in DEC-009. The owner’s confirmation of live-check readiness is recorded in discussion.md. Q-009 is answered in DEC-010 (owner: «Да, дополнить эталоны сейчас»). Additional guided setup succeeded with 15 face and 18 voice templates; no reference media remain.

## Criteria matrix

Criterion numbers refer to the order in each requirement's Acceptance Criteria section. Test references expand in the catalog below. A check means the implementation contract passed controlled automated verification; physical operating limits remain explicit above and in README.md.

### REQ-001

[Requirement](../../docs/features/butler-vision-and-ambient-awareness/requirements/REQ-001.md) · Code: [service.py](butler_ambient/service.py), [mcp.py](butler_ambient/mcp.py), [policy.py](butler_ambient/policy.py)

| Criterion | Required behavior | Automated evidence |
| --- | --- | --- |
| ✓ AC1 | Outside permission-related recording pauses, Butler can request a fresh screenshot and a fresh camera observation through MCP capabilities. | [S7](#test-s7), [S16](#test-s16), [S10](#test-s10) |
| ✓ AC2 | An on-demand result identifies its source and capture time. | [S7](#test-s7), [B1](#test-b1) |
| ✓ AC3 | An unavailable source is reported as unavailable rather than represented by an old observation as if it were current. | [S17](#test-s17), [S3](#test-s3), [P5](#test-p5) |
| ✓ AC4 | During a permission-related recording pause, on-demand MCP access cannot initiate recording, general analysis, external transmission, or retention of frames, audio, or derived information about people. | [S1](#test-s1), [S5](#test-s5), [S16](#test-s16) |
| ✓ AC5 | During a permission-related recording pause, only transient local processing of camera and microphone input exclusively to determine who remains present is permitted; on-demand MCP access cannot bypass this restriction. | [S1](#test-s1), [S3](#test-s3), [S10](#test-s10) |
| ✓ AC6 | Recording automatically resumes only when the participant set is established as permitted; uncertain authorization keeps recording paused. | [P1](#test-p1), [P5](#test-p5) |
| ✓ AC7 | An inferred name match alone does not authorize recording to resume. | [P2](#test-p2), [P4](#test-p4) |
| ✓ AC8 | Permission-related recording pauses and resulting observation gaps remain visible to the owner. | [P1](#test-p1), [S1](#test-s1), [B3](#test-b3) |

### REQ-002

[Requirement](../../docs/features/butler-vision-and-ambient-awareness/requirements/REQ-002.md) · Code: [sensing.py](butler_ambient/sensing.py), [service.py](butler_ambient/service.py), [policy.py](butler_ambient/policy.py)

| Criterion | Required behavior | Automated evidence |
| --- | --- | --- |
| ✓ AC1 | While observation is enabled and participant permissions allow capture, camera video and ambient audio are recorded continuously. | [S2](#test-s2), [T1](#test-t1) |
| ✓ AC2 | While observation is enabled and participant permissions allow capture, automatic analysis runs once per minute and screen capture runs once per minute in addition to on-demand capture. | [S2](#test-s2) |
| ✓ AC3 | The owner can pause and resume collection and inspect each source's active, paused or unavailable state. | [P3](#test-p3), [S4](#test-s4), [S17](#test-s17) |
| ✓ AC4 | During permission-related recording pauses, only transient local processing of camera and microphone input is allowed, exclusively to determine who remains present. | [S1](#test-s1), [T3](#test-t3), [T4](#test-t4) |
| ✓ AC5 | During permission-related recording pauses, recording, screen capture, general analysis and external transmission are prohibited. | [S1](#test-s1), [S16](#test-s16), [S9](#test-s9) |
| ✓ AC6 | During permission-related recording pauses, frames, audio and derived information about people are not retained. | [S1](#test-s1) |
| ✓ AC7 | The same permission-pause restrictions apply to on-demand MCP access. | [S3](#test-s3), [S10](#test-s10) |
| ✓ AC8 | Recording resumes automatically after a permission-related pause only when the participant set is established as permitted and observation remains enabled. | [P1](#test-p1), [P3](#test-p3), [S4](#test-s4) |
| ✓ AC9 | Uncertain authorization keeps recording paused; an inferred name match alone does not authorize resumption. | [P2](#test-p2), [P5](#test-p5), [T5](#test-t5), [T6](#test-t6) |
| ✓ AC10 | Permission-related recording pauses and resulting observation gaps remain visible to the owner. | [P1](#test-p1), [S13](#test-s13) |
| ✓ AC11 | Unavailable sources, capture gaps and analysis failures are recorded explicitly; stale observations are not presented as current. | [S17](#test-s17), [S6](#test-s6), [S14](#test-s14) |
| ✓ AC12 | Retention expiration continues independently of analysis success. | [R1](#test-r1), [S6](#test-s6), [S9](#test-s9) |

### REQ-003

[Requirement](../../docs/features/butler-vision-and-ambient-awareness/requirements/REQ-003.md) · Code: [store.py](butler_ambient/store.py), [service.py](butler_ambient/service.py)

| Criterion | Required behavior | Automated evidence |
| --- | --- | --- |
| ✓ AC1 | Unpreserved photos, video, audio and screenshots expire 24 hours after capture, including media awaiting or failing analysis. | [R1](#test-r1), [S6](#test-s6) |
| ✓ AC2 | The owner can select available media for separate preservation until owner deletion; automatic long-term preservation is disabled. | [R2](#test-r2), [R3](#test-r3) |
| ✓ AC3 | Derived results remain until owner deletion independently of source-media expiration, except that derived information about people from restricted participant sensing during permission-related recording pauses must not be retained. | [R4](#test-r4), [S1](#test-s1) |
| ✓ AC4 | Retained results preserve source identifiers and observation times and indicate when original media is no longer available. | [R4](#test-r4), [R7](#test-r7) |
| ✓ AC5 | During permission-related recording pauses, transient local camera and microphone processing exclusively to determine who remains present must not retain frames, audio, or derived information about people, including through on-demand MCP access or owner-selected media preservation. | [S1](#test-s1), [S3](#test-s3), [S16](#test-s16) |

### REQ-004

[Requirement](../../docs/features/butler-vision-and-ambient-awareness/requirements/REQ-004.md) · Code: [analysis.py](butler_ambient/analysis.py), [store.py](butler_ambient/store.py), [service.py](butler_ambient/service.py)

| Criterion | Required behavior | Automated evidence |
| --- | --- | --- |
| ✓ AC1 | Outside permission-related recording pauses, record supported appearances, people, the dog, identifiable books and activities with source references and observation times. | [B10](#test-b10), [S15](#test-s15), [R7](#test-r7) |
| ✓ AC2 | Automatically associate appearances with names from existing context without requiring initial owner confirmation; mark these associations as model inferences. | [S15](#test-s15), [B1](#test-b1) |
| ✓ AC3 | Keep ambiguous identities, unreadable titles and inferred activities uncertain rather than inventing missing information. | [B2](#test-b2), [B10](#test-b10) |
| ✓ AC4 | Allow the owner to correct identity associations and apply corrections when retrieving or summarizing affected history. | [R5](#test-r5) |
| ✓ AC5 | Keep observations, model inferences and owner-confirmed facts distinguishable; do not automatically promote inferred associations or personal claims to canonical facts. | [R5](#test-r5), [S15](#test-s15), [B1](#test-b1) |
| ✓ AC6 | An inferred identity match does not count as evidence of participant agreement and must not, by itself, authorize recording resumption. | [P2](#test-p2), [P4](#test-p4) |
| ✓ AC7 | During permission-related recording pauses, allow only transient local processing of camera and microphone input exclusively to determine who remains present. | [S1](#test-s1), [T3](#test-t3), [T4](#test-t4) |
| ✓ AC8 | During permission-related recording pauses, prohibit recording, general analysis, external transmission, and retention of frames, audio, or derived information about people, including identity associations and personal-context updates. | [S1](#test-s1), [S5](#test-s5), [S16](#test-s16) |
| ✓ AC9 | Apply the same permission-pause restrictions to on-demand MCP access. | [S3](#test-s3), [S10](#test-s10) |
| ✓ AC10 | Automatically resume recording only when the participant set is established as permitted; uncertain authorization must keep recording paused. | [P1](#test-p1), [P2](#test-p2), [P5](#test-p5) |
| ✓ AC11 | Keep permission-related pause status and resulting observation gaps visible to the owner. | [P1](#test-p1), [S13](#test-s13) |

### REQ-005

[Requirement](../../docs/features/butler-vision-and-ambient-awareness/requirements/REQ-005.md) · Code: [summaries.py](butler_ambient/summaries.py), [service.py](butler_ambient/service.py)

| Criterion | Required behavior | Automated evidence |
| --- | --- | --- |
| ✓ AC1 | Provide summaries on request for a selected period and automatically once daily at an owner-configurable delivery time outside quiet hours. | [B3](#test-b3), [B5](#test-b5), [S11](#test-s11), [S8](#test-s8) |
| ✓ AC2 | Cover observed work, identifiable work topics, web browsing and YouTube use when supported by available evidence. | [B3](#test-b3), [B1](#test-b1) |
| ✓ AC3 | Show observation coverage and uncertainty with duration estimates; identify unobserved intervals as gaps rather than assigning them to work or leisure. | [B3](#test-b3), [B4](#test-b4), [S13](#test-s13) |
| ✓ AC4 | Ground summary claims in retained observations and distinguish inferred activities and identities from confirmed facts. | [B1](#test-b1), [R5](#test-r5), [R7](#test-r7) |

### REQ-006

[Requirement](../../docs/features/butler-vision-and-ambient-awareness/requirements/REQ-006.md) · Code: [analysis.py](butler_ambient/analysis.py), [mcp.py](butler_ambient/mcp.py), [delivery.py](butler_ambient/delivery.py)

| Criterion | Required behavior | Automated evidence |
| --- | --- | --- |
| ✓ AC1 | Raw images, screenshots, video and audio are processed locally and are not sent to external models. | [B6](#test-b6), [B9](#test-b9), [T1](#test-t1), [S16](#test-s16) |
| ✓ AC2 | Only locally derived textual summaries may be sent to external models; raw transcripts and verbatim screen-content dumps do not qualify as summaries. | [B1](#test-b1), [B2](#test-b2), [B10](#test-b10) |
| ✓ AC3 | The same boundary applies to scheduled analysis, MCP responses and downstream agent context. | [B1](#test-b1), [S7](#test-s7), [S10](#test-s10) |
| ✓ AC4 | Local processing failure does not trigger an external raw-media fallback. | [B7](#test-b7), [B9](#test-b9), [S6](#test-s6) |
| ✓ AC5 | Existing cost approval and budget controls remain applicable; this permission does not authorize paid API use. | [B7](#test-b7), [B9](#test-b9), [D9](#test-d9) |

### REQ-007

[Requirement](../../docs/features/butler-vision-and-ambient-awareness/requirements/REQ-007.md) · Code: [policy.py](butler_ambient/policy.py), [sensing.py](butler_ambient/sensing.py), [cli.py](butler_ambient/cli.py), [service.py](butler_ambient/service.py)

| Criterion | Required behavior | Automated evidence |
| --- | --- | --- |
| ✓ AC1 | Allow the owner to record and revoke prior agreement for participating people. | [M2](#test-m2), [P4](#test-p4), [S4](#test-s4) |
| ✓ AC2 | Pause camera and microphone recording when a person without established prior agreement is detected, including when authorization is uncertain. | [P2](#test-p2), [P4](#test-p4), [T5](#test-t5) |
| ✓ AC3 | During permission-related recording pauses, allow only transient local processing of camera and microphone input exclusively to determine who remains present. | [S1](#test-s1), [T3](#test-t3), [T4](#test-t4) |
| ✓ AC4 | During permission-related recording pauses, prohibit recording, general analysis, external transmission, and retention of frames, audio, or derived information about people. | [S1](#test-s1), [S5](#test-s5), [S16](#test-s16) |
| ✓ AC5 | Automatically resume ambient recording only when the participant set is established as permitted; uncertain authorization must keep recording paused. | [P1](#test-p1), [P5](#test-p5), [P6](#test-p6) |
| ✓ AC6 | Do not rely solely on an inferred name match to authorize recording or resumption. | [P2](#test-p2), [S15](#test-s15) |
| ✓ AC7 | Apply the same participant restrictions and permission-pause processing limits to on-demand MCP camera and microphone access. | [S3](#test-s3), [S10](#test-s10), [M1](#test-m1) |
| ✓ AC8 | Expose permission-related recording pauses to the owner and mark affected intervals as observation gaps. | [P1](#test-p1), [S1](#test-s1), [S17](#test-s17) |

### REQ-008

[Requirement](../../docs/features/butler-vision-and-ambient-awareness/requirements/REQ-008.md) · Code: [delivery.py](butler_ambient/delivery.py), [service.py](butler_ambient/service.py)

| Criterion | Required behavior | Automated evidence |
| --- | --- | --- |
| ✓ AC1 | The first release can proactively deliver situational advice about work, rest, reading, and entertainment based on observed activities and retained history. | [D8](#test-d8) |
| ✓ AC2 | Butler can independently identify useful opportunities for advice within these domains without requiring predefined owner goals. | [D8](#test-d8) |
| ✓ AC3 | Each advice message identifies its observational basis, communicates uncertainty, and explains its expected benefit. | [D8](#test-d8), [B1](#test-b1) |
| ✓ AC4 | Inferred identity is not presented as confirmed. | [B1](#test-b1), [D8](#test-d8) |
| ✓ AC5 | Watching YouTube alone must not justify advice to reduce YouTube viewing. | [D8](#test-d8) |
| ✓ AC6 | Deliver advice through the owner's existing Butler Telegram conversation only outside quiet hours, 22:00–08:00 America/Chicago. | [D1](#test-d1), [D9](#test-d9) |
| ✓ AC7 | Deliver at most three proactive advice messages per calendar day in America/Chicago. | [D2](#test-d2) |
| ✓ AC8 | Allow at least 60 minutes between consecutive proactive advice deliveries, including across midnight. | [D2](#test-d2), [D3](#test-d3) |
| ✓ AC9 | Exclude the daily summary and responses to owner requests from the proactive advice quota. | [D5](#test-d5), [S11](#test-s11) |
| ✓ AC10 | Support owner controls to mute proactive advice. | [D4](#test-d4), [S8](#test-s8) |
| ✓ AC11 | Allow the owner to disable each advice topic individually: work, rest, reading, and entertainment; do not deliver proactive advice on disabled topics. | [D4](#test-d4), [S8](#test-s8) |
| ✓ AC12 | Suppressed advice must not produce a catch-up burst when delivery becomes available. | [D4](#test-d4), [S14](#test-s14) |
| ✓ AC13 | Enforce notification limits across retries and service restarts without duplicate deliveries. | [D2](#test-d2), [D6](#test-d6), [D7](#test-d7), [S11](#test-s11) |
| ✓ AC14 | Advice does not execute actions on the owner's behalf. | [D8](#test-d8), [D9](#test-d9) |

## Test catalog

<a id="test-p1"></a> **P1** — [ParticipantTests.test_three_fresh_checks_before_start_and_auto_resumption](tests/test_feature.py#L72).

<a id="test-p2"></a> **P2** — [ParticipantTests.test_inferred_name_missing_audio_or_unconsented_person_cannot_authorize](tests/test_feature.py#L83).

<a id="test-p3"></a> **P3** — [ParticipantTests.test_owner_pause_is_authoritative](tests/test_feature.py#L89).

<a id="test-p4"></a> **P4** — [ParticipantTests.test_revocation_pauses_even_on_otherwise_valid_evidence](tests/test_feature.py#L93).

<a id="test-p5"></a> **P5** — [ParticipantTests.test_stale_replayed_future_or_delayed_checks_do_not_authorize](tests/test_feature.py#L97).

<a id="test-p6"></a> **P6** — [ParticipantTests.test_restart_never_reuses_permit](tests/test_feature.py#L107).

<a id="test-r1"></a> **R1** — [RetentionTests.test_all_raw_types_expire_after_24h_regardless_of_analysis_state](tests/test_feature.py#L149).

<a id="test-r2"></a> **R2** — [RetentionTests.test_preservation_is_explicit_separate_and_survives_raw_expiry](tests/test_feature.py#L158).

<a id="test-r3"></a> **R3** — [RetentionTests.test_expired_media_cannot_be_resurrected_by_preserve](tests/test_feature.py#L170).

<a id="test-r4"></a> **R4** — [RetentionTests.test_derived_results_keep_source_references_after_expiration_and_can_be_deleted](tests/test_feature.py#L173).

<a id="test-r5"></a> **R5** — [RetentionTests.test_identity_correction_applies_to_history_without_rewriting_inferences](tests/test_feature.py#L182).

<a id="test-r6"></a> **R6** — [RetentionTests.test_directory_and_database_are_private](tests/test_feature.py#L191).

<a id="test-r7"></a> **R7** — [RetentionTests.test_grouped_evidence_preserves_all_original_ids](tests/test_feature.py#L194).

<a id="test-b1"></a> **B1** — [BoundaryAndSummaryTests.test_export_excludes_raw_details_and_inferred_names_but_keeps_provenance](tests/test_feature.py#L210).

<a id="test-b2"></a> **B2** — [BoundaryAndSummaryTests.test_facts_reject_raw_fields_nonfinite_confidence_and_unknown_enums](tests/test_feature.py#L219).

<a id="test-b3"></a> **B3** — [BoundaryAndSummaryTests.test_work_browsing_youtube_durations_no_overlap_or_gap_invention](tests/test_feature.py#L226).

<a id="test-b4"></a> **B4** — [BoundaryAndSummaryTests.test_screenshot_only_informs_observed_time_and_never_covers_a_gap](tests/test_feature.py#L239).

<a id="test-b5"></a> **B5** — [BoundaryAndSummaryTests.test_summary_rejects_invalid_periods](tests/test_feature.py#L246).

<a id="test-b6"></a> **B6** — [BoundaryAndSummaryTests.test_analyzer_connects_only_to_numeric_loopback_and_never_follows_remote_redirect](tests/test_feature.py#L249).

<a id="test-b7"></a> **B7** — [BoundaryAndSummaryTests.test_local_failure_has_no_external_raw_fallback](tests/test_feature.py#L255).

<a id="test-b8"></a> **B8** — [BoundaryAndSummaryTests.test_canceled_analysis_never_starts_model_or_transcriber](tests/test_feature.py#L274).

<a id="test-b9"></a> **B9** — [BoundaryAndSummaryTests.test_local_ollama_cloud_proxy_is_rejected_before_raw_media_is_sent](tests/test_feature.py#L259).

<a id="test-b10"></a> **B10** — [BoundaryAndSummaryTests.test_supported_person_dog_appearance_book_and_activity_facts_remain_inferences](tests/test_feature.py#L206).

<a id="test-d1"></a> **D1** — [DeliveryTests.test_quiet_hour_boundaries](tests/test_feature.py#L295).

<a id="test-d2"></a> **D2** — [DeliveryTests.test_three_per_local_day_and_hour_spacing_survive_restart](tests/test_feature.py#L300).

<a id="test-d3"></a> **D3** — [DeliveryTests.test_spacing_does_not_reset_at_calendar_midnight](tests/test_feature.py#L310).

<a id="test-d4"></a> **D4** — [DeliveryTests.test_mute_topic_controls_and_stale_candidates_do_not_queue_a_burst](tests/test_feature.py#L316).

<a id="test-d5"></a> **D5** — [DeliveryTests.test_daily_is_excluded_from_advice_quota](tests/test_feature.py#L325).

<a id="test-d6"></a> **D6** — [DeliveryTests.test_ambiguous_delivery_is_reserved_and_never_retried](tests/test_feature.py#L329).

<a id="test-d7"></a> **D7** — [DeliveryTests.test_concurrent_services_cannot_both_deliver_same_message](tests/test_feature.py#L336).

<a id="test-d8"></a> **D8** — [DeliveryTests.test_all_advice_domains_explain_benefit_grounding_and_uncertainty](tests/test_feature.py#L347).

<a id="test-d9"></a> **D9** — [DeliveryTests.test_telegram_uses_only_existing_openclaw_account_and_requires_receipt](tests/test_feature.py#L358).

<a id="test-s1"></a> **S1** — [ServiceTests.test_permission_pause_keeps_zero_media_and_only_anonymous_gaps](tests/test_feature.py#L402).

<a id="test-s2"></a> **S2** — [ServiceTests.test_continuous_verified_media_and_minute_schedule](tests/test_feature.py#L412).

<a id="test-s3"></a> **S3** — [ServiceTests.test_mcp_cannot_start_capture_while_paused](tests/test_feature.py#L424).

<a id="test-s4"></a> **S4** — [ServiceTests.test_owner_pause_and_revoke_cancel_analysis_and_require_new_checks](tests/test_feature.py#L429).

<a id="test-s5"></a> **S5** — [ServiceTests.test_late_analysis_after_permission_change_is_discarded](tests/test_feature.py#L440).

<a id="test-s6"></a> **S6** — [ServiceTests.test_analysis_failure_is_recorded_and_cannot_extend_raw_lifetime](tests/test_feature.py#L457).

<a id="test-s7"></a> **S7** — [ServiceTests.test_fresh_camera_request_waits_for_new_verified_frame](tests/test_feature.py#L467).

<a id="test-s8"></a> **S8** — [ServiceTests.test_preferences_reject_quiet_time_and_support_each_topic](tests/test_feature.py#L479).

<a id="test-s9"></a> **S9** — [ServiceTests.test_maintenance_expires_media_while_paused_and_cannot_transmit](tests/test_feature.py#L483).

<a id="test-s10"></a> **S10** — [ServiceTests.test_real_local_rpc_and_mcp_surface_have_no_raw_or_control_tools](tests/test_feature.py#L600).

<a id="test-s11"></a> **S11** — [ServiceTests.test_daily_summary_time_mute_exemption_and_restart_deduplication](tests/test_feature.py#L488).

<a id="test-s12"></a> **S12** — [ServiceTests.test_biometric_work_does_not_block_owner_revocation](tests/test_feature.py#L501).

<a id="test-s13"></a> **S13** — [ServiceTests.test_minute_analysis_never_bridges_gaps_or_stretches_screenshots](tests/test_feature.py#L514).

<a id="test-s14"></a> **S14** — [ServiceTests.test_old_untreated_media_are_marked_as_missed_without_catchup_analysis](tests/test_feature.py#L548).

<a id="test-s15"></a> **S15** — [ServiceTests.test_prior_inferences_and_owner_context_are_available_without_becoming_canonical](tests/test_feature.py#L556).

<a id="test-s16"></a> **S16** — [ServiceTests.test_screen_helper_uses_only_memory_and_discards_capture_on_permission_change](tests/test_feature.py#L571).

<a id="test-s17"></a> **S17** — [ServiceTests.test_unavailable_sources_and_recovery_have_explicit_intervals](tests/test_feature.py#L589).

<a id="test-m1"></a> **M1** — [MCPProtocolTests.test_parse_errors_notifications_and_invalid_tools](tests/test_feature.py#L633).

<a id="test-m2"></a> **M2** — [MCPProtocolTests.test_owner_enrollment_requires_explicit_prior_agreement](tests/test_feature.py#L628).

<a id="test-t1"></a> **T1** — [RuntimeTests.test_real_ffmpeg_streams_produce_bounded_in_memory_video_and_audio](tests/test_runtime.py#L27).

<a id="test-t2"></a> **T2** — [RuntimeTests.test_temporary_service_process_starts_paused_without_opening_devices](tests/test_runtime.py#L49).

<a id="test-t3"></a> **T3** — [LocalVerifierTests.test_real_local_models_reject_blank_and_unreadable_camera_data](tests/test_runtime.py#L81).

<a id="test-t4"></a> **T4** — [LocalVerifierTests.test_real_vad_recognizes_silence_without_inventing_speaker](tests/test_runtime.py#L88).

<a id="test-t5"></a> **T5** — [LocalVerifierTests.test_unknown_voice_or_ambiguous_face_invalidates_whole_batch](tests/test_runtime.py#L120).

<a id="test-t6"></a> **T6** — [LocalVerifierTests.test_dropped_capture_evidence_cannot_authorize_a_batch](tests/test_runtime.py#L100).

## Required workflow checklist

- ✓ (1) Branch/origin/main workflow waived by owner in DEC-005. `git rev-parse --show-toplevel` reported that the workspace is not a Git repository. No main branch was committed to.
- ✓ (2) Feature records updated during implementation and final acceptance audit; see requirements, plan, decisions and discussion.
- ✓ (3) GitHub issue workflow waived by DEC-005; no issue was closed.
- ✓ (4) Automated tests and both builds pass; exact outputs and hashes are linked above.
- ✓ (5) Git commit/clean-tree operations are unavailable in this non-Git workspace and are covered by the local-only scope in DEC-005. No repository was initialized. All implementation files and evidence remain locally; unrelated existing files were not changed.
- ✓ (6) Push and pull request waived by DEC-005.
- ✓ (7) Remote CI waived by DEC-005; the complete local verification command passes.
- ✓ Merge is outside the revised local-only scope; no merge was attempted.

No existing version was bumped. The original deployment exclusion was superseded by DEC-007; authorized deployment is documented below. No manual Telegram test message was sent, no MarkView browser tab was used, and no manual test checklist was requested from the owner.

## Live deployment follow-up — deployed; live verification partly complete

- Runtime installed under `~/.openclaw/tools/ambient-awareness`; signed `ButlerAwareness.app` provides a stable macOS device-access identity. A private launch agent runs the local service. Camera, microphone and screen access are granted.
- Local Gemma 3 4B, Whisper base, YOLOX, YuNet and SFace are installed. Actual local transcription and constrained-schema vision inference succeeded. No paid API or external raw-media fallback was used.
- Owner enrollment succeeded; only face/voice reference vectors remain. The live state database contains permitted camera/audio segments and automatic screenshots. The owner enabled observation, which still requires three fresh participant checks on every resumption.
- [A fresh live camera observation](evidence/live-camera-cli.json) returned a locally derived typed summary and source metadata. This is actual device/model evidence, separate from synthetic tests.
- The existing Butler agent successfully called the workspace-scoped MCP status tool; deployment probe is retained at `~/.local/share/butler-ambient/logs/butler-mcp-probe.json`. Other bots do not inherit this MCP configuration.
- [Fresh camera MCP analysis](evidence/live-camera-mcp.json) and [fresh screen MCP analysis](evidence/live-screen-mcp.json) both succeeded. Automatic camera and microphone analysis also succeeded. A single uninterrupted scheduled cycle covering all three sources was not observed in the final probe: participant checks reported `face-unavailable` and correctly paused processing. Minute scheduling and screen analysis have separate automated/live evidence; this combined live check remains unconfirmed. [Additional enrollment](evidence/additional-enrollment-result.json) was explicitly authorized by DEC-010 and succeeded. No automatic template learning, threshold reduction or permission bypass was added.
- Additional tests cover enrollment-media disposal, speech after silence, person-box association, fresh-image sampling, permission-only setup during pauses, bounded explicit template banks and nonfinite-match rejection. [Latest results](evidence/results.json) and [feature output](evidence/feature-tests.log) are authoritative.

### Deployment evidence

[deployment.json](evidence/deployment.json) records the running launch agent, all three macOS grants, exact installed-source/hash agreement, successful signature verification, source counts, model hashes and the current policy pause. [deployment-update.json](evidence/deployment-update.json) records the successful installation/restart commands. [live-pipeline.json](evidence/live-pipeline.json) records the interrupted combined live probe without claiming success. The complete automated verification passes **141 tests**, native screen/launcher builds and the wheel build. Physical recognition accuracy and live Telegram delivery remain unmeasured.


## Interaction extension — REQ-009–012

The original participant/capture contracts still pass. Q-010/DEC-015 explicitly add only a separate local wake-address dialogue lease after three fresh checks of the sole recognized owner face and microphone availability. The full face+voice gate continues to control recording, images, ambient analysis, preservation and every transmission. DEC-016 records the current owner-selected baritone, superseding the initial calm choice. Every new owner answer, including the accidental screenshot clarification, is in discussion.md.

| Requirement/criteria | Implementation and evidence |
| --- | --- |
| REQ-009 AC1–2 | `local_ui.snapshot`, native `panel.swift`; `test_local_state_labels_historical_data_without_new_sensing`; actual consecutive fresh-camera metadata in [camera-preview-live.json](evidence/camera-preview-live.json). Source times are labeled and old dates displayed. |
| REQ-009 AC3–4 | `ui-observe` uses `request_fresh`; pause/resume uses the existing gate. `test_paused_local_preview_cannot_capture_or_return_image`, `test_live_panel_only_reuses_recorded_permitted_frames_and_removes_preview_on_pause`; native panel build. |
| REQ-009 AC5 | AppKit menu-bar panel, private Unix socket/owner CLI, no HTTP/webview/telemetry; [native-panel-build.log](evidence/native-panel-build.log). |
| REQ-010 AC1 | Synthetic previews and explicit persistent choice: `test_synthetic_preview_does_not_activate_voice_and_requires_known_profile`, `test_arbitrary_or_nonsynthetic_voice_references_are_not_offered`; DEC-014/016. Owner enrollment never feeds synthesis. |
| REQ-010 AC2–3 | Independent `Conversation` queue, local Whisper + Ollama; alias, uncertainty and recent-context tests in `test_conversation.py`. `test_real_whisper_stdin_returns_synthetic_wake_words_without_transcript_files` runs the actual installed CLI; [local-conversation-turn.json](evidence/local-conversation-turn.json) verifies actual ASR, local intent and reply using a synthetic fixture. |
| REQ-010 AC4 | Owner barge-in, pause/no-ASR, other-speaker rejection, DEC-015 nonarchive separation, unknown-face cancellation and stale/revoked lease tests. Native VPIO startup/rate correction and 13 permitted checks during synthetic playback: [audio-live.json](evidence/audio-live.json). |
| REQ-010 AC5 | `test_advice_across_voice_and_telegram_uses_same_durable_quota`, existing delivery quiet/day/hour/mute/topic/dedup tests; requested turns do not use autonomous delivery reservations. |
| REQ-010 AC6 | Native voice controls, face-mode checkbox, phases and failures; echo support mandatory (`test_echo_reference_is_mandatory_for_voice_enablement`); local-only worker environment and no external fallback. |
| REQ-010 AC7 | `test_spoken_description_uses_new_permitted_camera_and_separate_cancelable_vision`, `test_face_dialogue_never_uses_participant_only_camera_for_description`; permission cancellation cannot be bypassed by the dialogue lease. |
| REQ-011 AC1, AC3, AC6 | `test_capture_requires_new_frame_and_expiry_does_not_resurrect_source`; source IDs/times, distinct kinds, candidate labels and original availability are controlled metadata. |
| REQ-011 AC2 | `test_selected_roots_only_and_deleting_candidate_preserves_original`; native owner picker, symlink/hidden/package exclusions, bounded local caption/ranking search, no implicit library scan. |
| REQ-011 AC4, AC7 | `test_pause_prohibits_requests_and_hides_gallery_paths`, `test_mcp_cannot_supply_a_prompt_recipient_or_control`; rich local gallery paths never enter MCP. |
| REQ-011 AC5 | `test_owner_request_is_actual_fresh_user_message_in_pinned_private_session`, `test_requested_telegram_delivery_reserved_once_and_no_raw_mcp`, `test_group_recipient_or_ambiguous_send_never_gets_retried`; fixed owner account/chat, direct request + high-confidence intent, reserve/receipt/dedup. |
| REQ-012 AC1 | Specific criteria required, progress stages/cancel/failure; `test_generation_needs_specific_request_and_keeps_storage_reserve`, `test_cancel_stops_isolated_generation_process_without_touching_capture`. |
| REQ-012 AC2–3 | Offline isolated SD-Turbo/MPS worker, pinned manifests/dependencies, distinct generated provenance; actual 512×512 nonblank output and SHA in [local-interaction-models.json](evidence/local-interaction-models.json), generator stages in [image-smoke.jsonl](evidence/image-smoke.jsonl). The initial ASR result in that combined report was superseded by the corrected real turn above. |
| REQ-012 AC4–5 | Shared local gallery/explicit requested delivery path; `test_generated_delete_is_local_only_and_provenance_is_distinct`, `test_gallery_history_survives_restart_without_mutating_current_job_or_resurrecting_deletion`. Originals are preserved. |
| REQ-012 AC6 | 11 GiB storage reserve, bounded low-priority isolated process, cancellation test proves capture remains untouched. |

### Deployment and actual operating limits

[interaction-deployment.json](evidence/interaction-deployment.json) installs the dialogue/visual path; [asr-stdout-deployment.json](evidence/asr-stdout-deployment.json) installs the confirmed Whisper stdout correction; [interaction-final-deployment.json](evidence/interaction-final-deployment.json) identifies the final verified wheel. The stable signed parent, all device grants and shared gateway remain unchanged. [interaction-runtime-integrity.json](evidence/interaction-runtime-integrity.json) compares installed source/helpers with this snapshot. Four MCP tools were discovered through the real workspace-scoped mcporter bridge; a no-authorized-request image probe correctly returned unavailable and sent nothing.

The real CLI regression proves the earlier empty-transcript fault is fixed. `-otxt -of -` writes only to `/dev/stdout`, not a text file. A complete synthetic local-model turn produced a reply; separate actual synthetic TTS and echo-safe playback checks passed. The post-fix live status probe shows ongoing face-gated listening/transcription, but **does not yet establish a complete live owner utterance → spoken reply**. It is not presented as one. Physical recognition accuracy, missed/wrong wakes, off-camera/TV speech in the explicitly accepted face mode and face-related interruptions remain operating limitations. No new enrollment was performed or requested again.

No real Telegram image test was sent because there is no specific current owner request for one. Its authorization/recipient/dedup transport contract is covered with controlled fixtures. Personal retrieval was tested only with explicitly selected synthetic files; the owner has not yet selected a personal library. Searches report bounded partial coverage (up to24 candidate images), not an exhaustive search of all private stores. The generated smoke artifacts were removed after metadata/hashes and a clearly synthetic ASR regression fixture were retained.

Git/GitHub steps 1/3/5/6/7 remain waived by DEC-005 in this non-Git workspace. No merge, shared gateway restart, version bump, MarkView browser or owner manual test checklist was performed.

The final [runtime state](evidence/interaction-final-state.json) reports current source/voice permissions and proves verification hashes still match after installation. [Telegram media dry-run](evidence/telegram-image-dry-run.json) exits successfully using a synthetic image with sending disabled.

## Current operational repair and main Settings · 2026-10-22

Latest verification is **208 tests (137 feature + 71 regressions)**, all passing, with no skips. All four native helpers, AEC/raw-input smoke test, wheel and doctor pass. [results.json](evidence/results.json) records exact source/artifact hashes. [main-settings-deployment.json](evidence/main-settings-deployment.json) records the installed wheel/helpers and unchanged signed parent. [main-settings-live.json](evidence/main-settings-live.json) confirms installed Python matches source, 22 available AI components, 17 Butler cron jobs plus 3 built-in jobs, installed routing/embedding adapters, a no-op model control and rejection of an out-of-scope job. No real scheduled run, message, image delivery or agent model change was triggered for verification.

### REQ-013 · owner-local diagnostics

| Criterion | State and evidence |
| --- | --- |
| AC1 | ✓ Fresh camera/PCM lamps, hardware mute/gain/formats, separate dialogue/recording permissions and exact anonymous cancellation reasons. `test_hardware_mute_is_not_a_green_working_microphone`, `test_native_audio_health_updates_are_metadata_only`, `test_hardware_mute_or_zero_gain_cannot_supply_participant_audio_evidence`; [installed metadata](evidence/main-settings-live.json). |
| AC2 | ✓ Pre-wake local ASR text, address/intent, reply/synthesis/playback and cancellation events. `test_debug_shows_unaddressed_words_locally_but_not_in_status_and_clears_on_pause`, `test_policy_cancelled_interruption_is_not_reported_as_asr_failure`. |
| AC3 | ✓ Transient text erasure and no model-facing debug/control surface. Conversation privacy/lease tests and existing MCP boundary tests pass. |
| AC4 | Partial: native split selectable window installed; ordinary audio I/O replaces VPIO. Native smoke reports no ducking and ~38 dB echo reduction, while preserving ordinary PCM exactly. Owner audibility after this replacement is still unconfirmed; the earlier negative answer remains binding (DEC-024). |

### REQ-014 · AI/CLI and daily-run Settings

| Criterion | State and evidence |
| --- | --- |
| AC1 | ✓ Installed inventory lists 22 actual roles, paths/models, CLI/loopback transport, availability and running state. [Live inventory](evidence/main-settings-live.json). |
| AC2 | ✓ Compatible recognition files, local-model capability/weights, Python/CLI, language/device/resolution validation; Butler agent primary is scoped and fallbacks preserved. [test_local_settings.py](tests/test_local_settings.py), [test_management.py](tests/test_management.py). |
| AC3 | ✓ Seventeen actual Butler jobs and active dispatch discovery; separate run/pause/resume/stop/update controls. Scoped isolated-run abort, truthful queue receipts and rejection of another agent are tested; live read-only discovery passes. |
| AC4 | ✓ Persistent built-in daily/advice/analysis controls preserve gates, quotas, quiet hours, deduplication and no catch-up. Stopping analysis cancels/discards late results; pausing does not abort a current run. |
| AC5 | ✓ All controls remain owner-local, auth/provider secrets and prompts are omitted, validation errors are controlled, and unavailable service/input shows red. [Scoped control tests](tests/test_management.py), [live receipt](evidence/main-settings-live.json). |

### Live voice remains unresolved

DEC-024 records the owner answer «Текст неверный или ответа нет». Keep REQ-010/I-10 and F-004 open. Current live input is Jabra, unmuted, hardware input/output 48 kHz, gain ~0.286, converted to PCM16/16 kHz locally. The latest probe had fresh camera/microphone but `face-unavailable`, so dialogue was not permitted. Earlier admitted inputs had both no-wake recognition and cancellations after uncertain face evidence; neither synthetic ASR nor a prepared synthetic reply proves live owner recognition.

[Native format probe](evidence/native-input-resampling.json) passes all words in the synthetic counting phrase through streaming AVAudioConverter 48→16 kHz, preserving 99.991% of frames (six startup samples). [AEC/raw-input smoke](evidence/native-aec-test.log) confirms ordinary input is unchanged outside own playback. No owner media were retained by these probes, no new enrollment occurred, and no participant thresholds were relaxed. A pending diagnostic question asks whether the dialogue lamp becomes red during the owner's speech; it does not repeat any accepted policy or preference.

[Live integration proof](evidence/main-settings-integration-live.json) imports the actual Butler dispatch/semantic modules, verifies 10 approved routes and local embedding availability, and proves all source outside the narrow adapters is unchanged. Exact originals are privately backed up; [adapter manifest](evidence/main-settings-adapters.json) records hashes. Git/GitHub/PR/CI remain waived by DEC-005. The goal remains unfinished until the outstanding live behavior is verified.

The hardware-mute/zero-gain correction is now verified and installed. A complete silent PCM buffer from an explicitly disabled input cannot authorize either participant gate. Latest verification is 208 tests (137 feature +71 regressions), all build/check exits 0. [Named native windows](evidence/main-settings-window.json) prove both Butler windows are running, using window metadata only. No screen image or owner transcript was exported.

### Actual microphone and voice-processing readback

[Installed audio readback](evidence/actual-audio-device-live.json) now queries the actual running AudioUnit and AVAudioIONode state. macOS opens an internal aggregate; the resolver validates active sub-devices, channel layout and the input channel map. Actual physical input is **Jabra SPEAK 410 USB**, channel0, unmuted, gain~0.286. `input_verified=true` and `voice_processing_enabled=false` are observed properties, not inferred from configuration. Unknown, mixed, silent channel maps or a different selected source cannot authorize either participant gate. Hardware UIDs stay local and are omitted from the debug metadata. Native synthetic mapping checks pass alongside ~38 dB AEC reduction and exact preservation of ordinary input.

The first direct-ID comparison correctly rejected the aggregate but was insufficient to resolve the physical source; aggregate-aware resolution is now implemented, tested and installed. Latest total is **208 tests (137 feature +71 regressions)** and all builds/checks pass. The actual voice gate is still face-unavailable; this does not establish live recognition accuracy or the owner's background-audio audibility. REQ-010/F-004 and the remaining REQ-013 criterion stay open.

### Live verification is blocked, not complete

A third consecutive goal-turn audit still lacks admitted actual speech and the pending owner diagnostic answer. [Fresh live process poll](evidence/blocked-audit-live.json): PID44515,45samples,0dialogue-permitted,45fresh microphone samples; no ASR counters changed. [Completion audit](evidence/current-completion-status.json) preserves208 passing tests/builds, matching installed source/helpers and the two unresolved live requirements. Previous turn classification is progress (actual device readback was implemented/proven); this turn is a verified wait on that confirmed-live service followed by a blocked audit. No remaining independent implementation work currently identified can supply the missing live evidence. The goal must be marked blocked and remain unfinished; the service stays running.


## Unified native UI — REQ-015 / DEC-025

All five acceptance criteria pass. Code: `butler_ambient/native/panel.swift`, `build_app.py`. One native Butler.app window provides eight sections and all existing controls. The preview reserves geometry independent of image intrinsic dimensions; scrollbars reserve width. Native synthetic tests cover absent, landscape and portrait images at three window sizes, restored window/navigation state, stable selection IDs across reordered inventories, unsaved settings and scroll retention. Refresh never opens a new window or navigates to an image result automatically. Models for the agent, local conversation, local vision and named CLI tasks are independent.

The first bundled startup failed before rendering because a force-unwrapped preference suite for the same application domain returned nil; owner supplied the crash screenshot. BUG-001 is resolved using the standard preference store, with a bundled regression and installed normal startup/reopen checks. New installed UI has one window, one persistent process and matching verified binary/source hashes. Synthetic screenshots in `evidence/ui-preview/` contain no live media. Evidence: `unified-ui-live.json`, `unified-ui-window.json`, `unified-ui-stability.json`, and the later `model-catalog-runtime-live.json` / `model-catalog-window.json`.

## Provider catalogs — REQ-016 / DEC-026

| Criterion | Verification |
| --- | --- |
| AC1: metadata-only discovery | `tests/test_catalog.py` checks exact Codex initialize/model-list pagination and Claude initialize-only traffic, bounded output and subprocess cleanup; auth/prompt fields are dropped. Installed `model-catalog-live.json` shows successful provider metadata refresh without inference or new credentials. |
| AC2: background/manual freshness | Hourly nonblocking refresh runs from maintenance even with the UI closed. Tests cover concurrency, retries, stale cache and seven-day expiry. Live explicit refresh returned in 0.08 seconds, reported busy, then advanced both successful timestamps. |
| AC3: dynamic compatible choices | Live catalog has 7 OpenAI and 12 canonical Anthropic entries; task choices include new Astra and Claude 5.5 IDs plus aliases. Main-agent/cron choices additionally intersect the installed Butler-scoped OpenClaw provider registry. Native checks show new choices appear without losing a draft and removed choices remain visible with Apply disabled. |
| AC4: effective validation and scope | Configuration and dispatch use the same private metadata cache. `model-catalog-integration.json` applies a new catalog model through the actual deployed adapter in an isolated workspace and preserves all other routes/fields. Only the owned Settings block changed; exact private backups remain. No live route, agent model, global allowlist, provider credentials or job schedule was changed by refresh. |
| AC5: tests, build and installation | Full run: 223 tests, 38 native UI checks, four native helpers, app packaging, wheel and doctor pass. Installed 24 Python sources and the UI binary match verification. `model-catalog-deployment.json`, `model-catalog-runtime-live.json`, `model-catalog-live.json`, `model-catalog-window.json` and `model-catalog-stability.json` are the runtime evidence. |

Sources: [Codex model/list](https://learn.chatgpt.com/docs/app-server#models), [OpenAI catalog limitations](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [Claude model configuration](https://code.claude.com/docs/en/model-config), and [official Claude SDK initialize protocol](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py). CLI catalogs may be cached by the CLI and do not prove inference entitlement. No live inference was sent to validate a new model; requested model choice remains an explicit owner action. The installed OpenClaw registry lags the CLI catalogs, so newer CLI models are offered for CLI tasks but excluded from incompatible agent/cron choices. No global CLI upgrade or shared gateway restart was performed.

REQ-010/I-10 and F-004 remain open for real owner speech accuracy/reply; REQ-013/I-13 retain the unconfirmed background-audio audibility item. Green input lamps and these UI/catalog checks do not resolve those findings. Git/GitHub work remains waived by DEC-005; no merge or version bump.


## Continuous contextual streaming dialogue — DEC-027–030

Latest full verification: **239 automated tests (168 feature +71 regressions),38 native UI checks, all14 jobs exit0**, including four native helpers, native AEC/raw-input checks, application packaging, wheel and doctor. See [results.json](evidence/results.json).

| Contract | Evidence |
| --- | --- |
| Resident local readiness | `transcriber.py` owns a numeric-loopback-only Whisper server, WAV stays in memory, no converter or transcript files; cancellation closes the HTTP connection and retains weights. `streaming-voice-asr-probe.json`:0.26–0.31s fixed-sample decoding. Model and selected synthetic voice warm independently of participant permission. |
| Text/audio overlap | A complete confident plan header precedes any spoken text. `streaming.py` decodes only safe JSON prefixes; the first complete useful clause enters TTS while generation continues. Tests prove speech starts before the model completes, and reject incomplete/duplicate addressing metadata. |
| Bounded native stream | Credit-controlled MLX chunks (~0.48s) feed at most two queued AVAudioPlayerNode buffers using the existing Speex playback reference. `streaming-voice-tts-probe.json`: first warm chunk0.276s. Cancellation test keeps the same worker and yields the next first chunk0.239s. |
| Always-active context | Actual local Gemma3 tests accept an unnamed initial question, a follow-up after120seconds and stay silent for unrelated speech. Explicit silence cannot be overridden. Every admitted utterance can contribute transient context, including no-reply turns under the face-only lease. Completed replies alone enter dialogue history. Pause/lease loss clears all ephemeral text; no recording/image permission is gained. |
| Owner-visible diagnostics | Dialogue displays warm readiness and first text/audio timings; the last response measurement survives a later no-reply utterance. Diagnostics show the actual resident Whisper executable. No raw text is added to model-facing status. |
| Portrait icon | Native multi-resolution `assets/Butler.icns`, `CFBundleIconFile` and installed hash match in `streaming-deployment.json`. Original portrait is preserved; it is not an enrollment source. |
| Installation/scope | `streaming-deployment.json` compares before/after hashes for the stable TCC parent, configuration, global agent configuration, agreements and voice/preferences.26 installed Python sources, UI and icon match checked artifacts. Only own service/UI refreshed; no gateway, volume, version or Telegram media change. |

The serial baseline (`streaming-voice-baseline.json`) was15.638s before first output for a short fixed sample, including cold voice load and whole-file synthesis. First installed audio-only streaming revision completed native playback with5.308s input-ready to first playback (`streaming-live-benchmark.json`). The newer text/audio overlap revision is measured separately in `contextual-streaming-live-benchmark.json`; do not substitute component times for complete interaction latency. Fixed fixtures do not prove actual owner speech accuracy. REQ-010/I-10/F-004 and the background audibility criterion of REQ-013/I-13 remain open.

Architecture sources consulted: [OpenAI Realtime conversation/interruptions](https://developers.openai.com/api/docs/guides/realtime-conversations), [Ollama generation/keep-alive](https://docs.ollama.com/api/generate), [whisper.cpp in-memory WAV and client-disconnect cancellation](https://github.com/ggml-org/whisper.cpp/blob/master/examples/server/server.cpp), and [MLX Qwen3-TTS streaming](https://github.com/Blaizzy/mlx-audio). These informed the local implementation; no cloud speech service or provider API key is used.

### Final native first-playback measurement

[contextual-streaming-live-benchmark.json](evidence/contextual-streaming-live-benchmark.json) records successful actual native playback of the fixed3.027s Russian fixture: ASR0.313s, first reply clause2.020s, complete model response2.225s, first synthesized chunk2.337s, first playback2.350s, completed playback8.983s. This is input-ready timing; the full end-of-speech target remains unverified. The separate [runtime readback](evidence/contextual-streaming-runtime-live.json) shows a completed actual microphone response with first playback2.574s and completion7.875s. Timings begin when a complete admitted utterance is ready and exclude endpoint/participant-check wait; examples are not a latency guarantee. Recognition correctness is still open. Source/install hashes match; all models report ready, Jabra input works, Voice Processing=false, other-app ducking=none. Portrait I-18 is verified. DEC-031 reopens REQ-017/I-17: the owner reports that live unnamed questions and follow-ups still receive no reply. Live contextual behavior, recognition accuracy and audibility remain open.

## DEC-031: live contextual verification failed

The owner reports only the name triggers replies. Controlled model fixtures and playback counts do not establish successful live conversation. REQ-017/I-17 are in progress. `evidence/contextual-live-failure.json` records anonymous sampling: only11/20 snapshots permitted dialogue, with ambiguous face/presence failures. Before this diagnostic patch,33 ASR starts included22 address-stage cancellations. The last cancellation code alone cannot establish the distribution; bounded anonymous per-turn decision/cancellation metadata is being installed. Raw owner speech remains local.

## Owner answers Q-013–Q-016 — DEC-062/063/065/066 · 2026-10-24

Final verification: `evidence/results.json` (2026-10-24T06:45Z) passes all 21 jobs: 420 feature tests, news-video, Bamberg and WhatsApp regressions, every native build and UI layout test, the wheel and doctor. Its source hashes match the installed sources (`evidence/streaming-deployment.json`).

### REQ-026 / DEC-062 · general conversation for unverified speakers

| Criterion | Evidence |
| --- | --- |
| Entry and addressing | `tests/test_general_dialogue.py`: an addressed unverified utterance is routed to general mode, unnamed speech without an active general conversation stays silent, follow-ups continue within 300 s and not after 301 s, and a low-confidence follow-up is silent. |
| No private context | The general prompt's data keys are exactly speech, wake_name_heard, conversation_active and dialogue. Private history, ambient speech, observations and knowledge never reach it, and a private turn never sees general dialogue. |
| Fixed owner-required reply | Commands, confirmations with or without a pending review, and describe/show/image/control intents get the fixed reply with no adapter, capture or pending operation touched. On the real local Qwen3.5:9b model, a describe request, a screen request and an image request each got the fixed reply. |
| Identity loss | A routed or running general turn survives identity loss while private context is erased. Identity-loss cancellation is atomic with routing: an interleaving test injects routing inside the cancellation. An unverified utterance never preempts a private turn in progress. |
| Pause, settings, disable, stop | Pause, settings change, continuous_archive off, general_dialogue off and service stop each cancel the general turn and erase general context, including after an intervening private turn. |
| Private advice | Advice after a general turn is private: hidden from the general view, stopped with its playback on identity loss, and refused while a general turn is routed. Delivery then falls back to the default transport instead of losing it. |
| Diagnostics | Turn audit and native panel show general versus private mode with anonymous outcomes. The general view shows only general events, and an interruption transcript is never tagged general after routing. |

An independent review of the first version found one critical defect, four major and three minor. Spoken advice could run, show and keep playing under the general mode. Separately, general context survived disabling, identity-loss cancellation raced routing, the debug buffer was iterated without its lock, and unchecked speech preempted private turns. All were fixed by replacing a sticky mode flag with explicit general-turn token ownership. A re-review confirmed every fix. Its three residual minor issues were also fixed, and the new tests were mutation-checked to fail without their fixes.

Real local model: the first real-model run showed that an unnamed contextual follow-up was rejected and a describe request was answered as chat. The general prompt was corrected, and three consecutive probe runs then classified all eight synthetic utterances correctly.

### REQ-027 / DEC-065 · automatic event key frames

`tests/test_event_frames.py` has 22 tests covering:

- new known and unknown people, dogs and books at confidence at least 0.7;
- activity compared with the immediately previous camera observation of any age, and owner corrections applied on both sides;
- per-key 10-minute spacing, including the boundary through the service;
- the 20-frames-per-day limit in America/Chicago, durable across a restart and counting owner-deleted frames;
- 0600 files in the preserved area with provenance rows;
- fsync and folder sync before commit, and on a write or database failure no file or record and the committed observation kept;
- startup removal of only uncommitted event-frame files;
- expiry and cleanup never removing frames, owner delete removing them, Pause before commit keeping none, and non-camera sources keeping none.

Installed metadata (`evidence/general-frames-live-after-qwen35.json`) records two frames kept in the first ten minutes, one person and one dog, with no error. Frames were not opened for this verification.

### DEC-066 · background vision on Qwen3.5:9b

The change used `ui-settings-update`, whose validation requires a local model with vision capability. Doctor reports both vision and dialogue as qwen3.5:9b and ready. All local requests send `think: false`. Shared-model dialogue preemption already exists in the listener and conversation code, and cancelled analysis retries from the spool.

Ten-minute counters from `evidence/general-frames-live-before-gemma3.json` and `evidence/general-frames-live-after-qwen35.json`:

| Measure | gemma3:4b | qwen3.5:9b |
| --- | --- | --- |
| Camera observations added | 60 | 62 |
| Microphone observations added | 34 | 34 |
| System-audio observations added | 32 | 32 |
| Screen observations added | 11 | 14 |
| Camera backlog change | 1527 → 1470 | 1427 → 1279 |

The multi-hour unanalyzed backlog, already present before the switch, remains. Live dialogue latency on the shared model is not measured, because no spoken turn occurred in the window.

### Deployment and open items

`evidence/streaming-deployment.json` records the refresh: backup, own LaunchAgent bootout and bootstrap, wheel, helpers and UI installed. The stable permission parent, configuration, global agent configuration, agreements and preferences hashes are unchanged, and 40 installed sources match. Only Butler's own UI was restarted (`evidence/camera-clips-ui-restart.json`). No gateway, other bot, global volume, device, threshold, enrollment or version was changed.

Installed listening after deployment heard 28 utterances. None addressed Butler by name, and all stayed silent as general-not-addressed, which DEC-062 intends. A live spoken general reply, real owner dialogue (REQ-010/F-004 under DEC-067) and live shared-model latency remain open.

# Pending verification: single-user mode

Recordings DEC-097 supersedes mandatory owner verification for local history when the requested mode is enabled. A follow-up audit found the first release had mistakenly reused history visibility for gallery paths and had left the native gallery cache intact when raw permission fell; both now use the stricter `recordings_raw_allowed` signal. The synthetic gallery regression passed against both source and installed code. Focused recordings tests: 23/23; observation browser: 24/24; listening: 37/37; synthetic bundled panel: 279/279; full Python suite: 893/893. Installed six changed ambient modules and the panel executable; the service was restarted and single-user mode remains enabled. Metadata-only live checks after the final restart: `ui-recordings` returned hidden=false, 11,191 rows and 7 days, and `ui-observations` returned 3,048 rows and 7 days with no frame exposed; `action_permission=false`. Earlier, observation count was 49,728 and transcript row count 619. Synthetic tests cover local voice navigation without forged identity, denying detected other speakers, and avoiding agent dispatch; no live spoken phrase was tested. No media or transcript content was inspected. The owner's wider request to remove mandatory identity gating in other local and voice paths remains under review; the raw/playback and external-action gates remain unchanged.

The follow-up was installed: an explicitly addressed local voice summary uses only stored observations without retention cleanup or agent dispatch, and uses a generation invalidated by mode changes, hard closes, recording changes, failed participant sensing, detected company and unsafe or unknown call state. It does not require general conversation to be enabled. Synthetic focused tests cover actual voice queue routing, stale requests, no owner identity claim, stopping local playback on scope loss and leaving unrelated disclosure audio alone. Panel 280/280 covers immediate clearing when the owner switches mode off. Full Python suite on that source passed 908/908. Voice output is currently explicitly disabled; no real spoken output was tested or voice setting changed.

Metadata-only sampling then found the history still disappeared in 2/19 successful polls when the scene detector reported multiple faces; the owner reports being alone. The explicitly selected single-user local display now ignores those transient automatic classifications while explicit pause, archive off, stop and participant consent revocation still hide it. Strict participant, raw playback, external-action and spoken-summary gates remain independent. Installed-source focused regressions 3/3 and the final Python suite 909/909 pass; the tested panel passes 280/280, and installed module and panel hashes match. After installation, 25/25 metadata-only polls kept history visible, including one reporting multiple faces; raw access remained denied in all 25, with zero request failures.

Feedback follow-up: local voice-episode rows survive soft identity loss in single-user mode, while export cancellation, privileged-context clearing and the pre-existing correction gate remain. Explicit pause and consent revocation still clear rows. The native panel disables corrections without the action gate and clears restricted episodes/images on mode-off or the first failed poll. Synthetic full Python suite 911/911, panel 282/282, and installed focused regressions 3/3 pass. Installed sources and panel match the tested artifacts. Metadata-only installed status: single-user mode and history visible, raw access denied; fresh sensing and call safety true at the sampled instant. Voice remains disabled by the owner's existing setting; no actual speech, media or transcript contents were inspected.

# Dictation boundary verification — 2026-10-28

MarkView BUG-011 records the owner's report. Synthetic tests cover a pre-start recognition already in flight, a queued old phrase, sample clipping at both service-command boundaries, explicit stop, silence, the text limit and interruption. Focused tests pass 30/30 both from source and against the installed package. Installed only `dictation.py` and `listening.py`, backed up at `~/.local/share/butler-ambient/release-backups/dictation-boundary-20261029T025926Z`, and restarted only the Butler service. Metadata-only status reports history visible and the microphone healthy. No real audio or transcript content was inspected; a real spoken check by the owner is still needed. Boundaries use service command receipt and one-second audio-block timestamps, so UI command latency and capture timestamp jitter limit accuracy at the physical button press.

The final full source suite passed 914/914 after this change. The first standalone Swift executable layout run failed the two bundle-resource checks because it was outside an app bundle; the same freshly compiled executable passed all 282/282 checks inside a temporary app bundle with the installed resources.

# Single-user feedback restart verification — 2026-10-28

Saved dialogue-feedback episodes previously vanished after a restart when the automatic owner session was closed. A separate bounded read-only display cache now restores them in explicitly enabled single-user mode, subject to active owner consent, archive-on and no pause. It reads only unexpired text, excludes deleted rows, does not run retention or populate agent context, memory fragments or correction targets, and clears on a hard close. Synthetic source and installed recordings/feedback regressions pass 44/44; feedback learning tests pass 20/20. Installed only feedback.py and a metadata-only service status field; backup `~/.local/share/butler-ambient/release-backups/local-feedback-restore-20261029T030702Z`. After the Butler-only restart, metadata reports local feedback restoration, history visible, and raw access denied. No actual episode, recording or transcript contents were inspected.

Final full Python suite after the feedback restart fix: 917/917. All 13 changed runtime Python modules match their source hashes, and the installed panel matches the previously tested panel binary; the panel source matches the prior build manifest.

# Single-user local rule browsing — 2026-10-28

`RuleStore.scan()` performs deletion cleanup, so the owner-local rule list uses a separate nonmutating reader that filters tombstones and preserves duplicate and symlink validation. It checks the existing vault source grant before and after reading and checks the local access policy again before returning; rule mutations remain behind the pre-existing dialogue gate. The panel disables mutation buttons in the resulting read-only view. Synthetic tests confirm tombstoned files and history remain present, mode-off, pause, archive-off, consent revocation and source denial reject the local list, and access changing during the read aborts it. Feedback suite 22/22 from both source and installed package; freshly built bundled panel 284/284. Scoped install backup `~/.local/share/butler-ambient/release-backups/local-rules-20261029T031258Z`; only Butler was restarted. Metadata-only live status reports single-user mode, history and local feedback restoration active and raw access denied. No real rule contents were inspected.

Final full ambient Python suite after local-rule browsing: 919/919. All 13 changed Python modules match their installed counterparts; the installed panel matches the just-tested 284/284 binary and the Butler panel process is running. Actual rule content and live dictated speech were not inspected.

# Panel snapshot permission recheck — 2026-10-28

Synthetic tests simulate strict raw permission expiring while the gallery snapshot is assembled, and archive being switched off during assembly. They verify raw paths and images are removed before the response while independently authorized history remains, and history/feedback disappear on a hard close. Recordings regressions pass 46/46 from source and installed package. Installed only local_ui.py with backup `~/.local/share/butler-ambient/release-backups/snapshot-recheck-20261029T031641Z`, then restarted Butler. Ten metadata-only status polls: 10/10 single-user and history visible, 10/10 local feedback restored, 0/10 raw access allowed, zero errors. Read-only database counts: 50,210 observations, 643 recording_transcripts, one active and zero inactive agreements. No stored content was inspected.

Final full ambient suite after the response-boundary fix: 921/921. Panel source is unchanged from the bundled 284/284 check.

# Local rule-dialog scope — 2026-10-28

The panel tracks the rule-list request and current modal, discards stale replies, and closes the modal when access is lost. A timer runs in the modal event-loop mode so state polling continues during the dialog. The synthetic bundled panel passes 289/289 checks, including an actually running synthetic modal closed by a failed poll, mode-specific access and a stale response. The full Python suite remains 921/921 from the immediately preceding change; no Python source changed. Installed only the tested panel, backup `~/.local/share/butler-ambient/release-backups/rule-scope-panel-20261029T032258Z`, and restarted Butler. The installed panel hash matches the tested binary, the panel process runs, and metadata reports local history visible and raw access denied. Actual rule contents were not inspected.

The final panel follow-up separately requires the dialogue lease for an editable rule dialog, so a soft identity failure closes it rather than leaving mutation controls active; read-only reopening remains available. The final bundled panel passes 290/290, including that transition, and matches the installed running binary. Backup `~/.local/share/butler-ambient/release-backups/rule-scope-panel-v4-20261029T032433Z`. Metadata reports history visible and raw access denied even while the face detector reports multiple faces; real owner speech and rule content were not inspected.

# Playback generation regression — 2026-10-28

A synthetic conversion test independently closes and reacquires the capture and dialogue permission during conversion. Both permissions are active again at return, yet each changed generation correctly rejects playback. It uses only a synthetic temporary file and mocked conversion, and passes 47/47 recordings tests both from source and against the installed package. This changes tests and documentation only.

Final full ambient Python suite including both playback-generation races: 922/922. No production code or installed runtime changed in this test-only follow-up.

# Stop during panel response — 2026-10-28

A synthetic regression exposed that `stop_event` can be set outside the service lock while a UI response is assembled, leaving live preview and recording status in that response. The regression failed on the previous code and passes after the fix, which skips preview processing when stop is already set and suppresses preview and recording permission again before returning. Source and installed recordings suites pass 48/48. Only local_ui.py was installed, with backup `~/.local/share/butler-ambient/release-backups/stop-snapshot-20261029T033023Z`; Butler was restarted. Metadata-only status confirms single-user history visible and raw access denied. No real audio, media or transcript content was inspected.

# Stop during playback conversion — 2026-10-28

The stop event is set before the service can acquire its lock and invalidate the gates. A synthetic converter reproduced a stale playable path during that gap. The stricter raw gate now also denies as soon as the stop event is set; the new regression was red before the fix and source and installed recordings tests pass 49/49. Only recordings.py was installed, with backup `~/.local/share/butler-ambient/release-backups/stop-playback-20261029T033214Z`. Metadata-only live status after restart shows local history visible and raw access denied, including with a multiple-face classification. No real media or transcript content was inspected.

Final full ambient suite after the stop-boundary fixes: 924/924. The panel remains the installed, tested 290/290 binary.

# Preserve history across raw-lease loss — 2026-10-28

A native red/green test found that the previous panel cleared the Recordings rows and selected transcript on strict raw lease loss before reloading them. When only raw permission drops, the panel now preserves rows, expansion and selection; it immediately stops the player, removes media/image paths, disables raw actions and invalidates pending replies. Raw permission recovery triggers a refresh; hard-close and first-failed-poll cache clearing remain. The test failed on the previous code and the final bundled panel passes 291/291. Python source is unchanged from the full 924/924 run. The tested panel is installed and running, hash matched, with backup `~/.local/share/butler-ambient/release-backups/history-preserve-panel-20261029T033850Z`. Live metadata reports history visible and raw access denied, including when a multiple-face classification was present. The Recordings AVPlayer open implementation remains in place. No real recording or transcript content was inspected.

# Late endpointer and dictation — 2026-10-28

A synthetic race held a pre-start Smart Turn endpointer open while dictation started. Its old callback previously restored pre-start PCM into pending_endpoint, from which the next dictated phrase could consume it. The test failed on the previous code and passes with a generation and current-mode check; the next synthetic dictated phrase goes to the field alone. Dictation tests pass 31/31 from source and the installed package, listening tests 37/37. Only listening.py was installed, with backup `~/.local/share/butler-ambient/release-backups/dictation-endpoint-20261029T034156Z`, and Butler was restarted. Metadata-only status reports history visible and raw access denied. Service command receipt and one-second capture timestamps still limit accuracy at the physical click; real speech was not inspected.

Final full ambient suite after the late-endpointer fix: 925/925; panel unchanged since 291/291.

# Owner-local access audit — 2026-10-28

The owner-selected mode changes local visibility without marking identity as verified. The following remaining distinctions were reviewed in source against DEC-097:

| Surface | Policy and evidence |
| --- | --- |
| Recordings and Observations | `history_visible` requires an enabled service, active owner agreement, archive on and no stop; single-user mode bypasses transient automatic identity failure. Owner pause, archive off and any explicit consent revocation close that scope. `test_single_user_history_does_not_grant_raw_access_and_pause_or_revocation_hide_it`, `test_revoking_guest_consent_disables_single_user_mode`, `test_single_user_observation_command_returns_facts_without_frame`. |
| Originals, frames and playback | `_allowed` still requires the capture and dialogue gates and no stop. Playback rechecks both generations after conversion. Tests cover denied paths, symlink escape, stop and close/reopen races. Native caches clear originals while retaining independently permitted history. |
| Local voice navigation and summary | Navigation is intercepted locally before agent dispatch without assigning owner identity. The deterministic local summary additionally requires healthy fresh participant sensing and safe call state; stop, mode/scope change, detected company and call-state loss invalidate pending output. Synthetic tests cover these paths. Actual owner speech was not inspected or exercised; voice was already disabled in preferences and remains disabled. |
| Feedback and rules | Saved feedback is restored into a bounded read-only local cache without restoring private agent context. Rules can be viewed by a nonmutating source-grant-checked reader; corrections and rule mutations retain the stricter gate because they can repair or dispatch work or publish agent context. |
| Transmission and identity | Existing MCP observation tools withhold facts/transcripts; the single-user summary is local and bypasses the agent. No owner biometric success, new cloud permission, or external delivery is granted by the mode. Existing panel chat and protected actions retain their separate behavior. |

Final current-state check: all 13 modified Python modules match the installed package, and the installed running panel matches the 291/291 tested binary. The last complete Python suite passed 925/925. Metadata-only database counts were 50,291 observations and 645 recording-transcript rows, with one active agreement; status reported single-user and history enabled, raw access denied. No actual media or transcript contents were inspected. BUG-010 and accepted DEC-097 are recorded in MarkView; BUG-011 tracks the separately requested dictation fix. The real spoken dictation result remains unconfirmed and its physical-click boundary is subject to service-command and capture timestamp latency.

# Recordings content opening — 2026-10-28

Boris reported that a populated list did not reveal content on double-click (MarkView BUG-012). Two synthetic tests reproduced empty child pages when the session anchor changed as pending media became an observation or an earlier observation joined the same session; a third covers a transcript becoming linked. A source-grant-neutral results-only filter defaults the panel to observations with facts or transcripts, with a toggle for all records. One selection expands the session and shows its first result; asynchronous loading, pause during load and periodic refresh are tested. Results-only skips file checks for unrelated pending media. Source and installed recordings tests pass 53/53; bundled panel 297/297. A synthetic 18,000-observation / 2,200-gap benchmark returned the first page in 0.109 s on this machine. Metadata-only counts for the selected day: 3,322 observations with facts, 653 transcript rows, 9,613 pending originals. No real content was read. Installed only recordings.py and the tested panel; backup `~/.local/share/butler-ambient/release-backups/recordings-open-20261029T041624Z`, Butler restarted. Owner live confirmation remains open.

Final full ambient Python suite after the content-opening fix: 929/929. The installed recordings.py and panel match the tested artifacts; live owner confirmation remains open.

# Stable selection and on-demand summaries — 2026-10-28

After the owner reported that the detail opened but refreshing lost its selection, two native red/green checks covered background polling and raw-permission recovery. The 299/299 panel was installed with backup `~/.local/share/butler-ambient/release-backups/selection-fix-panel-20261029T042435Z`. Hard scope loss and failed polls still clear sensitive views.

The owner also reported that long histories lacked a useful summary (MarkView BUG-013). Added owner-requested session, recordings-day and observation-day summaries. They read bounded locally stored derived facts/text and invoke the checked loopback-only dialogue model; they do not send to an agent or external channel or save summary text. Input above 5,000 items or 400,000 characters is marked partial. Synthetic tests cover chunking, session/day inputs, missing data, transient identity loss, pause, archive-off, consent revocation and cancellation. Source and installed summary tests pass 13/13; bundled native panel 300/300. A real local-model request with synthetic text returned the expected structured fields. Installed only the new module, service.py, cli.py and the tested panel, with backup `~/.local/share/butler-ambient/release-backups/local-summaries-20261029T043138Z`; restarted only Butler. A live request for the empty date 1900-01-01 completed and cancelled; status metadata shows history visible and raw access denied. No real transcript, audio or media content was inspected. The owner must assess real summary usefulness, and generation may take time on large histories.

Final full ambient suite: 942/942 (`.venv/bin/python -m unittest discover -s tests -v`). `git diff --check` passed.

# Summary progress and image visibility — 2026-10-28

After the above diagnosis, Boris clarified that the empty view is the separate «Изображения» page, and explicitly chose local display of saved camera/screen frames without automatic identity confirmation. MarkView DEC-098 records this narrow change to DEC-097. The gallery now includes up to 200 preserved still-image files under the `preserved` root, without exposing raw-only files, audio/video, Finder access or deletion through this new route. Symlinks and outside paths are rejected. Owner pause, archive off, stop, consent revocation, mode-off with the strict gate closed and failed UI polls clear access and cached frames; a scope serial rejects a stale response following a local close. Synthetic source and installed frame tests pass 6/6, summary tests 16/16, recordings regressions 53/53, and the bundled native panel passes 306/306, including frame selection across refresh. Only local_ui.py, history_summary.py and the panel were installed, with backup `~/.local/share/butler-ambient/release-backups/stored-frames-20261029T045354Z`. After the Butler-only restart, metadata reports 109 eligible camera stills, local history visible and strict raw access false. No actual image, audio or transcript content was inspected. A later summary-model failure now returns the first completed fragment with an explicit interruption warning instead of hiding it.

Full ambient suite: 951/951. After a defensive error-metadata check was tightened, the 16/16 focused summary tests were rerun and the installed module matched the source. `git diff --check` passed.

The final panel-only follow-up replaces an English internal status with a Russian explanation when saved frames are shown. Bundled panel remains 306/306; installed binary backup `~/.local/share/butler-ambient/release-backups/frame-label-20261029T045555Z`. The already-running panel has the functional gallery; this label takes effect on its next start, without disrupting the owner's current verification.

An end-to-end synthetic run using the installed package, an isolated temporary database, one synthetic transcript and the real local model returned a ready structured summary with one transcript. This does not establish usefulness on the owner's actual records.

The owner reported no useful summary and no images. A synthetic 8,000-character local model request took 9.7 seconds, so a multi-chunk day can wait minutes for the old final-only display. The first completed fragment is now shown in the detail pane as an unfinished preview; final truncation has a distinct warning. Pause hides the preview and cancellation prevents its return. Focused synthetic source and installed tests 15/15; native bundled panel 302/302. Installed only history_summary.py and the panel, backup `~/.local/share/butler-ambient/release-backups/summary-progress-20261029T044644Z`, then restarted Butler. The panel now reports when raw-file permission hides images. Read-only metadata counted 109 preserved camera/screen media rows; raw permission was false. No image, audio or transcript contents were inspected, and no image access permission was changed.

## Display delivery and scheduled advice — prepared, not installed (2026-10-30)

Read-only metadata showed the October 29 daily summary was delivered through the existing core/Telegram route at approximately 20:03, while the ambient advice ledger had no advice deliveries. Source review and synthetic regressions found that advice could be skipped when archive analysis returned after a different source failed or yielded to fresh capture. The prepared change shows the built-in daily summary and advice as Russian-language Display cards, without speech. Their independent Telegram switches default off; optional Telegram uses the existing transport, requires the owner gate, and records an unknown outcome without a blind retry. Local single-user history can supply a Display summary without widening Telegram permission. The confirmed voice schedule command can request these two switches. External OpenClaw cron jobs retain their existing routes and are not covered by these switches.

Synthetic source tests: native panel 307/307; feature 63/63; archive 29/29; management 33/33; voice commands 17/17; focused general-advice 4/4. The full isolated Python suite passed 961 tests with 15 skipped before the final cancellation-guard and voice-command additions; their focused suites passed after those edits. Native build used `swiftc -warnings-as-errors` into `/tmp/butler-schedule-fix`. No live Display commands, delivery, installation or restart were performed. Owner confirmation of real Display results remains open.

Installed locally as version 0.12.0 on 2026-10-30. The package, wheel metadata, service status and signed app bundle agree on 0.12.0; the running panel inode matches the installed executable. The exact signed panel passed 307/307 synthetic layout checks. Full isolated Python suite before the version-only change: 964 tests, 15 skipped; installed-package display 12/12 and management 33/33, wheel feature 63/63 and archive 29/29. The daily and advice schedules report enabled with Telegram off by default. Backup: `~/.local/share/butler-ambient/release-backups/display-schedules-20261030T113112Z`, including the previous 0.11.3 package and bundle. No actual Telegram delivery or Display output was triggered during verification. Other OpenClaw cron routes remain unchanged. `pip check` reports three inherited dependency metadata conflicts in the existing system-site environment; the no-dependencies wheel reinstall did not change those packages, and `webrtcvad`, OpenCV and NumPy import successfully.

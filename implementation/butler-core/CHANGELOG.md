# Changelog — butler-core

Semantic versioning per package (DEC-040). Each entry names the migration wave and the decisions it implements.

## 0.17.0 — 2026-10-27 (Back Burner nightly research)

- Add the `backburner.cycle` operation: one tracked, detached night cycle (adapter `backburner`) that runs every due research folder through `lib.backburner`, with the identity check, stop-on-cancel and no-restart rules of the dispatcher.
- Schedule it once per local day between the configured start and last-start times from the new `backburner` configuration section (`time`, `last_start`, `zone`, `max_runs`); the schedule is passed on to the cycle. The scenario switch `backburner` ships OFF.
- Raise alarms `backburner:cycle` (a failed or interrupted cycle) and `backburner:folders` (folders failing three nights in a row); each clears on the next good cycle.
- Hold the harness's `research` notes outside quiet hours with a daily cap (delivery initiative kind `research`).
- Workspace harness only, installed without a core release (DEC-006): a run that speaks also shows one card on the Butler display through the ambient service socket, redrawing the folder's earlier card (`replaces`) and shown again as a new card when the display has forgotten it. Text the worker wrote is escaped so it cannot become a link or other markup on the display. A display that is down or refuses the card is recorded as `display_error` and never fails the run; the Telegram note is still sent.

## 0.16.0 — 2026-10-26 (voice dialogue support)

- Extend metadata-only feedback and learning receipts to bind voice turns, corrections, decisions and rule revisions.
- Keep transcript-derived private material subject to the established retention and workspace boundaries; publish canonical rules through the existing rule store.
- Bound and stage semantic-index rebuilds so an incomplete rebuild cannot replace the last complete generation.

## 0.14.1 — 2026-10-26

- Bound semantic-index windows even for a multi-megabyte single paragraph, preserve source coverage/line references, and limit repeated heading metadata in embedding inputs.
- Rebuild the eligible-vault index in private staging storage before publishing a complete generation.

## 0.14.0 — 2026-10-25 (conversation feedback)

- Add canonical vault rules, stable revisions, idempotent write receipts, generated read-only lessons, scope selection and deletion manifests (DEC-071).
- Separate expiring private episode text from backup-safe decision, annotation, application and evaluation metadata. Keep no feedback audio.
- Evaluate false interventions and missed addresses separately, reject session leakage and unmeasured activation, and record reversible activation and rollback.
- System 3 requires application evidence for compliance; repeated complaints alone remain unknown. Export summaries omit identifying references and private text.
- Align System 3 with the background runner's 8K context, bound generated output, and defer/cancel analysis for live speech or Full Pause without losing its retry schedule.

## 0.12.0 — 2026-10-24 (W11, System 3)

- System 3 (REQ-027): every six hours, never during a Full Pause, the core asks the local model why the owner reacted (pause, «не мне», rejected approval, silence request, complaint…) using metadata of the preceding window only, and keeps the answer as a lesson candidate with provenance and confidence; repeated signals merge into one candidate. An unclear cause earns the owner one short question (at most two a week, outside quiet hours); the answer is evidence. Confirmed lessons are exported into the persona's `butler-lesson` store (DEC-058) and removed when retired; a daily compliance check counts the same signal after confirmation and raises an alarm and an owner notice at three. `butler-core lessons list|get|confirm|retire|delete|correct|answer|analyse`. De-identified summaries are kept for System 2 clustering (DEC-059). Feedback kinds gain `consent_revoked`, `silence_request` and `not_addressed`.
- ConfigVersion 11: the `system3` switch on.

## 0.11.0 — 2026-10-24 (W10, part 1)

- System 1 shadow runs on the local `s1-llm-auto-router` ONNX model (DEC-057): every message routed to System 2 is also classified locally (category and difficulty, a few milliseconds on the CPU) and the prediction is recorded next to the actual route; it never acts. `doctor` shows the model's health and the shadow report.
- `butler-core admin restore <empty dir> --state <scratch>`: the timed disk-loss drill from the newest Storage0 generation (REQ-025).

## 0.10.0 — 2026-10-24 (W9)

- `github.pr` (persona tetyana-dyck, export repository→github): the workspace's new `tetyana-pr` wrapper pushes the feature branch, opens the pull request and arms squash auto-merge as one operation keyed by branch and head commit; a lost answer is settled from GitHub's pull request list before any retry, so no second pull request is opened. The wrapper enforces the hard invariants (feature/chore/fix branches only, never main, never --admin). ConfigVersion 10: the `github` switch on.

## 0.9.0 — 2026-10-24 (W8)

- ConfigVersion 9: the `observation` and `voice_tasks` switches on. The ambient module (0.3.1) reports every released observation as an ingest event under a stable key (source, span, fact counts and kinds; never the facts) and the owner's signals — full pause, resume, revoked consent, silence request — as feedback events, through a local outbox that survives a core outage. Owner-confirmed voice jobs now start through the core.

## 0.8.0 — 2026-10-24 (W7)

- ConfigVersion 8: the `ingest` switch on. The workspace inbox buffer (userbot, WhatsApp, `butler-inbox add`) admits every buffered message to the core's ingest store under its stable message key as a `raw_local_only` event with metadata only (platform, chat, size, direction); the text stays in the local buffer. A core that does not answer leaves the row marked unadmitted and the next buffer or `pending` call catches up; processed messages are recorded as `message.processed`.

## 0.7.0 — 2026-10-24 (W6)

- Operation rules may name `sources`; submission checks each at the search boundary (REQ-023), so a persona reads or writes only the sources it is scoped to.
- `vault.write` and `vault.write_confirmed` apply a write the workspace's vault library decided on: the content lives in a spec file whose checksum the operation and the owner's approval carry; the core runs `lib.vault core-apply` (policy, schema, snapshot and audit stay in the library) and settles a lost answer with `core-check` before any retry.
- `knowledge.recall` records a search before it runs; the `butler-recall` output names each hit's provenance (canonical owner statement, human note, model inference, session transcript, semantic match).
- ConfigVersion 7: the `vault` and `knowledge` switches on.

## 0.6.0 — 2026-10-24 (email digest and approved replies)

- The core schedules `mail.scan` at the configured local times (`mail.times`, default 08:30 and 17:30 America/Chicago), one operation per slot; the workspace's `butler-mail` reads every Mail.app account, classifies locally and returns the digest, which the core delivers as a `digest` initiative message. A reading that fails raises the `mail:reading` alarm; the next slot clears it.
- `mail.reply` needs the owner's bound approval; the send goes through Mail.app's reply from the receiving account and a lost outcome is settled from the Sent mailbox (10-minute grace) before any retry.
- Configuration gains a `mail` section; ConfigVersion 6 turns the `mail` switch on.

## 0.5.0 — 2026-10-24 (W5)

- YouTube uploads are core operations (`youtube.publish_news` under the standing authorization, `youtube.publish` with the owner's bound approval, DEC-064). The core runs the workspace's `butler-youtube` with the persona's own credentials directory; a lost outcome is settled from the channel's uploads (`butler-youtube find`) before any retry, so a video is never uploaded twice. Bamberg News publishes through the same path with its own credentials.
- Export operations (`export.narration.openai`, `export.narration.edge`, `export.voice-note`) record the policy decision before narration or a voice note leaves the machine; `news_video` renders with the offline macOS voice when the export is refused or the core is unreachable, and `transcribe-via-openai` sends nothing without the record.
- ConfigVersion 5: the `youtube` and `exports` switches on.

## 0.4.0 — 2026-10-24 (W4)

- Todoist writes are core operations through the `todoist` adapter: create, update, close, reopen, comment, delete and bulk import. Each runs as a Sync command whose uuid derives from the operation id, so Todoist never executes it twice; an unknown outcome is reconciled from the task itself (found by content and placement, by state, by absence, by comment text).
- Delete and bulk import need the owner's approval; the Butler task library reports the pending approval as one line and never repeats the request.
- ConfigVersion 4: the `todoist` switch on.

## 0.3.1 — 2026-10-24

- `delivery.list` includes each delivery's text, so the owner panel can show the notices Butler sent.

## 0.3.0 — 2026-10-24 (W3)

- Owner notices from any local program go through core delivery: `notify` on the socket, `butler-core notify` and `Client.notify`. Status lines are silent; quiet hours, initiative limits and the no-blind-repeat rule apply to every notice (REQ-024).
- A delivery may carry a file: the file is copied into private state at enqueue and removed when the delivery ends. The core sends it through the Telegram Bot API with the account's own token, resolved at send time and never stored (DEC-063).
- `delivery.import` (terminal only) and `delivery.list`; the deployment imports the gateway's failed queue read-only, refusals as `failed`, everything else as `uncertain`.
- `butler-core switched <scenario>` reads the switch file offline, for shell callers.
- The bridge's 60-second configuration refresh counts as its liveness signal, so `doctor` no longer reports a stale bridge between messages.
- ConfigVersion 3: the `delivery` switch on.

## 0.2.0 — 2026-10-24 (W2)

- Coding dispatches (`dispatch.codex`, `dispatch.claude`) and owner-confirmed voice jobs (`voice.codex`, `voice.claude`) are tracked operations through the `dispatcher` and `voice-tasks` adapters. The caller-chosen job id makes retries, reconciliation and recovery find the existing job instead of starting a second worker.
- A worker that ends without a result is reported to the owner as interrupted and never restarted automatically; stopping a worker kills only a process whose command line still names the job and reports partial changes.
- Workers start with a minimal environment and receive no credentials from the core.
- The switch file `switches.json` is written on every activation and at start, so a switched CLI knows its path while the daemon is down; switched CLIs wait up to 30 s for the core and then refuse (DEC-061).
- `operation.get` finds an operation by its idempotency key; `admin activate` leaves an unchanged configuration alone (DEC-060).
- ConfigVersion 2: W2 operations and grants, the `dispatch` switch on, `voice_tasks` off until the ambient launcher ships, Bamberg News export grants (DEC-062).

## 0.1.0 — 2026-10-24 (W0, W1)

- Core daemon, contracts v1, store, admission, tasks, operations, policy, approvals, delivery, configuration, diagnostics, Storage0 backup, cron accounting, System 3 feedback capture, socket API, CLI and the `butler-core-bridge` 0.1.0 gateway plugin.

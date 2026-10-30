# Verification — Butler modular core and reliable tasks

Three kinds of evidence are kept apart: **controlled** (suites run by `verify.py`, results and hashes in
`evidence/results.json`), **installed** (checks against the deployed system on this Mac), and **live** (real
owner traffic, metadata only). A criterion is marked ✓ only where evidence exists; ✗ names what is missing.

Versions on 2026-10-24: butler-core 0.12.0 (ConfigVersion 11), butler-core-bridge 0.1.0, butler-ambient 0.3.2.
Switches on: chat, cron, dispatch, delivery, todoist, youtube, exports, mail, vault, knowledge, ingest, observation,
voice_tasks, github, system3.

## Controlled suites (`python3 verify.py`)

| Suite | Interpreter | Tests | Result |
|---|---|---|---|
| butler-core unit, contract, compatibility | system python 3.12 | 197 (2 skipped: router model lives in the runtime venv) | PASS |
| butler-core System 1 (router model) | runtime venv | 4 | PASS |
| bridge plugin (`node --test`) | node 22 | 9 | PASS |
| butler-ambient | ambient venv | 460 | PASS |
| workspace-butler (news-video, research dedup) | miniconda python | 25 | PASS |

Compatibility harnesses run the **installed** workspace libraries (`lib/dispatch.py`, `lib/tasksync.py`,
`bin/butler-youtube`, `lib/news_video.py`, `lib/vault.py`, `lib/memory.py`, `lib/inbox.py`, `lib/mail.py`,
`bin/tetyana-pr`) copied into temporary workspaces with only paths, binaries and tokens pointed elsewhere.

## Requirements

### REQ-021 Modular core with explicit contracts
- ✓ Contracts v1 (event, task, operation, policy decision, approval, delivery, config version) with validation and
  retained examples — controlled: `test_config_store.py`, `test_admission.py`.
- ✓ Module boundaries enforced by an AST import test — controlled: `test_boundaries.py` (every module has a rule).
- ✓ Adapters depend only on `contracts` and `adapters.base` — controlled: `test_boundaries.py`.

### REQ-022 Durable task execution and recovery
- ✓ Receipt after durable persistence; duplicate keys return the original — controlled: `test_admission.py`,
  `test_operations.py`; the bridge admits before dispatch (`bridge.test.js`).
- ✓ Crash drills (`kill -9` between admission and receipt, between dispatch and receipt, during cancel) keep
  accounting — controlled: `test_runtime.py`, `test_dispatch.py`.
- ✓ Lost responses reconciled from the external system (Todoist by content/state/absence, YouTube by channel listing,
  vault by content, GitHub by pull-request list, mail by Sent mailbox); unknown outcomes block repetition until the
  owner resolves — controlled: `test_todoist.py`, `test_media.py`, `test_knowledge.py`, `test_github.py`, `test_mail.py`.
- ✓ Resolution is recorded apart from automatic verification and never dispatches — controlled: `test_operations.py`.
- ✓ Bounded retries; cancel_wait / stop_executor / effect_done distinguished; a stopped worker reports partial changes —
  controlled: `test_operations.py`, `test_dispatch.py`.
- ✓ Storage exhaustion rejected explicitly (`max_page_count` drill) — controlled: `test_runtime.py`.
- ✓ Installed: `recovery.accounting` p95 30 ms over 15 restarts today; every deploy restarts the daemon with
  `core.recovered` recorded.

### REQ-023 Authorization, approvals and grounded knowledge
- ✓ Cross-persona isolation at search, export, execute and disclose boundaries — controlled: `test_policy.py`,
  `test_knowledge.py` (tetyana-dyck denied vault and recall), `test_github.py` (butler denied `github.pr`).
- ✓ Raw categories never exportable; narration and voice notes recorded as exports before leaving; refused export →
  offline voice — controlled: `test_media.py`, `test_narration_export.py`.
- ✓ Approvals bound to parameters (hash), approver and TTL, single use; changed spec refused — controlled:
  `test_operations.py`, `test_knowledge.py` (tampered checksum), `test_todoist.py` (delete), `test_youtube_cli.py`.
- ✓ Revocation blocks later execution and disclosure — controlled: `test_policy.py`, `test_delivery.py`.
- ✓ Provenance on every recall hit; model inference distinguished from canonical facts — controlled:
  `test_vault_workspace.py`.

### REQ-024 Interfaces, status and delivery
- ✓ Delivery tracked apart from execution; bounded retries; uncertain never retransmitted; quiet hours and initiative
  limits — controlled: `test_delivery.py`. Installed: the gateway's 36 stuck entries imported read-only (30 uncertain,
  6 failed), never resent.
- ✓ Owner notices, status lines and the tasks board on one path; files via the Bot API with the account token
  resolved at send time — controlled: `test_delivery.py` (attachments), `test_runtime.py` (`notify`).
- ✓ Interrupted chat turns and lost workers reported, never re-run — controlled: `test_dispatch.py`; installed:
  `butler-dispatch` shows `interrupted` instead of running forever.
- ✓ Live: 10 deliveries delivered through the core today (5 status lines from dispatched workers, digests), 7 agent
  replies observed; `delivery.send` p50 12.0 s / p95 20.2 s (the gateway CLI round trip).
- ✗ Live 👌 receipt latency: no owner Telegram message has been admitted since the bridge went in (0 samples);
  measured when the owner next writes to the bot.
- System 1 shadow (DEC-047): the router model loads and answers in the installed venv (controlled: `test_system1.py`,
  4 tests, 4.5 ms per prediction; installed: doctor shows the model available). It observes only admitted owner chat
  messages routed to System 2, so it has ✗ 0 live samples for the same reason as the receipt latency above.

### REQ-025 Operations, timing and recovery
- ✓ Config activation with preview and rollback; offline activation by deploy as `deploy:offline` (DEC-060) —
  controlled: `test_config_store.py`, `test_runtime.py`; installed: 11 ConfigVersions activated today, each diff
  recorded as paths only.
- ✓ Bounded queues and workers; watchdog for stalled executors; dialogue never waits on an executor — controlled:
  `test_runtime.py`, `test_operations.py`.
- ✓ Ready background tasks start within 60 s — installed: `task.start` p50 21 ms / p95 156 ms (6 samples).
- ✓ Observation events reach the core within 60 s of release — controlled: `test_core_link.py` (outbox, order,
  refusal handling); live: 98 ambient observation events in the first hour after W8.
- ✓ Backup every 6 h to Storage0 without secrets; staleness alarm at 12 h — installed: `backup.completed`, newest
  generation 4.2 h old at the drill.
- ✓ Disk-loss drill: `butler-core admin restore` of gen-20261024T133928Z into an empty root — **24,760 files,
  25.4 s, 0 corrupt, task accounting readable (113 tasks)**; the bound is ≤ 8 h after hardware and backup are available.
- ✓ Diagnostics correlated and redacted; secrets and transcripts excluded — controlled: `test_runtime.py`,
  `test_dispatch.py` (workers never inherit the core environment).

### REQ-026 Scenario migration with switches and rollback
- ✓ Every inventoried scenario migrated behind a ConfigVersion switch with compatibility evidence: W1 chat and cron,
  W2 dispatch and voice jobs, W3 delivery, W4 Todoist, W5 YouTube/TTS/whisper, W6 vault and recall, W7 ingest,
  W8 ambient events and voice jobs, W9 Tatyana's PRs; switched CLIs refuse without the core (DEC-061) and keep their
  legacy path when the switch is off — every harness has a switch-off scene.
- ✓ Rollback is `config rollback` (controlled: `test_config_store.py`); recorded effects are never repeated.
- ✓ Integrated failure matrix: duplicate input (every harness), restart (`test_runtime.py`), timeout
  (`test_operations.py`, `test_media.py`), lost receipt (`test_todoist.py`, `test_media.py`, `test_github.py`,
  `test_mail.py`), disk exhaustion (`test_runtime.py`), delivery failure (`test_delivery.py`).
- Live evidence so far: 2 real `todoist.create`, 1 real `dispatch.claude`, 98 ambient observations, 10 deliveries,
  36 imported gateway entries, first email reading (148 messages, 8 important). Pending: first real YouTube upload,
  vault write, Tatyana PR, mail digest (17:30 slot) — all recorded as metadata when they occur.

### REQ-027 System 3 — slow learning from owner feedback
- ✓ Signals recorded with the preceding window (deliveries and operations, metadata) — controlled:
  `test_runtime.py` (ambient reports metadata only), `test_core_link.py`.
- ✓ Slow local analysis (every 6 h, never during Full Pause) assigns a cause from the fixed taxonomy with confidence
  and source events; candidates merge — controlled: `test_system3.py`.
- ✓ Lessons with provenance, confidence and status; confirmed lessons exported to the persona's `butler-lesson` store
  (DEC-058), retired ones removed — controlled: `test_system3.py`.
- ✓ Unclear cause → one short owner question, at most two a week, outside quiet hours and Full Pause; the answer is
  evidence — controlled: `test_system3.py`.
- ✓ Daily compliance check; alarm and owner notice at three violations — controlled: `test_system3.py`.
- ✓ Owner commands list/confirm/correct/retire/delete — controlled + installed (`butler-core lessons`).
- ✓ Never acts on the outside world — controlled: no operation is created by System 3.
- ✗ Live: no lesson yet (the first analysis window runs six hours after deployment; the first signals arrive from
  the ambient module).

## Load profile and timing boundaries

- Admission: the bridge waits ≤ 1.2 s for the core per message; one gateway, two managed bot accounts, bursts of up to
  10 messages per minute observed in the gateway's failed queue history.
- Executors: dispatcher 2, voice-tasks 1, delivery 2, todoist 1, youtube 1, mail 1, vault 1, knowledge 2, github 1,
  export 2, periodic 6, backup 1, reconcile 2 (ConfigVersion 11 `limits.workers`).
- Timing boundaries: receipt ≤ 2 s (bridge 1.2 s + reaction), background start ≤ 60 s (measured p95 156 ms),
  recovery ≤ 60 s (measured p95 30 ms), observation availability ≤ 60 s (outbox flush every 5 s), restore ≤ 8 h
  (measured 25.4 s for 3.5 GB), backup loss ≤ 24 h (6-hour cadence).
- Approval and external waits are measured apart from completion: `approval.wait` and `external.wait`
  (p50 0.39 s, p95 88 s today — the long tail is a real Claude dispatch).

## What is excluded from this goal

GitHub issues, commits, PRs, CI and merges (DEC-039), the MarkView browser check and the manual test checklist.

## Semantic reindex transaction — prepared, not installed (2026-10-30)

The current installed full reindexer commits its deletion before rebuilding. Read-only index metadata after a failed October 30 run showed two indexed files and 48 chunks. A synthetic regression failed on the previous source and passes after wrapping the rebuild in one SQLite transaction; tests also cover a skipped file and a reader seeing the old generation until commit. The workspace knowledge-source suite passed 19/19. A long rebuild holds the SQLite writer reservation; the exact cause of the owner's cron error remains unverified. The existing index was not modified or rebuilt, and this source change was not installed.

The tested workspace indexer was installed on 2026-10-30 and its source and installed hashes match; the earlier index was left untouched and the cause of the failed cron run remains unverified. The 19/19 synthetic knowledge-source checks passed. Rollback copy: `~/.local/share/butler-ambient/release-backups/display-schedules-20261030T113112Z/embeddings.py`.

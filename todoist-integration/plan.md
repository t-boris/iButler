# Implementation Plan — Todoist Integration for the AI Butler

Status: **DRAFT — awaiting Boris's approval** (no implementation begins until requirement.md +
design.md + this plan are approved)
Based on: `requirement.md`, `design.md`
Date: 2026-06-24

---

## 0. Pre-work (Boris-provided / one-time)

- [ ] Boris generates a Todoist API token (Todoist → Settings → Integrations → Developer → API token).
- [ ] Add `TODOIST_API_TOKEN=…` to `~/.openclaw/.env` (Boris pastes; never committed).
- [ ] Confirm §3.4 bucket mapping choice (butler-labels vs due-date-only) and §7 decisions in design.

---

## Phase 1 — API client (`lib/todoist.py`)  ·  no side effects on the live butler

- [ ] 1.1 Implement Bearer-token client over `https://api.todoist.com/api/v1` (stdlib urllib/json).
- [ ] 1.2 Tasks: get/get-one/create/update/close/reopen/delete + `get_completed(since,until)`.
- [ ] 1.3 Projects, sections, labels, comments read/create per design §2.
- [ ] 1.4 Cursor pagination, 429 backoff (`Retry-After`), typed `TodoistError` (token-safe messages).
- [ ] 1.5 Unit tests against recorded/fake JSON (no network in CI). 
- **Verify**: `python3 -c "from lib import todoist; print(len(todoist.get_projects()))"` lists real
  projects read-only. **Gate**: read-only calls only until Phase 3.

## Phase 2 — Sync engine (`lib/tasksync.py`) + mapping store

- [ ] 2.1 `state/todoist-map.json` schema + load/save helpers (atomic write).
- [ ] 2.2 Bucket mapping (design §3.4) — pure function, fully unit-tested.
- [ ] 2.3 Pull/reconcile: fetch → diff → rebuild `Tasks/*.md` + HTML board via `lib/vault.py`
      (read-only mirror headers). Idempotent full-state reconcile.
- [ ] 2.4 Push helpers (create/update/close/reopen/delete/move-label/comment) updating the map.
- [ ] 2.5 Unit tests: reconcile diff, idempotent double-pull, bucket edge cases.
- **Verify**: run a pull into a **scratch copy** of the vault; diff output vs current `Tasks/*.md`;
  confirm no data loss and sensible buckets. **Gate**: do not point at the real vault yet.

## Phase 3 — Migration (`bin/butler-todoist migrate`)

- [ ] 3.1 Snapshot + back up current `Tasks/*.md` to `state/` (reversible — FR-4).
- [ ] 3.2 Import legacy open tasks → Todoist (resolve/create projects, due, priority), record map.
- [ ] 3.3 Idempotency: re-run → zero duplicates (asserted by test).
- [ ] 3.4 Write `state/todoist-migration-report.md`; then full pull to rebuild the mirror.
- **Verify**: dry-run report first (`migrate --dry-run`), Boris reviews the mapping table, then run
  for real; confirm every legacy task appears once in the Todoist app. **Gate**: Boris signs off on
  the dry-run report before the real migration.

## Phase 4 — CLI + agent wiring

- [ ] 4.1 `bin/butler-todoist` (`sync`|`pull`|`migrate`|`status`), executable, `./bin/` callable.
- [ ] 4.2 Rework `bin/butler-task` (`add`/`move`/`done`/`reopen`/`delete`/`comment`) → push via tasksync.
- [ ] 4.3 `bin/butler-send-tasks` + `butler-tasks-html`: light pull before render (FR-6 parity).
- [ ] 4.4 Hook scheduled pull into the morning/daily loop (`lib/daily.py`/`bin/butler-daily`).
- [ ] 4.5 Update workspace `AGENTS.md` **and** `CLAUDE.md` with the command playbook (design §4),
      incl. mandatory-confirmation rule for destructive/bulk ops (FR-7).
- [ ] 4.6 Update `policies/safety.yaml` to gate task/project/label deletion + bulk complete.
- **Verify**: each command run manually end-to-end (see Phase 6).

## Phase 5 — Secrets, security, observability

- [ ] 5.1 Token read solely from env; grep logs to confirm it never appears (NFR-1).
- [ ] 5.2 `logs/todoist-sync.log` + audit lines for every Todoist write (no secrets).
- [ ] 5.3 `butler-doctor` check: token present + a `GET /projects` smoke call.

## Phase 6 — Acceptance verification (maps to requirement §7)

Run on the live butler and demonstrate:
- [ ] 6.1 Create / list / update / complete / reopen / delete by chat → visible in Todoist app. (AC1)
- [ ] 6.2 Project, section, label, priority, recurring due, sub-task, comment all work. (AC2)
- [ ] 6.3 Edit a task in the Todoist phone app → appears in `butler-send-tasks` after sync. (AC3)
- [ ] 6.4 Migration imported legacy tasks once + mapping report exists; mirror is read-only. (AC4)
- [ ] 6.5 HTML board / morning plan unchanged for Boris. (AC5)
- [ ] 6.6 Delete + bulk actions ask for confirmation. (AC6)
- [ ] 6.7 Token absent from logs/chat. (AC7)
- [ ] 6.8 Todoist offline → clear error, mirror intact. (AC8)

---

## Rollout & rollback

- **Order**: Phases 1–2 are inert (read-only/scratch). Phase 3 migration is the cutover point.
- **Rollback**: restore `Tasks/*.md` from the `state/` backup; revert `bin/butler-task` to the
  Markdown-master version (kept in git/history); the Todoist account is unaffected (tasks remain).
- Feature can be disabled by removing the token + reverting `butler-task` without touching Todoist.

## Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| Migration duplicates tasks | Idempotent mapping + dry-run report + Boris sign-off (Phase 3 gate). |
| Mirror rebuild loses a task | Atomic staged writes via `lib/vault.py`; backup before cutover; scratch-vault test (Phase 2). |
| Rate limits during bulk migrate | Throttle + 429 backoff; migrate is one-time. |
| Unified API field/endpoint drift | Thin client isolates all API specifics in `lib/todoist.py`; smoke check in `butler-doctor`. |
| Agent runs destructive op without confirm | Playbook rule + `policies/safety.yaml` gate (FR-7). |
| Token leakage | Env-only, log-grep check (Phase 5). |

## Estimated sequencing

Phase 1–2 (client + sync core, with tests) → Phase 3 (migration, gated) → Phase 4 (CLI + agent) →
Phase 5–6 (security + acceptance). Each phase ends in a verify gate; Phase 3 and Phase 6 require
Boris in the loop.

---

## Approval

- [ ] **Boris approves this plan** → implementation may begin at Phase 1.
- [ ] Changes requested (note below).

> Notes:

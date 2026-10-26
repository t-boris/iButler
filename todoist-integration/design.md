# Design — Todoist Integration for the AI Butler

Status: **DRAFT — awaiting Boris's approval** (do not implement yet)
Based on: `requirement.md` (also DRAFT — approve requirements first; this design assumes them)
Date: 2026-06-24

> Note: this design is presented together with `requirement.md` and `plan.md` so you can review
> the whole picture, but the intended gate order is still **requirements → design → plan**.
> If you change requirements, this design will be revised accordingly.

---

## 1. Architectural overview

Todoist is the master (D1). The butler reads Boris's natural-language intent, calls the Todoist
API to make the change, and keeps a **read-only local mirror** in the vault for the existing
"show tasks" experience. The design adds two thin Python modules + one CLI entrypoint, repurposes
`lib/tasks.py`, and teaches the agent (via its workspace `AGENTS.md`/`CLAUDE.md`) how to drive it.

```
            Telegram (NL)                     scheduled / on-demand
Boris ───────────────────▶ butler agent ───────────────────────────┐
                               │  (interprets intent, picks command)│
                               ▼                                    ▼
                         bin/butler-task            bin/butler-todoist sync
                         bin/butler-todoist             │
                               │                        │
                               ▼                        ▼
                         lib/todoist.py  ◀── API ──▶  Todoist  (api.todoist.com/api/v1)  [MASTER]
                               │                        │
                               ▼                        ▼
                         lib/tasksync.py  (map + reconcile)
                               │
                               ▼
                         lib/vault.py  (policy + audit)
                               │
                               ▼
                   Vault Tasks/*.md  +  HTML board   [READ-ONLY MIRROR]
```

### New / changed components

| Component | Type | Responsibility |
|-----------|------|----------------|
| `lib/todoist.py` | **new** | Pure Todoist API client. Auth, pagination, 429 backoff, error mapping. No business logic. |
| `lib/tasksync.py` | **new** | Sync engine: id mapping, push, pull/reconcile, mirror rebuild, migration. |
| `lib/tasks.py` | **repurposed** | Keeps `Task` dataclass + list-file rendering, but data now comes from Todoist via `tasksync`, not from authoritative vault parsing. |
| `bin/butler-todoist` | **new** | CLI: `sync`, `migrate`, `status`, `pull`, callable as `./bin/butler-todoist`. |
| `bin/butler-task` | **changed** | `add`/`move`/`done` now push to Todoist via `tasksync` instead of writing the vault as master. |
| `bin/butler-send-tasks`, `bin/butler-tasks-html` | **changed** | Refresh mirror (light pull) before rendering. |
| `lib/daily.py` / `bin/butler-daily` (morning loop) | **changed** | Trigger scheduled pull (FR-3 / D3). |
| Workspace `AGENTS.md` + `CLAUDE.md` | **changed** | Add "Todoist task management" command playbook for the agent (edit **both** — codex reads AGENTS.md, claude reads CLAUDE.md). |
| `state/todoist-map.json` | **new** | Persistent id mapping + sync metadata. |
| `~/.openclaw/.env` | **changed** | Add `TODOIST_API_TOKEN`. |

No new third-party libraries: use Python stdlib `urllib.request`/`json` (or `httpx` only if Boris
approves adding it). No changes to the `tetyana-dyck` agent or unrelated flows (NFR-5).

---

## 2. `lib/todoist.py` — API client

- **Auth**: `Authorization: Bearer $TODOIST_API_TOKEN` (read from env; never logged).
- **Base**: `https://api.todoist.com/api/v1`.
- **Functions** (thin wrappers, return parsed dicts):
  - Tasks: `get_tasks(filter…)`, `get_task(id)`, `create_task(**fields)`, `update_task(id, **fields)`,
    `close_task(id)`, `reopen_task(id)`, `delete_task(id)`, `get_completed(since, until)`.
  - Projects: `get_projects()`, `create_project(name)`.
  - Sections: `get_sections(project_id)`, `create_section(...)`.
  - Labels: `get_labels()`, `create_label(name)`.
  - Comments: `get_comments(task_id)`, `add_comment(task_id, content)`.
- **Task fields used** (D2): `content`, `description`, `project_id`, `section_id`, `parent_id`,
  `due_string` (handles recurring server-side, A4), `priority` (1–4, **4 = highest**), `labels`.
- **Pagination**: cursor-based; client transparently follows `next_cursor` to assemble full lists.
- **Resilience** (NFR-3): on HTTP 429 → exponential backoff honoring `Retry-After`; on 5xx → bounded
  retry; on other errors → raise a typed `TodoistError` carrying status + message (no token in text).
- **Idempotency** (NFR-2): the REST surface is not transactional, so idempotency is enforced one
  layer up by `tasksync` via the mapping table (never create if a live mapping exists). Optimization
  note: the unified API's Sync command protocol supports a client-supplied `uuid` for command
  idempotency — deferred (see A1 decision below).

---

## 3. `lib/tasksync.py` — sync engine

### 3.1 Mapping store — `state/todoist-map.json`
```jsonc
{
  "version": 1,
  "last_pull": "2026-06-24T08:00:00Z",
  "tasks": {
    "<todoist_id>": {
      "vault_uuid": "<uuid|null>",   // links to legacy vault id (migration + audit)
      "content": "…",                // last-seen title (for diffing / disambiguation)
      "project_id": "…",
      "list": "active",              // computed mirror bucket: inbox|active|waiting|done
      "checksum": "…",               // detect external edits
      "updated_at": "…"
    }
  },
  "projects": { "<name>": "<todoist_id>" },
  "labels":   { "<name>": "<todoist_id>" }
}
```
The map makes pull/migration idempotent (NFR-2) and lets chat references resolve to ids (FR-5).

### 3.2 Push (immediate — FR-2)
`tasksync.push_*` thin functions: call `lib/todoist`, on success update the map, then mark the
mirror dirty (a light targeted mirror update, or schedule a refresh). The conversational reply
echoes Todoist's authoritative result (e.g. the date Todoist parsed from a NL `due_string`).

### 3.3 Pull / reconcile (scheduled + on-demand — FR-3)
1. `get_tasks()` (all active) + `get_completed(last 7 days)` (see OQ1 decision below).
2. Diff against the map; compute each task's mirror **bucket** (§3.4).
3. Rebuild `Tasks/Inbox.md`, `Active.md`, `Waiting.md`, `Done.md` and the HTML board by writing
   through `lib/vault.py` (atomic staged write — NFR-2, NFR-4). Each file gets a regenerated
   header: `managed_by: todoist-mirror (READ-ONLY — edit in Todoist)`.
4. Update `last_pull` + per-task checksums.

Pull is a **full-state reconcile**, so it is idempotent by construction and self-heals drift.

### 3.4 Bucket mapping (resolves A3) — **proposed, needs approval**
Todoist stays master; the 4 local files are a butler convenience view derived from native Todoist
fields plus a small Butler-managed label namespace:

| Mirror file | Rule (first match wins) |
|-------------|-------------------------|
| `Done.md` | task completed within the last 7 days |
| `Waiting.md` | open task carrying label `butler:waiting` |
| `Active.md` | open task that is **scheduled** (has a due date) OR carries label `butler:active` |
| `Inbox.md` | any other open task (no due date, no butler workflow label) |

- `butler:active` / `butler:waiting` are auto-created labels (FR-5). "move X to waiting" =
  add/remove these labels in Todoist. This keeps Todoist authoritative while preserving the
  existing 4-view workflow. **Alternative if you prefer zero butler labels:** drive Active purely
  by "has due date", drop Waiting (fold into Inbox). Pick in approval.

### 3.5 Migration (one-time — FR-4)
`butler-todoist migrate`:
1. Read legacy vault tasks via the *current* `lib/tasks` parser (pre-cutover snapshot).
2. For each open task with no live mapping: resolve/create its project, `create_task` in Todoist
   (title, due, priority, project), record `vault_uuid ↔ todoist_id` in the map. Idempotent: a task
   already mapped is skipped, so re-running never duplicates.
3. Write a report to `state/todoist-migration-report.md` (imported, skipped, project mapping table).
4. Flip the mirror to Todoist-master mode and run a full pull (§3.3) to rebuild `Tasks/*.md`.
5. The pre-migration vault files are backed up to `state/` first (reversible — FR-4).

---

## 4. Agent command surface (how NL maps to actions — FR-1)

The LLM agent does the NL understanding; it executes via the deterministic CLI. The butler's
workspace `AGENTS.md`/`CLAUDE.md` gets a playbook section, e.g.:

| Intent (RU/EN/UK) | Command |
|-------------------|---------|
| add / напомни / создай задачу | `./bin/butler-task add [--project P] [--due "D"] [--priority N] "title"` (pushes to Todoist) |
| show / покажи задачи | `./bin/butler-send-tasks` (pulls first, sends HTML board) |
| what's due today / overdue | `./bin/butler-task review` (reads mirror) |
| mark done / выполнено | `./bin/butler-task done <id-prefix>` → `close_task` |
| reopen | `./bin/butler-task reopen <id-prefix>` |
| move to waiting/active | `./bin/butler-task move <id-prefix> waiting|active` (label op) |
| delete (⚠ confirm) | `./bin/butler-task delete <id-prefix>` — **agent must confirm first (FR-7)** |
| add note / subtask | `./bin/butler-task comment <id> "…"` / `./bin/butler-task add --parent <id> "…"` |
| sync now | `./bin/butler-todoist sync` |

The playbook instructs the agent to (a) resolve ambiguous references against the mirror and ask
when >1 match, (b) **always confirm destructive/bulk actions** before running them, and (c) echo
Todoist's parsed due date back to Boris.

---

## 5. Scheduling (D3)

- Hook a pull into the existing **morning/daily loop** (`lib/daily.py` / `bin/butler-daily` /
  `butler-morning`) so the morning plan reflects external Todoist edits.
- Add a lightweight **intraday pull** before any "show tasks" render (on-demand freshness) so Boris
  never sees stale data when he explicitly asks. (Resolves OQ2: morning loop + on-demand, no extra
  cron — keeps the scheduler surface unchanged. An hourly cron is a trivial later add if wanted.)

---

## 6. Security & policy (NFR-1/4)

- `TODOIST_API_TOKEN` lives only in `~/.openclaw/.env`; `lib/todoist` reads it; it is never echoed
  to chat, logs, or audit records.
- All **mirror writes** go through `lib/vault.py` (existing deny-by-default policy + audit + staged
  writes). All **Todoist writes** are logged to `logs/todoist-sync.log` and an audit line (action,
  todoist_id, timestamp — no token, no secret).
- **Destructive actions** (delete task, delete project/label, bulk complete) are confirmation-gated
  at the agent layer (FR-7) and reflected in `policies/safety.yaml`.
- External Todoist text (titles/comments) is treated as untrusted data when re-fed to the model.
- Egress restricted to `https://api.todoist.com`.

---

## 7. Decisions on open questions

- **A1 (REST vs Sync API)** → **Use the unified REST API (`/api/v1`) with full-list pulls for v1.**
  Boris's personal task volume is small; full reconcile is simplest and most robust. The incremental
  Sync command/`uuid` protocol is recorded as a future optimization, not built now.
- **OQ1 (completed tasks)** → mirror completed tasks for a **rolling 7-day window** into `Done.md`
  (enough for "what did I finish this week"); older completed tasks are not mirrored (they remain in
  Todoist). Configurable constant.
- **OQ2 (pull cadence)** → **morning loop + on-demand-before-render**; no new standalone cron in v1.

---

## 8. Error & edge handling

| Case | Behavior |
|------|----------|
| Todoist unreachable on push | Report failure to Boris in-chat; do not update map; mirror untouched (NFR-3). |
| Todoist unreachable on scheduled pull | Log error; keep previous mirror; retry next cycle. |
| 429 rate limit | Exponential backoff honoring `Retry-After`. |
| Duplicate detection | Mapping table prevents re-creating mapped tasks (migration + push). |
| External delete in Todoist | Pull drops it from the mirror and the map. |
| Ambiguous chat reference | Agent asks Boris to disambiguate (FR-1). |
| Token missing/invalid | `butler-doctor`-style clear error; feature disabled, rest of butler unaffected. |

---

## 9. Testing strategy (detailed in `plan.md`)

- Unit: `lib/todoist` against recorded/fake responses; `lib/tasksync` bucket mapping + reconcile diff;
  migration idempotency (run twice → no dupes).
- Integration (against a real token, low-risk): create→list→complete→reopen→delete round-trip;
  recurring due parse; label move; pull reflects an external app edit.
- Regression: `butler-send-tasks` / HTML board / morning plan render from the mirror unchanged.

---

## 10. Approval

- [ ] **Boris approves this design** (incl. §3.4 bucket mapping and §7 decisions) → proceed to build per `plan.md`.
- [ ] Changes requested (note below).

> Notes:

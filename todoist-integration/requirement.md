# Requirements — Todoist Integration for the AI Butler

Status: **DRAFT — awaiting Boris's approval**
Author: Claude (Butler dev session)
Date: 2026-06-24
Feature folder: `MacStudioButler/todoist-integration/`

---

## 1. Goal

Give the `butler` agent full, conversational control over Boris's Todoist account through
the Todoist API. Boris talks to the butler in natural language (via the existing Telegram
butler bot) to manage his tasks; the butler executes the change in Todoist and keeps a local
mirror in sync. Sync is **bidirectional** in the sense that changes made anywhere in Todoist
(phone app, web, other clients) are reflected back to the butler's local view, and changes
the butler makes are pushed to Todoist.

This requirement document must be **approved before** `design.md` is written.

---

## 2. Actors

| Actor | Role |
|-------|------|
| **Boris** | Primary user. Issues natural-language task commands via the Telegram butler bot. Also edits tasks directly in Todoist (phone/web). |
| **Butler agent** | Reads Boris's intent, calls Todoist, updates the local mirror, replies. Runs proactive/scheduled loops (morning plan, sync). |
| **Todoist** | External system of record (master) — `api.todoist.com` REST API v2. |
| **Local mirror** | The butler's read-only local cache of Todoist state, surfaced as the existing vault Task views and the HTML task board. |

---

## 3. Confirmed scope decisions (from Boris, 2026-06-24)

These three decisions are **locked** and frame all functional requirements below.

### D1 — Source of truth: **Todoist is the master**
- Todoist is the single source of truth. The Obsidian vault `Tasks/*.md` files become a
  **read-only local mirror** that reflects Todoist state. Boris (and the butler) make
  authoritative changes in Todoist; the mirror is regenerated from Todoist, never the reverse.
- On first run, **existing vault tasks are migrated into Todoist once** (one-time import), then
  the vault files are rebuilt from Todoist going forward.
- `lib/tasks.py` is repurposed: instead of being the task store, it becomes a Todoist-backed
  cache/renderer. No task data originates in the vault after migration.
- **Conflict resolution is therefore trivial: Todoist always wins.** There is no peer merge.

### D2 — API scope: **Full task lifecycle**
In scope (Todoist REST API v2 entities and operations):
- **Tasks**: create, read, update, complete, reopen, delete.
- **Due dates**: one-off and **recurring** (natural-language due strings, e.g. "every Monday").
- **Priorities**: p1–p4.
- **Projects**: read, create, reference tasks by project.
- **Sections**: read, reference, assign tasks to sections.
- **Labels**: read, create, apply/remove on tasks.
- **Sub-tasks**: create and nest under a parent task.
- **Comments**: read and add task comments.

Explicitly **out of scope** (D2 boundary): filters, shared/collaborator/team projects,
karma/productivity admin, activity log, backups, the incremental Sync API delta protocol
(unless the design later justifies it for efficiency — see §8).

### D3 — Interface & sync cadence: **Conversational + scheduled sync**
- **Interface**: natural language via the existing Telegram butler bot (account `butler`,
  allowlisted to Boris's TG id `253483520`). No new UI.
- **Push**: every butler-initiated change is written to Todoist **immediately** (synchronous
  with the conversation turn).
- **Pull**: a **scheduled** background job (hooked into the existing morning/daily loop)
  pulls external Todoist changes into the local mirror. Boris can also force a pull on demand
  ("sync" / "покажи задачи" refreshes first).
- Real-time webhooks are **out of scope for v1** (may be a later enhancement).

---

## 4. Functional requirements

### FR-1 — Natural-language task operations
The butler MUST understand and execute, from free-form RU/EN/UK chat, at least:
- **Create**: "add task X", "напомни купить молоко завтра", with optional project, due date
  (incl. recurring), priority, labels, parent task.
- **List / query**: "what's due today", "покажи задачи", "show overdue", "tasks for project Viva",
  "what's in my inbox".
- **Update**: rename, reschedule, change priority, move project/section, add/remove labels.
- **Complete / reopen**: "mark X done", "выполнено", "reopen Y".
- **Delete**: "delete task X" (irreversible → see FR-7 confirmation).
- **Comment / sub-task**: "add a note to X", "add subtask Z under X".

Ambiguous references ("that task", partial titles) MUST be resolved against the current
mirror; if multiple match, the butler asks for disambiguation rather than guessing.

### FR-2 — Immediate push to Todoist
Any create/update/complete/reopen/delete the butler performs MUST be sent to Todoist within
the same turn, and the reply MUST reflect the confirmed Todoist result (e.g. echo the new
task id / due date as Todoist parsed it, since Todoist parses NL due strings server-side).

### FR-3 — Scheduled pull + local mirror
- A scheduled job MUST pull the full relevant Todoist task set and rebuild the local mirror
  (`Tasks/Inbox.md`, `Active.md`, `Waiting.md`, `Done.md`, and the HTML board) so Boris's
  existing "show tasks" experience keeps working and reflects external edits.
- Mapping of Todoist state → the four local list files MUST be defined in `design.md`
  (e.g. by Todoist project/section/label or completion state).
- The mirror MUST be clearly read-only (regenerated header), so no one mistakes it for the master.

### FR-4 — One-time migration of existing vault tasks
- On first activation, each existing open task in the vault (`Tasks/*.md`) MUST be created in
  Todoist exactly once (idempotent — re-running migration MUST NOT duplicate), preserving
  title, project (mapped to a Todoist project), due date, and a marker linking the vault UUID
  to the new Todoist id for audit.
- After migration, the vault files are rebuilt from Todoist (D1).
- Migration MUST be reversible/abortable and produce a report (what was imported, mapping table).

### FR-5 — Identity & mapping
- The butler MUST maintain a stable mapping between Todoist ids and the local mirror so that
  repeated pulls are idempotent and updates target the correct Todoist object.
- Projects/labels referenced by name in chat MUST resolve to Todoist ids (create-if-missing
  only when Boris's intent is clearly to create, otherwise ask).

### FR-6 — Conversational surface parity with existing commands
Existing flows MUST keep working on top of the new master:
- `bin/butler-task list/add/move/review`, `butler-send-tasks`, `butler-tasks-html`,
  the morning/daily plan, and any "show my tasks" path MUST read from the Todoist-backed
  mirror without behavioral regression for Boris.

### FR-7 — Safety confirmations
- **Destructive/irreversible** actions (delete task, bulk complete, delete project/label)
  MUST require explicit confirmation before execution, consistent with the existing
  `policies/*.yaml` deny-by-default posture and the vault write-adapter approval model.
- Bulk operations (e.g. "delete all done") MUST summarize the affected set and ask first.

---

## 5. Non-functional requirements

### NFR-1 — Secrets
- The Todoist API token is provided by Boris and MUST be stored as an env var in
  `~/.openclaw/.env` (e.g. `TODOIST_API_TOKEN`), never committed, never echoed in logs,
  chat, or audit entries — same trust handling as `BUTLER_BOT_TOKEN`.

### NFR-2 — Reliability & idempotency
- All write operations MUST be idempotent or guarded so retries (after a network error or
  partial failure) do not create duplicates. Todoist's request-id/`X-Request-Id` idempotency
  mechanism SHOULD be used where available.
- Pull/mirror rebuild MUST be atomic enough that a crash mid-rebuild does not leave the mirror
  half-written (use the existing staged-write / write-adapter path).

### NFR-3 — Rate limits & resilience
- The integration MUST respect Todoist API rate limits (back off on HTTP 429) and degrade
  gracefully: if Todoist is unreachable, the butler reports the failure clearly and does NOT
  silently drop the user's command or corrupt the mirror.

### NFR-4 — Security & policy
- Outbound calls go only to `api.todoist.com` over HTTPS.
- All task mutations MUST pass through the existing policy/audit path (`lib/vault.py`
  write-adapter + `policies/*.yaml` for mirror writes; an analogous audit log for Todoist
  writes) so every change is attributable and reviewable.
- External Todoist content (task titles, comments) MUST be treated as untrusted data
  (prompt-injection hygiene) when fed back into the agent's reasoning.

### NFR-5 — Fit with existing architecture
- Implementation MUST follow the real workspace conventions: `bin/butler-*` entrypoints
  callable as `./bin/butler-*` (Codex PATH constraint), `lib/` Python modules, writes through
  `lib/vault.py`, no breaking changes to the `tetyana-dyck` agent or existing butler flows.
- No new libraries or architectural changes without Boris's approval (per global rules).

### NFR-6 — Observability
- Sync runs and errors MUST be logged under the existing `logs/` location with enough detail
  to debug via trajectory (consistent with how butler crons are currently debugged).

---

## 6. Out of scope (v1)

- Real-time Todoist **webhooks** (push from Todoist within seconds).
- Peer-to-peer / two-master conflict merging (excluded by D1).
- Filters, shared/team projects, collaborators, karma, activity log, backups (excluded by D2).
- Any non-Todoist task system, calendar sync, or reminders beyond Todoist's own.
- Multi-user / accounts other than Boris's.

---

## 7. Acceptance criteria

The feature is accepted when, on the live butler:
1. Boris can create, list, update, complete, reopen, and delete tasks by chatting with the
   butler bot in natural language, and each change is visible in the Todoist app.
2. Tasks support project, section, label, priority, recurring due date, sub-task, and comment
   as specified in D2.
3. An external change made in the Todoist phone app appears in the butler's "show tasks" output
   after the next scheduled (or on-demand) sync.
4. Existing vault tasks were migrated into Todoist exactly once, with a mapping report, and the
   vault `Tasks/*.md` now render from Todoist as a read-only mirror.
5. `butler-send-tasks` / the HTML board / morning plan all reflect Todoist state with no
   regression for Boris.
6. Deleting a task or any bulk/destructive action asks for confirmation first.
7. The Todoist token is read from `~/.openclaw/.env` and never appears in logs or chat.
8. With Todoist unreachable, the butler reports the error and the mirror is not corrupted.

---

## 8. Assumptions & open questions (to resolve in design)

- **A1**: Todoist REST API **v2** is the target (current stable). Design will confirm whether
  the incremental **Sync API** is worth adopting for efficient pulls, or whether plain REST
  list calls suffice for Boris's task volume.
- **A2**: Boris will provide a personal API token (Settings → Integrations → Developer) with
  full account scope.
- **A3**: Mapping of Todoist projects/sections/completion → the four local list files
  (Inbox/Active/Waiting/Done) is a **design decision**; a candidate mapping will be proposed
  for approval in `design.md`.
- **A4**: "Recurring" tasks are handled by Todoist server-side; the butler stores/echoes the
  recurrence Todoist computes rather than computing recurrence locally.
- **OQ1**: Should completed tasks be mirrored at all (Todoist hides completed by default), or
  only kept for a short window in `Done.md`? — propose in design.
- **OQ2**: Pull cadence — tie strictly to the existing morning loop, or add an additional
  interval (e.g. hourly) during the day? — propose in design.

---

## 9. Approval

- [ ] **Boris approves these requirements** → proceed to `design.md`.
- [ ] Changes requested (note below).

> Notes:

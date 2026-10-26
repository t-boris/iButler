# butler-core datasets

Ownership, schema migration, backup and deletion rules for every dataset the core writes or reads. This covers REQ-025 and DEC-038, DEC-047 and DEC-053.

## Datasets written by the core

The core daemon is the only writer of these datasets. Clients reach them through the socket.

| Dataset | Location | Owner | Contents | Schema / migration | Backup | Deletion |
|---|---|---|---|---|---|---|
| Task store | `~/.local/share/butler-core/core.sqlite3` | butler-core | Tasks and steps (with their history), operations and attempts, reconciliations, resolutions, approvals, policy decisions, deliveries, configuration versions, conversations, selections, cron run accounting, diagnostics, metrics, alarms, System 1 shadow, feedback events | `PRAGMA user_version`; `store.MIGRATIONS` is append-only. A newer schema than the code knows refuses to start | SQLite online backup every 6 h | Diagnostics older than 90 days may be pruned. Tasks, operations and decisions are kept, because they are the evidence of what was done |
| Ingest store | `~/.local/share/butler-core/ingest.sqlite3` | butler-core | High-volume inbound events (userbot, WhatsApp) with a stable `message_key` | Same migrations as the task store | SQLite online backup every 6 h | Raw message payloads stay local (`raw_local_only`) and are never exported |
| Configuration | `config_versions` table + `bootstrap-config.json` / `config-generated.json` (0600) | Owner (activates); deployment (generates) | Personas, grants, exports, operation rules, scenario switches, limits, bridge accounts | Validated by `config.validate_content` (schema 1) | Inside the task store | Versions are never deleted; rollback walks back through predecessors |
| Logs | `~/.local/share/butler-core/logs/` | butler-core | Process start and stop lines, tracebacks. No message text, no secrets | — | Not backed up | Free to delete |

## Datasets read but not owned

The core never writes these.

| Dataset | Owner | How the core uses it |
|---|---|---|
| `~/.openclaw/cron/jobs.json`, `jobs-state.json`, `runs/*.jsonl` | OpenClaw gateway | Read-only accounting of cron runs; key `cron:<jobId>:<runAtMs>` |
| `~/.openclaw/openclaw.json` | Owner / OpenClaw | Deployment reads the Telegram allowlists to generate ConfigVersion v1; values are never printed |
| `~/.openclaw/tmp/openclaw-<uid>/gateway.*.lock` | OpenClaw gateway | Gateway instance identity (pid@createdAt) for delivery retry decisions |
| `~/.openclaw/delivery-queue/failed/` | OpenClaw gateway | One read-only import as `uncertain` deliveries (W3) |

## Backup file sets

Backups go to `/Volumes/Storage0/ButlerBackup/butler-core.sparsebundle`, attached with `-owners on` at `~/.local/share/butler-core/backup-mount`.

**Schedule.** A new generation is written every 6 hours and at least 4 generations are kept (8 by default). Files that have not changed are hard links to the previous generation.

**Integrity.** Every generation has a `MANIFEST.json` with a sha256, size, mtime and mode for each file.

**Alarms.** The alarm `backup:stale` fires when the newest backup is more than 12 hours old. `backup:disk-absent` fires when the disk is not mounted.

**What is copied:**

| Set | Source | Excluded |
|---|---|---|
| databases | core, ingest, ambient SQLite (consistent online copies) | — |
| workspace-butler | `~/.openclaw/workspace-butler` | `runs`, `logs`, `backups`, `scratch`, `work`, the regenerable `state/memory-index.db`, dispatch output logs |
| workspace-bamberg-news | `~/.openclaw/workspace-bamberg-news` | `runs`, `credentials` |
| workspace-tetyana-dyck | `~/.openclaw/workspace-tetyana-dyck` | CLI image cache, `runs`, `media`, repository checkouts |
| workspace-main | `~/.openclaw/workspace` | — |
| openclaw-cron / agents / extensions | `~/.openclaw/{cron,agents,extensions}` | agent runtime homes (`*/agent`) |
| openclaw-config | `~/.openclaw/openclaw.json` | Secret values replaced by `{"secretRef": …}` |
| launch-agents | `~/Library/LaunchAgents/com.boris.*.plist`, `ai.openclaw.*.plist` | Secret values replaced by references |
| macstudio-butler | `~/Projects/MacStudioButler` | `evidence/logs` |
| ambient-settings | `~/.local/share/butler-ambient` | raw media, images, voice, biometric references, preserved clips, models, spool, the ambient database (copied in *databases*) |
| vault | `~/Documents/KnowledgeDB` | Obsidian cache and trash. iCloud placeholder (dataless) files are skipped and counted rather than downloaded |

**Excluded in every set:**

- Media files (video and audio).
- Files larger than the set limit.
- Version-control and dependency trees.
- SQLite `-wal`, `-shm` and `-journal` files. Live databases are copied through SQLite instead.

**No secrets (DEC-053).**

- Files whose names mark them as credentials (`*token*`, `*secret*`, `*credential*`, `auth-profiles.json`, keys, sessions, cookies) are never copied.
- Text files and database copies in which a credential pattern is found are left out and listed by path in `secrets_excluded`.
- After a disk loss, services are re-authorized; tokens are not restored.

## Restore

`Backup.restore(root)`:

1. Verifies each file of the chosen generation against its manifest hash.
2. Copies the files into an empty root.
3. Reports corrupt files and the restored task and operation accounting.

The timed drill (W10) restores into a blank root and reinstalls the runtime with `deploy.py`.

## Deletion rules

- Raw observation media, transcripts and biometric data never enter the core or a backup.
- A persona's data is deleted by deleting its tasks and events by persona. Operations stay as evidence, with their parameters redacted to their hash.
- Feedback events (REQ-027) contain metadata and references only; deleting the referenced material leaves dangling references, which the analysis ignores.

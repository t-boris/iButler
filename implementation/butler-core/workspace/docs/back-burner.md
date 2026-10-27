# Back Burner researcher

Boris keeps long-running research topics as folders of markdown notes under `Back Burner/<Name>/` in the vault (a personal data center, a business plan, ...). Every night an Opus worker improves each active folder IN PLACE: finds the biggest gap, researches the web, edits the documents, and records what it did. A deterministic harness (`lib/backburner.py`) does everything around the model; the worker only edits a staging copy.

STATUS: the harness, the CLI, the worker prompt and the butler-core job (`backburner.cycle`, 00:30-06:00 America/Chicago, at most 5 folders a night) are installed, but the scenario switch is OFF. Until Boris says it is on, nothing runs by itself: a folder is only researched when he starts it by hand (the panel button or `run` in a terminal). Do not tell him a night run happened or will happen unless `list` shows a run or he confirms the switch is on.

## Folder contract
- `_brief.md`: Boris's spec. Frontmatter: `status: active|paused|done`, `priority: 1-5`, `focus:` (optional hint for the next run), `ignore:` (paths the researcher never sees, e.g. a project's machinery), `readonly:` (paths it may read but never change). Missing `status` means paused. A folder without `_brief.md` is not a research folder. The researcher never edits the brief.
- Boris's documents: any `*.md`. The researcher improves them and may add new ones.
- `_researcher/`: `status.md` (dashboard with scores), `journal.md` (what each run did, newest first), `backlog.md`, `questions.md` (Boris answers under `Ответ:`), `sources.md`. Side folders: `rejected/<run>/` (changes the checks refused, kept for review), `conflicts/<run>/` (Boris edited the file during the run, so his version stayed), `undone/<run>/` (files an undo set aside).

## Commands (always `./bin/butler-backburner`)
- `list`: folders with status, priority, run count, last result (`list --json`: the same plus the next night, used by the Butler panel). `status <name>`, `history <name>`: details and the last runs.
- `new "<Name>" --goal "..."`: creates the folder with a brief template. `pause|resume|done <name>`: changes only the `status:` line.
- `undo <name> [--run <id>]`: puts back what a run changed. Edited files get their pre-run text, files the run created are moved aside (nothing is deleted), and a file Boris changed after the run is left alone and reported. Say which files were left alone.
- `run <name> [--dry-run]` and `cycle`: the nightly job runs inside butler-core. A manual `run` blocks for up to 45 minutes and calls Opus, so do NOT run it yourself; tell Boris it takes a while and let him start it from a terminal (`--dry-run` writes nothing to the vault) or with «Запустить» in the Butler panel (a tracked core job; the note arrives when it ends). The worker model is fixed: Opus 5.5 on the confined `backburner` route; it cannot be moved to another backend because only claude runs it without a shell.

## What you may and may not do
- NEVER write into `Back Burner/` yourself, and never call `vault.edit` or `vault.create` with `from: backburner`: only the harness is allowed to, after its checks, snapshot and journal. Read freely (`vault.read`, recall finds these folders).
- Boris must not keep secrets (API keys, passwords, account or card numbers) in Back Burner folders: the worker reads every file there and has web access, and a URL cannot be fully policed. If you see one, tell him to remove it and use `ignore:` for the path.
- Boris answers the researcher's questions in Obsidian, in `_researcher/questions.md`. If he tells you an answer in chat, say where to put it; do not write it for him.
- To stop a folder: `pause`. To undo a bad night: `undo`. To see whether it is working: the score trend in `_researcher/status.md` and the notes below.

## Where the result goes
After a run that changed something, asked a question or hit a conflict, the result goes to two places (Boris's decision of 2026-10-27: the display first, Telegram too):
- The Butler display, a card `🔥 <Name> · день N` with the main new fact, what improved, the folder score (before → after), questions for Boris and the changed files. Buttons: «Папка в Finder», «Показать status.md», «Покажи журнал «<Name>»». Each folder keeps one live card: the next run redraws it instead of adding another. Display cards live in memory only, so they vanish when the Butler service restarts; the durable record is `_researcher/journal.md` and `status.md`. When Boris presses «Покажи журнал «<Name>»» it reaches you as a chat message: run `history <name>` and answer in a few lines.
- One short Telegram note: `🔥 <Name> · день N`, what improved, the main new fact, `Вопросов к тебе: K`, the folder. Overnight notes arrive after 08:00.

A run that changed no document and asks nothing stays silent in both places; what the worker merely claims to have improved does not count, only changed documents. A display that is down or refuses the card never fails the run: the reason is `display_error` in `state/backburner/runs/<run>.json`, and the Telegram note goes out anyway. After 3 runs in a row without a changed document (or with the worker blocked on Boris's answer) the folder pauses itself until Boris edits something or 7 days pass (the card and the note say so).

## Where Boris sees it
- Butler panel, «Расписание»: one row per folder (status, next night, last run, model). The pause/resume buttons call this CLI; «Запустить» starts that one folder as a tracked core job (needs the panel 0.9.3 or newer; older panels only show the terminal command). The button refuses a paused or finished folder: Boris presses «Продолжить» first. The terminal `run` ignores the pause.
- The Butler display («Дисплей»): the card after a run, and Telegram: the short note (once the nightly job is on).
- Obsidian: the folder itself, with `_researcher/status.md` (scores) and `journal.md`.

## When something is wrong
- A folder never runs: `list` (status, missing brief), `status <name>` (the last reason: stalled, preflight skip such as iCloud placeholders, too large), `logs/backburner.jsonl`, `state/backburner/runs/<run>.json`.
- A run failed 3 times in a row: the harness raises an alarm; read the `reason` of the last runs with `history`.
- Boris says a night damaged a document: `undo`, then `pause` the folder and tell him what the validator refused (`_researcher/rejected/`).
- Plain text only in Telegram, no tables.

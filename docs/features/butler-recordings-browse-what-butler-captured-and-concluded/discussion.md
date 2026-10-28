# Discussion — Butler Recordings: browse what Butler captured and concluded

### Agent · 2026-10-26

2026-10-25, during a Google Meet. The owner said there is no place in the app to see recordings, audio, video and images, and that he wants every conversation recorded and analysed later. Done the same evening, outside this feature: observation was resumed at the owner's word. The meeting mode was set back to auto, so Butler stays silent in the call. In ambient 0.4.1 the raw spool limit became the config setting spool_limit_mb, set to 4096; it had been hard-coded at 512 MiB and full since the afternoon. The owner chose to keep only the analysis conclusions of this meeting, not its originals. Store inventory (metadata only): 29 001 observations since 2026-10-22 with facts of the kinds activity, topic, person, appearance and work-topic; 54 event frames; 51 preserved originals (camera key frames); 0 visual artifacts. Transcripts are never written to disk, and the analysis prompt forbids quoting speech, so the meeting will leave no record of what was said. Q-001 asks the owner to decide this.

### Agent · 2026-10-27

2026-10-26, ambient 0.8.0: a first slice of this feature is built and installed, deliberately smaller than the spec. It lives in «Наблюдения → История», not in a new «Записи» section, so REQ-001 criterion 1 (sidebar entry) is not met. What it does: browse stored conclusions day by day (America/Chicago) with counts, one source popup (not four toggles), paged list, a detail pane with every stored fact and its confidence, the «Оригинал удалён после анализа» statement, and gaps between observations labelled by reason, including the owner pause of 2026-10-25 19:50–20:07 (verified live against the real store: one «пауза» gap 19:50:56–20:06:57; 14 110 rows of that day paged in about 0.3 s per call). Facts only: no transcripts, no media playback, retention unchanged, preserve/delete NOT included, the data is owner-local and not on the MCP surface. Not met, left unticked on purpose: REQ-001 criteria 1, 3, 4 (four source toggles, time range and badges per row); REQ-002 criterion 1 (facts are listed with their kind, not grouped); REQ-006 criterion 2 (labels differ: «сервис остановлен», «анализ не успел», «удалено без анализа» instead of «перезапуск» / «не проанализировано»). REQ-003, REQ-004 and REQ-005 are untouched. Q-001 to Q-004 stay open for the owner: this slice behaves as if Q-001 and Q-002 were «A» (no transcripts, no automatic originals) and Q-003 «A» (flat list), but nobody has answered them. Limits of the evidence: the panel itself was verified headless (186 native layout checks, rendered PNGs) and by data checks against the live store, not by watching the real window.

### Answer to Q-001 · 2026-10-28

Should Butler keep a text transcript of meetings and conversations?

C. Transcripts of all of the owner's speech and of every detected call, local, with a fixed retention

### Answer to Q-002 · 2026-10-28

Should the original audio and video of a marked meeting be kept after analysis, and for how long?

B. Keep the audio (mic + system) of marked meetings for 30 days, no camera video

### Answer to Q-003 · 2026-10-28

Should the timeline group observations into sessions or show every observation?

B. Sessions (consecutive minutes with the same activity or call), expandable to the minute observations

### Answer to Q-004 · 2026-10-28

Should Butler be able to search and show recordings by voice ("покажи, что было на встрече в 8 вечера")?

B. A local voice command opens the Recordings section at the named time; nothing goes to the cloud

### Answer to Q-005 · 2026-10-28

Какой фиксированный срок хранения установить для всех транскриптов?

A. Хранить транскрипты 30 дней.

### Answer to Q-006 · 2026-10-28

Как определять встречи, аудио которых нужно сохранять на 30 дней?

A. Владелец отмечает текущую обнаруженную встречу для сохранения аудио.

### AI updated the requirements · 2026-10-28

DEC-001, DEC-002, DEC-003 written into the overview.

### AI updated the requirements · 2026-10-28

DEC-004 written into REQ-005.

### AI updated the requirements · 2026-10-28

DEC-005 written into REQ-004, the overview.

### AI updated the requirements · 2026-10-28

DEC-006 written into REQ-004, the overview.

### Implementation · 2026-10-27

Read every file in this feature folder, including all six answered questions, six accepted decisions and all three resolved findings. DEC-006 supersedes Q-002’s camera-video exclusion. DEC-004 supersedes the older Display always-visible policy. Implementation is underway on feat/butler-recordings, from the newly created origin/main. The owner’s repository and leftover-work answers are recorded in DEC-007.

The existing slice only exposes local observation media. I-1–I-6 remain in progress until session grouping, transcript persistence, fixed expiry, marked audio/video retention, owner-only Display visibility and local voice routing have automated verification.

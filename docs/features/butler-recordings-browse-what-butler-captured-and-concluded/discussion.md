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

### Implemented · 2026-10-27

All eight requirements and all six implementation tasks now have code and automated coverage in ambient 0.10.0. Applied every original answered question, accepted decision and resolved finding. DEC-007 records both new owner answers: create/use main and include all prior unpublished work. The feature now includes video under DEC-006, verified-owner Display visibility under DEC-004, and file-only controls under DEC-005. See implementation/verification.md for the requirement map, tests, source fingerprints, CI and deployment evidence locations. Initial full ambient run: 867 tests passed; focused regressions on the subsequent transcript-association fix: 14 passed; native UI: 262 checks passed. Full local release gates, CI, merge and deployed-version checks are required before the goal can close. No issue synchronization, MarkView browser verification or owner manual-test checklist was added.

### Agent · 2026-10-29

2026-10-28: Metadata-only sampling after DEC-097 found local history hidden in 2 of 19 successful samples when the scene detector reported multiple faces; Boris reports he is alone. This does not independently prove the detector's cause. Under the explicitly chosen single-user local-display assumption, stored history no longer inherits transient automatic face/voice or crowd classification. Owner pause, recording off, service stop and explicit participant consent revocation still hide it; revocation clears the mode. Capture, raw media, playback and external-action permissions remain strict. Speaking private summaries remains separately blocked by detected company, stale/unhealthy participant sensing and unsafe or unavailable call detection. Regression and live metadata verification are in progress.

### Agent · 2026-10-29

Local-use audit: the feedback/voice-episode panel also hid and discarded in-memory rows on transient owner-verification loss. A candidate uses the single-user history permission for local read-only viewing, while still cancelling export, clearing privileged context, retaining the stricter gate for corrections, clearing the rows on explicit pause or consent revocation, and clearing cached private rows/images on a failed poll or mode-off. Synthetic service and panel tests pass; full regression and installation are pending.

### Agent · 2026-10-29

DEC-097 local-history and feedback viewing were installed and verified with synthetic tests and metadata-only live checks. Manual pause, archive off, revoked consent, stricter raw/playback and transmission gates remain. The broader local identity-gated paths, live speech and real user verification remain open; voice output was already disabled and was not enabled.

### Agent · 2026-10-29

Single-user follow-up: saved dialogue feedback episodes now restore after a Butler service restart to a separate read-only local display cache without biometric owner confirmation. The cache does not restore agent context, fragments or correction authority and does not run retention; pause and consent revocation clear it. Synthetic source and installed regression tests pass 44/44. Metadata-only installed status confirms local feedback restoration, history visible and raw access denied. Actual episode contents were not inspected.

### Agent · 2026-10-29

Single-user audit: the local rule list previously required the identity gate, and its RuleStore.scan could remove tombstoned files and history. The list now uses a separate nonmutating reader, checks the existing source grant and local consent/pause/archive gates before and after reading, and is read-only without a verified dialogue session. The native dialog disables mutation controls in that state. Synthetic source and installed feedback tests 22/22 and panel checks 284/284 pass; installed with a scoped backup and Butler-only restart. No actual rule contents were inspected.

### Agent · 2026-10-29

Final response-boundary audit found raw permission could expire while ui-state was being assembled. The installed panel snapshot now rechecks history and raw permissions immediately before return, removes image paths and image feed entries when raw access expires, and clears local history if the hard scope closes. Synthetic recordings tests pass 46/46 against both source and installed package. After the Butler-only restart, 10/10 metadata-only polls showed single-user history and local feedback restored, 0/10 allowed raw, and 0 failures. Read-only database counts: 50,210 observations and 643 recording transcript rows; no stored content inspected.

### Agent · 2026-10-29

The owner-local rule dialog now discards in-flight responses on scope loss and closes an already open rule modal on pause, loss of local history access, mode switch or first failed poll. A modal-loop timer continues status polling while the dialog is open; verified-authorized rules retain their existing access policy. Synthetic bundled panel tests pass 289/289, including a real synthetic modal closed by a failed poll. The tested panel is installed and running, with a scoped backup; metadata-only status shows history visible and raw access denied. No real rule contents were inspected.

### Agent · 2026-10-29

Follow-up: an editable rule dialog now also closes when the stricter dialogue lease expires, even if the single-user read-only history remains visible. It can then be reopened as read-only. The final bundled panel passed 290/290 synthetic checks, was installed with a separate backup, and the running panel matches the tested executable. The metadata status still shows history visible and raw access denied, including a false multiple-face signal.

### Agent · 2026-10-29

Added a synthetic playback generation regression: separately close and reacquire capture permission and dialogue permission during a mocked local conversion. Even though both permissions are active again before conversion returns, the changed generation rejects playback. Recordings tests pass 47/47 against both source and the installed package; no user media is read and no runtime change is needed.

### Agent · 2026-10-29

A stop-during-ui-state regression found that a live preview and recording status could survive after stop_event was set while a snapshot was being assembled. The final response now suppresses preview and marks recording disabled during stop; the preview path is not started when stop is already set. The regression was observed failing on the previous code and passes on the source and installed package, with 48/48 recordings tests. Installed only local_ui.py with backup and restarted Butler. No real preview, audio or transcript content was inspected.

### Agent · 2026-10-29

A second synthetic stop race reproduced playback returning a prepared path after stop_event was set but before stop could acquire the service lock and invalidate the gates. The strict raw permission now also checks stop_event, so playback refuses this race; the regression was red before the change and passes in both source and installed package, 49/49 recordings tests. Only recordings.py was installed with a scoped backup. No real media or transcript contents were inspected.

### Agent · 2026-10-29

A native red/green regression found another direct coupling: when raw access fell, RecordingsPane cleared all stored rows and the selected transcript before reloading. The panel now keeps the existing history, expansion and selection during a raw-only lease loss; it invalidates pending replies, stops playback, removes image and media paths, and keeps the hard-close/failed-poll clear behavior. When raw permission returns the panel refreshes. The earlier Recordings player implementation remains in place. The bundled panel passes 291/291 and the exact tested binary is installed and running with a scoped backup. Live metadata confirms history visible while raw is denied, including a multiple-face signal; no real contents were read.

### Agent · 2026-10-29

Completed a source and current-installation access audit against DEC-097: owner-local history and rule/feedback viewing use the chosen mode without asserting biometric identity; capture and playback gates, consent/pause/stop, source grants, private spoken-output checks and privileged/externally dispatched operations remain separate. Details and exact regression coverage are recorded in implementation/ambient-awareness/VERIFICATION.md. Current hashes: 13/13 installed Python modules match source and the installed panel matches the 291/291 tested binary; full suite 925/925. Metadata-only counts 50,291 observations and 645 recording-transcript rows; no real content read. Voice remains disabled by the existing preference; live spoken dictation is still unconfirmed.

### Agent · 2026-10-29

Владелец сообщил, что открытие заработало, но обновление сбрасывало выделение; исправление установлено, синтетические проверки покрывают фоновое обновление и возврат разрешения на исходники. Затем владелец попросил понятные сводки и выводы для длинных записей и наблюдений (BUG-013). Установлены локальные сводки по выбранной сессии и за день, с ограничением объёма и предупреждением о неполном охвате. Реальное содержимое агент не просматривал; проверка удобства владельцем остаётся открытой.

### Agent · 2026-10-29

Борис сообщает: «Все равное нет smmary нет толку от длинных записей и кстати нет никаких изображений». Выявлено: раньше результат долгой сводки появлялся только после обработки всех частей; синтетический запрос одного фрагмента к локальной модели занял 9,7 секунды. Установлена индикация первого готового фрагмента и предупреждение о незавершённости. По метаданным есть 109 сохранённых медиа камеры/экрана; текущий строгий доступ к исходникам закрыт. Изображения не прочитаны; изменение разрешения ожидает явного выбора владельца.

### Agent · 2026-10-29

Борис уточнил: сводка выдаёт ошибку или не заканчивается; изображения отсутствуют в отдельном разделе «Изображения». Он явно выбрал разрешить локальный просмотр сохранённых кадров камеры/экрана без автоматической проверки личности. Решение DEC-098 принято. Галерея теперь включает сохранённые кадры; после установки метаданные показывают 109 доступных кадров камеры при закрытом строгом доступе к исходникам. Длинная сводка показывает первый фрагмент до окончания, а при последующем сбое сохраняет его с предупреждением. Проверка владельцем остаётся открытой.

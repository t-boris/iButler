# Changelog — butler-ambient

## Unreleased — owner-local history summaries and selection

- Preserve an open recording selection across periodic refreshes and raw-access recovery; explicit refresh and hard permission loss still clear or reload as before.
- Add on-demand local summaries for a recording session, a day of recordings, and a day of observations. Summaries distinguish key points, possible conclusions and limitations, warn when bounded input is incomplete, and are cancelled on hard history-scope loss. They do not transmit or persist summary text.
- Show a clearly labelled first-fragment preview while a long summary is still running; explain when the current raw-file permission hides images.
- Under owner decision DEC-098, show already-preserved camera/screen stills in the local Images gallery in single-user mode even without automatic identity confirmation. Hard history closes still clear them; this does not unlock raw audio/video, capture, deletion, Finder access or delivery.

## 0.12.2 — 2026-10-30 (useful summaries and advice)

- Replace the activity-counter daily card and template advice with locally generated narratives based on sampled, dated observation facts. Make uncertainty and incomplete samples explicit; do not expose internal observation identifiers. Manual advice examines up to 30 days; automatic advice keeps fresh-evidence, quota, meeting and quiet-hour checks.
- Generate automatic advice in the background and throttle unsuccessful attempts. Missing evidence yields an explicit message on manual request and no autonomous message.

## 0.12.1 — 2026-10-30 (manual Display summaries and advice)

- Allow an explicit button press to show a daily summary or advice in Display even during quiet hours and after earlier automatic delivery. Automatic schedules keep their existing quotas and quiet hours; optional Telegram remains subject to quiet hours and owner verification.
- Base manual advice on the latest eligible observation in the previous 30 days and label its time; report when none is available. Show a generic error when manual preparation or Display delivery fails.

## 0.12.0 — 2026-10-30 (Display delivery for local schedules)

- Show built-in daily summaries and proactive advice as Russian-language Display cards, with a separate opt-in Telegram setting for each; neither is spoken. Local single-user history permits the summary without biometric confirmation, while optional Telegram delivery still requires the owner gate. The settings can also be requested through the existing confirmed schedule command. External OpenClaw schedules keep their existing delivery behavior.
- Consider fresh advice before an archive analysis yields to new capture or returns after a different source fails; retain the existing policy, meeting and quota checks.

## 0.11.3 — 2026-10-28 (Dictation while an agent is working)

- Let panel dictation start while an agent request is pending without audible speech. Defer its spoken output until dictation ends.

## 0.11.2 — 2026-10-28 (Display video refresh)

- Preserve the current video player across Display refreshes, while stopping it when the card or media changes.

## 0.11.1 — 2026-10-28 (Display without owner checks)

- Show the local conversation and persistent Display history without camera/voice verification or observation permission, as explicitly requested in DEC-009. Card selection, local controls and Clear also work without the biometric gate.
- Keep rendered Display material through failed state polls, and remove the owner-verification blocking placeholder from the panel.
- Finish gateway history bookkeeping before publishing a completed written turn, so the next asynchronous result cannot be swallowed by that turn's history marker.

## 0.11.0 — 2026-10-28 (persistent local history and honest settings)

- Retain Display and written conversation history locally until Clear, including across service restarts and pauses. Preserve verified-owner Display visibility and actions; interrupted written turns are not replayed. Mail bodies/drafts are not duplicated in the history database.
- Wrap component names and help text, contain table drawing, and keep component controls reachable at supported window widths. Show configured Knowledge separately from a successful or failed search.
- Read the active core Back Burner switch and time window. Show the future plan separately from last confirmed execution; disabled or unavailable automation cannot claim an enabled next run. Folder resume no longer claims the nightly scenario is enabled.

## 0.10.2 — 2026-10-28 (large recording days)

- Resolve media containment directories once per browse request, retaining individual file/symlink validation while avoiding repeated filesystem walks on large capture days.

## 0.10.1 — 2026-10-27 (pending-media days)

- Index direct observation/media links so large pending-original backlogs do not scan all observations for every file.
- Keep days containing only available originals in the Recordings day picker, even before analysis produces observations or transcripts.
- The release installer tolerates an absent optional Homebrew Ollama launch agent, preserving independently managed background models.

## 0.10.0 — 2026-10-27 (local Recordings)

- Add «Записи»: day/source browsing, expandable activity/call sessions, stored facts, retained transcripts, key frames, generated images and current Display history. Play individual audio/video files in the panel; prepare disposable MP4 playback copies of camera MJPEG originals.
- Retain owner speech and detected-call transcripts locally for 30 days. Explicitly mark the current detected meeting to retain subsequent permitted microphone/system audio and camera video for 30 days; new calls and service restarts do not inherit the mark. Fixed expiry also removes manually preserved copies.
- Preserve/delete only the selected media file; shared observations, facts and transcripts keep their independent lifetime. Show pause, buffer-full, restart and analysis gaps.
- Enforce verified-owner visibility for Recordings, the gallery, Display history and Display actions (Recordings DEC-004 supersedes older Display DEC-047/048). Clear cached private panel media on access loss or disconnection. Older MCP observation tools return metadata without observation content.
- Intercept time-based recordings navigation locally before voice or panel requests reach the cloud agent.
- Align existing waiting-reaction regressions with accepted attention DEC-090; preserve the existing speech behavior.

## 0.9.5 — 2026-10-27 (weekly dialogue report)

- Show the weekly report's total sample size and unknown-outcome share, and disclose that false-interruption accounting is not yet connected (REQ-061).

## 0.9.4 — 2026-10-27 (manual Busy stays authoritative)

- Preserve a manually selected Busy state when the owner repeats a spoken busy cue (DEC-095).
- Apply spoken availability cues only to a verified owner utterance, and describe a persistent manual Busy state accurately in the panel.

## 0.9.3 — 2026-10-27 (Back Burner in the schedule list)

- The «Расписание» list shows one row per Back Burner research folder (status, next night, last run, model, runs), read from `bin/butler-backburner list --json`. Pause, resume and stop call the same CLI. «Запустить» submits one `backburner.cycle` operation for that folder to butler-core (principal `voice:owner`, a fresh key per press) and says in a line whether the core took it; when the core is missing, down or refuses, the message says why and shows the terminal command. The operation's target is a short hash of the folder name (butler-core takes ids only: a name with a space or Cyrillic letters was refused before anything started) and a refusal from the core is shown with its own reason. A folder that is paused, done or without a status is refused with «сначала нажмите «Продолжить»» (a manual run ignores the pause, so the button asks for the explicit step first; the terminal `run` is unchanged). A failure of the Back Burner CLI is reported in the list's error line and never hides the cron rows.
- The model editor refuses a non-claude model for a route that is confined (a route with `tools`, today `backburner`): the confinement flags exist only for claude, so such a choice would only fail at night. Other routes are unchanged.

## 0.9.2 — 2026-10-27 (the display has no privacy gate)

- A valid card is shown whatever the state of the private session. Before, `butler_display` was refused (opaquely) whenever the owner was not confirmed by camera or voice (here: another face in view, one minute after a restart), and the bot fell back to text bars in the chat. Owner's instruction: «Убери все приватные фичи. Показывай все всегда» (DEC-048, supersedes the creation gate of DEC-042 and REQ-010).
- Image and video cards keep their picture when recording is paused (no more "paused" placeholder); a deleted file still shows "missing". `Display.snapshot` takes no arguments; `hidden` stays in the payload as `None`.
- Unchanged on purpose: card validation (media only from Butler's own folders, link schemes, svg cleaning, button targets), the history ends at a real close (pause, consent revoked, settings change, enrollment, stop), the recording and consent gates, the camera preview and the «Изображения» gallery.
- Consequence the owner accepted: any agent run (Telegram, cron) can put cards, mail cards included, on the screen regardless of who is at the Mac.

## 0.9.1 — 2026-10-27 (the display is no longer cleared or hidden for nothing)

- A brief loss of the private session (a participant check that blinks, a stale check, a capture gap) no longer clears the display history and no longer hides the cards. Before, every such drop wiped the display: after the 0.9.0 restart the gate dropped more than ten times in half an hour and the owner's cards, buttons included, vanished each time (DEC-047, owner's choice: "always show", no idle timer).
- The history still ends at a real close: pause, consent revoked, settings change, voice enrollment or reference update, service stop. A new card still needs the owner's confirmed session (the privacy gate is unchanged for creation, and its refusal stays opaque). Image and video paths are still withheld while recording is paused.
- Consequence the owner accepted: private cards (mail, drafts) stay on screen while the owner is not recognised, including when someone else is at the Mac.
- `Display.snapshot` lost its `dialogue_allowed` argument; `hidden` stays in the payload (always `None`).

## 0.9.0 — 2026-10-26 (display: buttons, a live canvas, tested diagrams and drawings; panel follow-ups; no-API proof)

- Cards get buttons (closed set, DEC: no approval buttons): `reply` sends exactly its visible label to the butler agent as the owner's message, `link` opens an http/https/mailto URL, `reveal` shows a file or folder under the home directory in Finder. Targets and reply text stay in the service; the panel only sees id, label, kind and whether the button was used. A used reply button disables its siblings and survives a refresh; an answered card paints no live buttons even on its first frame.
- One live canvas per topic: `butler_display` takes `replaces: <card id>`; the card is redrawn in place (↻ in the history), keeps its row and origin, and only steals the window when the owner was not already looking at it.
- Captions (`markdown`, ≤2000) under image and diagram cards; diagrams zoom with a pinch.
- 11 tested Mermaid templates (`assets/diagram_templates.json`: architecture, layers, pipeline, c4-context, sequence, state, er, timeline, bars, quadrant, mindmap) in the console palette; each is rendered by the native layout test, and `python -m butler_ambient.diagram_templates --markdown` generates the agent's playbook (`docs/display-diagrams.md`) so the two cannot drift.
- New card kind `svg` for drawings that Mermaid cannot make: a strict allow-list sanitizer (`svg_clean.py`) on the service, a page with JavaScript off and a `default-src 'none'` Content-Security-Policy in the panel (two independent layers), pinch zoom, caption and buttons. The playbook example ships in the app bundle and is what the tests render.
- Panel follow-ups (`session_follow.py`): answers that arrive after the panel's own request ended (a sub-agent's result, a long task) now appear in the thread; the panel reads the gateway's session history read-only, shows only the assistant's own text, once.
- The panel talks to the agent's own model (`panel_model_override`, default empty = the butler's native model, `openai/gpt-6.1-sol` on Codex); the voice keeps its fast model. `panel_preamble` tells the agent that the display is available.
- `python -m butler_ambient.provider_audit` counts recent assistant messages by provider and fails on anything that is not a subscription login (`openai-codex`, `claude-cli`) or OpenClaw's own delivery mirror: the proof that nothing runs on a paid API key.
## 0.8.0 — 2026-10-26 (panel: write and dictate, observation history, image provenance, settings fixes)

- «Дисплей»: a message box and «Диктовать» (local Whisper) next to the conversation thread. Typed or dictated text goes to the butler agent through its own gateway session `agent:butler:panel`; the answer comes back in the panel as text and is never delivered to a channel, while Telegram conversations keep answering in Telegram. Dictated text lands in the field for review and is never sent by itself. The retired «Диалог» page is no longer in the sidebar (its code stays; device controls moved to «Голос и язык»).
- «Наблюдения»: «Сейчас | История». History browses stored conclusions day by day (America/Chicago), by source, with the gaps between them labelled by reason (pause, capture gap, analysis down or behind, original deleted unanalysed) and a banner when analysis has stalled. Facts only, owner-local, read-only; retention is unchanged. New local command `ui-observations`; `ui-state.recent` now lists only observations that concluded something and carries `analysis`.
- «Изображения»: every image says what it is, where it came from (camera device, display, folder, model), when, where the file is («Показать в Finder») and whether it was sent to Telegram. `visual_artifacts` gains `origin_kind` and `place`; older rows are labelled from what is known. The local view no longer overwrites `source`.
- Settings fixes: «Настройки…» and «Диагностика…» are separate menu items; read-only components cannot be «saved»; `voice-select` runs inside the service; owner and guest setup report why they did not start and can be cancelled; the archive checkbox shows the choice (with «на паузе»); the dialogue model is probed on its own port; «Прослушать», «Применить язык» and device changes report results; schedule control gets a 60 s panel timeout and finished jobs read «выполнено»; the diagnostics text is rewritten only when it changes; the Pause menu item says what it will do and decides from fresh state.
- Panel reliability: a slow or dropped poll keeps the last good state with its age and shows «Нет связи» only after three failures; command failures are logged by class only (`panel.log`), and the service logs handler failures by class.
- App icon: the Dock and Cmd-Tab now use the system rendition of the icon (the same squircle Finder shows).

## 0.7.5 — 2026-10-26 (manual Busy authority)

- Keep owner-selected Busy active across automatic situation changes until the owner selects Free or Auto (DEC-095).
- Keep manual Free situation-bound and preserve the existing ten-minute spoken busy/free cues.

## 0.7.4 — 2026-10-26 (visual-only progress)

- Stop periodic spoken waiting/status messages. Ordinary dialogue gets no waiting announcement; a real task may receive one brief notice that progress is shown in the panel (DEC-094).
- Label active voice tasks as work in the owner panel; keep subsequent progress visual and truthful.

## 0.7.3 — 2026-10-26 (responsive ordinary dialogue)

- Answer ordinary questions directly from the current conversation context without unnecessary tools or long analysis (DEC-092).
- Answer questions about current work immediately from coordinator state.
- Keep explicit analysis and action requests on the task path with truthful status updates (DEC-091).

## 0.7.2 — 2026-10-26 (truthful work updates)

- Keep a short natural pause before ordinary answers, then answer directly without a spoken placeholder.
- Acknowledge pending work after 1.2 seconds and provide truthful status updates at intervals no longer than 20 seconds while no answer is ready (DEC-091).
- Tell the agent to provide a real stage and honest approximate timing for long work; remove the former one-minute silence allowance.

## 0.7.1 — 2026-10-26 (natural response timing)

- Remove the timed spoken placeholder before ordinary answers; allow the short real processing pause and speak substantive content as soon as it is available (DEC-090).
- Keep speech interruption separate from accepted work and retain actual streamed response delivery.

## 0.7.0 — 2026-10-26 (unified voice dialogue)

- Use one continuous input assembler and one dialogue coordinator for voice turns, interruption drafts, queued work, pending questions and streamed response delivery.
- Keep accepted work alive when speech output is interrupted, preserve dialogue across pauses and segmentation, and invalidate private work on explicit scope changes.
- Remove the separate barge-in ASR, contextless aside requests, detached result queue and repeated generic waiting phrases.
- Surface request and delivery state in the owner panel; keep automatic endpointing in shadow mode pending owner-speech calibration.
- Preserve owner attribution, privacy boundaries, explicit action authorization and the existing local memory-rule reload path.

## 0.5.2 — 2026-10-26

- Preserve the reply/committed state across real playback completion, including acknowledgement and streamed-sentence gaps.
- Keep eventual results of accepted agent work after an ordinary spoken interruption.
- Preserve bounded candidate speech prefixes before ASR admission and expose active capture in the dialogue.
- Enforce one acknowledgement plus at most three progress phrases; reject repeated, unrelated or action-inventing waiting text and prevent a generic fallback loop.
- Yield background work during capture, ASR, local dialogue inference and speech output while allowing it during a remote agent wait.

## 0.5.1 — 2026-10-26

- Send voice-channel and memory-tool instructions through the gateway's trusted developer channel, separate from the owner's utterance, so the agent does not reject rule reload as an injected command.
- Preserve explicit manual availability through service restarts; unchanged signals retain the owner's choice.

## 0.5.0 — 2026-10-25

- Require positive local attribution for unnamed busy-session follow-ups and preserve busy evidence after Butler is named (DEC-072).
- Isolate dialogue and interruption inference on Butler's dedicated Ollama service at numeric loopback port 11435 (DEC-073).
- Add exact episode corrections, immediate repair, asynchronous persistence, scoped canonical preferences and panel controls. Surface storage errors and backlog without claiming unsaved work succeeded.
- Correct the conversion between WebRTC's supported frame sizes and the specified 60 ms speech windows.
- Reload private conversation rules through the agent's existing memory tool (DEC-075), and honor any grammatical-case Butler-name mention against an unsure negative judge (DEC-076).
- Add Auto / Busy / Free controls that retain the owner's availability choice until established situation changes (DEC-077). Repeated microphone samples cannot defeat it.
- Add a Pipecat/Silero/Smart Turn endpoint adapter. Active endpointing remains gated on calibration using owner speech; the default remains the existing shadow mode.

# Pending: owner-local single-user mode

The owner authorized a single-user mode in Recordings DEC-097 after reporting false owner-verification blocks (MarkView BUG-010). A local opt-in separates history visibility from raw playback permission; pause, explicit archive off and consent revocation hide history. Automatic face/voice or crowd classifications no longer hide already-stored local history in this explicitly chosen mode. Local voice navigation accepts checked but unattributed speech without identifying the speaker or dispatching the request externally; spoken private summaries still require fresh healthy participant sensing and a safe call state. Media playback and file actions retain the existing stricter gates. A follow-up corrected gallery paths and the native gallery cache to use the stricter gate and made denied originals and paused history visibly distinct from missing data. This history scope was tested, installed and enabled; wider identity-gated local and voice paths remain under review.

Local feedback and voice-episode viewing also uses the single-user history signal. Soft identity loss keeps local rows while cancelling exports and privileged context; pause and consent revocation still clear them. Corrections retain their pre-existing stricter action gate, and the panel clears restricted episode/image caches on access loss or the first failed poll.

# Dictation boundary fix — 2026-10-28

- Start a new in-memory capture generation for each panel dictation, discard earlier pending or in-flight speech, and clip microphone blocks at the service's start and stop command timestamps. Stop waits for the final clipped block and its local recognition; silence and duration no longer end the session automatically. Manual pause and loss of microphone access still interrupt it.
- Dictation still stays in the message field, never sends itself, and does not change owner, participant, media or transmission permissions. The text limit remains in force.

- Restore saved dialogue-feedback episodes after a service restart into a separate single-user local read-only cache, without restoring agent context or correction authority. Respect consent, pause, archive-off and text expiry; the new read does not run retention.

- Permit source-authorized local rule browsing in single-user mode without automatic owner identity. Listing uses a nonmutating reader, filters tombstones, checks access again before returning, and marks the view read-only; the native dialog disables mutations while the stricter gate is closed.

- Recheck history and raw permissions before returning a panel snapshot. If the raw lease expires during assembly, remove gallery paths and image feed entries while preserving independently authorized local history; a hard close also removes history and feedback.

- Invalidate delayed rule-list replies and close the local rule dialog on access loss, a failed poll, pause or mode change. Poll during the modal event loop so an external pause or revocation is observed even while rules are open.
- Close an editable rule dialog if the dialogue lease expires, even when single-user read-only history stays visible; reopening uses the current access mode.
- If service stop begins during a panel snapshot, suppress live camera preview and report recording disabled in the final response.
- Deny strict raw access immediately when service stop begins, including playback already converting outside the service lock.
- Keep cached Recordings history and selection when only raw permission falls; stop playback, remove paths, invalidate pending media replies and refresh when raw permission returns. Hard close and failed poll still clear history.
- Reject a late Smart Turn result from a pre-dictation generation so it cannot reinsert old audio into the next dictated phrase.

# Recordings content opening — 2026-10-28

- Open a session’s first observation with one selection, report pending or failed analysis clearly, and preserve the opened result through periodic refresh. Default to records with facts or transcripts; the checkbox can show all records.
- Resolve a session using its former media, observation or transcript anchor if ongoing analysis changes the session identity between list and open. Preserve existing raw-media gates and pause/disconnection clearing.

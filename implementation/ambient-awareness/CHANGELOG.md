# Changelog — butler-ambient

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

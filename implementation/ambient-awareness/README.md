# Butler ambient awareness

Local observation service implementing `docs/features/butler-vision-and-ambient-awareness`.
It adds continuous camera/audio streams, minute-based screen capture and local
analysis, fresh on-demand MCP observations, retained context, summaries and bounded
Telegram advice. It does not replace OpenClaw's channel or session layer.

## State and privacy

Collection starts disabled and is enabled by the local owner. Under DEC-038,
`continuous_archive` records and analyzes camera, microphone, screen and separate
system playback locally, including meetings/media and unidentified interlocutors.
Unknown/action-identity uncertainty does not stop the passive archive. Full owner
Pause stops observation, pending analysis, transient context and speech. Recording
and speaking are independent: meeting quiet keeps the archive active and permits
only explicit Butler-directed replies.

Capture and bounded local utterance ASR run independently of participant inference
(DEC-052). A one-item latest-check queue prevents a slow biometric model from
blocking passive recording/hearing. Private replies wait for completion receipts
covering the exact PCM, including prefix and ending; only matched owner voice and
verified silence can support owner command authority. Skipped, stale or failed
checks break the qualifying permission sequence. The UI distinguishes capture age
from inference-completion age and shows ASR time, check wait, skipped checks and
controlled errors. Full Pause and settings/consent/reference changes reject old
in-flight work. No earlier owner lease authorizes a new unchecked utterance.

Privileged camera/MCP/image/external operations retain the original strict gate:
three consecutive fresh one-second participant checks against deliberately enrolled
face/voice references with active agreements. General-model identity guesses never
authenticate. Owner voice is independently checked for command confirmations;
guest speech cannot approve. DEC-035 unknown encounter fingerprints stay in volatile
memory at most120seconds and never become enrolled identities. Guest enrollment
requires explicit owner review, Mac account authentication and a fresh15second
spoken challenge with one distinct guest face/voice. Pause/cancel clears setup
media. Fixed public newcomer disclosures contain no private conversation.

Raw one-second MJPEG, PCM16 WAV and screenshots live in the private state directory
`~/.local/share/butler-ambient`. Native camera input remains available at its selected
resolution for identity/UI; both recording paths respect `recording_width`
(currently640pixels,5fps). Unpreserved media expire24hours after segment **start**,
including pending/failed analysis. Expiration remains independent of analysis.
Owner-selected media have separate preservation; derived results retain explicit
source/time provenance until owner deletion. Source/analysis failures and backlog
remain visible; no automatic long-term preservation occurs.

Raw media, transcripts and local free-text facts stay local. Ollama is hard-wired
to numeric loopback without proxies or redirects; model metadata must establish
local completion/vision capability before any raw input is sent. Ollama cloud
models and remote proxies are rejected. Whisper runs as a local process.
External consumers receive deterministic text summaries built from controlled
activity/work-topic vocabularies, source IDs/times and explicitly owner-corrected
names. Model free text, inferred names, transcripts, images and screen dumps are
excluded. Local detailed history retains book titles, appearances, inferred names,
and other supported details. These remain inferences; nothing writes to canonical
Obsidian knowledge. Recent evidence and owner-supplied identity context inform
subsequent local analyses. Corrections apply to historical retrieval.

## Components

| File | Responsibility |
| --- | --- |
| `sensing.py`, `people.py`, native audio/camera helpers | Native AVFoundation camera and input-only AUHAL/SpeexDSP microphone; local enrolled identity checks; FFmpeg device enumeration/fallback |
| `policy.py` | Owner enablement, freshness leases, permission pauses and automatic resumption |
| `store.py` | SQLite provenance, local media, expiration, preservation and deletion |
| `analysis.py` | Local Whisper/Ollama processing and typed summary export |
| `summaries.py` | Approximate durations, work topics, coverage and explicit gaps |
| `delivery.py` | Four advice domains, quiet hours, durable quotas and at-most-once delivery |
| `service.py` | Independent capture, analysis, maintenance and local Unix-socket control |
| `mcp.py` | Derived-text RPC and provenance-verified owner image requests; no permission/preservation tools |
| `native/screenshot.swift` | ScreenCaptureKit PNG capture directly to memory/stdout; separate permission-only mode |
| `native/launcher.swift` | Stable macOS app identity for device access and local service lifecycle |
| `deploy.py` | Private runtime, signed app, launch agent and Butler-scoped MCP installation |

## Build and dependencies

The current default dialogue/intent model is local `qwen3.5:9b` (DEC-040);
explicit `conversation_model` choices are preserved. Background vision uses the
separately selected `vision_model` (currently `gemma3:4b`). Install these through
Ollama before enabling; the model picker lists actual installed local models.
Structured realtime requests use `think:false`; no cloud/paid fallback is allowed.
Control addressing and action planning are separate; planner output cannot execute
without strict adapter checks and a bound owner confirmation. The main conversation
still streams reply text into the selected synthetic baritone.


Python 3.11+, macOS 14+, Xcode command-line tools, FFmpeg, local Ollama,
`whisper-cli`, and the existing OpenClaw CLI are required for runtime operation.
The repository launcher uses its `.venv` when present, with `BUTLER_AMBIENT_PYTHON` available as an explicit override. The core package and policy tests use only the Python standard library. Optional
face/voice dependencies are isolated in the `sensing` extra. No paid API or API key
is used, and local-model failures never trigger an external fallback.

```sh
cd implementation/ambient-awareness
python3 -m venv .venv
.venv/bin/pip install '.[sensing]'
make build
```

Supply local YOLOX/YuNet/SFace ONNX files, a local Whisper GGML model and a vision-capable
Ollama model in a copy of `config.example.json`. Model files are never downloaded
automatically by the service. `screen_helper` may point at the absolute built
`build/butler-screen` binary; the repository default finds it relative to this
implementation. An installed wheel includes Swift source; compile that source and
set `screen_helper` to its output path. macOS Camera, Microphone and Screen Recording
permissions must be granted to the launch context. `doctor` inspects dependencies
without opening devices. A missing model or denied permission reports unavailable
and preserves the policy boundary.

## Local owner interface

Deployment is authorized by DEC-007; live owner enrollment is authorized by
DEC-008. `deploy.py` installs the built wheel, native helpers, a signed
`ButlerAwareness.app`, and a private launch agent. Installation alone never
enables collection or enrolls participants. Runtime state and deployment evidence
are separate from the offline acceptance results.

```sh
bin/butler-ambient --config /path/to/local-config.json doctor
bin/butler-ambient --config /path/to/local-config.json enroll owner Boris \
  --photo /path/to/owner-reference.jpg --voice /path/to/owner-16khz-mono.wav --prior-agreement
bin/butler-ambient context owner Boris 'Owner appearance from existing confirmed context'
bin/butler-ambient --config /path/to/local-config.json run
```

While `run` owns the state directory, another local terminal can use `status`,
`resume`, `pause`, `revoke <participant-id>`, `observe camera`, `observe screen`,
`summary <start-unix-time> <end-unix-time> [--local-details]`, `preserve <source-id>`,
`delete <source-id> [--results]`, and `correct <appearance-key> <name>`.
Enrollment uses owner-selected existing reference media or explicit `enroll-live`
setup while collection is disabled. Live setup discards reference media and saves
only local reference vectors. Optional `enroll-live --multiple-samples` saves
several explicitly supplied face poses and speech-window references (at most 30
per modality); normal observation never adds templates. Further live enrollment
still requires owner authorization. `screen-permission` requests the macOS screen access
dialog through the running app without capturing a screen, even during a pause.
Identity context and recording permission are separate owner controls.

`preferences '{"muted":true}'` mutes advice. `disabled_topics` accepts any subset
of `work`, `rest`, `reading`, and `entertainment`. `daily_time` is configurable
as `HH:MM` between 08:00 and 21:59 America/Chicago; the default is 20:00.
Daily summaries and requested replies do not consume the advice quota.

Advice is autonomous within the four agreed domains and explains its expected
benefit, uncertain observational basis and evidence IDs. YouTube alone never
causes reduction advice. No suggestion executes an action. The existing Butler
Telegram conversation is configured by `telegram_target`, using OpenClaw account
`butler`; there is no second bot or direct Telegram API implementation.

Quiet hours are 22:00–08:00 America/Chicago. At most three advice messages may be
delivered per local calendar day, separated by at least 60 minutes after receipt.
A transactional reservation precedes transmission. A crash, timeout or missing
receipt leaves an uncertain reservation that is never resent; this deliberately
prefers a missed message over a duplicate when the transport cannot prove delivery.
Uncertain reservations consume quota. Suppressed candidates expire rather than
forming a catch-up queue. A 45-second delivery margin protects quiet-hour edges.

## MCP integration

Launch `bin/butler-ambient mcp` as a stdio MCP server with the same `--state` as
the running service. Register this command with the existing Butler MCP client.
The deployed entry is scoped to `workspace-butler/config/mcporter.json`; it is
not shared with other bots. Butler instructions use mcporter to invoke it.
Example client entry:

```json
{
  "mcpServers": {
    "butler-ambient": {
      "command": "/absolute/path/implementation/ambient-awareness/bin/butler-ambient",
      "args": ["--state", "/absolute/private/state/path", "mcp"]
    }
  }
}
```

Tools are `butler_observation_status`, `butler_observe` (`camera` or `screen`),
`butler_activity_summary` (Unix `start`/`end`), and `butler_visual_request` (no arguments). The bridge supports
MCP 2025-06-18. It returns only text, source metadata and explicit unavailable or
paused results. On-demand observations wait for capture after the request;
cached or stale images cannot masquerade as fresh ones. The bridge cannot enroll,
revoke, enable, preserve, delete, correct identities or change notification settings.

## Verification and operating limits

`make verify` runs feature tests, all three existing implementation suites, the
native build and wheel build, and saves exact commands/output in `evidence/`.
`VERIFICATION.md` maps acceptance criteria to code and tests. Runtime tests use
synthetic FFmpeg video/audio, blank images and a temporary disabled service.
They never open a real camera, microphone or screen, or send a Telegram message.

Reference matching is probabilistic: SFace cosine threshold 0.363 (OpenCV documented model
recommendation; DEC-009), voice cosine threshold 0.82, and ambiguity margin 0.10 are
model settings,
not measured accuracy guarantees. Off-camera silent people, occlusion, overlapping
speech, replay attacks and false matches remain limitations of these sensors.
Freshness and confidence failures pause capture; a general-model name cannot
override them. Physical recognition accuracy and live delivery cannot be established by offline
tests. Separate deployment evidence records device grants, actual local inference,
MCP integration and successful or paused capture without exporting raw media. Analysis
ticks that cannot complete are recorded as busy/missed/failure gaps; the service
does not invent coverage or extend retention to catch up. Expiration cannot run
while the machine or service is stopped; it catches up on startup/access.

## Primary technical references

- [Ollama generation API](https://docs.ollama.com/api/generate)
- [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [OpenCV YOLOX model and preprocessing](https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox)
- [OpenCV YuNet/SFace APIs and models](https://docs.opencv.org/4.13.0/d0/dd4/tutorial_dnn_face.html)
- [Resemblyzer local voice encoder](https://github.com/resemble-ai/Resemblyzer)
- [Whisper.cpp in-memory stdin audio decoding](https://github.com/ggml-org/whisper.cpp/blob/master/examples/common-whisper.cpp)
- [Apple ScreenCaptureKit screenshot API](https://developer.apple.com/documentation/screencapturekit/scscreenshotmanager)


## Local panel, spoken dialogue and requested images

The native menu-bar panel reports current capture/analysis times, anonymous pause
reasons and permitted camera previews without waiting for minute analysis. Preview
frames are already-recorded media, hidden on a recording pause. Rich local UI
commands never go through model-facing MCP. A scrollable panel exposes voice,
selected-material roots, search, drawing, cancellation and result deletion.

The owner first chose calm (DEC-014), then selected warm baritone (current DEC-016). Qwen3-TTS Base 1.7B 8bit
runs in an isolated local MLX environment and uses only the reviewed synthetic
preset, never biometric enrollment. The VoiceDesign download was removed after
preview creation; its revision manifest and all three presets remain. Native
ordinary AVAudioEngine I/O and local SpeexDSP 1.2.1 supply echo cancellation with
a playback reference. VPIO is deliberately not enabled: it attenuated YouTube
even with minimum adaptive ducking. Hardware input format is preserved; output
uses its normal hardware rate. The bundled local DSP dependency is installed with
`brew install speexdsp` and copied beside the helper. If this
path fails, plain FFmpeg microphone observation resumes under the original gate;
conversation remains unavailable without echo support.

DEC-030/032 make the wake name optional and use context to distinguish a question,
follow-up, speech to another person and a reason to stay silent. Fresh enrolled
owner voice or the accepted face route can establish dialogue permission after
three checks; uncertain/stale evidence on the selected route cancels private
dialogue. Q-013 is still open on a separate unauthenticated general-conversation
mode; it has not been enabled. Privileged commands require independently verified
owner speech. DEC-038/042 separately keep the authorized passive local microphone
ASR/archive active even when private dialogue is blocked. Local diagnostics show
what was heard and why a reply was blocked; transient hearing debug expires after
120 seconds. A fixed public hearing acknowledgement grants no private authority.
Full Pause stops capture, ASR, context and speech. UI foreground is not required:
the own LaunchAgent runs observation independently. Actual Butler input, Mac
default input and output are shown separately in diagnostics. Spoken unsolicited
advice and Telegram share one quota ledger. Latest real owner conversation is
still failing; see F-004 and the current completion evidence rather than treating
healthy source counters or synthetic tests as a working live dialogue.

The voice registry uses the existing settings, schedules, CLI workers and local
storage/image adapters (DEC-034/036/047). Personal reads require a fresh owner
lease; mutations first name their concrete target and parameters, then require a
one-use owner confirmation. For example, “Составь сводку за последние полчаса”,
“Сохрани отдельно последнюю запись с микрофона”, or “Удали только оригинал этой
записи, анализ оставь”. Unknown/ambiguous targets need clarification. Expiry,
permission loss, cancellation or a replacement request invalidate approval.
Original-only deletion leaves analysis; deleting analysis too must be explicit.
An observed-name correction never enrolls a person or grants recording consent.

Personal-file search reads only files/folders chosen with the native picker or
explicitly added through an owner-reviewed voice operation.
Originals remain in place; hidden files, symlinks and application/photo-library
packages are skipped. Each explicit search examines up to 24 candidates (filename
matches then recency), reports partial coverage and ranks local visual captions.
Results remain candidates. Captured, imported and generated images carry distinct
provenance. Expired/deleted camera media cannot be resurrected; removing a search
result never deletes a personal original. Generated results can be deleted locally.

Requested drawing uses an isolated offline Diffusers/MPS SD-Turbo pipeline at
512×512, four steps, with progress/cancellation, no paid/external fallback and an
11 GiB free-space reserve. Pinned model revisions are in the private installation
manifests. `image-requirements.txt` pins inference dependencies. Large jobs run in
a separate low-priority process and release resources afterward.

`butler_visual_request` accepts no prompt, path, recipient or authorization flag.
The service reads the actual latest fresh owner user message from the pinned
private Butler Telegram session, validates routing/source/time, requires a direct
image request and high-confidence local intent, and binds work to its event ID.
The service uploads only to private chat 253483520 on account butler. It reserves
before upload and records the receipt; uncertain outcomes are never retried. MCP
returns only controlled status/IDs/provenance, never images, paths, captions or
raw request text. Voice and local UI can also make specific requests; Telegram is
used only when explicitly requested. No real test image is sent without that request.

Additional primary references:
- [Qwen3-TTS](https://qwen.ai/blog?id=qwen3tts-0115)
- [MLX Audio Qwen3-TTS API](https://github.com/Blaizzy/mlx-audio/blob/main/docs/models/tts/qwen3-tts.md)
- [Apple voice-processing API](https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled(_:))
- [SD-Turbo model card](https://huggingface.co/stabilityai/sd-turbo)
- [Diffusers on Apple Silicon](https://huggingface.co/docs/diffusers/optimization/mps)

Whisper.cpp 1.8.x stdin output requires explicit `-otxt -of -`; the real synthetic-audio regression test confirms the transcript arrives in stdout and no text file is created. A successful process exit alone does not prove speech recognition.

### Operational voice and Settings repair in progress

DEC-017 resumes the unfinished live voice path. DEC-018–022 add owner-local split diagnostics, actual input level/freshness, green/red working lamps, editable model/device controls and a main AI/CLI/schedule control center. Transcripts/events remain in memory on this Mac and clear on dialogue permission loss; they never enter MCP. DEC-023 defaults speech recognition explicitly to Russian, with an editable language selector. Whisper Large v3 Turbo Q5_0 is pinned in evidence/whisper-turbo-manifest.json. A permitted one-second pre-roll preserves early speech; a separate local recognizer distinguishes owner interruption from residual self-echo during playback. A fixed synthetic check is labelled as such and cannot establish live microphone recognition. Exact hearing-check replies use a prepared recording of the selected synthetic preset, never owner voice. I-14 is verified and installed; I-10/I-13 remain in progress because actual live recognition/reply and post-replacement background audibility remain unconfirmed (DEC-024).

### Main Settings: AI, CLI and daily runs

Open **Butler** from `~/Applications/Butler.app`, the menu bar or Dock. The UI is a separate app from the stable sensing permission parent. Reopening the app brings the existing window forward; only one UI instance runs. Closing the window leaves background work running. One native window has a sidebar: Дисплей, Наблюдения, Голос и язык, Изображения, Модели по задачам, Расписание, Компоненты и CLI, Диагностика. «Голос и язык» keeps device controls and a fixed preview on the left and the voice, meeting and owner-setup settings on the right; the former «Диалог» page is retired from the sidebar (its code remains). Green/red lamps distinguish device health, permission and voice readiness. Detailed hardware health is in Диагностика. The preview keeps its dimensions with no image, landscape or portrait input. Window geometry and selected section persist; refresh preserves selected IDs, unsaved editors and table scroll position. Twenty-two effective AI components and twenty Butler jobs are currently discovered; readiness and running work are separate states.

Select a component to edit a compatible model/file/folder or executable. Local models are capability-checked; recognition files are exercised on synthetic input; configured Python environments must import the required runtime. Only existing approved Butler agent/subscription routes are selectable. Ambient model/device changes restart only the own observation service. Dispatch/semantic changes apply on the next task; a changed embedding model/dimension makes the old index incompatible. Other agents and existing cost guards are preserved.

Scheduled jobs support run, pause future launches, resume, stop current isolated work, and supported schedule/model editing. Built-in daily summary/advice/analysis pauses persist. Advice still follows evidence, shared quota and quiet hours; manual summary/analysis still requires participant permission. No schedule is removed and the shared gateway is not restarted.

Owner-local CLI mirrors: `ui-components`, `ui-components-update JSON`, `ui-schedules`, `ui-schedule-control JSON`, alongside `ui-settings`/`ui-settings-update JSON`. These are absent from model-facing MCP. Credentials, provider auth and job prompts are never returned. Runtime receipts and exact verification are in [VERIFICATION.md](VERIFICATION.md).

Current installed verification: **223 automated tests**, 38 native UI checks, native AEC/raw-input smoke, four native builds, wheel and doctor all pass. Hardware mute/zero gain blocks both participant gates; unknown hardware health is reported as unknown. Live owner recognition/reply and background audibility remain open, as recorded in DEC-024 and F-004.


### Models by task — DEC-025 / REQ-015

In **Модели по задачам**, select the main Butler agent, local dialogue or vision, or a named task (architecture, code, review, research, documentation, tests, curation, classification, news, video). Choose a supported model from the menu and click **Применить к задаче**. Only that stable task/component ID is saved. Local dialogue and vision are separate choices; editing one no longer changes both. For a particular scheduled job, choose it in **Расписание** and edit **Модель задания**. Compatible model files and CLI paths remain in **Компоненты и CLI**. Existing approval/cost rules and other agents are preserved.

The native `--test-layout <output-directory>` mode uses only built-in synthetic state and images, never a CLI, service, devices or network. It exports layout results and synthetic view bitmaps for inspection, including light/dark appearances. `verify.py` runs it after compiling the native panel. Live owner recognition and background audibility remain separate unresolved checks under DEC-024.


### Dynamic provider catalogs — DEC-026 / REQ-016

**Модели по задачам → Обновить каталоги** retrieves metadata from the installed Codex and Claude CLIs using their existing configured sign-ins. Codex uses initialize + model/list (including pagination); Claude uses only the initialize control handshake with tools, customizations and MCP disabled. No user prompt, inference turn, observation or credential is passed by Butler. The service refreshes hourly even with the window closed. Current source, refresh time, failures and stale cache are visible. CLI catalogs can themselves be cached and listing is not an inference entitlement test. See [Codex model/list](https://learn.chatgpt.com/docs/app-server#models), [OpenAI catalog limitations](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) and [Claude model configuration](https://code.claude.com/docs/en/model-config).

The private `workspace-butler/config/model-catalog.json` cache supplies both selectors and dispatch validation. New model IDs need no source edit. Missing selections stay visible with Apply disabled until a supported choice is made; no current model is silently replaced. Cached catalog IDs have a seven-day selection lease; provider failure retains the last successful catalog with an error and age. Only the intersection with available models in the installed OpenClaw provider registry is offered to the Butler agent and cron jobs; CLI task routes can use newer models independently. The UI reports this runtime compatibility boundary. Existing provider prefixes only are offered; these saves do not change global allowlists, other agents or active model settings automatically. Choosing a listed model still can fail at execution if account/runtime access differs; that error remains visible in job/agent diagnostics. Cloud catalog retrieval does not download proprietary model weights. Local sensing, ASR, TTS and generation retain local model compatibility checks and no cloud fallback.

Owner CLI mirrors: `ui-catalog`, `ui-catalog-refresh`. These are absent from MCP. The refreshed dispatch adapter changes only the previously owned Settings block and retains exact backups and all downstream cost guards.


## Always-active local conversation

During a fresh permitted dialogue lease, Butler evaluates admitted speech without requiring a wake name. It retains a bounded temporary conversation and ambient-speech context, including when it stays silent. A name is a useful cue; follow-ups no longer expire after20seconds. Explicit requests not to answer stay silent, and unnamed stop/interruption phrases cancel output. Pause stops observation and clears ephemeral context; recording/images keep the stricter original gate.

Whisper, the local dialogue model and the selected synthetic Qwen voice remain loaded. A validated first reply clause streams into incremental native playback while the rest is generated. Main Dialogue shows model readiness and last response timings; detailed diagnostics show actual ASR executable and stage counters. The supplied portrait is packaged as Butler.app's icon. See DEC-027–030 and VERIFICATION.md for actual measurements and remaining live limits.

Developer-only fixed fixture command `butler-ambient ui-voice-benchmark` uses the existing real participant gate and labels its source synthetic. It does not accept arbitrary input, export owner text, bypass permissions or establish live microphone correctness.

Current installed measurement: first playback2.350s from ready fixed Russian input, with full native playback completion; separate real microphone response metadata shows2.574s. Endpoint/check wait is additional. These measurements do not establish recognition accuracy for your voice or guarantee every turn has the same latency.

## Current live conversation limitation (DEC-031)

The owner reports ordinary unnamed questions and follow-ups are still ignored. Always-active contextual conversation is the required behavior, but its live verification has failed; controlled fixtures do not establish success. REQ-017/I-17 remain in progress while actual per-turn cancellation and addressing are diagnosed. Pause and participant controls remain binding.

## Conversational display — feature butler-conversational-display (0.4.0)

The panel has a «Дисплей» section for material that is better seen than heard. Cards are structured JSON (DEC-002): `text` (Markdown with lists and links), `image` (a file Butler already owns), `diagram` (Mermaid source, rendered offline by the bundled `assets/mermaid.min.js` + `assets/diagram.html` in a WKWebView), `video` (a prepared file under `~/.openclaw/media`, AVKit controls; no video generation, DEC-001) and `svg` (a drawing; see below) and `email` (built locally from the mail store by message key; bodies never travel). The butler agent presents cards through the MCP tool `butler_display` (socket command `display`; valid cards can queue locally, while visibility and actions require the verified owner); ready visual results become image cards. A new card brings the window forward; deploys never open it. History is kept in memory for the current conversation only and cleared when the owner session hard-closes or the service restarts (REQ-011). Visibility follows the dialogue lease; image and video paths are withheld while recording is paused (REQ-010). The email card's only action opens the prefilled reply in Mail.app and never sends; sending is `butler-mail reply <n> --from-mail` with an approval bound to the captured draft (DEC-041). The app shows its version in the sidebar, the window subtitle and `Info.plist`.

Display 0.9.0 makes it a visual layer the agent draws on its own. Any card except `email` may carry up to six `buttons` (`reply`, `link`, `reveal`; the targets stay in the service, no approval buttons exist). `replaces: <card id>` redraws one canvas per topic in place. `image` and `diagram` cards take a `markdown` caption and zoom by pinch. `assets/diagram_templates.json` holds 11 Mermaid templates that the native layout test renders; `python -m butler_ambient.diagram_templates --markdown` writes the agent playbook from them. The `svg` kind shows a drawing after `svg_clean.py` (allow-listed elements and attributes, local `url(#id)` only, `viewBox` required) and then only in a page without JavaScript under a `default-src 'none'` policy. Recordings DEC-004 supersedes Display DEC-047/048: cards and history are visible only to the verified owner while the observation gate permits access; selection and actions have the same check. A temporary loss hides the history, and a real close (pause, consent revoked, settings change, enrollment, service stop) clears it. Replies that arrive after the panel's own request ended are followed through the gateway's read-only session history (`session_follow.py`). The panel agent runs on the butler's native model unless `panel_model_override` is set; `python -m butler_ambient.provider_audit` proves from the session transcripts that only subscription logins answered.

Below the Display thread there is a message box. Typing, or «Диктовать» (one utterance transcribed locally by Whisper and placed in the field for review), sends to the butler agent through the gateway session `agent:butler:panel`; the answer is shown in the panel only (the gateway call never delivers to a channel), and a conversation that starts in Telegram is answered in Telegram by OpenClaw itself. The thread is kept in memory for the current service run and cleared when consent is revoked. «Наблюдения» has «Сейчас» and «История»: history is the stored conclusions by day and source with the gaps between them labelled (`ui-observations`, owner-local, read-only, facts only). «Изображения» shows for every image what it is, where it came from, when, the file location and whether it was sent.


## Recordings (0.10.0)

«Записи» is the local owner-only browser for observations and retained content. Choose a day and source, then double-click an activity/call session to expand its individual observations. The detail shows all stored fact fields and any retained transcript. Each media file has its own selector and preserve/delete controls; deleting a shared file leaves the associated facts and transcripts intact. Key frames and generated images are shown inline. Audio/video use AVKit; FFmpeg creates a disposable MP4 playback copy for concatenated camera MJPEG originals. Missing originals are labelled explicitly. Gap rows show pause, buffer-full, restart and missed/expired-analysis intervals and cannot be selected as observations.

Owner speech and detected-call transcripts expire 30 days after their capture start. In a detected call, «Сохранять текущую встречу» explicitly marks that call; subsequent permitted microphone/system audio and camera video expire 30 days after each file’s capture start, even if preserved manually. The mark is not inherited by a new call or restart. Capture permissions, participant checks, Pause and consent revocation continue to govern acquisition. Marking does not recreate earlier deleted originals or start disabled sources.

«Батлер, покажи, что было на встрече в 8 вечера» navigates locally to that day/time (America/Chicago); yesterday and ISO dates are supported. Navigation is intercepted before agent routing, including the panel’s dictated/typed route. Recordings APIs and transcript tables are absent from MCP. Legacy observation MCP responses expose coverage/status metadata only, with local facts withheld. Generated images, Display history, cached recordings and their actions are hidden when owner verification is absent; disconnection clears the panel’s private recordings cache.

Versioning uses the package `__version__` as the wheel and app bundle version, with additive features advancing the minor version; this release is 0.10.2, paired with core 0.17.1. Run `python verify_attention.py` for the full local test/build gate, then `python release_attention.py` to deploy the exact verified wheels, native helpers and UI via the existing launch agents. The release script keeps private rollback copies and checks installed, running and bundle versions plus UI binary hashes.

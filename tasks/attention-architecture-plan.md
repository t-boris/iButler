# Butler attention architecture — rebuild plan (proposal, 2026-10-24)

## Why the current dialogue layer fails (what the audits showed today)
- No model of *whom the owner is talking to*. The addressee decision is a single LLM call on the transcript plus
  flags; a phone call, a person beside, dictation to an AI are invisible to it. Result: «я помогу найти парковку?»
  during a phone call, and silence on real questions.
- Identity lease, meeting mode, source gaps and the LLM decision all cancel or suppress turns independently;
  the owner sees «sometimes answers, sometimes not».
- Hand-rolled barge-in, endpointing and streaming (feed/_barge/_turn) duplicate what mature pipelines already do
  with tested components, and every patch adds a state. The owner's verdict: throw it away.

## What existing systems do (research, 2026-10-24)
- **Device-directed speech detection (DDSD)** is a classifier, not a prompt: Amazon fuses acoustic, lexical and
  ASR-decoder signals (EER −39 % vs text-only, arXiv 2403.14438) and, on Echo Show, camera body position
  (Alexa "natural turn-taking", "Conversation Mode"). Apple classifies follow-ups with the prior query and ASR
  uncertainty (arXiv 2411.00023). No open local model exists; attenlabs SAA is a hosted classifier (not acceptable:
  raw audio leaves the Mac).
- **Turn-taking frameworks**: Pipecat (BSD-2, Python) has a frame pipeline with Silero VAD, user-turn *start*
  strategies (`MinWordsUserTurnStartStrategy`, so «угу» does not interrupt), a *stop* strategy with **Smart Turn v3**
  (BSD-2, 8 MB, CPU, 23 languages incl. Russian, ~10–65 ms) that decides when a thought is finished from prosody,
  local Whisper STT, Ollama LLM, pluggable TTS and a local audio transport. LiveKit's turn detector is comparable but
  its weights carry a custom license.
- **Full-duplex speech models** (Moshi family) handle overlaps natively but are English/French, heavy, and cannot
  be constrained for privacy and Russian: not a fit.

## Target architecture
1. **Attention state machine** (new module, the heart): IDLE · ENGAGED (Butler session, follow-ups accepted for 5 min)
   · OWNER_BUSY (owner in a conversation with someone else) · PAUSED. OWNER_BUSY is entered from fused signals:
   phone at ear (local vision on the preview frame, when lit), a second face or voice, a microphone app (call,
   dictation to an AI), a meeting app, "alone but looking away while talking" for >10 s. In OWNER_BUSY Butler only
   records; only the name or a stop word opens a turn.
2. **Addressee classifier** (local): inputs = attention state, lexical features (vocative, question form,
   second person, ASR confidence), gaze/facing, lip motion, session recency; output = addressed probability.
   First version: a bounded local-LLM judge with these as structured inputs plus hard rules; later a small
   trained classifier on the owner's own labelled turns (the panel already shows every heard line: one tap
   "это было мне / не мне" builds the set).
3. **Turn pipeline on Pipecat**: AEC'd PCM from the existing native audio helper → Silero VAD → Smart Turn v3
   (end of thought) → Whisper (existing server) → addressee gate → Ollama (existing model) → existing TTS worker →
   speaker. Interruption = MinWords(3) + stop-word escape («стоп», «подожди», «хватит») + owner-voice check;
   other voices never interrupt. Identity lease decides *scope* (private vs general) at turn start and never
   cancels a running turn.
4. **Privacy unchanged**: raw audio/frames never leave the Mac; Full Pause, consent, biometric thresholds as before.
5. **Evaluation**: a labelled set of the owner's real utterances (addressed / not) and the four metrics the owner
   cares about: false replies during calls, missed questions, interruptions by noise, time to first word.

## Phases
- P0 (done today, 0.3.6): no default-addressee unless facing the Mac; interruptions only by real words.
- P1 (1–2 days): attention state machine + busy signals (phone-at-ear vision, mic apps, second face/voice) inside
  the current service; silence in OWNER_BUSY; name always answers.
- P2 (2–4 days): Pipecat turn pipeline replacing feed/_barge/_turn; Smart Turn v3; min-words interruption.
- P3 (ongoing): labelled set from the panel; classifier replaces the LLM judge; weekly numbers.

## Open for the owner
- Approve Pipecat + Smart Turn v3 as new dependencies (BSD-2 both; local only).
- Camera policy: keep continuous capture (his DEC-001/DEC-038) or reduce to verification + on demand.

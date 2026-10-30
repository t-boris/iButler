"""DEC-038 local hearing is independent of permission to answer or execute."""
from __future__ import annotations

import queue
import threading
import math
from array import array
import time
from collections import deque
from dataclasses import replace

from .analysis import LocalAnalyzer
from .attention import BUSY, CAUSE_TEXT
from .conversation import GENERAL, Conversation, explicit_address, hearing_check, hearing_vocative, silence_requested, single_user_summary_request, speech_echo, STOP_CUE
from .sensing import Batch
from .transcript import DraftDecoder, TranscriptStream, join_segments
from .dialogue_state import unfinished


class MicrophoneCheck:
    """Completion of identity processing for one exact captured microphone second."""
    def __init__(self, at):
        self.at = at
        self.done = threading.Event()
        self.checked = self.owner = self.silent = self.other = self.nonowner = False

    def resolve(self, evidence=None, *, now=None, trusted=frozenset({'owner'})):
        if self.done.is_set(): return
        at = getattr(evidence, 'captured_at', None)
        self.checked = (type(at) in (int,float) and abs(at-self.at)<1e-6
            and now is not None and 0 <= now-self.at <= 2.5
            and getattr(evidence,'microphone_ok',False) is True)
        self.owner = (self.checked and getattr(evidence,'voice_certain',False) is True
            and evidence.voice_ids == frozenset({'owner'}))
        self.silent = (self.checked and getattr(evidence,'voice_certain',False) is True
            and not evidence.speech_detected and not evidence.voice_ids)
        # DEC-066: a certain voice that is neither the owner's nor a household member's is company.
        self.other = (self.checked and getattr(evidence,'voice_certain',False) is True
            and bool(frozenset(evidence.voice_ids) - trusted))
        self.nonowner = (self.checked and getattr(evidence, 'voice_certain', False) is True
                         and bool(evidence.voice_ids) and evidence.voice_ids != frozenset({'owner'}))
        self.done.set()


class UtteranceChecks:
    def __init__(self): self.items = []

    @property
    def complete(self): return bool(self.items) and all(item.done.is_set() for item in self.items)

    @property
    def checked(self): return self.complete and all(item.checked for item in self.items)

    @property
    def company(self): return any(item.other for item in self.items)

    def __bool__(self):
        return (self.checked and any(item.owner for item in self.items)
            and all(item.owner or item.silent for item in self.items))


class Listening:
    def __init__(self, service):
        self.service = service
        voice = service.voice
        self.model = LocalAnalyzer(whisper_model=service.config.get('whisper_model'),
            whisper_language=service.config.get('speech_language', 'ru'),
            whisper_cli=service.config.get('whisper_cli', 'whisper-cli'))
        if voice.transcriber.available:
            self.model.transcriber = voice.transcriber.client()
        self.token = threading.Event()
        self.audio = bytearray()
        self.pre_roll = b''
        self.dictation_generation = 0
        self.dictation_id = None
        self.pre_check = None
        self.pre_checks = []
        self.candidate_active = False
        self.candidate_quiet = 0
        self.checks = UtteranceChecks()
        self.last_end = None
        self.last_sample = None
        self.stream_id = None
        self.phase = 'waiting'
        self.last_heard = ''
        self.heard_at = float('-inf')
        self.events = deque(maxlen=24)
        self.counts = {}
        self.timings = {}
        self.jobs = queue.Queue(maxsize=3)
        self.public_attention = None
        self.attention_epoch = 0
        self.pending_endpoint = None
        self.pending_thought = None
        self.prior_checks = []
        self.audio_confirmation_only = False
        self.transcripts = TranscriptStream()
        self.utterance = None
        self.last_draft_size = 0
        self.segment_text = {}
        self.processing = False
        self.drafts = None
        if voice.transcriber.available:
            draft_model = LocalAnalyzer()
            draft_model.transcriber = voice.transcriber.client()
            self.drafts = DraftDecoder(draft_model, service.stop_event, self._draft)

    def _begin(self, born):
        if self.utterance is None:
            pending, self.pending_thought = self.pending_thought, None
            if pending and (pending['epoch'] != self.service.voice_gate.generation
                            or pending['id'] not in self.transcripts.rows):
                self._revision(pending['id'], '', 'scope-closed')
                self.segment_text.pop(pending['id'], None)
                pending = None
            if pending:
                self.utterance = pending['id']
                self.prior_checks = list(pending['checks'].items) if isinstance(pending['checks'], UtteranceChecks) else []
            else:
                self.utterance = self.transcripts.begin(born)
                current_dictation = self._dictating()
                self.transcripts.rows[self.utterance]['dictation_id'] = current_dictation.id if current_dictation else None
                self.transcripts.rows[self.utterance]['dictation_generation'] = self.dictation_generation
                self.transcripts.rows[self.utterance]['private_epoch'] = (self.service.voice_gate.generation
                    if self.service.voice_gate.permits(self.service.clock()) else None)
                self.prior_checks = []
            self.last_draft_size = 0
        return self.utterance

    def begin_dictation(self, identifier):
        """Start a fresh assembler without cancelling already accepted agent work."""
        self.dictation_generation += 1
        self.dictation_id = identifier
        self._cancel_capture('dictation-boundary')
        self._discard_pending('dictation-boundary')
        if self.pending_endpoint is not None:
            pending = self.pending_endpoint
            if len(pending) > 5: self._revision(pending[5], '', 'dictation-boundary')
            self.pending_endpoint = None
        self.audio.clear(); self.pre_roll = b''; self.pre_check = None; self.pre_checks = []
        self.candidate_active = False; self.candidate_quiet = 0; self.checks = UtteranceChecks()
        self.last_draft_size = 0
        if not self.processing: self.phase = 'waiting'

    def stop_dictation(self, identifier, at):
        # The next complete microphone block is clipped at this timestamp;
        # this includes its pre-stop portion but never admits its later samples.
        if identifier != self.dictation_id: raise ValueError('dictation session changed')

    def _revision(self, identifier, text, state):
        revision = self.transcripts.update(identifier, text, state)
        feedback = getattr(self.service, 'feedback', None)
        if revision is not None and feedback is not None:
            feedback.transcript(revision)
        return revision

    def _dictating(self):
        """The owner pressed «Диктовать» in the panel: finished utterances belong to the message field only."""
        dictation = getattr(self.service, 'dictation', None)
        return dictation if dictation is not None and dictation.listening() else None

    def _draft(self, identifier, text):
        with self.service.lock:
            if not self.enabled or identifier != self.utterance: return
            text = join_segments(self.segment_text.get(identifier, ''), text)
            dictation = self._dictating()
            if dictation is not None:
                # No transcript revision (the feedback store keeps those) and no barge-in check: the words reach the panel only.
                dictation.partial(text)
                return
            self._revision(identifier, text, 'draft')
            checks = self.checks
            owner, other = bool(checks), checks.company or any(item.nonowner for item in checks.items)
        self.service.voice.observe_input(text, identifier, owner=owner, other=other)

    def _cancel_capture(self, state):
        if self.utterance is not None:
            self._revision(self.utterance, '', state)
            self.segment_text.pop(self.utterance, None)
        self.utterance = None
        if self.drafts is not None: self.drafts.cancel()

    def _discard_pending(self, state):
        if self.pending_thought:
            identifier = self.pending_thought['id']
            self._revision(identifier, '', state)
            self.segment_text.pop(identifier, None)
            self.pending_thought = None
        self.prior_checks = []

    def expire(self):
        self.transcripts.expire(self.service.clock())
        self.segment_text = {key: text for key, text in self.segment_text.items() if key in self.transcripts.rows}
        if self.pending_thought and self.pending_thought['id'] not in self.transcripts.rows:
            self._discard_pending('expired')
        if self.utterance is not None and self.utterance not in self.transcripts.rows:
            self._cancel_capture('expired')
            self.audio.clear()

    @property
    def enabled(self):
        s = self.service
        return (s.gate.enabled and not s.stop_event.is_set() and not s.owner_setup.capturing
                and (s.archive.enabled or s.voice_gate.permits(s.clock())))

    def mark(self, key, detail=''):
        self.counts[key] = self.counts.get(key, 0) + 1
        # Passive hearing never verifies a speaker, so a heard line is an unnamed voice.
        who = 'Butler' if key in ('reply', 'hearing-replied') else 'Голос' if key == 'heard' else 'Система'
        self.events.append({'at': self.service.clock(), 'event': key, 'detail': detail[:900], 'who': who})
        feedback = getattr(self.service, 'feedback', None)
        episode = getattr(self, 'current_episode', None)
        if feedback is not None and episode and key not in {'speech-candidate','input-discontinuity','queue-full'}:
            fields = {'outcome': key}
            if key in {'owner-busy', 'echo', 'general-not-addressed', 'reply-blocked', 'reply-blocked-unchecked'}:
                fields['accepted'] = False
            feedback.decision(episode, fields)

    def clear(self):
        dictation = getattr(self.service, 'dictation', None)
        if dictation is not None: dictation.interrupt('hearing-stopped')
        self._cancel_capture('cancelled')
        self.transcripts.rows.clear()
        self.segment_text.clear()
        self._clear_attention()
        # DEC-062: general dialogue exists only while passive hearing does.
        voice = getattr(self.service, 'voice', None)
        if voice is not None: voice.end_general('hearing-stopped')
        self.token.set()
        self.model.cancel()
        self.token = threading.Event()
        self.audio.clear()
        self.pending_endpoint = None
        self.pending_thought = None
        self.prior_checks = []
        self.checks = UtteranceChecks()
        self.pre_roll = b''
        self.pre_check = None
        self.pre_checks = []
        self.candidate_active = False
        self.candidate_quiet = 0
        self.last_end = None
        self.last_sample = None
        self.stream_id = None
        self.last_heard = ''
        self.heard_at = float('-inf')
        self.events.clear()
        self.timings = {}
        self.phase = 'paused'
        while not self.jobs.empty():
            try: self.jobs.get_nowait()
            except queue.Empty: break

    def _clear_attention(self):
        self.public_attention=None
        self.attention_epoch+=1

    @staticmethod
    def speech(pcm):
        import webrtcvad
        vad = webrtcvad.Vad(2)
        # WebRTC accepts 20 ms frames, not 60 ms. Three positive frames form
        # each 60 ms window. The owner approved five (300 ms), retaining
        # all downstream identity, address, echo and confidence checks.
        frames = [vad.is_speech(pcm[i:i+640], 16000) for i in range(0, len(pcm)-639, 640)]
        return sum(all(frames[i:i+3]) for i in range(0, len(frames)-2, 3)) >= 5

    @staticmethod
    def confirmation_candidate(pcm):
        import webrtcvad
        vad = webrtcvad.Vad(2)
        return any(vad.is_speech(pcm[i:i+640], 16000) for i in range(0, len(pcm)-639, 640))

    def feed(self, batch, evidence=None, *, prefix=False):
        s = self.service
        self.expire()
        if not self.enabled and not prefix:
            if self.audio or self.last_heard or self.events or self.transcripts.rows or not self.jobs.empty(): self.clear()
            return
        while self.events and s.clock()-self.events[0]['at']>120:self.events.popleft()
        if s.clock()-self.heard_at>120:self.last_heard=''
        if (len(batch.pcm) != 32000 or 'microphone' in batch.gaps
                or not 0 <= s.clock()-batch.end <= 2.5):
            self._cancel_capture('input-unavailable')
            self._discard_pending('input-unavailable')
            self._clear_attention()
            self.audio.clear(); self.pre_roll = b''; self.last_end = None
            self.pre_check = None
            self.pre_checks = []; self.candidate_active = False; self.candidate_quiet = 0
            self.checks = UtteranceChecks()
            self.phase = 'input-unavailable'
            return
        dictation = self._dictating()
        boundary = False
        if dictation is not None and dictation.id == self.dictation_id:
            check = MicrophoneCheck(batch.end)
            if evidence is not None: check.resolve(evidence, now=s.clock(), trusted=s.trusted_ids())
            if dictation.stopping_at is not None and not dictation.stop_boundary_pending:
                return check
            start = max(0, min(16000, math.ceil((dictation.armed_at - batch.start) * 16000)))
            end = 16000 if dictation.stopping_at is None else max(0, min(16000, math.floor((dictation.stopping_at - batch.start) * 16000)))
            boundary = dictation.stopping_at is not None and batch.end >= dictation.stopping_at
            if end <= start and not boundary:
                return check
            end = max(start, end)
            batch = replace(batch, pcm=b'\0' * (start * 2) + batch.pcm[start * 2:end * 2] + b'\0' * ((16000 - end) * 2))
        # Complete PCM seconds are timestamped at pipe-read completion. Small
        # early/late scheduling jitter does not overlap or lose PCM samples.
        numbered = batch.stream_id is not None and batch.sample_start is not None and batch.sample_end is not None
        backward = not numbered and self.last_end is not None and batch.end <= self.last_end
        discontinuity = ((self.stream_id is not None and (batch.stream_id != self.stream_id or batch.sample_start != self.last_sample))
                         if numbered else self.last_end is not None and (backward or abs(batch.start-self.last_end) > .25))
        if discontinuity:
            self._cancel_capture('input-discontinuity')
            self._discard_pending('input-discontinuity')
            self._clear_attention()
            self.audio.clear(); self.pre_roll = b''
            self.pre_check = None
            self.pre_checks = []; self.candidate_active = False; self.candidate_quiet = 0
            self.checks = UtteranceChecks()
            self.mark('input-discontinuity', 'Разрыв или неверный порядок аудиоблоков: незавершённая реплика сброшена.')
            if backward:
                self.phase = 'input-unavailable'
                return
        self.last_end = batch.end
        self.stream_id, self.last_sample = (batch.stream_id, batch.sample_end) if numbered else (None, None)
        check = MicrophoneCheck(batch.end)
        if evidence is not None: check.resolve(evidence,now=s.clock(),trusted=s.trusted_ids())
        speaking = self.speech(batch.pcm)
        confirmation_only = (dictation is None and not speaking and (getattr(s.commands, 'pending', None) is not None or s.voice.session.question is not None)
                             and s.voice_gate.permits(s.clock()) and self.confirmation_candidate(batch.pcm))
        self.counts['batches'] = self.counts.get('batches', 0)+1
        if speaking or confirmation_only:
            self._begin(batch.start - len(self.pre_roll)/32000)
            if not self.audio:
                self.audio_confirmation_only = confirmation_only
                self.checks = UtteranceChecks()
                self.checks.items.extend(self.prior_checks)
                self.prior_checks = []
                if self.pre_roll:
                    priors = self.pre_checks or ([self.pre_check] if self.pre_check else [])
                    if not priors:
                        prior = MicrophoneCheck(batch.start);prior.resolve();priors = [prior]
                    self.checks.items.extend(priors)
                self.audio.extend(self.pre_roll)
                self.pre_roll = b''
                self.pre_check = None
                self.pre_checks = []; self.candidate_active = False; self.candidate_quiet = 0
            elif speaking:
                self.audio_confirmation_only = False
            self.checks.items.append(check)
            self.audio.extend(batch.pcm)
            self.phase = 'listening'
            if not prefix and self.drafts is not None and len(self.audio)-self.last_draft_size >= 32000:
                self.last_draft_size = len(self.audio)
                self.drafts.submit(self.utterance, self.audio)
        elif self.audio:
            self.audio.extend(batch.pcm)
            self.checks.items.append(check)
        else:
            # A subthreshold first second may contain the name. Preserve a
            # bounded candidate phrase rather than replacing it every second.
            activity = self.confirmation_candidate(batch.pcm)
            keep = activity or (self.candidate_active and self.candidate_quiet < 1)
            if keep:
                self.pre_roll = (self.pre_roll + bytes(batch.pcm))[-32000*8:]
                self.pre_checks = (self.pre_checks + [check])[-8:]
                self.candidate_active = self.candidate_active or activity
                self.candidate_quiet = 0 if activity else self.candidate_quiet + 1
                self.phase = 'capturing-speech'
                if activity: self.mark('speech-candidate','Слышу речь, сохраняю начало фразы.')
            else:
                if self.candidate_active: self._cancel_capture('below-speech-threshold')
                self.pre_roll = bytes(batch.pcm)
                self.pre_checks = [check]
                self.candidate_active = False; self.candidate_quiet = 0
            self.pre_check = check
            if boundary:
                self.pre_roll = b''; self.pre_checks = []; self.pre_check = None
                self.candidate_active = False; self.candidate_quiet = 0
                self.phase = 'waiting'
                dictation.stop_boundary_pending = False
            if not keep and self.phase not in {'transcription', 'participant-check', 'blocked', 'routed', 'routed-general', 'heard', 'error'}:
                self.phase = 'waiting'
            return check
        if prefix:
            # The same assembler retains the first qualifying owner checks.
            # No ASR, answer or action is released before the dialogue opens.
            if len(self.audio) > 32000*3:
                self._cancel_capture('owner-check-unavailable')
                self.audio.clear()
            return check
        # Admission requires 300 ms, but an admitted phrase ends only on an
        # actual quiet tail. A sparse voiced second is not a completed turn.
        tail = Conversation._silence_tail(batch.pcm)
        activity = speaking or confirmation_only or self.confirmation_candidate(batch.pcm)
        final = boundary or tail >= .45 or not activity
        if not final and len(self.audio) < 32000*20:
            return check
        identifier = self.utterance
        if self.drafts is not None: self.drafts.cancel(identifier)
        task = (bytes(self.audio), self.token, batch.end, self.checks, time.monotonic(), self.audio_confirmation_only, identifier, final)
        if final:
            self.utterance = None
            self.audio.clear()
        else:
            # The ASR buffer limit is not an end of thought. Keep one second
            # of overlap and the original ID/checks; only the eventual quiet
            # tail may publish an actionable final transcript.
            del self.audio[:-32000]
            self.last_draft_size = 0
        # Preserve completed turns in order. Refuse overflow visibly instead
        # of silently replacing an already completed phrase with later speech.
        if self.jobs.full():
            self._clear_attention()
            self._revision(identifier, '', 'queue-full')
            self.mark('queue-full', 'Фраза завершена, но очередь распознавания заполнена: обработать её не удалось.')
            if boundary:
                self.phase = 'waiting'
                dictation.stop_boundary_pending = False
            return check
        self.jobs.put_nowait(task)
        if boundary: dictation.stop_boundary_pending = False
        return check

    def valid(self, token, at):
        return self.enabled and token is self.token and not token.is_set() and 0 <= self.service.clock()-at <= 8

    def valid_dictation(self, token, at, identifier):
        row = self.transcripts.rows.get(identifier, {})
        current = self._dictating()
        return (current is not None and row.get('dictation_id') == current.id
                and row.get('dictation_generation') == self.dictation_generation
                and self.enabled and token is self.token and not token.is_set()
                and 0 <= self.service.clock() - at <= 90)

    def _report_expired(self, token):
        if token is self.token and not token.is_set() and self.enabled:
            self.phase = 'blocked'
            self.mark('reply-blocked-unchecked', 'Истёк срок обработки или ожидания свежей проверки реплики. Личный ответ отменён.')

    def process(self, *args, **kwargs):
        self.processing = True
        try: return self._process(*args, **kwargs)
        finally: self.processing = False

    def _process(self, pcm, token, at, owner, queued, confirmation_only=False, identifier=None, final=True):
        s = self.service
        self.current_episode = None
        with s.lock:
            if not self.valid(token, at) and not self.valid_dictation(token, at, identifier):
                self._revision(identifier, '', 'expired')
                self._report_expired(token)
                return
            identifier = identifier or self.transcripts.begin(at-len(pcm)/32000)
            row = self.transcripts.rows.get(identifier)
            if row is None or row['state'] not in {'capturing', 'draft'}:
                return
            dictation = self._dictating()
            row.setdefault('dictation_generation', self.dictation_generation)
            row.setdefault('dictation_id', dictation.id if dictation is not None else None)
            generation, dictation_id = row['dictation_generation'], row['dictation_id']
            if generation != self.dictation_generation or (dictation_id is not None and (dictation is None or dictation.id != dictation_id)):
                self._revision(identifier, '', 'dictation-boundary')
                return
            row.setdefault('private_epoch', s.voice_gate.generation if s.voice_gate.permits(s.clock()) else None)
            pending, self.pending_endpoint = self.pending_endpoint, None
            if pending and pending[1] is token and 0 <= at - pending[2] <= 8:
                pcm = pending[0] + pcm
                if isinstance(owner, UtteranceChecks) and isinstance(pending[3], UtteranceChecks):
                    owner.items = pending[3].items + owner.items
                queued = pending[4]
                if len(pending) > 5 and identifier != pending[5]:
                    self._revision(identifier, '', 'joined-continuation')
                    identifier = pending[5]
            elif pending and len(pending) > 5:
                self._revision(pending[5], '', 'expired')
            self.phase = 'transcription'
            self.mark('transcription', 'Распознаю микрофон локально, независимо от проверки владельца.')
            attention_epoch=self.attention_epoch
        endpointer = getattr(s, 'smart_turn', None)
        if (final and identifier not in self.segment_text and hasattr(endpointer, 'complete') and not confirmation_only
                and self._dictating() is None):
            completed = endpointer.complete(pcm)
            if completed is False and len(pcm) < 32000 * 20:
                with s.lock:
                    if (generation == self.dictation_generation and self._dictating() is None
                            and self.valid(token, at)):
                        self.pending_endpoint = (pcm, token, at, owner, queued, identifier)
                        self.phase = 'awaiting-continuation'
                        self.mark('end-of-thought-wait', 'Мысль ещё не завершена; жду продолжения.')
                return
            if completed is None: self.mark('endpoint-degraded', 'Smart Turn недоступен; использую прежний локальный endpointer.')
        started = time.monotonic()
        text = self.model.transcribe(Batch(0, 1, [], pcm).wav(), token=token).strip()
        if confirmation_only:
            from .voice_commands import confirmation
            if confirmation(text) is None or (s.commands.pending is None and s.voice.session.question is None):
                self.phase = 'waiting'
                self.mark('short-confirmation-ignored', 'Короткий звук не является ответом на подтверждение.')
                return
        transcribed = time.monotonic()
        with s.lock:
            dictation = self._dictating()
            if generation != self.dictation_generation or (dictation_id is not None and (dictation is None or dictation.id != dictation_id)):
                self._revision(identifier, '', 'dictation-boundary')
                return
            if not self.valid(token, at) and not self.valid_dictation(token, at, identifier):
                self._revision(identifier, '', 'expired')
                self._report_expired(token)
                return
            if not text and (not final or identifier in self.segment_text):
                self._revision(identifier, '', 'unrecognized')
                self.mark('asr-empty', 'Часть длинной фразы не распознана; неполный запрос не будет выполнен.')
                self.segment_text.pop(identifier, None)
                return
            text = join_segments(self.segment_text.get(identifier, ''), text)
            if dictation_id is not None and dictation is not None and not confirmation_only:
                # Dictation claims every utterance that finishes while it listens (the first sentence must never reach the
                # voice dialogue). The words are kept nowhere else: the transcript row is closed empty, no episode is
                # captured, nothing is routed, and the event line carries no text.
                if not final:
                    self.segment_text[identifier] = text[:12000]
                    return
                self.segment_text.pop(identifier, None)
                self._revision(identifier, '', 'dictated')
                self.phase = 'waiting'
                if text: dictation.hear(text)
                self.mark('dictated' if text else 'asr-empty', 'Диктовка: фраза передана в поле сообщения панели.' if text
                          else 'Диктовка: речь не распознана.')
                return
            if len(text) > 12000:
                self._revision(identifier, '', 'text-limit')
                self.segment_text.pop(identifier, None)
                self.mark('text-limit', 'Длинная реплика превысила предел текста; усечённая команда не будет выполнена.')
                return
            if not final:
                self.segment_text[identifier] = text[:12000]
                # Bounded state, matching the transcript cache. Old entries
                # cannot become a new utterance or authorize any operation.
                self.segment_text = {key: value for key, value in self.segment_text.items() if key in self.transcripts.rows}
                self._revision(identifier, text, 'draft')
                self.phase = 'listening'
                self.mark('segment-transcribed', 'Продолжаю слушать ту же фразу; промежуточный текст не запускает действий.')
                return
            self.segment_text.pop(identifier, None)
            if final and text and unfinished(text) and not confirmation_only:
                self.segment_text[identifier] = text
                self.pending_thought = {'id': identifier, 'checks': owner, 'epoch': s.voice_gate.generation}
                self._revision(identifier, text, 'draft')
                self.phase = 'awaiting-continuation'
                self.mark('end-of-thought-wait', 'Мысль не закончена; сохраняю её и слушаю продолжение.')
                return
            self._revision(identifier, text, 'final' if text else 'unrecognized')
            if not text:
                self.phase = 'waiting'
                seconds = len(pcm) / 32000
                if seconds >= 1.0:
                    samples = array('h', pcm[:len(pcm) - len(pcm) % 2])
                    rms = math.sqrt(sum(float(x) * x for x in samples) / max(len(samples), 1)) / 32768 if samples else 0
                    level = f"{20 * math.log10(max(rms, 1e-5)):.0f} dBFS"
                    # The owner sees that speech was heard but not recognized instead of nothing at all (2026-10-25).
                    quality = getattr(getattr(self.model, 'transcriber', None), 'last_quality', None) or {}
                    numbers = (f" · logprob {quality.get('mean_logprob')}, no_speech {quality.get('no_speech')}"
                               if quality.get('mean_logprob') is not None else '')
                    self.mark('asr-empty', f'Речь слышна ({seconds:.0f} с, {level}), но не распознана: тихо или неразборчиво.' + numbers)
                return
            if not self.transcripts.claim(identifier): return
            from .recordings import voice_target
            local_navigation = voice_target(text, s.clock()) is not None
            aliases = s.store.get('address_aliases', ['Бата', 'Батлер', 'Butler'])
            if s.store.get('single_user_local', False) is True and single_user_summary_request(text, aliases):
                local_navigation = True
            feedback = None if local_navigation else getattr(s, 'feedback', None)
            if feedback is not None:
                text, joined = feedback.join(text)
                self.current_episode = joined or feedback.capture(text, at=at,
                    mode='private' if s.voice_gate.permits(s.clock()) else 'general',
                    identifier=identifier, born=self.transcripts.rows[identifier]['born'])
            self.last_heard = text[:900]
            self.timings = {'asr_ms':round((transcribed-started)*1000,1),
                'input_ready_to_transcript_ms':round((transcribed-queued)*1000,1)}
            self.heard_at = s.clock()
            self.phase = 'heard'
            self.mark('heard', self.last_heard)
            voice = s.voice
            if s.clock()-voice.last_playback_finished < 2.5 and speech_echo(text, voice.last_spoken):
                self._clear_attention()
                self.mark('echo', 'Мой голос из динамика: ответ не требуется.'); return
            hearing = explicit_address(text, aliases) and hearing_check(text, aliases)
            prior=self.public_attention if attention_epoch==self.attention_epoch else None
            self.public_attention=None
            if (not hearing and prior is not None and 0<at-prior[1]<=4
                    and 0<=s.clock()-prior[1]<=4):
                hearing=hearing_check(prior[0]+' '+text,aliases)
                if hearing:self.mark('hearing-joined','Имя и точный вопрос распознаны отдельными свежими фрагментами.')
            alias=hearing_vocative(text,aliases)
            if alias is not None and attention_epoch==self.attention_epoch and 0<=s.clock()-at<=4:
                self.public_attention=(alias,at)
                self.mark('attention','Имя услышано; жду короткое продолжение обращения.')
        if hearing:
            # A public device status contains no name, personal context, model
            # output or action, and never establishes an authenticated session.
            completed = s.disclosures.play_fixed('hearing', explicit=True,interrupt=True,
                valid=lambda: self.valid(token, at))
            with s.lock:
                if self.valid(token, at):
                    self.mark('hearing-replied' if completed else 'hearing-unavailable',
                        'Да, я вас слышу.' if completed else 'Речь распознана, но воспроизвести ответ не удалось.')
            return
        waiting = time.monotonic()
        if isinstance(owner, UtteranceChecks):
            if not owner.complete:
                with s.lock:
                    if self.valid(token,at):
                        self.phase = 'participant-check'
                        self.mark('participant-check-wait', 'Слова распознаны. Ожидаю проверки именно этой реплики.')
            while not owner.complete and self.valid(token,at):
                token.wait(.02)
            # DEC-066 «один дома»: while the owner session holds, the utterance is his unless a non-trusted voice
            # was certain during it; a check that did not complete is not company.
            checked, company = owner.checked, owner.company
            verified_owner = bool(owner)
            actor_owner = verified_owner or (s.voice_gate.permits(s.clock()) and not company
                                            and not any(item.nonowner for item in owner.items))
        else:
            checked, company, actor_owner = True, False, owner
            verified_owner = owner
        with s.lock:
            if not self.valid(token, at):
                self._report_expired(token)
                return
            self.timings['participant_wait_ms'] = round((time.monotonic()-waiting)*1000,1)
            if actor_owner and not company and s.gate.permits(s.clock()):
                born = self.transcripts.rows.get(identifier, {}).get('born', at-len(pcm)/32000)
                s.store.retain_transcript("owner:" + identifier, "microphone", born, at, text,
                    meeting_id=getattr(s.meeting, "recording_id", None), now=s.clock())
            # Intercept before corrections, dialogue classification, feedback
            # joining or any cloud dispatch. A recordings request is local.
            from .recordings import history_visible, local_voice_history_allowed, navigate_voice
            local_only = (s.store.get('single_user_local', False) is True and not company
                          and isinstance(owner, UtteranceChecks) and owner.checked
                          and not any(item.nonowner for item in owner.items)
                          and history_visible(s))
            if navigate_voice(s, text, authorized=bool((actor_owner and not company) or local_only)):
                self.phase = 'heard'
                self.mark('recordings-navigation', 'Записи открыты локально.')
                return
            # A fixed, read-only local summary has no owner-identity claim and
            # never enters the agent or the privileged command planner.
            local_summary = local_only and single_user_summary_request(text, aliases)
            if local_summary:
                s.meeting.update()
                if not s.meeting.local_summary_safe() or not local_voice_history_allowed(s):
                    self.phase = 'blocked'
                    self.mark('local-summary-unavailable', 'Локальная сводка не озвучивается во время встречи или при недоступной проверке вызова.')
                    return
                if not (voice.ready and voice.local_history_audio_permitted()) or voice.jobs.full():
                    self.phase = 'blocked'
                    self.mark('local-summary-unavailable', 'Локальная сводка сейчас недоступна.')
                    return
                input_token = threading.Event()
                voice.pending_pcm, voice.pending_generation, voice.pending_actor = pcm, GENERAL, False
                voice.jobs.put_nowait((pcm, input_token, GENERAL, False, 'single-user-history:' + str(s.local_history_epoch), queued, False, text))
                self.phase = 'routed-general'
                self.mark('local-summary', 'Готовлю сводку локально.')
                return
            if feedback is not None and actor_owner and s.voice_gate.permits(s.clock()):
                correction = feedback.voice_correction(text, current=self.current_episode, allow_remember=bool(verified_owner))
                if correction is not None:
                    self.mark('correction', correction.get('message', 'Исправление принято.'))
                    return
            attention = getattr(s, 'attention', None)
            if attention is not None:
                from .attention import BUSY_CUE, FREE_CUE
                if (not verified_owner or company) and (BUSY_CUE.search(text) or FREE_CUE.search(text)):
                    self.phase = 'blocked'
                    self.mark('attention-cue-denied', 'Изменение занятости требует подтверждения владельца.')
                    return
                # Only the owner may change an owner-selected availability
                # override; another speaker's words are ordinary local speech.
                cue = attention.spoken_cue(text, s.clock()) if verified_owner and not company else None
                if cue is not None:
                    self.phase = 'blocked'
                    # Acknowledged in the feed only: a spoken «понял» would be exactly the interruption asked against.
                    message = ('Занятость задана вручную и сохранится до вашего переключения.'
                               if cue == 'busy' and attention.manual_mode == 'busy'
                               else 'Понял: занят, молчу до «я свободен» или 10 минут.' if cue == 'busy'
                               else 'Понял: свободен.')
                    self.mark('attention-cue', message)
                    return
                if explicit_address(text, aliases):
                    attention.named(s.clock())
                elif (attention.state(s.clock()) == BUSY and not voice.conversation_active()
                      and not (silence_requested(text) or STOP_CUE.match(text))):
                    # REQ-001: the owner is talking to someone else. Record (the archive already has it), say nothing.
                    self.phase = 'blocked'
                    self.mark('owner-busy', 'Владелец занят разговором с другим (' + CAUSE_TEXT.get(attention.busy_cause, '') + ') — не отвечаю; нужно имя.')
                    return
            general = not s.voice_gate.permits(s.clock()) or company or not actor_owner
            private_epoch = self.transcripts.rows.get(identifier, {}).get('private_epoch')
            wholly_other = (isinstance(owner, UtteranceChecks) and owner.checked
                and any(item.nonowner for item in owner.items)
                and all(item.nonowner or item.silent for item in owner.items))
            if (private_epoch is not None and (general or private_epoch != s.voice_gate.generation)
                    and not wholly_other):
                self.phase = 'blocked'
                self.mark('private-input-withheld', 'Приватная реплика сохранена локально; после смены доступа общий ответ не получает её контекст.')
                return
            if general and not (voice.ready and voice.general_permitted()):
                self.phase = 'blocked'
                if not checked:
                    self.mark('reply-blocked-unchecked', 'Слова услышаны. Проверка этой реплики пропущена, устарела или не завершена; личный ответ запрещён.')
                else:
                    self.mark('reply-blocked', 'Слова услышаны. Ответ заблокирован: владелец пока не подтверждён.')
                return
            if general and voice.private_busy():
                # An unverified utterance never preempts private dialogue in
                # progress; it stays blocked exactly as before DEC-062.
                self.phase = 'blocked'
                self.mark('reply-blocked-unchecked' if not checked else 'reply-blocked',
                          'Слова услышаны. Идёт личный разговор с владельцем; общий ответ не прерывает его.')
                return
            if general and not (explicit_address(text, aliases) or voice.conversation_active()):
                # Attention DEC-002 (supersedes the name requirement of DEC-062): without a lease the answer is
                # general (no private context, commands or actions), and it is given unless the owner is busy
                # with someone else; the dialogue model still judges whether the words are for Butler.
                if attention is None or attention.state(s.clock()) == BUSY:
                    self.phase = 'blocked'
                    self.mark('general-not-addressed', 'Слова услышаны. Владелец занят разговором — общий ответ только по имени.')
                    return
                self.mark('general-unnamed', 'Владелец не подтверждён: отвечу в общем режиме, без личных данных, если слова обращены ко мне.')
            if voice.reply_in_progress:
                pending_answer = (voice.session.answering('owner', s.clock()) or s.commands.pending is not None)
                interrupt_owner = verified_owner or (pending_answer and actor_owner)
                if not voice.observe_input(text, identifier, owner=interrupt_owner,
                        other=company or not actor_owner, final=True):
                    self.mark('playback-input', 'Слова сохранены; признаков адресованного перебивания нет.')
                    return
            continuing = (not general and actor_owner and feedback is not None
                and voice.state == 'thinking' and voice.phase in {'transcription','address','awaiting-continuation'}
                and voice.token_mode != GENERAL and not voice.token.is_set()
                and not (silence_requested(text) or STOP_CUE.match(text)))
            previous_episode = self.current_episode
            if continuing:
                text, self.current_episode = feedback.extend_pending(voice.token, text, self.current_episode)
                continuing = self.current_episode != previous_episode
                if continuing:
                    self.last_heard = text[:900]
            if continuing: voice.cancel(clear_context=False, reason='owner-continuation')
            s.yield_analysis()
            input_token = threading.Event()
            if feedback is not None: feedback.attach(input_token, self.current_episode)
            voice.pending_pcm, voice.pending_generation, voice.pending_actor = pcm, GENERAL if general else s.voice_gate.generation, actor_owner
            if general:
                self.phase = 'routed-general'
                voice.jobs.put_nowait((pcm, input_token, GENERAL, False, 'microphone', queued, False, text))
                self.mark('routed-general', 'Общий разговор: владелец не подтверждён, личные данные, команды и действия недоступны.')
                return
            self.phase = 'routed'
            voice.jobs.put_nowait((pcm, input_token, s.voice_gate.generation, True,
                'microphone', queued, actor_owner, text))
            self.mark('routed', 'Полная реплика передана в диалог. Проверки владельца и команд сохранены.')

    def loop(self):
        while not self.service.stop_event.is_set():
            try: task = self.jobs.get(timeout=.2)
            except queue.Empty: continue
            try: self.process(*task)
            except Exception as error:
                with self.service.lock:
                    if task[1] is self.token and not task[1].is_set():
                        if len(task) > 6: self._revision(task[6], '', 'error')
                        self.phase = 'error'
                        self.mark('error', 'Локальное распознавание: '+type(error).__name__)

    def snapshot(self):
        self.expire()
        return {'enabled': self.enabled, 'phase': self.phase,
                'speech_active':self.phase in {'capturing-speech','listening'},
                'captured_seconds':round((len(self.audio) if self.audio else len(self.pre_roll) if self.candidate_active else 0)/32000,2),
                'last_heard': self.last_heard if self.enabled else '',
                'transcript': self.transcripts.snapshot() if self.enabled else None,
                'events': list(self.events) if self.enabled else [], 'counts': dict(self.counts),
                'timings':dict(self.timings) if self.enabled else {},
                'authority': 'passive local hearing only; general replies need no identity, private context and actions need a verified owner'}

"""DEC-038 local hearing is independent of permission to answer or execute."""
from __future__ import annotations

import queue
import threading
import math
from array import array
import time
from collections import deque

from .analysis import LocalAnalyzer
from .attention import BUSY, CAUSE_TEXT
from .conversation import GENERAL, Conversation, explicit_address, hearing_check, hearing_vocative, silence_requested, speech_echo, STOP_CUE
from .sensing import Batch


class MicrophoneCheck:
    """Completion of identity processing for one exact captured microphone second."""
    def __init__(self, at):
        self.at = at
        self.done = threading.Event()
        self.checked = self.owner = self.silent = self.other = False

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
        self.pre_check = None
        self.checks = UtteranceChecks()
        self.last_end = None
        self.phase = 'waiting'
        self.last_heard = ''
        self.heard_at = float('-inf')
        self.events = deque(maxlen=24)
        self.counts = {}
        self.timings = {}
        self.jobs = queue.Queue(maxsize=1)
        self.public_attention = None
        self.attention_epoch = 0

    @property
    def enabled(self):
        s = self.service
        return s.archive.enabled and not s.stop_event.is_set() and not s.owner_setup.capturing

    def mark(self, key, detail=''):
        self.counts[key] = self.counts.get(key, 0) + 1
        # Passive hearing never verifies a speaker, so a heard line is an unnamed voice.
        who = 'Butler' if key in ('reply', 'hearing-replied') else 'Голос' if key == 'heard' else 'Система'
        self.events.append({'at': self.service.clock(), 'event': key, 'detail': detail[:900], 'who': who})

    def clear(self):
        self._clear_attention()
        # DEC-062: general dialogue exists only while passive hearing does.
        voice = getattr(self.service, 'voice', None)
        if voice is not None: voice.end_general('hearing-stopped')
        self.token.set()
        self.model.cancel()
        self.token = threading.Event()
        self.audio.clear()
        self.checks = UtteranceChecks()
        self.pre_roll = b''
        self.pre_check = None
        self.last_end = None
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
        # At least 0.3 s of voiced audio in the second: a cough or a creak is not an utterance.
        return sum(vad.is_speech(pcm[i:i+640], 16000)
                   for i in range(0, len(pcm)-639, 640)) >= 15

    def feed(self, batch, evidence=None):
        s = self.service
        if not self.enabled:
            if self.audio or self.last_heard or self.events: self.clear()
            return
        while self.events and s.clock()-self.events[0]['at']>120:self.events.popleft()
        if s.clock()-self.heard_at>120:self.last_heard=''
        if (len(batch.pcm) != 32000 or 'microphone' in batch.gaps
                or not 0 <= s.clock()-batch.end <= 2.5):
            self._clear_attention()
            self.audio.clear(); self.pre_roll = b''; self.last_end = None
            self.pre_check = None
            self.checks = UtteranceChecks()
            self.phase = 'input-unavailable'
            return
        # Complete PCM seconds are timestamped at pipe-read completion. Small
        # early/late scheduling jitter does not overlap or lose PCM samples.
        backward = self.last_end is not None and batch.end <= self.last_end
        if self.last_end is not None and (backward or abs(batch.start-self.last_end) > .25):
            self._clear_attention()
            self.audio.clear(); self.pre_roll = b''
            self.pre_check = None
            self.checks = UtteranceChecks()
            self.mark('input-discontinuity', 'Разрыв или неверный порядок аудиоблоков: незавершённая реплика сброшена.')
            if backward:
                self.phase = 'input-unavailable'
                return
        self.last_end = batch.end
        check = MicrophoneCheck(batch.end)
        if evidence is not None: check.resolve(evidence,now=s.clock(),trusted=s.trusted_ids())
        speaking = self.speech(batch.pcm)
        self.counts['batches'] = self.counts.get('batches', 0)+1
        if speaking:
            if not self.audio:
                self.checks = UtteranceChecks()
                if self.pre_roll:
                    prior = self.pre_check
                    if prior is None:
                        prior = MicrophoneCheck(batch.start);prior.resolve()
                    self.checks.items.append(prior)
                self.audio.extend(self.pre_roll)
                self.pre_roll = b''
                self.pre_check = None
            self.checks.items.append(check)
            self.audio.extend(batch.pcm)
            self.phase = 'listening'
            if len(self.audio) < 32000*20 and Conversation._silence_tail(batch.pcm) < .45:
                return check
        elif self.audio:
            self.audio.extend(batch.pcm)
            self.checks.items.append(check)
        else:
            self.pre_roll = bytes(batch.pcm)
            self.pre_check = check
            if self.phase not in {'transcription', 'participant-check', 'blocked', 'routed', 'routed-general', 'heard', 'error'}:
                self.phase = 'waiting'
            return check
        task = (bytes(self.audio), self.token, batch.end, self.checks, time.monotonic())
        self.audio.clear()
        # Keep the latest complete input, never build an unbounded ASR backlog.
        if self.jobs.full():
            self._clear_attention()
            try: self.jobs.get_nowait()
            except queue.Empty: pass
            self.mark('superseded', 'Очередь распознавания занята: оставлена новая полная реплика.')
        self.jobs.put_nowait(task)
        return check

    def valid(self, token, at):
        return self.enabled and token is self.token and not token.is_set() and 0 <= self.service.clock()-at <= 8

    def _report_expired(self, token):
        if token is self.token and not token.is_set() and self.enabled:
            self.phase = 'blocked'
            self.mark('reply-blocked-unchecked', 'Истёк срок обработки или ожидания свежей проверки реплики. Личный ответ отменён.')

    def process(self, pcm, token, at, owner, queued):
        s = self.service
        with s.lock:
            if not self.valid(token, at): return
            self.phase = 'transcription'
            self.mark('transcription', 'Распознаю микрофон локально, независимо от проверки владельца.')
            attention_epoch=self.attention_epoch
        started = time.monotonic()
        text = self.model.transcribe(Batch(0, 1, [], pcm).wav(), token=token).strip()
        transcribed = time.monotonic()
        with s.lock:
            if not self.valid(token, at):
                self._report_expired(token)
                return
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
            aliases = s.store.get('address_aliases', ['Бата', 'Батлер', 'Butler'])
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
            actor_owner = bool(owner) or (s.voice_gate.permits(s.clock()) and not company)
        else:
            checked, company, actor_owner = True, False, owner
        with s.lock:
            if not self.valid(token, at):
                self._report_expired(token)
                return
            self.timings['participant_wait_ms'] = round((time.monotonic()-waiting)*1000,1)
            attention = getattr(s, 'attention', None)
            if attention is not None:
                cue = attention.spoken_cue(text, s.clock())
                if cue is not None:
                    self.phase = 'blocked'
                    # Acknowledged in the feed only: a spoken «понял» would be exactly the interruption asked against.
                    self.mark('attention-cue', 'Понял: занят, молчу до «я свободен» или 10 минут.' if cue == 'busy' else 'Понял: свободен.')
                    return
                if explicit_address(text, aliases):
                    attention.named(s.clock())
                elif (attention.state(s.clock()) == BUSY and not voice.conversation_active()
                      and not (silence_requested(text) or STOP_CUE.match(text))):
                    # REQ-001: the owner is talking to someone else. Record (the archive already has it), say nothing.
                    self.phase = 'blocked'
                    self.mark('owner-busy', 'Владелец занят разговором с другим (' + CAUSE_TEXT.get(attention.busy_cause, '') + ') — не отвечаю; нужно имя.')
                    return
            general = not s.voice_gate.permits(s.clock()) or company
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
                # A reply being composed, synthesised or played is stopped only by a real interruption (REQ-038).
                self.mark('playback-input', 'Речь во время ответа проверяется отдельным обработчиком перебивания.')
                return
            voice.cancel(clear_context=False, reason='complete-new-utterance')
            s.yield_analysis()
            voice.token = threading.Event()
            voice.general_token = voice.token if general else None
            if general:
                self.phase = 'routed-general'
                voice.jobs.put_nowait((pcm, voice.token, GENERAL, False, 'microphone', queued, False, text))
                self.mark('routed-general', 'Общий разговор: владелец не подтверждён, личные данные, команды и действия недоступны.')
                return
            self.phase = 'routed'
            voice.jobs.put_nowait((pcm, voice.token, s.voice_gate.generation, True,
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
                        self.phase = 'error'
                        self.mark('error', 'Локальное распознавание: '+type(error).__name__)

    def snapshot(self):
        return {'enabled': self.enabled, 'phase': self.phase,
                'last_heard': self.last_heard if self.enabled else '',
                'events': list(self.events) if self.enabled else [], 'counts': dict(self.counts),
                'timings':dict(self.timings) if self.enabled else {},
                'authority': 'passive local hearing only; general replies need no identity, private context and actions need a verified owner'}

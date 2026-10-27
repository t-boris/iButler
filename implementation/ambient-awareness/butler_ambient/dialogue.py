"""One dialogue, independently running requests and independently delivered speech.

An output cancellation never retries or cancels accepted work. A privacy close
invalidates the request epoch, including queued output and pending questions.
The worker serializes the agent's existing conversation rather than inventing
an unrelated session when that agent is busy.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import re
import threading
import time
import uuid

from .brain import BrainSilent, BrainUnavailable, clean_for_speech


class TextStream:
    """Whole spoken sentences, bounded by the same contract for every producer."""
    LIMIT = 600

    def __init__(self):
        self.pending = ''

    def feed(self, text, final=False):
        self.pending += text
        parts = []
        while self.pending:
            stripped = self.pending.strip()
            if not final and 'NO_REPLY'.startswith(stripped): break
            if final and stripped == 'NO_REPLY':
                self.pending = ''; break
            boundary = next((m.end() for m in re.finditer(r'[.!?…](?:\s+|$)', self.pending)
                             if m.end() >= 12 and (final or m.end() < len(self.pending)
                                                   or self.pending[m.end()-1].isspace())), None)
            if (boundary is None or boundary > self.LIMIT) and len(self.pending) > self.LIMIT:
                boundary = self.pending.rfind(' ', 0, self.LIMIT + 1)
                if boundary <= 0: boundary = self.LIMIT
            if boundary is None:
                if not final: break
                boundary = min(len(self.pending), self.LIMIT)
            boundary = min(boundary, self.LIMIT)
            part, self.pending = self.pending[:boundary], self.pending[boundary:]
            part = clean_for_speech(part).strip()
            if part: parts.append(part)
        return parts


@dataclass
class Request:
    id: str
    dialogue: str
    epoch: int
    text: str
    at: float
    queued_at: float
    run: object = field(repr=False)
    state: str = 'queued'
    result: str = ''
    error: str | None = None
    parent_question: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    pieces: deque = field(default_factory=deque, repr=False)
    stream: TextStream = field(default_factory=TextStream, repr=False)
    delivery: str = 'pending'
    spoken: int = 0
    status_count: int = 0
    last_status_at: float | None = None
    raw: str = ''
    sequence: int = 0
    kind: str = 'request'


class Dialogue:
    MAX_PENDING = 32
    MAX_TEXT = 12000
    STATUS_FIRST_AFTER = 1.2

    def __init__(self, session, ended, *, clock=time.time, monotonic=time.monotonic):
        self.session, self.ended = session, ended
        self.clock, self.monotonic = clock, monotonic
        self.id = uuid.uuid4().hex
        self.epoch = 0
        self.condition = threading.Condition(threading.RLock())
        self.requests = OrderedDict()
        self.worker = None
        self.playing = None
        self.last_error = None

    def submit(self, text, run, *, kind="request"):
        if kind not in {"request", "task"}:
            raise ValueError("invalid dialogue request kind")
        with self.condition:
            self.expire()
            if len(self.requests) >= self.MAX_PENDING:
                removable = [key for key, r in self.requests.items()
                             if r.state not in {'queued', 'running', 'awaiting-answer'}
                             and r.delivery in {'completed', 'interrupted', 'failed', 'cancelled'}]
                for key in removable: del self.requests[key]
            if len(self.requests) >= self.MAX_PENDING:
                raise BrainUnavailable('voice request backlog full; nothing submitted')
            question = getattr(self.session, 'question_id', None)
            previous = self.requests.get(getattr(self.session, 'question_task', None))
            if previous is not None and previous.state == 'awaiting-answer': previous.state = 'completed'
            request = Request(uuid.uuid4().hex, self.id, self.epoch, text, self.clock(), self.monotonic(), run,
                              parent_question=question, kind=kind)
            self.requests[request.id] = request
            self.session.answered()
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._loop, name='butler-dialogue-agent', daemon=True)
                self.worker.start()
            self.condition.notify_all()
            return request

    def _current(self, request):
        return request.epoch == self.epoch and not request.cancelled.is_set() and not self.ended.is_set()

    def _emit(self, request, delta):
        if not isinstance(delta, str): raise BrainUnavailable('invalid agent text event')
        with self.condition:
            if not self._current(request): return
            if len(request.raw) + len(delta) > self.MAX_TEXT:
                raise BrainUnavailable('agent response exceeds bounded text contract')
            request.raw += delta
            request.pieces.extend(request.stream.feed(delta))
            self.condition.notify_all()

    def _loop(self):
        while not self.ended.is_set():
            with self.condition:
                request = next((r for r in self.requests.values() if r.state == 'queued' and self._current(r)), None)
                if request is None:
                    self.condition.wait(.1)
                    continue
                request.state = 'running'
            try:
                result = request.run(lambda text: self._emit(request, text), request.cancelled)
                with self.condition:
                    if not self._current(request): continue
                    if not isinstance(result, str) or not result.strip(): raise BrainUnavailable('empty agent result')
                    if not request.raw: self._emit(request, result)
                    request.pieces.extend(request.stream.feed('', final=True))
                    request.result = result
                    question = result.rstrip().endswith(('?', '？'))
                    request.state = 'awaiting-answer' if question else 'completed'
                    if not request.pieces and request.delivery == 'pending': request.delivery = 'completed'
                    if question:
                        self.session.ask(result, self.clock(), task=request.id)
                    self.condition.notify_all()
            except BrainSilent:
                with self.condition:
                    if self._current(request):
                        request.state = 'silent'; request.delivery = 'completed'; request.pieces.clear()
            except Exception as error:
                with self.condition:
                    if self._current(request):
                        request.state = 'failed'; request.error = type(error).__name__
                        self.last_error = request.error
                        request.pieces.clear()
                        request.pieces.append('Агент сейчас недоступен. Результат этой работы не подтверждён.')
                    self.condition.notify_all()
            finally:
                with self.condition: request.run = None

    def notice(self, text):
        with self.condition:
            if len(self.requests) >= self.MAX_PENDING: return False
            request = Request(uuid.uuid4().hex, self.id, self.epoch, '', self.clock(), self.monotonic(), None,
                              state='completed', result=text, kind='notice')
            request.pieces.extend(request.stream.feed(text, final=True))
            self.requests[request.id] = request
            return True

    @property
    def active(self):
        with self.condition:
            return any(self._current(r) and r.state in {'queued', 'running'} for r in self.requests.values())

    def claim_output(self, include_notices=True):
        with self.condition:
            self.expire()
            if self.playing is not None: return None
            for request in self.requests.values():
                if not self._current(request) or request.delivery in {'interrupted', 'failed', 'cancelled'}: continue
                if request.kind == 'notice' and not include_notices: continue
                if request.pieces:
                    part = request.pieces.popleft()
                    request.sequence += 1
                    request.delivery = 'playing'
                    self.playing = (request.id, request.sequence)
                    return request, part, request.sequence
            return None

    def delivered(self, request, sequence, text, error=None):
        with self.condition:
            if self.playing != (request.id, sequence): return
            self.playing = None
            if not self._current(request): return
            if error:
                request.delivery = 'failed'
                request.error = type(error).__name__
            else:
                request.spoken += len(text)
                request.delivery = ('completed' if not request.pieces and request.state != 'running' else 'pending')

    def interrupt_output(self):
        with self.condition:
            if self.playing:
                request = self.requests.get(self.playing[0])
                if request is not None: request.delivery = 'interrupted'
                self.playing = None

    def work_notice(self):
        """Speak one brief notice for a real task; ongoing progress stays in the panel."""
        with self.condition:
            self.expire()
            if self.playing is not None: return None
            now = self.monotonic()
            for request in self.requests.values():
                if (request.kind != 'task' or request.status_count or not self._current(request)
                        or request.state not in {'queued', 'running'}
                        or request.pieces or request.delivery in {'interrupted', 'failed', 'cancelled'}):
                    continue
                elapsed = now - request.queued_at
                if elapsed < self.STATUS_FIRST_AFTER: continue
                request.last_status_at = now
                request.status_count = 1
                return request, 'Покажу ход работы на экране.'
            return None

    def status_reply(self):
        with self.condition:
            active = [r for r in self.requests.values() if self._current(r) and r.state in {'queued', 'running'}]
            if active:
                request = active[0]
                if request.state == 'queued':
                    return f'Сначала завершу предыдущую работу, потом возьмусь за «{request.text[:100]}».'
                return f'Сейчас я работаю над «{request.text[:100]}». Результат ещё не готов.'
            if self.session.question:
                return 'Жду твоего ответа: ' + self.session.question
            return 'Я тебя слышу. Сейчас незавершённой работы нет.'

    def invalidate(self):
        with self.condition:
            self.epoch += 1
            self.id = uuid.uuid4().hex
            for request in self.requests.values():
                request.cancelled.set()
                request.pieces.clear()
                request.result = request.raw = request.text = ''
                request.stream.pending = ''
                request.run = None
                request.state = request.delivery = 'cancelled'
            self.playing = None
            self.session.end()
            self.condition.notify_all()

    def expire(self):
        # Retention, never conversational recency. Hard privacy closes clear sooner.
        for request in self.requests.values():
            if self.clock() - request.at >= 86400 and request.state != 'cancelled':
                request.cancelled.set(); request.pieces.clear()
                request.result = request.raw = request.text = ''
                request.stream.pending = ''
                request.run = None
                request.state = request.delivery = 'cancelled'
        if self.session.question and self.clock() - self.session.question_at >= 86400:
            self.session.answered()

    def snapshot(self, private=False):
        with self.condition:
            self.expire()
            return {'id': self.id, 'epoch': self.epoch, 'worker_alive': bool(self.worker and self.worker.is_alive()),
                    'last_error': self.last_error,
                    'requests': [dict(id=r.id, state=r.state, delivery=r.delivery, question=r.parent_question,
                                      kind=r.kind,
                                      at=r.at, spoken_characters=r.spoken,
                                      **({'request': r.text, 'result': r.result, 'error': r.error} if private else {}))
                                 for r in self.requests.values()]}

    def stop(self):
        self.invalidate()
        if self.worker is not None: self.worker.join(timeout=2)

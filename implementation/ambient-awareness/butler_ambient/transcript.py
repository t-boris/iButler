"""Bounded local transcript revisions. Drafts carry no dispatch authority."""
from __future__ import annotations

from collections import OrderedDict
import threading
import uuid
import re


def join_segments(prefix, suffix):
    """Reconcile the one-second PCM overlap between bounded ASR segments."""
    if not prefix: return suffix
    if not suffix: return prefix
    left = list(re.finditer(r"\w+", prefix))
    right = list(re.finditer(r"\w+", suffix))
    for size in range(min(len(left), len(right), 40), 0, -1):
        if [x.group().casefold() for x in left[-size:]] == [x.group().casefold() for x in right[:size]]:
            # Keep the new punctuation after the overlap, but not an old
            # hallucinated sentence boundary at the end of a cut segment.
            return prefix[:left[-size].start()].rstrip() + (' ' if left[-size].start() else '') + suffix
    return prefix.rstrip() + ' ' + suffix.lstrip()


class TranscriptStream:
    def __init__(self, limit=64):
        self.rows = OrderedDict()
        self.limit = limit

    def begin(self, born):
        identifier = 'utterance-' + uuid.uuid4().hex
        self.rows[identifier] = {'id': identifier, 'born': born, 'revision': 0,
                                 'text': '', 'state': 'capturing', 'dispatched': False}
        while len(self.rows) > self.limit: self.rows.popitem(last=False)
        return identifier

    def update(self, identifier, text, state='draft'):
        row = self.rows.get(identifier)
        if row is None or row['state'] not in {'capturing', 'draft'}: return None
        if row['text'] == text and row['state'] == state: return None
        row.update(text=text[:12000], state=state, revision=row['revision'] + 1)
        self.rows.move_to_end(identifier)
        return dict(row)

    def claim(self, identifier):
        row = self.rows.get(identifier)
        if row is None or row['state'] != 'final' or row['dispatched']: return False
        row['dispatched'] = True
        return True

    def snapshot(self):
        return dict(next(reversed(self.rows.values()))) if self.rows else None

    def expire(self, now):
        # The live caption is a short-lived view. Long-lived permitted text
        # belongs only to LearningStore's expiring private database/history.
        for identifier, row in list(self.rows.items()):
            if (now-row['born'] >= 86400
                    or row['state'] not in {'capturing', 'draft'} and now-row['born'] >= 120):
                del self.rows[identifier]


class DraftDecoder:
    """One cancellable decode plus one latest snapshot; never an audio backlog.

    A dedicated client scope lets final recognition cancel drafts without
    cancelling the final decoder or another consumer of the resident model.
    """
    def __init__(self, model, stopped, publish):
        self.model, self.stopped, self.publish = model, stopped, publish
        self.condition = threading.Condition()
        self.pending = self.current = None
        self.thread = None
        self.generation = 0

    def submit(self, identifier, pcm):
        with self.condition:
            self.pending = (identifier, bytes(pcm), self.generation)
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, name='butler-draft-asr', daemon=True)
                self.thread.start()
            self.condition.notify()

    def cancel(self, identifier=None):
        with self.condition:
            if self.pending and (identifier is None or self.pending[0] == identifier): self.pending = None
            if self.current and (identifier is None or self.current[0] == identifier):
                self.current[1].set()
                self.model.cancel()
            if identifier is None: self.generation += 1

    def _loop(self):
        from .sensing import Batch
        while not self.stopped.is_set():
            with self.condition:
                if self.pending is None:
                    self.condition.wait(.2)
                    continue
                identifier, pcm, generation = self.pending
                self.pending = None
                token = threading.Event()
                self.current = (identifier, token)
            try:
                text = self.model.transcribe(Batch(0, len(pcm)/32000, [], pcm).wav(), token=token).strip()
                if text and not token.is_set() and generation == self.generation and not self.stopped.is_set():
                    self.publish(identifier, text)
            except (RuntimeError, ValueError, OSError):
                # Final recognition reports an actionable error. An optional
                # partial never changes permissions or stops phrase capture.
                pass
            finally:
                with self.condition: self.current = None

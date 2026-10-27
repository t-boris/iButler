import threading
import time
import unittest
from unittest.mock import Mock

from butler_ambient.transcript import DraftDecoder, TranscriptStream, join_segments


class TranscriptTests(unittest.TestCase):
    def test_drafts_replace_text_without_dispatch_and_final_claim_is_once(self):
        stream = TranscriptStream()
        identifier = stream.begin(100)
        self.assertFalse(stream.claim(identifier))
        first = stream.update(identifier, 'Батлер помоги')
        second = stream.update(identifier, 'Эй Батлер помоги почистить диск')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['revision'], 2)
        self.assertEqual(second['born'], 100)
        self.assertFalse(stream.claim(identifier))
        final = stream.update(identifier, 'Эй, Батлер, помоги почистить диск.', 'final')
        self.assertTrue(stream.claim(identifier))
        self.assertFalse(stream.claim(identifier))
        self.assertIsNone(stream.update(identifier, 'late draft'))
        self.assertEqual(stream.snapshot()['text'], final['text'])

    def test_cancelled_and_expired_utterances_cannot_dispatch(self):
        stream = TranscriptStream(limit=2)
        first = stream.begin(1)
        stream.update(first, '', 'cancelled')
        self.assertFalse(stream.claim(first))
        self.assertIsNone(stream.update(first, 'late final', 'final'))
        second = stream.begin(2)
        stream.update(second, '', 'expired')
        stream.begin(3)
        self.assertNotIn(first, stream.rows)
        self.assertFalse(stream.claim(second))

    def test_continuation_final_is_latest_and_live_captions_expire_without_new_speech(self):
        stream = TranscriptStream()
        original = stream.begin(100)
        continuation = stream.begin(102)
        stream.update(continuation, '', 'joined-continuation')
        stream.update(original, 'The complete original phrase', 'final')
        self.assertEqual(stream.snapshot()['id'], original)
        stream.expire(220)
        self.assertNotIn(original, stream.rows)
        self.assertFalse(stream.claim(original))
        stream.expire(222)
        self.assertIsNone(stream.snapshot())

    def test_slow_draft_work_is_bounded_and_cancellation_cannot_publish_old_text(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def decode(*args, **kwargs):
            entered.set(); release.wait(2)
            return 'obsolete draft'
        model = Mock(); model.transcribe.side_effect = decode
        publish = Mock()
        decoder = DraftDecoder(model, stopped, publish)
        decoder.submit('first', b'\0'*64000)
        self.assertTrue(entered.wait(1))
        for n in range(20): decoder.submit('later', bytes([n, 0])*32000)
        self.assertEqual(decoder.pending[0], 'later')
        self.assertEqual(len(decoder.pending[1]), 64000)
        decoder.cancel()
        release.set()
        deadline = time.monotonic()+1
        while decoder.current is not None and time.monotonic()<deadline: time.sleep(.005)
        publish.assert_not_called()
        model.cancel.assert_called_once()
        stopped.set(); decoder.thread.join(1)

    def test_active_long_utterance_survives_caption_expiry_without_changing_retention_origin(self):
        stream = TranscriptStream()
        identifier = stream.begin(100)
        stream.update(identifier, 'ongoing speech')
        stream.expire(230)
        self.assertEqual(stream.snapshot()['born'], 100)
        stream.update(identifier, 'complete speech', 'final')
        self.assertTrue(stream.claim(identifier))
        stream.expire(231)
        self.assertIsNone(stream.snapshot())

    def test_segment_overlap_keeps_new_punctuation_and_unrelated_words(self):
        self.assertEqual(join_segments('Покажи каждый шаг.', 'каждый шаг, ничего не удаляй.'),
                         'Покажи каждый шаг, ничего не удаляй.')
        self.assertEqual(join_segments('Почисти диск', 'и сначала покажи список.'),
                         'Почисти диск и сначала покажи список.')


if __name__ == '__main__': unittest.main()

"""Whisper's own confidence decides whether a decode is a transcript (owner: «как он санжели не че»)."""
import unittest

from butler_ambient.transcriber import confident


class ConfidenceTests(unittest.TestCase):
    def test_low_logprob_or_no_speech_is_dropped_and_good_decodes_pass(self):
        good = [{"text": "Чему у тебя есть доступ?", "avg_logprob": -0.21, "no_speech_prob": 0.01}]
        self.assertEqual(confident(good)[0], True)
        guess = [{"text": "как он санжели не че как", "avg_logprob": -1.42, "no_speech_prob": 0.2}]
        self.assertEqual(confident(guess)[0], False)
        noise = [{"text": "спасибо", "avg_logprob": -0.4, "no_speech_prob": 0.85}]
        self.assertEqual(confident(noise)[0], False)
        mixed = [{"text": "Батлер, " * 10, "avg_logprob": -0.3, "no_speech_prob": 0.0}, {"text": "э", "avg_logprob": -2.5, "no_speech_prob": 0.0}]
        self.assertEqual(confident(mixed)[0], True)                      # weighted by length: one short bad token does not veto
        self.assertEqual(confident([])[0], True)                         # a server without the fields changes nothing


if __name__ == "__main__":
    unittest.main()

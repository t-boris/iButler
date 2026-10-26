"""Recognizer phantoms (Whisper's invented subtitle lines on silence) never become heard speech."""
import math
import struct
import unittest

from butler_ambient.transcriber import PHANTOM, phantom, speech_fraction


def wav(seconds=1.0, amplitude=0, tone=False):
    n = int(16000 * seconds)
    samples = [int(amplitude * (math.sin(2 * math.pi * 220 * i / 16000) if tone else 1)) for i in range(n)]
    data = struct.pack("<%dh" % n, *samples)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    return header + b"data" + struct.pack("<I", len(data)) + data


class PhantomTests(unittest.TestCase):
    def test_known_phantoms_are_matched_whole_only(self):
        for text in ("Спасибо.", "Спасибо за просмотр!", "Продолжение следует...", "Субтитры сделал DimaTorzok",
                     "Редактор субтитров А.Семкин", "Thank you.", "Thanks for watching"):
            self.assertTrue(PHANTOM.match(text), text)
        for text in ("Спасибо, поставь будильник", "Батлер, продолжение следует?", "Который час?"):
            self.assertFalse(PHANTOM.match(text), text)

    def test_phantom_is_dropped_only_when_the_audio_is_quiet(self):
        quiet, loud = wav(amplitude=30), wav(amplitude=6000, tone=True)
        self.assertLess(speech_fraction(quiet), 0.2)
        self.assertGreater(speech_fraction(loud), 0.8)
        self.assertTrue(phantom("Спасибо.", quiet))
        self.assertFalse(phantom("Спасибо.", loud))          # a real «спасибо» from the owner stays
        self.assertFalse(phantom("Который час?", quiet))
        self.assertTrue(phantom("Продолжение следует"))       # no audio given: the phrase alone decides
        self.assertEqual(speech_fraction(b"not a wav"), 0.0)


if __name__ == "__main__":
    unittest.main()

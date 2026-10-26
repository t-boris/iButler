"""DEC-066 «один дома»: the dialogue session opens on verified identity, holds while alone, never grants recording."""
import unittest
from butler_ambient.dialogue_state import Scene
from butler_ambient.policy import DialogueGate, Evidence


class VoiceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.gate = DialogueGate()
        self.now = 100.

    def check(self, *, owner=False, quiet=False, microphone=True, certain=True, enabled=True, voices=None, trusted=None, scene=None):
        self.now += 1
        ids = voices if voices is not None else frozenset({"owner"}) if owner else frozenset()
        evidence = Evidence(self.now, False, microphone, voice_ids=ids, speech_detected=not quiet, voice_certain=certain,
                            scene=scene or Scene())
        return self.gate.check(evidence, self.now, enabled, trusted=trusted)

    def establish(self):
        self.assertFalse(self.check(owner=True))
        self.assertFalse(self.check(owner=True))
        self.assertTrue(self.check(owner=True))
        self.assertEqual(self.gate.confirmed_by, "voice")

    def test_voice_only_owner_requires_three_checks_then_holds_through_silence_and_uncertainty(self):
        self.establish()
        for _ in range(40): self.assertTrue(self.check(quiet=True))
        self.assertTrue(self.check(certain=False))                 # speech the check cannot attribute is not company
        self.assertTrue(self.check(microphone=False))              # a sensor hiccup is not company
        self.assertEqual(self.gate.confirmed_by, "voice")

    def test_silence_cannot_enroll_identify_or_complete_owner_checks(self):
        for _ in range(4): self.assertFalse(self.check(quiet=True))
        self.assertFalse(self.check(owner=True))
        for _ in range(4): self.assertFalse(self.check(quiet=True))

    def test_stale_reused_missing_microphone_and_revocation_cannot_open_the_session(self):
        for cause in ("stale", "reused", "microphone", "revoked"):
            self.gate = DialogueGate()
            self.assertFalse(self.check(owner=True)); self.assertFalse(self.check(owner=True))
            at = self.now if cause == "reused" else self.now + 1
            now = self.now + 5 if cause == "stale" else self.now + 1
            evidence = Evidence(at, False, cause != "microphone", voice_ids=frozenset({"owner"}), voice_certain=True, speech_detected=True)
            self.assertFalse(self.gate.check(evidence, now, cause != "revoked"), cause)

    def test_another_certain_voice_closes_the_session_and_a_household_voice_does_not(self):
        self.establish()
        self.assertTrue(self.check(voices=frozenset({"partner"}), trusted=frozenset({"owner", "partner"})))
        self.assertFalse(self.check(voices=frozenset({"guest"})))
        self.assertEqual(self.gate.closed_reason, "company")
        self.assertEqual(self.gate.closes[-1]["reason"], "company")                        # the owner can ask why Butler fell silent
        self.assertIsNone(self.gate.confirmed_by)

    def test_a_crowd_of_unidentified_faces_closes_the_session(self):
        self.establish()
        self.assertTrue(self.check(quiet=True, scene=Scene(available=True, faces=1)))
        self.assertFalse(self.check(quiet=True, scene=Scene(available=True, faces=2, others_present=True)))
        self.assertEqual(self.gate.closed_reason, "company")

    def test_absence_closes_the_session_and_gaps_do_not(self):
        self.establish()
        self.assertTrue(self.gate.permits(self.now + 3))           # a stale check keeps the session (was a 2.5-s lease)
        self.assertTrue(self.gate.permits(self.now + 899))
        self.assertFalse(self.gate.permits(self.now + 901))
        self.assertEqual(self.gate.closed_reason, "absence")
        self.establish()
        self.now += 850
        self.assertTrue(self.check(owner=True))                    # seen again: the clock restarts
        self.assertTrue(self.gate.permits(self.now + 850))

    def test_disabling_closes_the_session_and_opening_bumps_the_generation(self):
        before = self.gate.generation
        self.establish()
        self.assertEqual(self.gate.generation, before + 1)
        self.assertFalse(self.check(owner=True, enabled=False))
        self.assertEqual((self.gate.generation, self.gate.closed_reason), (before + 2, "disabled"))


if __name__ == "__main__": unittest.main()

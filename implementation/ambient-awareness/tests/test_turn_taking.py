"""Turn-taking (feature butler-turn-taking): stop only when really interrupted; a committed turn survives a check gap."""
import datetime as dt
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from butler_ambient.conversation import Conversation
from butler_ambient.delivery import ZONE
from butler_ambient.listening import Listening
from butler_ambient.policy import Evidence
from butler_ambient.sensing import Batch
from butler_ambient.service import Service
from butler_ambient.store import Store


class TurnTakingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = dt.datetime.now(ZONE).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.inputs = Mock(audio_status={"ready": True, "echo_cancellation": True}, playback={"state": "idle", "id": None})
        self.service = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.voice = Conversation(self.service)
        self.service.voice = self.voice
        self.voice.model = Mock()
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        self.store.set("face_wake_dialogue", True)
        self.service.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .1
            self.service.dialogue_gate.check(self.evidence(), self.now, True)
        self.voice.token = threading.Event()
        self.voice.speak = Mock(return_value="spoken")

    def tearDown(self):
        self.service.stop(); self.store.close(); self.temp.cleanup()

    def evidence(self, voices=frozenset({"owner"}), certain=True, speech=True):
        return Evidence(self.now, True, True, frozenset({"owner"}), True, voices, speech_detected=speech, voice_certain=certain)

    def replying(self):
        self.voice.turn_active, self.voice.state, self.voice.phase = True, "thinking", "reply"

    def test_raw_speech_between_sentences_is_collected_without_stopping_output(self):
        listening = self.passive(); self.replying(); old = self.voice.token
        with patch.object(Listening, 'speech', return_value=True), patch.object(Conversation, '_silence_tail', return_value=0):
            listening.feed(Batch(self.now-1, self.now, [], b'\1\0'*16000), self.evidence())
        self.assertFalse(old.is_set())
        self.assertEqual(len(listening.audio), 32000)
        self.assertTrue(self.voice.jobs.empty())

    def test_other_voices_fragments_name_and_owner_stop_keep_their_distinct_authority(self):
        self.replying(); old = self.voice.token
        for text, owner, other in [('А ты что думаешь про отчёт?', False, True), ('Подожди', False, True),
                                    ('Да нет', True, False), ('Хватит, достаточно', False, False)]:
            self.assertFalse(self.voice.observe_input(text, text, owner=owner, other=other, final=True))
            self.assertFalse(old.is_set())
        self.voice.model.generate_json.assert_not_called()
        self.assertTrue(self.voice.observe_input('Батлер, хватит', 'named', other=True, final=True))
        self.assertTrue(old.is_set()); self.inputs.cancel_playback.assert_called()
        self.voice.token = old = threading.Event(); self.replying()
        self.assertTrue(self.voice.observe_input('Подожди', 'owner-stop', owner=True, final=True))
        self.assertTrue(old.is_set())

    def test_positive_company_stops_committed_private_output_dec066(self):
        self.replying()
        self.voice.history.append({"at": self.now, "owner": "вопрос", "butler": "ответ"})
        old = self.voice.token
        self.service.verifier.last_voice_failure = "voice-unrecognized-or-ambiguous"
        self.service.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, False, True, voice_ids=frozenset({"guest"}),
                                                                                 voice_certain=True, speech_detected=True)
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            for _ in range(2):
                self.now += 1
                self.service.accept_batch(Batch(self.now - 1, self.now, [], b"\0" * 32000))   # another person: the session closes (DEC-066)
            self.assertFalse(self.service.dialogue_gate.permits(self.now))
        self.assertTrue(old.is_set())
        self.assertFalse(self.voice.history)

    def test_pause_still_stops_a_reply_immediately(self):
        self.voice.state, self.voice.phase = "speaking", "playback"
        old = self.voice.token
        self.service.control("enabled", {"value": False})
        self.assertTrue(old.is_set())
        self.inputs.cancel_playback.assert_called()

    def test_passive_hearing_routes_nothing_while_a_reply_is_in_progress(self):
        self.service.listening = Listening(self.service)
        listening = self.service.listening
        listening.model = Mock(); listening.model.transcribe.return_value = "Расскажи ещё"
        self.store.set("continuous_archive", True); self.store.set("voice_enabled", True); self.store.set("voice_profile", "baritone")
        self.replying()
        old = self.voice.token
        with patch.object(Listening, "enabled", new_callable=PropertyMock, return_value=True):
            listening.process(b"\1\0" * 16000, listening.token, self.now, True, self.now)
        self.assertFalse(old.is_set())
        self.assertEqual(listening.counts.get("playback-input"), 1)
        self.assertTrue(self.voice.jobs.empty())

    def passive(self):
        self.service.listening = Listening(self.service)
        self.service.listening.model = Mock()
        self.store.set('continuous_archive', True)
        self.store.set('voice_enabled', True)
        self.store.set('voice_profile', 'baritone')
        return self.service.listening

    def test_named_draft_stops_output_and_only_the_complete_request_is_dispatched(self):
        listening = self.passive(); self.replying(); old = self.voice.token
        identifier = listening._begin(self.now-1); pcm = b'\1\0'*16000
        listening.audio.extend(pcm)
        listening._draft(identifier, 'Эй, Батлер')
        self.assertTrue(old.is_set()); self.assertTrue(self.voice.jobs.empty())
        self.assertEqual(bytes(listening.audio), pcm)
        text = 'Эй, Батлер, почисти систему и сначала покажи, что можно удалить.'
        listening.model.transcribe.return_value = text
        listening.process(pcm*4, listening.token, self.now, True, self.now, False, identifier)
        self.assertEqual(self.voice.jobs.get_nowait()[-1], text)
        self.assertTrue(self.voice.jobs.empty())

    def test_final_before_optional_draft_dispatches_the_whole_request_once(self):
        listening = self.passive(); self.replying()
        identifier = listening._begin(self.now-2)
        text = 'Эй, Батлер, почисти систему, но сначала покажи весь список.'
        listening.model.transcribe.return_value = text
        args = (b'\2\0'*64000, listening.token, self.now, True, self.now, False, identifier)
        listening.process(*args)
        listening.utterance = None
        listening._draft(identifier, 'Эй, Батлер')
        listening.process(*args)
        self.assertEqual(self.voice.jobs.get_nowait()[-1], text)
        self.assertTrue(self.voice.jobs.empty())

    def test_draft_during_final_decode_cannot_dispatch_its_prefix(self):
        listening = self.passive(); self.replying(); old = self.voice.token
        identifier = listening._begin(self.now-2)
        text = 'Батлер, почисти систему и спроси перед удалением.'
        def decode(*args, **kwargs):
            listening._draft(identifier, 'Батлер')
            self.assertTrue(old.is_set()); self.assertTrue(self.voice.jobs.empty())
            return text
        listening.model.transcribe.side_effect = decode
        listening.process(b'\1\0'*64000, listening.token, self.now, True, self.now, False, identifier)
        self.assertEqual(self.voice.jobs.get_nowait()[-1], text)
        self.assertTrue(self.voice.jobs.empty())

    def test_pause_clears_draft_admission_and_rejects_old_final(self):
        listening = self.passive(); self.replying()
        identifier = listening._begin(self.now-1); token = listening.token
        listening._draft(identifier, 'Батлер')
        self.assertIn(identifier, self.voice.accepted_interruptions)
        self.service.control('enabled', {'value': False})
        self.assertFalse(self.voice.accepted_interruptions)
        self.service.control('enabled', {'value': True})
        listening.model.transcribe.return_value = 'Батлер, старая просьба.'
        listening.process(b'\1\0'*16000, token, self.now, True, self.now, False, identifier)
        self.assertTrue(self.voice.jobs.empty())

    def test_neighboring_complete_inputs_remain_independent_and_ordered(self):
        listening = self.passive()
        for text in ['Батлер, это предыдущая просьба.', 'Батлер, это следующая просьба.']:
            listening.model.transcribe.return_value = text
            listening.process(b'\1\0'*16000, listening.token, self.now, True, self.now)
        self.assertEqual([self.voice.jobs.get_nowait()[-1] for _ in range(2)],
                         ['Батлер, это предыдущая просьба.', 'Батлер, это следующая просьба.'])

    def test_lease_streak_survives_pauses_and_turned_heads_but_not_another_person(self):
        from butler_ambient.policy import DialogueGate
        gate = DialogueGate(); now = 1000.0
        def check(voices=frozenset(), speech=False, certain=True, at=None):
            nonlocal now
            now = at if at is not None else now + 1
            return gate.check(Evidence(now, False, True, voice_ids=voices, speech_detected=speech, voice_certain=certain), now, True)
        self.assertFalse(check(frozenset({"owner"}), True)); self.assertFalse(check(frozenset({"owner"}), True))
        for _ in range(5): self.assertFalse(check())                                # five silent seconds keep the two
        self.assertTrue(check(frozenset({"owner"}), True))                           # the third verified second
        self.assertEqual(gate.confirmed_by, "voice")
        gate.invalidate()
        check(frozenset({"owner"}), True); check(frozenset({"owner"}), True)
        check(frozenset({"guest"}), True)                                            # another person resets
        self.assertFalse(check(frozenset({"owner"}), True))
        gate.invalidate()
        check(frozenset({"owner"}), True); check(frozenset({"owner"}), True)
        check(at=now + 11)                                                           # longer than GRACE resets
        self.assertFalse(check(frozenset({"owner"}), True))

    def test_soft_lease_loss_inside_a_committed_turn_does_not_raise_but_a_hard_stop_does(self):
        self.voice.turn_active, self.voice.state, self.voice.phase = True, "thinking", "address"
        token = threading.Event()
        generation = self.service.voice_gate.generation
        self.service.dialogue_gate.invalidate()                                      # lease gone mid-turn
        self.voice._permitted(token, generation)                                     # no Canceled
        self.assertEqual(self.voice.diagnostics()["counts"].get("lease_loss_deferred"), 1)
        token.set()
        from butler_ambient.conversation import Canceled
        with self.assertRaises(Canceled): self.voice._permitted(token, generation)
        self.voice.turn_active = False
        self.voice.state, self.voice.phase = "ready", "idle"
        with self.assertRaises(Canceled): self.voice._permitted(threading.Event(), generation)

    def test_passive_speech_needs_five_sixty_ms_windows(self):
        vad = Mock()
        with patch("webrtcvad.Vad", return_value=vad):
            vad.is_speech.side_effect = lambda frame, rate: frame[0] == 1
            self.assertFalse(Listening.speech(b"\1\0" * 320 * 10 + b"\0\0" * 320 * 40))   # 0.2 s of voice
            self.assertFalse(Listening.speech(b"\1\0" * 320 * 14 + b"\0\0" * 320 * 36))   # below 300 ms
            self.assertTrue(Listening.speech(b"\1\0" * 320 * 15 + b"\0\0" * 320 * 35))    # 300 ms
            self.assertTrue(Listening.speech(b"\1\0" * 320 * 20 + b"\0\0" * 320 * 30))    # 0.4 s
            self.assertTrue(Listening.speech(b"\1\0" * 320 * 45 + b"\0\0" * 320 * 5))     # 0.9 s

    def test_ignored_draft_cannot_erase_audio_arriving_for_the_same_utterance(self):
        listening = self.passive(); self.replying()
        identifier = listening._begin(self.now-1)
        prefix, suffix = b'\1\0'*16000, b'\2\0'*16000
        listening.audio.extend(prefix)
        listening._draft(identifier, 'Да нет')
        listening.audio.extend(suffix)
        listening._draft(identifier, 'Да нет я продолжаю')
        self.assertEqual(bytes(listening.audio), prefix+suffix)
        self.assertTrue(self.voice.jobs.empty())


if __name__ == "__main__":
    unittest.main()

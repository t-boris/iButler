"""Attention (feature butler-attention-architecture REQ-001/002): whom the owner is talking to."""
import datetime as dt
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from butler_ambient import attention as att
from butler_ambient.attention import Attention, BUSY, ENGAGED, IDLE, PAUSED
from butler_ambient.conversation import Conversation
from butler_ambient.delivery import ZONE
from butler_ambient.dialogue_state import Scene
from butler_ambient.listening import Listening
from butler_ambient.policy import Evidence
from butler_ambient.service import Service
from butler_ambient.store import Store


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.service = Mock(gate=Mock(enabled=True), meeting=Mock(quiet=False, state="available"), voice=Mock(), preview_frame=None)
        self.a = Attention(self.service, clock=lambda: self.now)

    def evidence(self, voices=frozenset(), scene=Scene(), certain=True):
        return Evidence(self.now, True, True, voice_ids=voices, voice_certain=certain, scene=scene)

    def test_other_voice_face_and_mic_app_make_busy_and_busy_expires(self):
        self.assertEqual(self.a.state(), IDLE)
        self.a.observe(self.evidence(frozenset({"guest"})), self.now)
        self.assertEqual((self.a.state(), self.a.busy_cause), (BUSY, "other-voice"))
        self.now += 31
        self.assertEqual(self.a.state(), IDLE)
        self.a.observe(self.evidence(scene=Scene(available=True, faces=1, owner_visible=False, others_present=True)), self.now)
        self.assertEqual(self.a.state(), IDLE)                 # one unidentified face in the dark is not company
        self.a.observe(self.evidence(scene=Scene(available=True, faces=2, owner_visible=True, others_present=True)), self.now)
        self.assertEqual(self.a.busy_cause, "other-face")
        self.now += 31
        self.service.meeting.quiet, self.service.meeting.state = True, "external-microphone-active"
        self.a.observe(self.evidence(), self.now)
        self.assertEqual(self.a.busy_cause, "mic-app")
        self.assertEqual(self.a.transitions[-1]["cause"], "mic-app")

    def test_talking_while_looking_away_for_ten_seconds_means_a_call(self):
        away = Scene(available=True, faces=1, owner_visible=True, owner_facing=False, owner_talking=True)
        for _ in range(10):
            self.a.observe(self.evidence(scene=away), self.now); self.now += 1
        self.assertEqual(self.a.state(), IDLE)
        self.a.observe(self.evidence(scene=away), self.now)
        self.assertEqual((self.a.state(), self.a.busy_cause), (BUSY, "looking-away-talking"))

    def test_name_clears_busy_and_an_answer_starts_a_session_that_ends_on_request(self):
        self.a.observe(self.evidence(frozenset({"guest"})), self.now)
        self.a.named(self.now)
        self.assertEqual(self.a.state(), ENGAGED)
        self.now += 301
        self.assertEqual(self.a.state(), IDLE)
        self.a.engaged(self.now); self.assertEqual(self.a.state(), ENGAGED)
        self.a.ended(self.now); self.assertEqual(self.a.state(), IDLE)
        self.service.gate.enabled = False
        self.assertEqual(self.a.state(), PAUSED)

    def test_spoken_cues_set_and_clear_busy_by_hand(self):
        self.assertEqual(self.a.spoken_cue("Батлер, я на звонке", self.now), "busy")
        self.assertEqual((self.a.state(), self.a.busy_cause), (BUSY, "owner-said"))
        self.now += 400
        self.assertEqual(self.a.state(), BUSY)                                      # ten minutes, not thirty seconds
        self.assertEqual(self.a.spoken_cue("Всё, я свободен", self.now), "free")
        self.assertEqual(self.a.state(), IDLE)
        self.assertIsNone(self.a.spoken_cue("Расскажи про звонок", self.now))

    def test_i_am_free_outranks_a_microphone_app_for_ten_minutes(self):
        self.service.meeting.quiet, self.service.meeting.state = True, "external-microphone-active"
        self.a.observe(self.evidence(), self.now)
        self.assertEqual((self.a.state(), self.a.busy_cause), (BUSY, "mic-app"))
        self.assertEqual(self.a.spoken_cue("Батлер, я свободен", self.now), "free")
        self.a.observe(self.evidence(), self.now + 1)
        self.assertEqual(self.a.state(self.now + 1), IDLE)                 # the tab holding the microphone no longer counts
        self.a.desktop({"call_app": True}, self.now + 2)
        self.assertEqual(self.a.state(self.now + 2), IDLE)
        self.a.observe(self.evidence(frozenset({"guest"})), self.now + 3)
        self.assertEqual(self.a.busy_cause, "other-voice")                  # a real other person still counts
        self.now += 700
        self.a.observe(self.evidence(), self.now)
        self.assertEqual(self.a.busy_cause, "mic-app")                      # the word expires after ten minutes

    def test_vision_judges_a_lit_fresh_frame_once_per_five_seconds_and_never_a_dark_one(self):
        self.service.preview_frame = (b"jpeg", self.now)
        self.service.voice.vision_model.generate_json.return_value = {"phone_at_ear": True, "talking_to_person": False, "person_visible": True}
        with patch.object(att, "luminance", return_value=120):
            self.assertEqual(self.a.look(self.now)["phone_at_ear"], True)
            self.assertEqual((self.a.state(), self.a.busy_cause), (BUSY, "phone-at-ear"))
            self.a.look(self.now + 2)
            self.assertEqual(self.service.voice.vision_model.generate_json.call_count, 1)        # cached
            self.assertNotIn("jpeg", str(self.a.snapshot()))                                     # only booleans kept
        self.a.busy_until = float("-inf")
        with patch.object(att, "luminance", return_value=10):
            self.assertIsNone(self.a.look(self.now + 10))
            self.assertEqual(self.service.voice.vision_model.generate_json.call_count, 1)        # dark: not judged
        self.assertEqual(self.a.state(), IDLE)

    def test_talking_to_person_from_one_frame_needs_a_second_detected_face(self):
        self.service.preview_frame = (b"jpeg", self.now)
        self.service.voice.vision_model.generate_json.return_value = {"phone_at_ear": False, "talking_to_person": True, "person_visible": True}
        with patch.object(att, "luminance", return_value=120):
            self.service.latest_scene = Scene(available=True, faces=1, owner_visible=True)
            self.a.look(self.now)
            self.assertEqual(self.a.state(), IDLE)                 # the owner alone, talking to the camera (2026-10-25)
            self.assertFalse(self.a.company_recent(self.now))
            self.service.latest_scene = Scene(available=True, faces=2, owner_visible=True, others_present=True)
            self.service.preview_frame = (b"jpeg", self.now + 6)
            self.a.look(self.now + 6)
            self.assertEqual((self.a.state(self.now + 6), self.a.busy_cause), (BUSY, "talking-to-person"))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = dt.datetime.now(ZONE).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.inputs = Mock(audio_status={"ready": True, "echo_cancellation": True}, playback={"state": "idle", "id": None})
        self.service = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.voice = Conversation(self.service); self.service.voice = self.voice
        self.voice.model = Mock(); self.voice.barge_model = Mock(); self.voice.vision_model = Mock()
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        self.store.set("face_wake_dialogue", True)
        self.service.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .1
            self.service.dialogue_gate.check(Evidence(self.now, True, True, frozenset({"owner"}), True, frozenset({"owner"}), speech_detected=True, voice_certain=True), self.now, True)
        self.voice.token = threading.Event()
        self.voice.speak = Mock(return_value="spoken")

    def tearDown(self):
        self.service.stop(); self.store.close(); self.temp.cleanup()

    def test_busy_owner_is_not_answered_unnamed_but_the_name_always_works(self):
        self.service.attention.observe(Evidence(self.now, True, True, voice_ids=frozenset({"guest"}), voice_certain=True), self.now)
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False, "reply": "Ответ."}
        self.voice.model.transcribe.return_value = "Что сегодня по плану?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_not_called()
        self.voice.model.generate_json.assert_not_called()
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["outcome"], "owner-busy")
        self.voice.model.transcribe.return_value = "Батлер, что сегодня по плану?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_called_once()
        self.assertEqual(self.service.attention.state(self.now), ENGAGED)

    def test_a_private_answer_keeps_an_unnamed_general_follow_up_addressed(self):
        # The lease flaps between turns; the Butler session (attention ENGAGED) does not (2026-10-25).
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False, "reply": "Ответ."}
        self.voice.model.transcribe.return_value = "Батлер, сколько будет семь на восемь?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_called_once()
        self.assertEqual(self.service.attention.state(self.now), ENGAGED)
        self.assertTrue(self.voice.conversation_active())
        self.voice.state = "ready"                                                     # as the worker loop leaves it after a turn
        self.service.listening = Listening(self.service); listening = self.service.listening
        listening.model = Mock()
        self.store.set("continuous_archive", True); self.store.set("voice_enabled", True); self.store.set("voice_profile", "baritone")
        self.service.dialogue_gate.invalidate()                                        # lease lost between the turns
        self.now += 20
        with patch.object(Listening, "enabled", new_callable=PropertyMock, return_value=True), \
             patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            listening.model.transcribe.return_value = "А почему так?"
            listening.process(b"\1\0" * 16000, listening.token, self.now, True, self.now)
        task = self.voice.jobs.get_nowait()
        self.assertEqual(task[2], "general")
        self.voice.speak.reset_mock(); self.voice.visible_mode = "general"
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False, "reply": "Потому что."}
        self.voice._general_turn(*task)
        self.voice.speak.assert_called_once()                                          # no name needed: the session is on
        turn = self.voice.diagnostics()["turns"][-1]
        self.assertEqual((turn["outcome"], turn["conversation_active"]), ("replied", True))
        self.assertEqual(self.service.attention.state(self.now), ENGAGED)             # and a general answer renews it

    def test_vision_vetoes_a_turn_in_parallel_and_a_lone_owner_is_never_vetoed(self):
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False, "reply": "Ответ."}
        self.service.preview_frame = (b"jpeg", self.now)
        self.service.latest_scene = Scene(available=True, faces=1, owner_visible=True)
        with patch.object(att, "luminance", return_value=120):
            self.voice.vision_model.generate_json.return_value = {"phone_at_ear": False, "talking_to_person": True, "person_visible": True}
            self.voice.model.transcribe.return_value = "Ты можешь мне помочь с Батлером?"
            self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
            self.voice.speak.assert_called_once()                                      # alone: the frame verdict is not company
            self.voice.token = threading.Event()
            self.service.attention.vision = {"at": float("-inf"), "result": None}
            self.voice.vision_model.generate_json.return_value = {"phone_at_ear": True, "talking_to_person": False, "person_visible": True}
            self.voice._look_and_veto(self.voice.token, self.service.attention, self.now)
        self.assertTrue(self.voice.token.is_set())                                     # the phone at the ear stops this turn
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["outcome"], "owner-busy")

    def test_agent_brain_answers_named_and_unnamed_turns_and_falls_back_locally(self):
        from butler_ambient.brain import BrainUnavailable
        from butler_ambient.conversation import Conversation as C
        self.store.set("voice_brain", "agent")
        brain = self.service.brain; brain.ask = Mock(return_value="Аренда — это плата за пользование.")
        self.voice.model.transcribe.return_value = "Батлер, что такое аренда?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_called_once_with("Аренда — это плата за пользование.", unittest.mock.ANY, unittest.mock.ANY)
        for call in self.voice.model.generate_json.call_args_list:                     # the name decides: no local judge
            self.assertNotIn("Examples of decisions", call.args[0])                   # (the local model only phrases the wait)
        self.assertEqual(brain.ask.call_args.args[0], "Батлер, что такое аренда?")
        self.assertIn("владелец один", brain.ask.call_args.args[1])
        turn = self.voice.diagnostics()["turns"][-1]
        self.assertEqual((turn["outcome"], turn["brain"], turn["judge"]), ("replied", "agent", "name"))
        # Unnamed follow-up inside the open session, nobody else around: no local judge at all, the agent answers.
        self.voice.speak.reset_mock(); brain.ask.reset_mock(); self.voice.token = threading.Event(); self.voice.state = "ready"
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .95, "intent": "chat", "criteria": "", "telegram": False}
        self.voice.model.transcribe.return_value = "А чем она отличается от лизинга?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.model.generate_json.assert_not_called()                             # a fast answer needs no local call at all
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["judge"], "session")
        brain.ask.assert_called_once(); self.voice.speak.assert_called_once()
        # The gateway is down: the local model answers with the same guard rails.
        self.voice.speak.reset_mock(); self.voice.token = threading.Event(); self.voice.state = "ready"
        brain.ask.side_effect = BrainUnavailable("gateway http 503")
        self.voice.model.generate_json.return_value = {"reply": "Локальный ответ."}
        self.voice.model.transcribe.return_value = "Батлер, а что такое ипотека?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_called_once_with("Локальный ответ.", unittest.mock.ANY, unittest.mock.ANY)
        self.assertEqual(self.voice.diagnostics()["counts"].get("brain_unavailable"), 1)
        # A slow agent is not an absent one: no local improvisation, an honest sentence instead.
        self.voice.speak.reset_mock(); self.voice.model.generate_json.reset_mock(); self.voice.token = threading.Event(); self.voice.state = "ready"
        brain.ask.side_effect = BrainUnavailable("TimeoutError")
        self.voice.model.transcribe.return_value = "Батлер, прочитай почту"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_called_once_with(C.BRAIN_TIMEOUT_REPLY, unittest.mock.ANY, unittest.mock.ANY)
        self.voice.model.generate_json.assert_not_called()
        # NO_REPLY from the agent is silence (2026-10-25: Butler said «NO_REPLY» aloud).
        from butler_ambient.brain import BrainSilent
        self.voice.speak.reset_mock(); self.voice.token = threading.Event(); self.voice.state = "ready"
        brain.ask.side_effect = BrainSilent()
        self.voice.model.transcribe.return_value = "Батлер, ну ладно"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_not_called()
        self.assertEqual(self.voice.diagnostics()["turns"][-1].get("brain_silent"), True)
        self.assertEqual(self.voice.diagnostics()["counts"].get("brain_silent"), 1)

    def test_direct_speech_outranks_the_local_judge_and_progress_is_spoken_while_the_agent_works(self):
        from butler_ambient.conversation import Conversation as C
        verdict = {"addressed": False, "confidence": .95, "intent": "chat", "criteria": "", "telegram": False}
        self.assertTrue(C._addressed(verdict, False, False, True, direct=True))                 # alone + second person
        self.assertFalse(C._addressed(verdict, False, False, True, direct=False))
        beside = Mock(available=True, others_present=True, owner_facing=False)
        self.assertFalse(C._addressed(verdict, False, False, True, scene=beside, direct=True))  # talking to the person beside
        self.store.set("voice_brain", "agent")
        brain = self.service.brain
        def slow(*args, **kwargs):
            time.sleep(.7); return "Сегодня три задачи."
        brain.ask = Mock(side_effect=slow)
        with patch.object(C, "BRAIN_FILLER_AFTER", .2), patch.object(C, "BRAIN_THINK_AFTER", 0), patch.object(C, "BRAIN_FILLER_EVERY", .25):
            self.voice.model.transcribe.return_value = "Батлер, что у меня сегодня по задачам?"
            self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        spoken = [call.args[0] for call in self.voice.speak.call_args_list]
        self.assertEqual(spoken[0], "Сейчас посмотрю.")                                          # progress, never silence
        self.assertEqual(spoken[-1], "Сегодня три задачи.")
        self.assertGreaterEqual(self.voice.diagnostics()["counts"].get("brain_filler"), 1)

    def test_long_agent_work_is_detached_and_its_result_is_spoken_later(self):
        from butler_ambient.conversation import Conversation as C
        self.store.set("voice_brain", "agent")
        brain = self.service.brain
        release = threading.Event()
        def slow(*args, **kwargs):
            release.wait(2); return "Писем три, срочных нет."
        brain.ask = Mock(side_effect=slow)
        with patch.object(C, "BRAIN_FILLER_AFTER", 5), patch.object(C, "BRAIN_DETACH_AFTER", .3):
            self.voice.model.transcribe.return_value = "Батлер, прочитай почту"
            self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_called_once_with(C.BRAIN_DETACHED_REPLY, unittest.mock.ANY, unittest.mock.ANY)
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["brain"], "agent-detached")
        release.set()
        deadline = time.time() + 2
        while not self.voice.deferred and time.time() < deadline: time.sleep(.05)
        self.assertEqual(self.voice.deferred[-1]["text"], "Писем три, срочных нет.")   # the work was never cut off
        self.voice.speak.reset_mock(); self.voice.state = "ready"
        self.voice._speak_deferred()                                                  # at the next idle moment
        self.voice.speak.assert_called_once_with("Готово. Писем три, срочных нет.", unittest.mock.ANY, unittest.mock.ANY)
        self.assertEqual(self.voice.history[-1]["butler"], "Готово. Писем три, срочных нет.")
        self.assertFalse(self.voice.deferred)
        self.service.attention.observe(Evidence(self.now, True, True, voice_ids=frozenset({"guest"}), voice_certain=True), self.now)
        self.voice.deferred.append({"at": self.now, "request": "x", "text": "y"}); self.voice.speak.reset_mock()
        self.voice._speak_deferred()                                                  # busy with someone: it waits
        self.voice.speak.assert_not_called(); self.assertTrue(self.voice.deferred)

    def test_the_wait_for_the_agent_is_acknowledged_with_request_aware_phrases(self):
        # The owner's rule (2026-10-25): no canned «сейчас посмотрю»; what Butler says while the agent works is about this request.
        from butler_ambient.conversation import Conversation as C
        self.store.set("voice_brain", "agent")
        brain = self.service.brain
        def slow(*args, **kwargs):
            time.sleep(1.0); return "Сегодня три задачи."
        brain.ask = Mock(side_effect=slow)
        self.voice.model.generate_json.return_value = {"ack": "Смотрю задачи на сегодня", "progress": ["Задачи ещё собираю.", "Список почти готов."]}
        with patch.object(C, "BRAIN_FILLER_AFTER", .2), patch.object(C, "BRAIN_THINK_AFTER", 0), patch.object(C, "BRAIN_FILLER_EVERY", .25):
            self.voice.model.transcribe.return_value = "Батлер, что у меня сегодня по задачам?"
            self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        spoken = [call.args[0] for call in self.voice.speak.call_args_list]
        self.assertEqual(spoken[:2], ["Смотрю задачи на сегодня.", "Задачи ещё собираю."])
        self.assertEqual(spoken[-1], "Сегодня три задачи.")
        self.assertNotIn("Сейчас посмотрю.", spoken)
        prompt = self.voice.model.generate_json.call_args.args[0]
        self.assertIn("что у меня сегодня по задачам", prompt)                                  # only the owner's words go in
        self.assertEqual(self.voice.diagnostics()["counts"].get("brain_ack_local"), 1)

    def test_an_acknowledgement_with_a_number_or_from_a_failed_model_falls_back_to_the_canned_phrase(self):
        from butler_ambient.conversation import Conversation as C
        self.assertIsNone(C._phrase("Нашёл 3 письма"))                                              # a number would be an invented result
        self.assertIsNone(C._phrase("**Смотрю**")); self.assertIsNone(C._phrase("Да")); self.assertIsNone(C._phrase(None))
        self.assertEqual(C._phrase(" «Проверяю почту» "), "Проверяю почту.")
        self.store.set("voice_brain", "agent")
        brain = self.service.brain
        def slow(*args, **kwargs):
            time.sleep(.7); return "Писем три."
        brain.ask = Mock(side_effect=slow)
        self.voice.model.generate_json.side_effect = ValueError("local model unavailable")
        with patch.object(C, "BRAIN_FILLER_AFTER", .2), patch.object(C, "BRAIN_THINK_AFTER", 0), patch.object(C, "BRAIN_ACK_GRACE", .1), patch.object(C, "BRAIN_FILLER_EVERY", .25):
            self.voice.model.transcribe.return_value = "Батлер, прочитай почту"
            self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        spoken = [call.args[0] for call in self.voice.speak.call_args_list]
        self.assertEqual(spoken[0], "Сейчас посмотрю.")                                            # progress, never silence
        self.assertEqual(spoken[-1], "Писем три.")
        self.assertEqual(self.voice.diagnostics()["counts"].get("brain_ack_failed"), 1)

    def test_a_desktop_busy_cause_appearing_during_the_look_does_not_veto_the_turn(self):
        # 2026-10-25: a Meet tab took the microphone while the frame was being judged; the veto threw away an answer the
        # agent had ready. Only what the look itself saw (phone at the ear, a person talked to) may veto.
        self.service.preview_frame = (b"jpeg", self.now)
        self.service.latest_scene = Scene(available=True, faces=1, owner_visible=True)
        self.service.attention.vision = {"at": float("-inf"), "result": None}
        self.voice._audit(self.voice.token, phase="test")
        with patch.object(att, "luminance", return_value=120):
            self.voice.vision_model.generate_json.return_value = {"phone_at_ear": False, "talking_to_person": False, "person_visible": True}
            self.service.attention.desktop({"call_app": True}, self.now)
            self.assertEqual((self.service.attention.state(self.now), self.service.attention.busy_cause), (BUSY, "call-app"))
            self.voice._look_and_veto(self.voice.token, self.service.attention, self.now)
        self.assertFalse(self.voice.token.is_set())
        self.assertEqual(self.voice.diagnostics()["counts"].get("look_ignored_desktop_busy"), 1)

    def test_a_follow_up_inside_an_open_session_with_nobody_else_skips_the_local_judge(self):
        self.store.set("voice_brain", "agent")
        brain = self.service.brain
        brain.ask = Mock(return_value="Пятьдесят шесть.")
        self.voice.model.transcribe.return_value = "Батлер, сколько будет семь на восемь?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(self.service.attention.state(self.now), ENGAGED)
        self.voice.token = threading.Event(); self.voice.speak.reset_mock(); self.voice.model.generate_json.reset_mock()
        self.voice.model.generate_json.return_value = {"addressed": False, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False}
        self.voice.model.transcribe.return_value = "А почему так?"                              # no name, no second person
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["judge"], "session")
        self.assertEqual(brain.ask.call_count, 2)                                                 # the agent, not a local judge, decides
        self.voice.speak.assert_called_once_with("Пятьдесят шесть.", unittest.mock.ANY, unittest.mock.ANY)
        for call in self.voice.model.generate_json.call_args_list:
            self.assertNotIn("Examples of decisions", call.args[0])                               # the judge prompt was never sent

    def test_agent_brain_keeps_device_controls_local_and_sends_frames_only_when_alone(self):
        self.store.set("voice_brain", "agent")
        brain = self.service.brain; brain.ask = Mock(return_value="Вижу стол и лампу.")
        self.voice.model.transcribe.return_value = "Батлер, я на встрече"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        brain.ask.assert_not_called()                                                  # a local mode control, not a question
        self.assertTrue(self.voice.model.generate_json.called)                         # the local command interpreter ran
        self.voice.token = threading.Event(); self.voice.state = "ready"
        # «Посмотри»: one fresh frame goes with the words while the owner is alone.
        mid = self.store.media("camera", self.now, self.now, b"\xff\xd8frame", "jpg")
        for _ in range(3):                                                             # the recording gate: a fresh frame needs it
            self.now += .1
            self.service.gate.check(Evidence(self.now, True, True, frozenset({"owner"}), True, voice_ids=frozenset({"owner"}),
                                             camera_ids=frozenset({"owner"}), camera_certain=True, voice_certain=True, speech_detected=True), self.now)
        self.assertTrue(self.service.gate.permits(self.now))
        self.service.request_fresh = Mock(return_value={"status": "captured", "source_id": mid, "generation": self.service.gate.generation})
        self.voice.model.transcribe.return_value = "Батлер, посмотри, что у меня на столе?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(brain.ask.call_args.kwargs["images"], [b"\xff\xd8frame"])
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["frames"], 1)
        # Someone else in the frame: the words go, the frame does not.
        brain.ask.reset_mock(); self.voice.token = threading.Event(); self.voice.state = "ready"
        self.voice.utterance_scene = Mock(available=True, others_present=True, owner_facing=True, owner_visible=True, faces=2,
                                          data=Mock(return_value={}))
        self.voice.model.transcribe.return_value = "Батлер, посмотри ещё раз"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(brain.ask.call_args.kwargs["images"], [])

    def test_without_a_lease_unnamed_speech_gets_a_general_turn_unless_busy(self):
        self.service.listening = Listening(self.service); listening = self.service.listening
        listening.model = Mock()
        self.store.set("continuous_archive", True); self.store.set("voice_enabled", True); self.store.set("voice_profile", "baritone")
        self.service.dialogue_gate.invalidate()                                        # dark room: no lease
        with patch.object(Listening, "enabled", new_callable=PropertyMock, return_value=True), \
             patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            listening.model.transcribe.return_value = "Ну хорошо. Чему у тебя есть доступ?"
            listening.process(b"\1\0" * 16000, listening.token, self.now, True, self.now)
        self.assertEqual(listening.counts.get("general-unnamed"), 1)
        task = self.voice.jobs.get_nowait()
        self.assertEqual(task[2], "general")                                           # general scope: no private context

    def test_passive_hearing_takes_the_busy_cue_without_routing_a_turn(self):
        self.service.listening = Listening(self.service); listening = self.service.listening
        listening.model = Mock()
        self.store.set("continuous_archive", True); self.store.set("voice_enabled", True); self.store.set("voice_profile", "baritone")
        with patch.object(Listening, "enabled", new_callable=PropertyMock, return_value=True), \
             patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            listening.model.transcribe.return_value = "Я сейчас на телефоне"
            listening.process(b"\1\0" * 16000, listening.token, self.now, True, self.now)
            self.assertEqual(listening.counts.get("attention-cue"), 1)
            self.assertTrue(self.voice.jobs.empty())
            self.assertEqual(self.service.attention.state(self.now), BUSY)
            listening.model.transcribe.return_value = "Что там по погоде?"
            listening.process(b"\1\0" * 16000, listening.token, self.now, True, self.now)
            self.assertTrue(self.voice.jobs.empty())                                 # busy: unnamed speech is left alone
            listening.model.transcribe.return_value = "Я свободен"
            listening.process(b"\1\0" * 16000, listening.token, self.now, True, self.now)
            self.assertEqual(self.service.attention.state(self.now), IDLE)

    def test_passive_hearing_drops_unnamed_speech_while_busy_and_routes_the_name(self):
        self.service.listening = Listening(self.service); listening = self.service.listening
        listening.model = Mock()
        self.store.set("continuous_archive", True); self.store.set("voice_enabled", True); self.store.set("voice_profile", "baritone")
        self.service.attention.observe(Evidence(self.now, True, True, voice_ids=frozenset({"guest"}), voice_certain=True), self.now)
        with patch.object(Listening, "enabled", new_callable=PropertyMock, return_value=True), \
             patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            listening.model.transcribe.return_value = "Так что с парковкой?"
            listening.process(b"\1\0" * 16000, listening.token, self.now, True, self.now)
            self.assertEqual(listening.counts.get("owner-busy"), 1)
            self.assertTrue(self.voice.jobs.empty())
            listening.model.transcribe.return_value = "Батлер, найди парковку"
            listening.process(b"\1\0" * 16000, listening.token, self.now, True, self.now)
        self.assertFalse(self.voice.jobs.empty())


if __name__ == "__main__":
    unittest.main()

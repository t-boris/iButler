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

    def test_manual_free_survives_unchanged_application_and_unknown_camera(self):
        self.a._busy("mic-app", self.now, ("Chrome",))
        self.a.manual("free")
        for _ in range(10):
            self.now += 60
            self.a._busy("mic-app", self.now, ("Chrome",))
            self.a.observe(Evidence(self.now, False, False), self.now)
            self.assertEqual(self.a.state(), IDLE)
            self.assertEqual(self.a.snapshot()["override"], "free")
        self.a._busy("mic-app", self.now, ("Zoom",))
        self.assertEqual(self.a.snapshot()["override"], "auto")
        self.assertEqual(self.a.state(), BUSY)

    def test_manual_busy_auto_reset_and_confirmed_end_do_not_change_permissions(self):
        self.a.manual("busy")
        self.now += 3600
        self.assertEqual(self.a.state(), BUSY)
        self.a.manual("auto")
        self.assertEqual(self.a.state(), IDLE)
        self.a._busy("mic-app", self.now, ("Chrome",))
        self.a.manual("free")
        self.a._situation("mic-app", False, self.now)
        self.assertEqual(self.a.snapshot()["override"], "auto")
        self.assertEqual(self.a.state(), IDLE)
        self.service.gate.enabled = False
        self.a.manual("free")
        self.assertEqual(self.a.state(), PAUSED)
        self.service.gate.enable.assert_not_called()

    def test_explicit_spoken_cue_can_replace_button_choice(self):
        self.a.manual("busy")
        self.assertEqual(self.a.spoken_cue("я свободен"), "free")
        self.assertEqual(self.a.state(), IDLE)
        self.assertEqual(self.a.snapshot()["override"], "auto")

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

    def test_name_opens_session_without_erasing_busy_evidence_dec072(self):
        self.a.observe(self.evidence(frozenset({"guest"})), self.now)
        self.a.named(self.now)
        self.assertEqual(self.a.state(), BUSY)
        self.now += 31
        self.assertEqual(self.a.state(), ENGAGED)
        self.now += 301
        self.assertEqual(self.a.state(), ENGAGED)
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
        self.voice.model = Mock(); self.voice.vision_model = Mock()
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

    def finish(self):
        deadline = time.monotonic()+2
        while self.voice.dialogue.active and time.monotonic()<deadline: time.sleep(.005)
        self.assertFalse(self.voice.dialogue.active, 'agent fixture did not finish')
        self.voice.state = 'ready'
        with patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True):
            for _ in range(64): self.voice._deliver_dialogue()

    def test_busy_owner_is_not_answered_unnamed_but_the_name_always_works(self):
        self.service.attention.observe(Evidence(self.now, True, True, voice_ids=frozenset({"guest"}), voice_certain=True), self.now)
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False, "reply": "Ответ."}
        self.voice.model.transcribe.return_value = "Что сегодня по плану?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish()
        self.voice.speak.assert_not_called()
        self.voice.model.generate_json.assert_not_called()
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["outcome"], "owner-busy")
        self.voice.model.transcribe.return_value = "Батлер, что сегодня по плану?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish()
        self.voice.speak.assert_called_once()
        self.assertEqual(self.service.attention.state(self.now), BUSY)

    def test_a_private_answer_keeps_an_unnamed_general_follow_up_addressed(self):
        # The lease flaps between turns; the Butler session (attention ENGAGED) does not (2026-10-25).
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False, "reply": "Ответ."}
        self.voice.model.transcribe.return_value = "Батлер, сколько будет семь на восемь?"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish()
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
            self.finish()
            self.voice.speak.assert_called_once()                                      # alone: the frame verdict is not company
            self.voice.token = threading.Event()
            self.service.attention.vision = {"at": float("-inf"), "result": None}
            self.voice.vision_model.generate_json.return_value = {"phone_at_ear": True, "talking_to_person": False, "person_visible": True}
            self.voice._look_and_veto(self.voice.token, self.service.attention, self.now)
        self.assertTrue(self.voice.token.is_set())                                     # the phone at the ear stops this turn
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["outcome"], "owner-busy")

    def test_agent_preserves_one_conversation_and_failure_has_explicit_local_fallback(self):
        from butler_ambient.brain import BrainUnavailable, BrainSilent
        self.store.set('voice_brain', 'agent')
        brain = self.service.brain; brain.ask = Mock(return_value='Аренда — плата за пользование.')
        self.voice.model.transcribe.return_value = 'Батлер, что такое аренда?'
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish()
        self.assertEqual(self.voice.speak.call_args.args[0], 'Аренда — плата за пользование.')
        self.assertEqual(self.voice.diagnostics()['turns'][-1]['judge'], 'name')
        self.voice.token = threading.Event(); self.voice.speak.reset_mock()
        self.voice.model.transcribe.return_value = 'А чем она отличается от лизинга?'
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish(); self.assertEqual(self.voice.diagnostics()['turns'][-1]['judge'], 'session')
        for error in [BrainUnavailable('http'), BrainUnavailable('TimeoutError'), BrainUnavailable('agent-error')]:
            self.voice.token = threading.Event(); self.voice.speak.reset_mock()
            brain.ask.side_effect = error
            self.voice.model.transcribe.return_value = 'Батлер, объясни ещё раз'
            self.voice.model.generate_json.return_value = {'reply': 'Локальное объяснение.'}
            self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
            self.finish()
            spoken = ' '.join(c.args[0] for c in self.voice.speak.call_args_list)
            self.assertIn('Агент сейчас недоступен', spoken); self.assertIn('Локальное объяснение', spoken)
        brain.ask.side_effect = BrainSilent(); self.voice.speak.reset_mock(); self.voice.token = threading.Event()
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish(); self.voice.speak.assert_not_called()

    def test_direct_speech_shortcut_respects_other_people(self):
        verdict = {'addressed': False, 'confidence': .95, 'intent': 'chat', 'criteria': '', 'telegram': False}
        self.assertTrue(Conversation._addressed(verdict, False, False, True, direct=True))
        self.assertFalse(Conversation._addressed(verdict, False, False, True, direct=False))
        self.assertFalse(Conversation._addressed(verdict, False, False, True, direct=True,
            scene=Mock(available=True, others_present=True, owner_facing=False)))

    def test_long_work_is_observable_without_holding_the_voice_turn(self):
        entered, release = threading.Event(), threading.Event()
        def run(*args, **kwargs):
            entered.set(); release.wait(1); return 'Писем три, срочных нет.'
        self.store.set('voice_brain', 'agent'); self.service.brain.ask = Mock(side_effect=run)
        self.voice.model.transcribe.return_value = 'Батлер, прочитай почту'
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertTrue(entered.wait(1)); self.assertEqual(self.voice.state, 'ready')
        self.assertTrue(self.voice.dialogue.active)
        release.set(); self.finish()
        spoken = ' '.join(c.args[0] for c in self.voice.speak.call_args_list)
        self.assertIn('Писем три, срочных нет.', spoken); self.assertNotIn('Готово.', spoken)

    def test_failed_result_delivery_does_not_kill_the_next_request(self):
        self.store.set('voice_brain', 'agent'); self.service.brain.ask = Mock(return_value='Ответ после проверки.')
        self.voice.speak.side_effect = RuntimeError('local audio deadline')
        self.voice.model.transcribe.return_value = 'Батлер, проверь статус'
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish(); self.assertEqual(self.voice.state, 'ready')
        self.assertEqual(self.voice.dialogue.snapshot(True)['requests'][-1]['delivery'], 'failed')
        self.voice.speak.side_effect = None; self.voice.token = threading.Event()
        self.service.brain.ask.return_value = 'Следующий ответ.'
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish(); self.assertEqual(self.voice.speak.call_args.args[0], 'Следующий ответ.')

    def test_agent_question_is_visible_and_retains_its_identity_after_a_pause(self):
        self.store.set('voice_brain', 'agent'); self.service.brain.ask = Mock(return_value='Показать список файлов?')
        self.voice.model.transcribe.return_value = 'Батлер, подготовь список'
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish(); question = self.voice.session.question_id
        self.now += 1200
        self.assertTrue(self.voice.session.answering('owner', self.now))
        self.assertEqual(self.voice.session.question_id, question)

    def test_fast_answer_needs_no_generated_waiting_text(self):
        self.store.set('voice_brain', 'agent'); self.service.brain.ask = Mock(return_value='Сегодня три задачи.')
        self.voice.model.transcribe.return_value = 'Батлер, что сегодня по задачам?'
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish()
        self.assertEqual([c.args[0] for c in self.voice.speak.call_args_list], ['Сегодня три задачи.'])
        self.voice.model.generate_json.assert_not_called()

    def test_delayed_reaction_is_bound_to_the_real_request_and_is_not_repeated(self):
        entered, release = threading.Event(), threading.Event()
        def run(*args, **kwargs): entered.set(); release.wait(1); return 'Содержательный ответ.'
        self.store.set('voice_brain', 'agent'); self.service.brain.ask = Mock(side_effect=run)
        self.voice.model.transcribe.return_value = 'Батлер, объясни устройство диска'
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertTrue(entered.wait(1))
        request = next(iter(self.voice.dialogue.requests.values())); request.queued_at -= 2
        with patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True):
            self.voice._deliver_dialogue(); self.voice._deliver_dialogue()
        self.assertEqual(self.voice.speak.call_count, 0)  # DEC-090: ordinary waiting is silent
        release.set(); self.finish()
        self.assertEqual(self.voice.speak.call_count, 1)
        self.assertEqual(self.voice.speak.call_args.args[0], 'Содержательный ответ.')
        self.voice.model.generate_json.assert_not_called()

    def test_status_question_reports_only_real_coordinator_state(self):
        self.store.set('voice_brain', 'agent')
        self.voice.model.transcribe.return_value = 'Что ты делаешь сейчас?'
        self.service.brain.ask = Mock()
        self.voice._turn(b'\0'*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertIn('незавершённой работы нет', self.voice.speak.call_args.args[0])
        self.service.brain.ask.assert_not_called()

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
        self.finish()
        self.assertEqual(self.service.attention.state(self.now), ENGAGED)
        self.voice.token = threading.Event(); self.voice.speak.reset_mock(); self.voice.model.generate_json.reset_mock()
        self.voice.model.generate_json.return_value = {"addressed": False, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False}
        self.voice.model.transcribe.return_value = "А почему так?"                              # no name, no second person
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish()
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
        self.finish()
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
        self.finish()
        self.assertEqual(brain.ask.call_args.kwargs["images"], [b"\xff\xd8frame"])
        self.assertEqual(self.voice.diagnostics()["turns"][-1]["frames"], 1)
        # Someone else in the frame: the words go, the frame does not.
        brain.ask.reset_mock(); self.voice.token = threading.Event(); self.voice.state = "ready"
        self.voice.utterance_scene = Mock(available=True, others_present=True, owner_facing=True, owner_visible=True, faces=2,
                                          data=Mock(return_value={}))
        self.voice.model.transcribe.return_value = "Батлер, посмотри ещё раз"
        self.voice._turn(b"\0" * 32000, self.voice.token, self.service.voice_gate.generation, True)
        self.finish()
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

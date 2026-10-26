"""Voice privacy, address uncertainty, turn cancellation and shared delivery."""
import datetime as dt
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from butler_ambient.conversation import Conversation, Canceled, explicit_address, HEARING_ACK, hearing_check, speech_echo
from butler_ambient.delivery import Delivery, ZONE
from butler_ambient.policy import Evidence
from butler_ambient.sensing import Batch
from butler_ambient.service import Service
from butler_ambient.store import Store


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = dt.datetime.now(ZONE).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.inputs = Mock(audio_status={"ready": True, "echo_cancellation": True}, playback={"state": "idle", "id": None})
        self.service = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.voice = Conversation(self.service)
        self.service.voice = self.voice
        self.voice.model = Mock()
        self.voice.barge_model = Mock()
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        self.service.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .1
            self.service.gate.check(self.evidence(), self.now)
        self.voice.token = threading.Event()
        self.voice.speak = Mock(return_value="spoken")

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def evidence(self, speakers=frozenset()):
        return Evidence(self.now, True, True, frozenset({"owner"}), True, speakers)

    def turn(self, text, *, addressed=True, confidence=.99, alone=True):
        self.voice.model.transcribe.return_value = text
        self.voice.model.generate_json.side_effect = [
            {"addressed": addressed, "confidence": confidence, "intent": "chat", "criteria": "", "telegram": False,
             "reply": "Да, я здесь."}]
        self.voice._turn(b"\0\0" * 16000, self.voice.token, self.service.gate.generation, alone)

    def test_alias_is_vocative_not_word_mentioned_in_conversation(self):
        for text in ("Бата, что видишь?", "Эй, Батлер!", "Butler, hello", "[Музыка] Батлер, ты здесь?", "Первая фраза. Батлер, ты здесь?"):
            self.assertTrue(explicit_address(text, ["Бата", "Батлер", "Butler"]))
        for text in ("Он сказал Батлер", "Батарея разрядилась", "Включи батат"):
            self.assertFalse(explicit_address(text, ["Бата", "Батлер", "Butler"]))

    def test_separate_dialogue_model_and_quiet_listening_do_not_cancel_passive_analysis(self):
        self.service.analyzer.model='gemma3:4b'
        self.voice.model.model='qwen3.5:9b'
        self.store.set('meeting_mode','quiet')
        batch=Batch(self.now-1,self.now,[],b'\x10\x01'*16000)
        with patch.object(Conversation,'ready',new_callable=PropertyMock,return_value=True):
            self.voice.feed(batch,self.evidence(frozenset({'owner'})))
            self.service.analyzer.cancel.assert_not_called()
            self.voice.cancel();self.voice.model.model='gemma3:4b'
            self.voice.feed(batch,self.evidence(frozenset({'owner'})))
            self.service.analyzer.cancel.assert_not_called()
            self.voice.cancel();self.store.set('meeting_mode','available')
            self.voice.feed(batch,self.evidence(frozenset({'owner'})))
            self.service.analyzer.cancel.assert_called_once()

    def test_short_owner_confirmation_is_not_an_echo_of_part_of_another_word(self):
        self.assertFalse(speech_echo("да", "Это важная задача"))
        self.assertTrue(speech_echo("Да", "Да, согласен"))

    def test_verified_owner_can_confirm_even_if_the_word_appeared_in_voice_instructions(self):
        self.face_dialogue()
        self.service.dialogue_gate.voice_streak = 3
        generation = self.service.voice_gate.generation
        self.service.commands.request("preferences",{"muted":True},owner=True,generation=generation)
        self.voice.last_spoken = "Можно сказать подтверждаю или отмена"
        self.voice.last_playback_finished = self.now - .5
        self.voice.model.transcribe.return_value = "подтверждаю"
        self.voice._turn(b"\0"*32000,self.voice.token,generation,True,"microphone",None,True)
        self.assertTrue(self.store.get("muted"))
        self.voice.model.generate_json.assert_not_called()

    def test_direct_hearing_check_replies_without_llm_delay_and_still_needs_permission(self):
        for text in ("Батлер, ты меня слышишь?", "Бата, слышишь?", "Butler, вы меня слышите?",
                     "Батлер, ты слышишь меня?", "Батлер, ты меня вообще слышишь?", "Батлер, слышишь ли ты меня?", "Батлер, ты меня слышишь или нет?"):
            self.assertTrue(hearing_check(text, ["Батлер", "Бата", "Butler"]))
        for text in ("Он сказал Батлер", "Батлер, расскажи о слышимости", "Батлер, ты меня не слышишь?"):
            self.assertFalse(hearing_check(text, ["Батлер", "Бата", "Butler"]))
        self.turn("Батлер, ты меня слышишь?")
        self.voice.model.generate_json.assert_not_called()
        self.assertEqual(self.voice.speak.call_args.args[0], HEARING_ACK)
        self.assertEqual(self.voice.last_completed_source, "microphone")
        self.service.control("enabled", {"value": False})
        with self.assertRaises(Canceled): self.turn("Батлер, ты меня слышишь?")

    def test_public_hearing_check_tolerates_only_punctuation_and_direct_attention(self):
        for text in ('Эй, Батлер. Ты меня слышишь?', 'Слушай, Батлер! Ты меня слышишь?',
                     'Батлер?.. Слышишь ли ты меня?', 'Ну, Бата: вы меня хорошо слышите?'):
            with self.subTest(text=text):self.assertTrue(hearing_check(text,['Батлер','Бата']))
        for text in ('Он сказал: Батлер. Ты меня слышишь?', 'Батлер. Ты меня слышишь? Удали запись.',
                     'Батлер, ты меня не слышишь?', 'Батлер, скажи «ты меня слышишь»',
                     '«Батлер, ты меня слышишь?»', '"Батлер, ты меня слышишь?"',
                     'Эй, другой помощник, ты меня слышишь?'):
            with self.subTest(text=text):self.assertFalse(hearing_check(text,['Батлер','Бата']))

    def test_fixed_synthetic_check_is_distinct_and_never_bypasses_dialogue_permission(self):
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            result = self.voice.check()
            self.assertTrue(result["synthetic"])
            self.assertFalse(result["microphone_test"])
            task = self.voice.jobs.get_nowait()
            self.assertEqual(task[-1], "synthetic-check")
            self.assertGreater(len(task[0]), 16000)
            self.service.control("enabled", {"value": False})
            with self.assertRaises(ValueError): self.voice.check()

    def test_meeting_suppresses_question_followup_control_and_approval_before_model(self):
        self.service.control("preferences", {"meeting_mode":"quiet"})
        self.voice.token = threading.Event()
        for text in ("Как расставить приоритеты?", "А почему именно так?", "Запусти задачу", "Подтверждаю"):
            self.turn(text)
        self.voice.model.generate_json.assert_not_called()
        self.voice.speak.assert_not_called()
        self.assertEqual(self.voice.diagnostics()["counts"]["meeting_suppressed"],4)

    def test_meeting_allows_only_clear_directed_request_without_continuation(self):
        self.service.control("preferences", {"meeting_mode":"quiet"})
        self.voice.token = threading.Event()
        self.turn("Батлер, ты меня слышишь?")
        self.assertEqual(self.voice.speak.call_args.args[0], HEARING_ACK)
        self.voice.speak.reset_mock()
        self.turn("А что дальше?")
        self.voice.speak.assert_not_called()
        self.voice.model.generate_json.assert_not_called()

    def test_quoted_or_other_interlocutor_command_is_silent_after_semantic_control_decision(self):
        self.service.commands.interpret=Mock(return_value=('silent',{}))
        self.service.commands.request=Mock()
        self.turn('Таня, запусти задачу, пожалуйста')
        self.voice.speak.assert_not_called()
        self.service.commands.request.assert_not_called()
        self.assertEqual(self.voice.diagnostics()['counts']['control_not_addressed'],1)

    def test_verified_owner_can_quiet_a_meeting_without_spoken_confirmation(self):
        self.service.dialogue_gate.voice_streak=3
        self.voice.model.transcribe.return_value='Я сейчас на встрече, не вмешивайся'
        self.voice._turn(b'\0'*32000,self.voice.token,self.service.voice_gate.generation,True,'microphone',None,True)
        self.assertEqual(self.store.get('meeting_mode'),'quiet')
        self.voice.speak.assert_not_called()
        self.voice.model.generate_json.assert_not_called()

    def test_uncertain_or_other_person_address_stays_silent(self):
        self.turn("Как думаешь?", confidence=.89)
        self.voice.speak.assert_not_called()
        self.turn("Как думаешь?", confidence=.99, alone=False)
        self.voice.speak.assert_not_called()
        self.turn("Бата, привет", addressed=False)
        self.voice.speak.assert_not_called()

    def test_anonymous_turn_audit_distinguishes_decision_from_cancellation_and_survives_text_clear(self):
        self.turn("Как лучше организовать рабочий день?")
        record = self.voice.diagnostics()["turns"][-1]
        self.assertFalse(record["wake_name"])
        self.assertEqual(record["outcome"], "replied")
        self.assertTrue(record["accepted"])
        self.assertNotIn("рабочий", json.dumps(record, ensure_ascii=False))
        self.voice.state, self.voice.phase = "thinking", "address"
        self.voice.cancel(reason="face-unrecognized-or-ambiguous")
        diagnostics = self.voice.diagnostics()
        self.assertEqual(diagnostics["turns"][-1]["reason"], "face-unrecognized-or-ambiguous")
        self.assertEqual(diagnostics["cancellations"]["address:face-unrecognized-or-ambiguous"], 1)
        self.assertEqual(list(self.voice.ambient_context), [])
        self.assertNotIn("рабочий", json.dumps(diagnostics, ensure_ascii=False))

    def test_explicit_and_recent_context_address_can_reply(self):
        self.turn("Бата, привет", confidence=.8)
        self.assertEqual(self.voice.last_reply, "Да, я здесь.")
        self.voice.speak.assert_called_once()
        self.voice.speak.reset_mock()
        self.turn("А дальше?", confidence=.92, alone=False)
        self.voice.speak.assert_called_once()

    def test_pause_never_runs_asr_and_clears_pending_text_and_pcm(self):
        self.voice.audio.extend(b"secret")
        self.voice.last_heard = "private"
        self.service.control("enabled", {"value": False})
        self.assertEqual(self.voice.audio, b"")
        self.assertEqual(self.voice.last_heard, "")
        with self.assertRaises(Canceled): self.turn("Бата, привет")
        self.voice.model.transcribe.assert_not_called()
        self.inputs.cancel_playback.assert_called()

    def test_owner_barge_in_cancels_old_turn_but_preserves_dialogue_context(self):
        old = self.voice.token
        self.voice.last_direct = self.now - 2
        batch = Batch(self.now - 1, self.now, [], b"\0\0" * 16000)
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            self.voice.feed(batch, self.evidence(frozenset({"owner"})))
            self.assertTrue(old.is_set())
            self.assertEqual(self.voice.last_direct, self.now - 2)
            self.voice.feed(batch, self.evidence())
        self.assertEqual(self.voice.jobs.qsize(), 1)
        self.assertEqual(self.voice.state, "listening")

    def test_self_echo_does_not_cancel_playback_before_address_check(self):
        self.voice.state, self.voice.phase = "speaking", "playback"
        self.voice.last_reply = HEARING_ACK
        self.voice.barge_model.transcribe.return_value = HEARING_ACK
        self.inputs.cancel_playback.reset_mock()
        self.voice._barge(b"\0\0"*16000,self.voice.token,self.service.voice_gate.generation)
        self.inputs.cancel_playback.assert_not_called()
        self.voice.barge_model.generate_json.assert_not_called()
        self.assertEqual(self.voice.diagnostics()["counts"]["echo_suppressed"], 1)
        self.assertEqual(self.voice.state, "speaking")

    def test_policy_cancelled_interruption_is_not_reported_as_asr_failure(self):
        def cancel(*args,**kwargs):
            self.voice.token.set()
            raise RuntimeError('cancelled subprocess')
        self.voice.barge_model.transcribe.side_effect=cancel
        self.voice._barge(b'\0\0'*16000,self.voice.token,self.service.voice_gate.generation)
        self.assertNotIn('failed_interruption',self.voice.diagnostics()['counts'])

    def test_playback_tail_cannot_start_a_new_dialogue_turn(self):
        self.voice.last_spoken = HEARING_ACK
        self.voice.last_playback_finished = self.now - .5
        self.voice.last_direct = self.now - 1
        self.turn(HEARING_ACK)
        self.voice.model.generate_json.assert_not_called()
        self.voice.speak.assert_not_called()
        self.assertEqual(self.voice.diagnostics()["counts"]["echo_suppressed"],1)

    def test_distinct_owner_address_can_interrupt_playback_even_with_uncertain_short_voice(self):
        self.face_dialogue()
        self.voice.state, self.voice.phase = "speaking", "playback"
        self.voice.last_reply = HEARING_ACK
        self.voice.barge_audio.extend(b"\1\0"*16000)
        self.voice.barge_model.transcribe.return_value = "Батлер, остановись"
        self.voice.barge_model.generate_json.return_value = {"addressed":True,"confidence":.99,"intent":"stop","criteria":"","telegram":False}
        old = self.voice.token
        self.voice._barge(bytes(self.voice.barge_audio),old,self.service.voice_gate.generation)
        self.assertTrue(old.is_set())
        self.inputs.cancel_playback.assert_called()
        self.assertEqual(self.voice.jobs.qsize(),1)
        self.assertEqual(self.voice.diagnostics()["counts"]["barge_in_accepted"],1)
        self.assertEqual(self.store.execute("SELECT * FROM media"),[])

    def test_unnamed_stop_interrupts_without_another_model_pass(self):
        self.face_dialogue()
        self.voice.state, self.voice.phase = "speaking", "playback"
        self.voice.last_reply = HEARING_ACK
        self.voice.barge_audio.extend(b"\1\0"*16000)
        self.voice.barge_model.transcribe.return_value = "Подожди, не перебивай"
        old = self.voice.token
        self.voice._barge(bytes(self.voice.barge_audio), old, self.service.voice_gate.generation)
        self.assertTrue(old.is_set())
        self.voice.barge_model.generate_json.assert_not_called()
        self.inputs.cancel_playback.assert_called()

    def test_permitted_pre_roll_preserves_early_words_and_is_erased_on_pause(self):
        first, second = b"\1\0"*16000, b"\2\0"*16000
        with patch.object(Conversation,"ready",new_callable=PropertyMock,return_value=True):
            self.voice.feed(Batch(self.now-1,self.now,[],first),self.evidence())
            self.voice.feed(Batch(self.now-1,self.now,[],second),self.evidence(frozenset({"owner"})))
        self.assertEqual(bytes(self.voice.audio),first+second)
        self.service.control("enabled",{"value":False})
        self.assertEqual(self.voice.pre_roll,b"")
        self.assertEqual(self.voice.barge_audio,bytearray())

    def test_name_followed_by_pause_and_question_keeps_unfinished_wake_audio(self):
        first = b"\1\0" * 16000
        second = b"\2\0" * 16000
        self.voice.pending_pcm = first
        self.voice.pending_generation = self.service.voice_gate.generation
        self.voice.phase, self.voice.state = "address", "thinking"
        old = self.voice.token
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            self.voice.feed(Batch(self.now-1,self.now,[],second), self.evidence(frozenset({"owner"})))
        self.assertTrue(old.is_set())
        self.assertEqual(bytes(self.voice.audio), first + second)
        self.service.control("enabled", {"value": False})
        self.assertEqual(bytes(self.voice.audio), b"")
        self.assertEqual(self.voice.pending_pcm, b"")

    def test_interjection_or_short_vocative_before_the_name_is_a_call(self):
        aliases = ["Бата", "Батлер", "Butler"]
        for text in ("Ай, Батлер, ты вообще слышал, о чём я говорил?", "Так вот, Батлер, что по задачам?", "Ой, Батлер! Погоди.", "Батлер, привет"):
            self.assertTrue(explicit_address(text, aliases), text)
        for text in ("Я вчера говорил с Батлером о делах", "Она сказала, что Батлер молчит", "Пять слов до того как Батлер, нет"):
            self.assertFalse(explicit_address(text, aliases), text)

    def test_debug_shows_unaddressed_words_locally_but_not_in_status_and_clears_on_pause(self):
        from butler_ambient.local_ui import snapshot
        self.face_dialogue()
        self.voice.model.transcribe.return_value = "Частная реплика без обращения"
        self.voice.model.generate_json.return_value = {"addressed":False,"confidence":.99,"intent":"chat","criteria":"","telegram":False,"reply":""}
        self.voice._turn(b"\0\0"*16000,self.voice.token,self.service.voice_gate.generation,True)
        self.assertEqual(snapshot(self.service)["voice"]["last_heard"], "Частная реплика без обращения")
        self.assertIn("heard", [e["event"] for e in snapshot(self.service)["voice"]["debug"]["events"]])
        self.assertEqual(self.voice.ambient_context[-1]["speech"], "Частная реплика без обращения")
        self.assertNotIn("Частная", json.dumps(self.service.status(),ensure_ascii=False))
        self.assertNotIn("Частная", json.dumps(self.voice.diagnostics(),ensure_ascii=False))
        self.assertEqual(self.store.execute("SELECT * FROM observations"), [])
        self.service.control("enabled", {"value": False})                              # a pause clears; a stale check no longer does (DEC-066)
        value = snapshot(self.service)["voice"]
        self.assertEqual(value["debug"]["events"], [])
        self.assertEqual(value["last_heard"], "")
        self.assertEqual(self.voice.last_heard, "")
        self.assertEqual(list(self.voice.debug_events), [])
        self.assertFalse(self.voice.ambient_context)

    def test_other_verified_speaker_never_enters_owner_conversation(self):
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            self.voice.feed(Batch(0, 1, [], b"private"), self.evidence(frozenset({"guest"})))
        self.assertFalse(self.voice.audio)
        self.assertTrue(self.voice.jobs.empty())

    def test_advice_across_voice_and_telegram_uses_same_durable_quota(self):
        telegram, speech = Mock(), Mock()
        telegram.send.return_value, speech.send.return_value = "tg", "voice"
        delivery = Delivery(self.store, telegram)
        self.assertEqual(delivery.send("v", "advice", "advice", self.now, topic="work", created_at=self.now, transport=speech), "sent")
        self.assertEqual(delivery.send("t", "advice", "advice", self.now + 1, topic="work", created_at=self.now), "quota")
        telegram.send.assert_not_called()
        self.assertEqual(delivery.send("v", "advice", "advice", self.now + 3601, topic="work", created_at=self.now + 3601), "duplicate")

    def test_echo_reference_is_mandatory_for_voice_enablement(self):
        self.store.set("voice_enabled", True)
        self.store.set("voice_profile", "calm")
        self.inputs.audio_status["echo_cancellation"] = False
        self.assertFalse(self.voice.ready)

    def face_dialogue(self):
        self.store.set("face_wake_dialogue", True)
        for _ in range(3):
            self.now += .1
            self.service.dialogue_gate.check(self.face_evidence(), self.now, True)
        self.service.gate.invalidate(self.now)

    def face_evidence(self, speech=True):
        return Evidence(self.now, True, True, camera_ids=frozenset({"owner"}), camera_certain=True, speech_detected=speech)

    def test_face_dialogue_can_hear_short_unknown_voice_without_authorizing_archive(self):
        self.face_dialogue()
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            batch = Batch(self.now-1, self.now, [], b"\0\0"*16000)
            self.voice.feed(batch, self.face_evidence())
            self.voice.feed(batch, self.face_evidence(False))
        self.assertEqual(self.voice.jobs.qsize(), 1)
        self.voice.model.transcribe.return_value = "Бата, привет"
        self.voice.model.generate_json.side_effect = [
            {"addressed":True,"confidence":.99,"intent":"chat","criteria":"","telegram":False,"reply":"Я вас слышу."}]
        self.voice._turn(*self.voice.jobs.get_nowait())
        self.voice.speak.assert_called_once()
        self.assertFalse(self.service.gate.permits(self.now))
        self.assertEqual(self.store.execute("SELECT * FROM media"), [])
        self.assertEqual(self.service.request_fresh("camera")["status"], "paused")

    def test_continuous_face_dialogue_classifies_unaddressed_speech_but_stays_silent(self):
        self.face_dialogue()
        self.voice.model.transcribe.return_value = "Это разговор с другим человеком"
        self.voice.model.generate_json.return_value = {"addressed":False,"confidence":.99,"intent":"chat","criteria":"","telegram":False,"reply":""}
        self.voice._turn(b"\0\0"*16000,self.voice.token,self.service.voice_gate.generation,True)
        self.voice.model.generate_json.assert_called_once()
        self.voice.speak.assert_not_called()

    def test_face_only_lease_allows_unnamed_owner_question_without_authorizing_recording(self):
        self.face_dialogue()
        self.voice.model.transcribe.return_value = "Как лучше организовать рабочий день?"
        self.voice.model.generate_json.return_value = {"addressed":True,"confidence":.9,"intent":"chat","criteria":"","telegram":False,"reply":"Начните с важной задачи."}
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            batch = Batch(self.now-1, self.now, [], b"\0\0"*16000)
            self.voice.feed(batch, self.face_evidence())
            self.voice.feed(batch, self.face_evidence(False))
        self.voice._turn(*self.voice.jobs.get_nowait())
        self.voice.speak.assert_called_once()
        self.assertFalse(self.service.gate.permits(self.now))
        self.assertEqual(self.store.execute("SELECT * FROM media"), [])

    def test_voice_only_owner_can_ask_without_name_when_camera_is_missing(self):
        self.store.set("face_wake_dialogue", True)
        self.service.verifier.last_voice_failure = "voice-uncertain"
        self.service.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, False, True,
            voice_ids=frozenset({"owner"}), speech_detected=True, voice_certain=True)
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True):
            for _ in range(3):
                self.now += 1
                self.assertFalse(self.service.accept_batch(Batch(self.now-1, self.now, [], b"\0"*32000)))
            self.assertEqual(self.service.dialogue_gate.confirmed_by, "voice")
            self.assertTrue(self.voice.alone)
            self.service.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, False, True, voice_certain=True)
            self.now += 1
            self.service.accept_batch(Batch(self.now-1, self.now, [], b"\0"*32000))
        self.voice.model.transcribe.return_value = "Что сделать в первую очередь?"
        self.voice.model.generate_json.return_value = {"addressed":True,"confidence":.9,"intent":"chat","criteria":"","telegram":False,"reply":"Начните с важного дела."}
        self.voice._turn(*self.voice.jobs.get_nowait())
        self.voice.speak.assert_called_once()
        self.assertFalse(self.service.gate.allowed)
        self.assertEqual(self.store.execute("SELECT * FROM media"), [])
        self.service.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, False, True, speech_detected=True)
        self.now += 1
        self.service.accept_batch(Batch(self.now-1, self.now, [], b"\0"*32000))
        self.assertTrue(self.service.voice_gate.allowed)                               # unattributed speech is not company (DEC-066)
        self.service.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, False, True, voice_ids=frozenset({"guest"}),
                                                                                 voice_certain=True, speech_detected=True)
        self.now += 1
        self.service.accept_batch(Batch(self.now-1, self.now, [], b"\0"*32000))
        self.assertFalse(self.service.voice_gate.allowed)                              # another person's voice is
        self.assertFalse(self.voice.history)

    def test_service_face_lease_never_records_and_company_cancels_dialogue(self):
        self.store.set("face_wake_dialogue", True)
        self.voice.feed = Mock()
        self.service.verifier.verify.side_effect = lambda batch, refs: self.face_evidence()
        for _ in range(3):
            self.now += 1
            self.assertFalse(self.service.accept_batch(Batch(self.now-1,self.now,[b"frame"],b"\0\0"*16000)))
        self.voice.feed.assert_called_once()
        self.assertTrue(self.service.voice_gate.permits(self.now))
        self.assertFalse(self.service.status()["recording_allowed"])
        self.assertEqual(self.store.execute("SELECT * FROM media"), [])
        self.inputs.cancel_playback.reset_mock()
        self.service.verifier.verify.return_value = Evidence(self.now+1,True,True)      # one unidentified face: the owner in bad light
        self.service.verifier.verify.side_effect = None
        self.now += 1
        self.service.accept_batch(Batch(self.now-1,self.now,[b"unknown"],b"\0\0"*16000))
        self.assertTrue(self.service.voice_gate.permits(self.now))
        from butler_ambient.dialogue_state import Scene
        self.service.verifier.verify.return_value = Evidence(self.now+1,True,True,scene=Scene(available=True,faces=2,others_present=True))
        self.now += 1
        self.service.accept_batch(Batch(self.now-1,self.now,[b"two"],b"\0\0"*16000))   # two faces: company (DEC-066)
        self.assertFalse(self.service.voice_gate.permits(self.now))
        self.inputs.cancel_playback.assert_called()

    def test_face_dialogue_survives_a_stale_check_and_revocation_stops_playback(self):
        self.face_dialogue()
        self.now += 3
        self.voice._permitted(self.voice.token,self.service.voice_gate.generation)        # a stale check keeps the session (DEC-066)
        self.assertTrue(self.service.voice_gate.permits(self.now))
        self.service.control("revoke", {"id":"owner"})
        self.assertFalse(self.service.voice_gate.permits(self.now))
        with self.assertRaises(Canceled): self.voice._permitted(self.voice.token,self.service.voice_gate.generation)
        self.inputs.cancel_playback.assert_called()

    def test_spoken_description_uses_new_permitted_camera_and_separate_cancelable_vision(self):
        mid=self.store.media("camera",self.now,self.now,b"synthetic camera fixture","jpg")
        self.service.request_fresh=Mock(return_value={"status":"captured","source_id":mid,"generation":self.service.gate.generation})
        self.voice.vision_model=Mock()
        self.voice.vision_model.generate_json.return_value={"reply":"Вижу проверочный кадр."}
        self.voice.model.transcribe.return_value="Бата, что видишь?"
        self.voice.model.generate_json.return_value={"addressed":True,"confidence":.99,"intent":"describe","criteria":"","telegram":False}
        self.voice._turn(b"\0\0"*16000,self.voice.token,self.service.voice_gate.generation,True)
        self.service.request_fresh.assert_called_once_with("camera")
        self.assertEqual(self.voice.vision_model.generate_json.call_args.kwargs["images"],[b"synthetic camera fixture"])
        self.service._invalidate()
        self.voice.vision_model.cancel.assert_called()

    def test_face_dialogue_never_uses_participant_only_camera_for_description(self):
        self.face_dialogue()
        self.service.request_fresh=Mock(return_value={"status":"paused"})
        self.voice.vision_model=Mock()
        self.voice.model.transcribe.return_value="Бата, что видишь?"
        self.voice.model.generate_json.return_value={"addressed":True,"confidence":.99,"intent":"describe","criteria":"","telegram":False}
        from itertools import count
        with patch("butler_ambient.conversation.time.monotonic",side_effect=count(step=6)):
            self.voice._turn(b"\0\0"*16000,self.voice.token,self.service.voice_gate.generation,True)
        self.voice.vision_model.generate_json.assert_not_called()
        self.voice.speak.assert_called_once()


if __name__ == "__main__": unittest.main()

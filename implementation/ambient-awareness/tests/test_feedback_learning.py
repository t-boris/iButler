"""Live repair is isolated from storage, exact corrections are idempotent, and busy admission is attributed."""
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient.analysis import LocalAnalyzer
from butler_ambient.attention import BUSY
from butler_ambient.conversation import Conversation, Tokens, Canceled, direct_speech, name_mentioned
from butler_ambient.policy import Evidence
from butler_ambient.service import Service
from butler_ambient.store import Store
from butler_ambient.dialogue_state import Scene


class FeedbackTest(unittest.TestCase):
    def test_inflected_names_are_not_unrelated_prefixes_or_invalid_confidence(self):
        for text in ("с Батлером", "о Батлере", "с Батой", "Butler's answer"):
            self.assertTrue(name_mentioned(text, ["Батлер", "Бата", "Butler"]))
        for text in ("батарея", "батон", "батальон"):
            self.assertFalse(name_mentioned(text, ["Батлер", "Бата", "Butler"]))
        self.assertFalse(Conversation._addressed({"intent": "chat", "addressed": False, "confidence": float("nan")}, False, False, True, direct_name=True))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.store = Store(root / "ambient"); self.now = 100.
        (root / "vault").mkdir(); (root / "workspace").mkdir()
        self.s = Service(self.store, Mock(audio_status={}, playback={"state": "idle"}), Mock(), Mock(),
                         config={"vault": str(root / "vault"), "workspace": str(root / "workspace")},
                         clock=lambda: self.now, transport=Mock())
        self.s.gate.enable(True, self.now); self.store.set("face_wake_dialogue", True)
        for _ in range(3):
            self.now += 1
            self.s.dialogue_gate.check(Evidence(self.now, True, True, voice_ids=frozenset({"owner"}),
                camera_ids=frozenset({"owner"}), camera_certain=True, voice_certain=True, speech_detected=True), self.now, True)
        self.s.voice = Mock(token=threading.Event(), jobs=queue.Queue(maxsize=1))
        self.fb = self.s.feedback

    def tearDown(self):
        self.s.stop(); self.store.close(); self.temp.cleanup()

    def episode(self, text="Ответ про погоду"):
        identifier = self.fb.capture(text, at=self.now, mode="private")
        self.fb.decision(identifier, {"accepted": True, "outcome": "replied"})
        return identifier

    def test_immediate_false_address_repair_survives_disk_failure_and_deduplicates(self):
        with patch.object(self.fb, "_open", side_effect=OSError("disk full")):
            identifier = self.episode()
            args = {"id": identifier, "revision": 1, "kind": "not_addressed", "event": "client-1"}
            started = time.monotonic()
            result = self.fb.correct(args)
            self.assertLess(time.monotonic() - started, .1)
            self.assertTrue(result["applied"]); self.assertFalse(result["saved"])
            self.fb.correct(args)
            self.s.voice.cancel.assert_called_once()
            self.assertEqual(len(self.fb.rows[0]["annotations"]), 1)

    def test_ambiguous_voice_binding_never_becomes_a_training_label(self):
        self.episode(); self.episode("Другая реплика")
        result = self.fb.voice_correction("Это было тебе")
        self.assertEqual(result["state"], "unresolved")
        self.assertFalse(result["saved"])
        self.assertTrue(all(not row["annotations"] for row in self.fb.rows))

    def test_completed_operation_correction_cannot_execute_it_again(self):
        identifier = self.episode()
        self.fb.decision(identifier, {"completed_operation": True})
        result = self.fb.correct({"id": identifier, "revision": 2, "kind": "missed_address", "event": "client-2"})
        self.assertIn("уже", result["message"])
        self.assertTrue(self.s.voice.jobs.empty())

    def test_exact_label_is_saved_once_and_hidden_without_authorization(self):
        identifier = self.episode()
        result = self.fb.correct({"id": identifier, "revision": 1, "kind": "label_other", "event": "client-3"})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.fb.events["client-3"]["saved"]: time.sleep(.01)
        self.assertTrue(self.fb.events["client-3"]["saved"], self.fb.error)
        self.assertEqual(self.fb.storage.dataset()[0]["label"], False)
        self.assertEqual(self.fb.snapshot(False)["episodes"], [])
        self.assertIsNone(self.fb.snapshot(False)["rules"])
        self.assertNotIn("Ответ", json.dumps(self.fb.storage.anonymized_summary()))

    def test_explicit_remember_and_retirement_publish_before_the_next_rule_match(self):
        saved = self.fb.rule_control({"action": "remember", "event": "remember1", "text": "Отвечай кратко."})
        self.assertTrue(saved["saved"] and saved["applied"])
        token = self.s.voice.token
        self.assertIn("кратко", self.fb.context(token, "что дальше", mode="private"))
        self.assertEqual(self.fb.context(token, "что дальше", mode="general"), "")
        self.fb.rule_control({"action": "retire", "id": saved["id"], "event": "retire1"})
        self.assertEqual(self.fb.context(token, "что дальше", mode="private"), "")
        self.s.dialogue_gate.invalidate("company")
        with self.assertRaises(ValueError): self.fb.rule_control({"action": "remember", "event": "x", "text": "anything"})

    def test_single_user_rule_list_is_read_only_and_does_not_clean_tombstones(self):
        from butler_core.rules import RuleStore
        rules = RuleStore(self.s.config['vault'], self.s.config['workspace'], clock=self.s.clock)
        rules.put('Visible', 'Visible', 'Synthetic rule', operation='one', authorized=True)
        rules.put('Tombstoned', 'Deleted', 'Synthetic deleted rule', operation='two', authorized=True)
        rules.deletions.write_text(json.dumps({'Tombstoned': {'at': self.now}}))
        removed_rule = rules.root / 'Tombstoned.md'
        history = rules.root / '.history' / 'Tombstoned' / '1.md'
        self.assertTrue(removed_rule.exists() and history.exists())
        self.store.execute("INSERT OR REPLACE INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.store.set('single_user_local', True)
        self.s.dialogue_gate.invalidate('absence'); self.s.gate.invalidate(self.now)
        with patch.object(RuleStore, 'scan', side_effect=AssertionError('scan deletes tombstoned files')):
            result = self.fb.rule_control({'action': 'list'})
        self.assertEqual([r['id'] for r in result['rules']], ['Visible'])
        self.assertTrue(result['read_only'])
        self.assertTrue(removed_rule.exists() and history.exists())
        with self.assertRaises(ValueError): self.fb.rule_control({'action': 'remember', 'event': 'no', 'text': 'No'})
        with patch.object(self.fb, '_source_allowed', return_value=False):
            with self.assertRaises(ValueError): self.fb.rule_control({'action': 'list'})
        with patch.object(self.fb, '_source_allowed', side_effect=[True, False]):
            with self.assertRaises(ValueError): self.fb.rule_control({'action': 'list'})
        with patch.object(self.fb, '_read_rules', side_effect=lambda rules: (self.store.set('single_user_local', False), [])[1]):
            with self.assertRaises(ValueError): self.fb.rule_control({'action': 'list'})
        with self.assertRaises(ValueError): self.fb.rule_control({'action': 'list'})
        self.store.set('single_user_local', True)
        self.store.set('continuous_archive', False)
        with self.assertRaises(ValueError): self.fb.rule_control({'action': 'list'})
        self.store.set('continuous_archive', True)
        self.s.control('enabled', {'value': False})
        with self.assertRaises(ValueError): self.fb.rule_control({'action': 'list'})
        self.s.control('enabled', {'value': True})
        self.s.control('revoke', {'id': 'owner'})
        with self.assertRaises(ValueError): self.fb.rule_control({'action': 'list'})

    def test_verified_owner_can_still_list_with_existing_controls(self):
        self.assertFalse(self.fb.rule_control({'action': 'list'})['read_only'])

    def test_pause_discards_private_work_and_unfinished_continuation(self):
        identifier = self.episode()
        self.fb.correct({"id": identifier, "revision": 1, "kind": "unfinished", "event": "wait-1"})
        self.fb.pause()
        text, identifier = self.fb.join("новая фраза")
        self.assertEqual(text, "новая фраза"); self.assertIsNone(identifier)
        self.assertEqual(self.fb.snapshot(True)["episodes"], [])

    def test_pending_extension_cannot_repeat_a_completed_operation_or_revive_expired_input(self):
        first = self.episode('Почисти диск')
        second = self.episode('И покажи шаги')
        token = threading.Event(); self.fb.attach(token,first)
        self.fb.decision(first,{'completed_operation':True})
        self.assertEqual(self.fb.extend_pending(token,'И покажи шаги',second),('И покажи шаги',second))
        self.fb.decision(first,{'completed_operation':False})
        self.now += 61
        self.assertEqual(self.fb.extend_pending(token,'И покажи шаги',second),('И покажи шаги',second))

    def test_background_yields_for_capture_asr_and_output_but_not_remote_agent_wait(self):
        from types import SimpleNamespace
        voice=SimpleNamespace(state='thinking',phase='reply',model=SimpleNamespace(operations=set()))
        with patch.object(self.s,'voice',voice):
            self.assertFalse(self.s.status()['analysis_busy'])
            voice.phase='transcription'
            self.assertTrue(self.s.status()['analysis_busy'])
            voice.phase='reply';voice.model.operations.add('local-request')
            self.assertTrue(self.s.status()['analysis_busy'])
            voice.model.operations.clear()
            with patch.object(self.s,'listening',SimpleNamespace(phase='capturing-speech')):
                self.assertFalse(self.s.status()['analysis_busy'])
            with patch.object(self.s,'listening',SimpleNamespace(phase='listening')):
                self.assertTrue(self.s.status()['analysis_busy'])

    def test_agent_reloads_current_scoped_rules_only_through_bound_memory_tool(self):
        identifier = self.episode()
        token = self.s.voice.token
        self.fb.attach(token, identifier)
        saved = self.fb.rule_control({"action": "remember", "event": "agent-rule", "text": "Отвечай кратко."})
        nonce = self.fb.register_agent(token, "что дальше", "Editor")
        self.assertFalse(self.fb.agent_read(nonce))
        context = self.fb.agent_context(nonce)
        self.assertTrue(self.fb.agent_read(nonce))
        self.assertEqual(context["rules"][0]["id"], saved["id"])
        self.fb.rule_control({"action": "retire", "id": saved["id"], "event": "agent-retire"})
        self.assertEqual(self.fb.agent_context(nonce)["rules"], [])
        self.fb.pause()
        with self.assertRaises(ValueError): self.fb.agent_context(nonce)
        with self.assertRaises(ValueError): self.fb.agent_context("not-a-current-turn")

    def test_restart_restores_exact_corrections_and_cannot_repeat_repair(self):
        identifier = self.episode()
        args = {"id": identifier, "revision": 1, "kind": "not_addressed", "event": "durable-event"}
        self.fb.correct(args)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.fb.events["durable-event"]["saved"]: time.sleep(.01)
        self.assertTrue(self.fb.events["durable-event"]["saved"])
        self.fb.stop()
        from butler_ambient.feedback import DialogueFeedback
        fresh = DialogueFeedback(self.s); self.s.feedback = fresh; self.fb = fresh
        fresh._open(); fresh._restore_history()
        self.assertEqual(fresh.rows[0]["id"], identifier)
        fresh.correct(args)
        self.s.voice.cancel.assert_called_once()
        fresh.storage.close(); fresh.storage = None

    def test_metadata_expiry_cleans_original_and_retry_copies_without_audio(self):
        identifier = self.episode("private-words")
        self.now += 86400
        self.fb._expire_memory()
        self.assertEqual(self.fb.rows[0]["text"], "")
        self.assertEqual(self.fb.rows[0]["original"], "")
        self.assertFalse(self.fb.snapshot(True)["audio_examples"])

    def test_certain_company_cannot_use_committed_turn_deferral(self):
        from butler_ambient.conversation import Canceled
        voice = Conversation(self.s); self.s.voice = voice
        voice.turn_active = True; voice.state = "speaking"
        self.s.dialogue_gate.invalidate("company")
        with self.assertRaises(Canceled): voice._permitted(Tokens(voice.token), self.s.voice_gate.generation)
        self.assertTrue(voice.token.is_set())

    def test_shared_session_survives_recency_and_closes_explicitly(self):
        voice = Conversation(self.s); self.s.voice = voice
        voice.last_direct = self.now-1800
        self.assertTrue(voice.conversation_active())
        voice.cancel(reason='owner-paused')
        self.assertFalse(voice.conversation_active())

    def test_stopping_voice_does_not_cancel_accepted_agent_work(self):
        voice = Conversation(self.s); self.s.voice = voice
        entered, release = threading.Event(), threading.Event()
        def answer(*args, **kwargs):
            entered.set(); release.wait(1); return 'Результат принятой работы.'
        self.s.brain.ask = Mock(side_effect=answer)
        token = voice.token
        voice._brain_reply(self.s.brain,'Проверь задачу',Tokens(token,voice.ended),self.s.voice_gate.generation,[],None,True,{},token)
        self.assertTrue(entered.wait(1))
        voice.cancel(clear_context=False,reason='confirmed-owner-interruption')
        request = next(iter(voice.dialogue.requests.values()))
        self.assertFalse(request.cancelled.is_set())
        release.set(); deadline=time.monotonic()+2
        while request.state == 'running' and time.monotonic()<deadline: time.sleep(.005)
        self.assertEqual(request.result, 'Результат принятой работы.')
        voice.stop()

    def test_memory_correction_binds_actual_fragment_and_retraction_restores_it(self):
        import hashlib
        token = self.s.voice.token
        identifier = self.fb.capture("Вопрос о памяти", at=self.now, mode="private", token=token)
        self.fb.memory_context(token, [{"scope": "vault", "path": "Knowledge/fact.md", "line_no": 4, "snippet": "Синтетический факт"}])
        self.fb.decision(identifier, {"accepted": True})
        memory = self.fb.rows[0]["memories"][0]
        result = self.fb.correct({"id": identifier, "revision": 1, "kind": "stale_memory", "memory": memory["id"], "event": "memory-fix"})
        self.assertTrue(result["applied"])
        self.assertIn(memory["hash"], self.s.knowledge.excluded_hashes)
        self.assertEqual(result["binding"], "memory:" + memory["id"])
        self.fb.correct({"id": identifier, "revision": 2, "kind": "retract", "annotation": result["id"], "event": "memory-retract"})
        self.assertNotIn(memory["hash"], self.s.knowledge.excluded_hashes)

    def test_voice_recognition_repair_replaces_text_with_same_utterance_identity(self):
        identifier = self.episode("Неверно распознано")
        result = self.fb.correct({"id": identifier, "revision": 1, "kind": "recognition", "event": "recognition-fix"})
        self.assertTrue(result["applied"])
        text, joined = self.fb.join("Правильные слова")
        self.assertEqual((text, joined), ("Правильные слова", identifier))
        token = threading.Event(); self.fb.attach(token, joined)
        self.assertTrue(self.fb.forced(token))


class AdmissionTest(unittest.TestCase):
    def test_dialogue_and_background_have_distinct_fixed_endpoints(self):
        self.assertEqual(LocalAnalyzer().port, 11434)
        self.assertEqual(LocalAnalyzer(dialogue=True).port, 11435)
        from dialogue_server import launch_agent
        plist = launch_agent("/tmp/home", "/opt/homebrew/bin/ollama")
        self.assertEqual(plist["EnvironmentVariables"]["OLLAMA_HOST"], "127.0.0.1:11435")
        self.assertEqual(plist["EnvironmentVariables"]["OLLAMA_NUM_PARALLEL"], "2")

    def test_busy_session_never_uses_session_shortcut_and_requires_positive_judge(self):
        voice = Conversation.__new__(Conversation)
        attention = Mock(engaged_until=200)
        attention.state.return_value = BUSY
        voice.service = Mock(attention=attention, clock=lambda: 100, feedback=None)
        voice.model = Mock(); voice._audit = Mock(); voice._timed = Mock()
        token = Tokens(threading.Event())
        for addressed, score, accepted in [(True, .99, True), (False, .99, False), (True, .7, False)]:
            voice.model.generate_json.return_value = {"addressed": addressed, "confidence": score, "intent": "chat"}
            intent = voice._judge("speech with actual dialogue", token, False, True, True, "Почему так?", company=False)
            self.assertEqual(voice._addressed(intent, False, True, True), accepted)
            self.assertEqual(voice._audit.call_args.kwargs["judge"], "local")
        self.assertEqual(voice.model.generate_json.call_count, 3)

    def test_desktop_playback_disables_second_person_shortcut(self):
        scene = Scene()
        direct, _ = direct_speech("Ты можешь помочь?", ["Батлер"], None, 100, scene=scene, desktop={"audio_playing": True})
        self.assertFalse(direct)


if __name__ == "__main__": unittest.main()

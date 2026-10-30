"""DEC-062/REQ-026: general replies without biometric identity, never private."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from butler_ambient.conversation import GENERAL, GENERAL_OWNER_REQUIRED, Conversation
from butler_ambient.listening import Listening
from butler_ambient.local_ui import snapshot
from butler_ambient.policy import Evidence
from butler_ambient.service import Service
from butler_ambient.store import Store

CHAT = {'addressed': True, 'confidence': .99, 'intent': 'chat', 'criteria': '', 'telegram': False,
        'reply': 'Пятьдесят шесть.'}


class GeneralDialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)); self.now = 100.
        self.inputs = Mock(audio_status={}, playback={'state': 'idle'})
        self.s = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.v, self.l = self.s.voice, self.s.listening
        self.l.model = Mock(); self.v.model = Mock(); self.v.speak = Mock()
        self.s.knowledge = Mock(); self.s.visual = Mock()
        self.store.set('continuous_archive', True); self.store.set('face_wake_dialogue', True)
        self.store.set('voice_enabled', True); self.store.set('voice_profile', 'baritone')
        self.s.gate.enable(True, self.now)
        ready = patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True)
        ready.start(); self.addCleanup(ready.stop)

    def tearDown(self):
        self.s.stop(); self.store.close(); self.temp.cleanup()

    def hear(self, text):
        self.l.model.transcribe.return_value = text
        self.l.process(b'\0' * 32000, self.l.token, self.now, False, time.monotonic())

    def admit(self):
        for _ in range(3):
            self.now += 1
            self.s.dialogue_gate.check(Evidence(self.now, True, True, voice_ids=frozenset({'owner'}),
                camera_ids=frozenset({'owner'}), camera_certain=True, voice_certain=True, speech_detected=True),
                self.now, True)

    def run_routed(self):
        # Mirrors one iteration of the worker loop for a routed general job.
        task = self.v.jobs.get_nowait()
        self.assertEqual(task[2], GENERAL)
        self.assertFalse(task[1].is_set())
        self.v.visible_mode = GENERAL
        try:
            self.v._general_turn(*task)
            if task[1] is self.v.token and self.v.state != 'listening': self.v.state = 'ready'
        finally:
            if self.v.general_token is task[1]: self.v.general_token = None
        return task

    def prompt(self):
        return self.v.model.generate_json.call_args.args[0]

    def test_addressed_unverified_speech_is_routed_to_general_mode_and_answered(self):
        self.v.model.generate_json.return_value = CHAT
        self.hear('Батлер, сколько будет семь на восемь?')
        self.assertEqual(self.l.phase, 'routed-general')
        self.assertEqual(self.l.counts.get('routed-general'), 1)
        self.assertFalse(self.s.voice_gate.permits(self.now))
        self.run_routed()
        self.v.speak.assert_called()
        self.assertEqual(self.v.speak.call_args.args[0], 'Пятьдесят шесть.')
        self.assertEqual(self.v.speak.call_args.args[2], GENERAL)
        self.assertEqual(len(self.v.general_history), 1)
        self.assertEqual(list(self.v.history), [], 'general replies never enter private history')
        record = self.v.diagnostics()['turns'][-1]
        self.assertEqual((record['mode'], record['outcome']), (GENERAL, 'replied'))

    def test_direct_second_person_speech_is_answered_in_general_scope_and_third_person_talk_is_not(self):
        # Owner, 19:16: «ты можешь мне помочь с Батлером … с которым я сейчас разговариваю?» was rejected as unaddressed.
        self.v.model.generate_json.return_value = {**CHAT, 'addressed': False, 'confidence': .6, 'reply': 'Конечно, с чем помочь?'}
        self.hear('Ну что, мой друг, ты можешь мне помочь с Батлером, с которым я сейчас разговариваю?'); self.run_routed()
        self.v.speak.assert_called()                                   # name mentioned + second person: an unsure judge does not veto
        self.v.speak.reset_mock()
        self.v.general_last = float('-inf')
        self.v.model.generate_json.return_value = {**CHAT, 'addressed': True, 'confidence': .7, 'reply': 'Пятьдесят шесть.'}
        self.hear('Скажи, сколько будет семь на восемь?'); self.run_routed()
        self.v.speak.assert_called()                                   # second person alone: 0.7 is enough
        self.v.speak.reset_mock()
        self.v.general_last = float('-inf')
        self.v.model.generate_json.return_value = {**CHAT, 'addressed': False, 'confidence': .99, 'reply': ''}
        self.hear('Он сказал, что Батлер всё знает.'); self.run_routed()
        self.v.speak.assert_not_called()                               # Q-006: only a definitive negative can veto a name mention

    def test_unsure_third_person_name_mention_generates_missing_general_reply(self):
        self.v.model.generate_json.side_effect = [
            {**CHAT, 'addressed': False, 'confidence': .8, 'reply': ''}, {'reply': 'Чем могу помочь?'}]
        self.hear('Он сказал, что Батлер всё знает.'); self.run_routed()
        self.v.speak.assert_called_once()
        self.assertEqual(self.v.speak.call_args.args[0], 'Чем могу помочь?')
        self.s.knowledge.recall.assert_not_called()

    def test_general_followup_uses_shared_session_admission_and_honors_no_reply(self):
        self.s.attention.engaged(self.now)
        self.v.model.generate_json.return_value = {**CHAT, 'addressed': False, 'confidence': .99, 'reply': 'Продолжим.'}
        self.hear('А что дальше?'); self.run_routed()
        self.assertEqual(self.v.speak.call_args.args[0], 'Продолжим.')
        self.assertIn('already admitted by the shared Butler session', self.prompt())
        self.v.speak.reset_mock()
        self.v.model.generate_json.return_value = {**CHAT, 'reply': 'NO_REPLY'}
        self.hear('Это было другому человеку.'); self.run_routed()
        self.v.speak.assert_not_called()

    def test_unnamed_speech_without_a_lease_gets_a_general_turn_unless_the_owner_is_busy(self):
        # Attention DEC-039 supersedes the name requirement: a general answer discloses nothing private.
        self.hear('Сколько будет семь на восемь?')
        self.assertEqual(self.l.phase, 'routed-general')
        self.assertEqual(self.l.counts.get('general-unnamed'), 1)
        self.assertFalse(self.v.jobs.empty())
        self.v.jobs.get_nowait()
        from butler_ambient.policy import Evidence
        self.s.attention.observe(Evidence(self.now, True, True, voice_ids=frozenset({'guest'}), voice_certain=True), self.now)
        self.hear('Сколько будет семь на восемь?')
        self.assertEqual(self.l.phase, 'blocked')
        self.assertEqual(self.l.counts.get('owner-busy'), 1)
        self.assertTrue(self.v.jobs.empty())

    def test_unchecked_utterance_during_owner_session_stays_private_unless_company_spoke(self):
        # DEC-066: a check that did not complete is not company; a certain other voice is.
        self.admit()
        from butler_ambient.listening import MicrophoneCheck, UtteranceChecks
        stale = MicrophoneCheck(self.now); stale.resolve()
        checks = UtteranceChecks(); checks.items.append(stale)
        self.l.model.transcribe.return_value = 'Батлер, который час?'
        self.l.process(b'\0' * 32000, self.l.token, self.now, checks, time.monotonic())
        self.assertTrue(self.s.voice_gate.permits(self.now))
        self.assertNotEqual(self.v.jobs.get_nowait()[2], GENERAL)
        other = MicrophoneCheck(self.now)
        other.resolve(Evidence(self.now, False, True, voice_ids=frozenset({'guest'}), voice_certain=True, speech_detected=True), now=self.now)
        checks = UtteranceChecks(); checks.items.append(other)
        self.l.process(b'\0' * 32000, self.l.token, self.now, checks, time.monotonic())
        self.assertEqual(self.v.jobs.get_nowait()[2], GENERAL)

    def test_general_prompt_contains_no_owner_name_knowledge_observations_or_private_context(self):
        self.v.history.append({'at': self.now, 'owner': 'PRIVATE_OWNER_QUESTION', 'butler': 'PRIVATE_REPLY'})
        self.v.ambient_context.append({'at': self.now, 'speech': 'PRIVATE_AMBIENT_SPEECH'})
        media = self.store.media('camera', self.now - 10, self.now - 5, b'x', 'mjpeg')
        self.store.save_observation(media, [{'kind': 'book', 'activity': 'reading', 'topic': 'unknown',
            'detail': 'PRIVATE_OBSERVATION', 'subject': 'b', 'identity': '', 'confidence': .9}])
        self.v.model.generate_json.return_value = CHAT
        self.hear('Батлер, мои проекты как продвигаются? Напомни, что мы решили.')
        self.run_routed()
        prompt = self.prompt()
        for private in ('Boris', 'Борис', 'PRIVATE_OWNER_QUESTION', 'PRIVATE_REPLY', 'PRIVATE_AMBIENT_SPEECH',
                        'PRIVATE_OBSERVATION'):
            self.assertNotIn(private, prompt)
        data = json.loads(prompt.split('Data:\n', 1)[1])
        self.assertEqual(set(data), {'speech', 'wake_name_heard', 'conversation_active', 'dialogue'})
        self.s.knowledge.recall.assert_not_called()

    def test_commands_and_confirmations_get_fixed_reply_without_touching_adapters(self):
        # The same fixed reply with or without a pending review: an unverified
        # speaker cannot learn that one exists.
        self.s.commands = Mock(pending=object())
        for text, pending in (('Батлер, запусти задачу в моём проекте.', object()),
                              ('Батлер, подтверждаю.', object()), ('Батлер, подтверждаю.', None)):
            with self.subTest(text=text, pending=pending is not None):
                self.s.commands.pending = pending
                self.v.speak.reset_mock(); self.v.model.generate_json.reset_mock()
                self.hear(text); self.run_routed()
                self.assertEqual(self.v.speak.call_args.args[0], GENERAL_OWNER_REQUIRED)
                self.v.model.generate_json.assert_not_called()
        for name in ('interpret', 'request', 'respond'):
            getattr(self.s.commands, name).assert_not_called()
        self.s.commands.clear.assert_not_called()

    def test_unnamed_command_during_general_conversation_is_not_answered(self):
        self.v.general_last = self.now
        self.hear('Запусти задачу в проекте.')
        self.v._general_turn(*self.v.jobs.get_nowait())
        self.v.speak.assert_not_called()
        self.assertEqual(self.v.diagnostics()['turns'][-1]['rule'], 'general-command-needs-address')

    def test_visual_and_control_intents_get_fixed_reply_and_no_capture(self):
        self.s.request_fresh = Mock()
        for intent in ('describe', 'show-camera', 'show-screen', 'find-image', 'generate-image', 'control'):
            with self.subTest(intent=intent):
                self.v.speak.reset_mock()
                self.v.model.generate_json.return_value = {**CHAT, 'intent': intent, 'reply': ''}
                self.hear('Батлер, что ты видишь вокруг?'); self.run_routed()
                self.assertEqual(self.v.speak.call_args.args[0], GENERAL_OWNER_REQUIRED)
        self.s.request_fresh.assert_not_called()
        self.s.visual.voice_request.assert_not_called()

    def test_follow_up_without_name_continues_only_while_general_conversation_is_active(self):
        self.v.model.generate_json.return_value = CHAT
        self.hear('Батлер, сколько будет семь на восемь?'); self.run_routed()
        self.now += 60
        self.v.model.generate_json.return_value = {**CHAT, 'reply': 'Потому что семь раз по восемь.'}
        self.hear('А почему?'); self.run_routed()
        data = json.loads(self.prompt().split('Data:\n', 1)[1])
        self.assertTrue(data['conversation_active'])
        self.assertEqual(data['dialogue'][0]['speaker'], 'Батлер, сколько будет семь на восемь?')
        self.now += 301
        self.hear('А ещё?')
        # After the window the words still get a general turn (DEC-039); only the dialogue context is gone.
        self.assertFalse(self.v.jobs.empty())
        self.assertTrue(self.v.conversation_active())
        self.run_routed()
        self.assertIn('семь на восемь', self.prompt())

    def test_low_confidence_unnamed_follow_up_stays_silent_while_busy(self):
        self.v.general_last = self.now
        self.s.attention._busy('owner-said',self.now)
        self.v.model.generate_json.return_value = {**CHAT, 'confidence': .8}
        self.hear('Интересно, а почему?'); self.run_routed()
        self.v.speak.assert_not_called()
        self.assertEqual(self.v.diagnostics()['turns'][-1]['outcome'], 'not-addressed')

    def test_identity_loss_does_not_cancel_general_turn_but_erases_private_context(self):
        self.v.model.generate_json.return_value = CHAT
        self.hear('Батлер, привет!')
        token = self.v.token
        self.inputs.cancel_playback.reset_mock()
        self.v.history.append({'at': self.now, 'owner': 'private', 'butler': 'private'})
        self.v.state = 'speaking'
        for reason in ('permission-paused', 'participant-check-gap', 'stale-participant-check'):
            self.s._invalidate(reason)
        self.v.cancel(reason='voice-uncertain', private_only=True)
        self.assertFalse(token.is_set())
        self.assertFalse(self.v.jobs.empty(), 'queued general job survived identity loss')
        self.inputs.cancel_playback.assert_not_called()
        self.assertEqual(list(self.v.history), [])
        self.assertGreater(sum(self.v.diagnostics()['general']['private_released'].values()), 0)

    def test_pause_settings_and_disable_stop_general_dialogue_and_erase_its_context(self):
        for stop in ('pause', 'archive', 'general', 'settings'):
            with self.subTest(stop=stop):
                self.s.control('enabled', {'value': True})
                self.store.set('continuous_archive', True); self.store.set('general_dialogue', True)
                self.v.model.generate_json.return_value = CHAT
                self.hear('Батлер, привет!'); task = self.run_routed()
                self.hear('Батлер, как дела?')
                token = self.v.token
                self.assertTrue(self.v.general_history)
                if stop == 'pause': self.s.control('enabled', {'value': False})
                elif stop == 'archive': self.s.control('preferences', {'continuous_archive': False})
                elif stop == 'general': self.s.control('preferences', {'general_dialogue': False})
                else: self.s._invalidate('settings-change')
                self.assertTrue(token.is_set())
                self.assertEqual(list(self.v.general_history), [])
                self.assertTrue(self.v.jobs.empty())

    def test_service_stop_cancels_general_turn_and_erases_its_context(self):
        self.v.model.generate_json.return_value = CHAT
        self.hear('Батлер, привет!'); self.run_routed()
        self.hear('Батлер, как дела?')
        token = self.v.token
        self.s.stop()
        self.assertTrue(token.is_set())
        self.assertEqual(list(self.v.general_history), [])
        self.assertTrue(self.v.jobs.empty())

    def test_general_permission_check_cancels_when_hearing_stops(self):
        from butler_ambient.conversation import Canceled
        token = threading.Event()
        self.v._permitted(token, GENERAL)
        self.store.set('continuous_archive', False)
        with self.assertRaises(Canceled):
            self.v._permitted(token, GENERAL)

    def test_private_turn_never_sees_general_dialogue(self):
        self.v.model.generate_json.return_value = CHAT
        self.hear('Батлер, GENERAL_GUEST_SECRET?'); self.run_routed()
        self.admit()
        self.v.token = threading.Event(); self.v.visible_mode = 'private'
        self.v.model.generate_json.return_value = {**CHAT, 'reply': 'Готово.'}
        self.v._turn(b'\0' * 32000, self.v.token, self.s.voice_gate.generation, True, 'microphone',
                     time.monotonic(), True, 'Батлер, как дела?')
        self.assertNotIn('GENERAL_GUEST_SECRET', self.prompt())

    def test_unverified_meeting_quiet_phrase_is_silent_without_changing_preference(self):
        from butler_ambient.attention import BUSY
        self.hear('Батлер, я на встрече, не вмешивайся.')
        # The spoken busy cue is taken by hearing itself: no turn, no speech, busy for ten minutes, preference untouched.
        self.assertTrue(self.v.jobs.empty())
        self.v.speak.assert_not_called()
        self.assertNotEqual(self.s.attention.state(self.now), BUSY)
        self.assertEqual(self.store.get('meeting_mode', 'auto'), 'auto')

    def test_local_view_shows_general_events_only_and_does_not_cancel_general_turn(self):
        self.v.model.generate_json.return_value = CHAT
        self.v._debug('reply', 'PRIVATE_DEBUG_REPLY')
        self.hear('Батлер, привет!')
        token = self.v.token
        live = snapshot(self.s)['voice']
        self.assertFalse(token.is_set(), 'viewing cancelled a live general turn')
        self.assertFalse(self.v.jobs.empty())
        self.assertEqual(live['dialogue_mode'], 'general')
        self.assertNotIn('PRIVATE_DEBUG_REPLY', json.dumps(live['debug']['events'], ensure_ascii=False))
        self.run_routed()
        value = snapshot(self.s)['voice']
        self.assertEqual(value['last_reply'], 'Пятьдесят шесть.')
        details = json.dumps(value['debug']['events'], ensure_ascii=False)
        self.assertNotIn('PRIVATE_DEBUG_REPLY', details)
        self.assertIn('Пятьдесят шесть.', details)

    def test_actual_worker_loop_answers_general_turn_without_owner_lease(self):
        spoken = threading.Event()
        self.v.speak.side_effect = lambda *args, **kwargs: spoken.set()
        self.v.model.generate_json.return_value = CHAT
        self.v.history.append({'at': self.now, 'owner': 'private', 'butler': 'private'})
        self.v.thread = threading.Thread(target=self.v._run, daemon=True); self.v.thread.start()
        self.hear('Батлер, сколько будет семь на восемь?')
        self.assertTrue(spoken.wait(3), 'general turn was not answered by the worker loop')
        deadline = time.monotonic() + 2
        while self.v.state != 'ready' and time.monotonic() < deadline: time.sleep(.02)
        self.assertEqual(self.v.state, 'ready')
        self.assertEqual(len(self.v.general_history), 1)
        self.assertEqual(list(self.v.history), [], 'private context was not released on lease loss')
        self.assertNotIn('dialogue-permission-lost', json.dumps(self.v.diagnostics()['cancellations']))

    def test_advice_after_general_turn_is_private_hidden_and_stopped_by_identity_loss(self):
        self.v.model.generate_json.return_value = CHAT
        self.hear('Батлер, привет!'); self.run_routed()
        self.v.state = 'ready'
        self.v.model.generate_json.return_value = {'reply': 'PRIVATE_ADVICE'}
        seen = {}
        def speak(text, token, generation, **kwargs):
            seen['general_view'] = snapshot(self.s)['voice']['last_reply']
            self.inputs.cancel_playback.reset_mock()
            self.v.cancel(reason='voice-uncertain', private_only=True)
            seen['stopped'], seen['playback'] = token.is_set(), self.inputs.cancel_playback.called
        self.v.speak.side_effect = speak
        self.v.send('Совет из наблюдений.')
        self.assertNotEqual(seen['general_view'], 'PRIVATE_ADVICE')
        self.assertTrue(seen['stopped'], 'identity loss did not stop private advice')
        self.assertTrue(seen['playback'], 'identity loss did not stop private advice playback')
        self.assertEqual(len(self.v.general_history), 1, 'general context survives identity loss')

    def test_advice_is_refused_while_a_general_turn_is_routed(self):
        self.hear('Батлер, привет!')
        self.v.state = 'ready'
        with self.assertRaises(RuntimeError):
            self.v.send('Совет из наблюдений.')
        self.v.model.generate_json.assert_not_called()

    def test_advice_during_a_routed_general_turn_is_not_given_to_the_voice(self):
        candidate = {'key': 'k', 'text': 'Совет.', 'topic': 'work', 'created_at': self.now}
        self.s.delivery.send = Mock(return_value='sent'); self.v.model.generate_json.return_value = CHAT
        with patch('butler_ambient.service.advice_candidate', return_value=candidate):
            self.hear('Батлер, привет!')
            self.v.state = 'ready'
            self.s.analysis_tick(self.s.archive.generation, archive=True)
            self.assertIs(self.s.delivery.send.call_args.kwargs['transport'], self.s.display_transport)
            self.run_routed()
            self.s.analysis_tick(self.s.archive.generation, archive=True)
            self.assertIs(self.s.delivery.send.call_args.kwargs['transport'], self.s.display_transport)

    def test_advice_refused_by_a_busy_voice_falls_back_to_the_default_transport(self):
        from butler_ambient.delivery import Delivery
        self.hear('Батлер, привет!')
        self.v.state = 'ready'
        default = Mock(); default.send.return_value = 'receipt-1'
        delivery = Delivery(self.store, default)
        with patch('butler_ambient.delivery.quiet', return_value=False):
            result = delivery.send('k', 'advice', 'Совет.', self.now, topic='work', created_at=self.now,
                                   transport=self.v)
        self.assertEqual(result, 'sent')
        default.send.assert_called_once()
        self.v.model.generate_json.assert_not_called()

    def test_interruption_text_is_never_shown_as_general_after_routing(self):
        self.admit()
        private = self.v.token = threading.Event(); self.v.state = 'speaking'
        def route(*args):
            # A general turn is routed in the gap after the permission check.
            self.v.cancel(clear_context=False, reason='complete-new-utterance')
            self.v.token = threading.Event(); self.v.general_token = self.v.token
            return False
        with patch('butler_ambient.conversation.speech_echo', side_effect=route):
            self.v.observe_input('OWNER_INTERRUPTION_WORDS', 'race', owner=True, final=True)
        self.assertNotIn('OWNER_INTERRUPTION_WORDS', json.dumps(self.v.debug_snapshot(general_only=True), ensure_ascii=False))
        self.assertNotIn('OWNER_INTERRUPTION_WORDS', json.dumps(list(self.v.debug_events), ensure_ascii=False))

    def test_disabling_erases_general_context_even_after_a_private_turn(self):
        for stop in ('general', 'archive'):
            with self.subTest(stop=stop):
                self.s.dialogue_gate.invalidate(); self.v.state = 'ready'
                self.store.set('continuous_archive', True); self.store.set('general_dialogue', True)
                self.v.model.generate_json.return_value = CHAT
                self.hear('Батлер, привет!'); self.run_routed()
                self.admit()
                self.v.token = threading.Event(); self.v.visible_mode = 'private'
                self.v._turn(b'\0' * 32000, self.v.token, self.s.voice_gate.generation, True, 'microphone',
                             time.monotonic(), True, 'Батлер, как дела?')
                self.assertTrue(self.v.general_history)
                if stop == 'general': self.s.control('preferences', {'general_dialogue': False})
                else: self.s.control('preferences', {'continuous_archive': False})
                self.assertEqual(list(self.v.general_history), [])
                self.assertFalse(self.v.general_active())

    def test_another_voice_never_preempts_a_private_turn_in_progress(self):
        self.admit()
        from butler_ambient.listening import MicrophoneCheck, UtteranceChecks
        private = self.v.token = threading.Event(); self.v.state = 'thinking'
        other = MicrophoneCheck(self.now)
        other.resolve(Evidence(self.now, False, True, voice_ids=frozenset({'guest'}), voice_certain=True, speech_detected=True), now=self.now)
        checks = UtteranceChecks(); checks.items.append(other)
        self.l.model.transcribe.return_value = 'Батлер, который час?'
        self.l.process(b'\0' * 32000, self.l.token, self.now, checks, time.monotonic())
        self.assertFalse(private.is_set(), 'general route cancelled the private turn')
        self.assertTrue(self.v.jobs.empty())
        self.assertEqual((self.l.phase, self.l.counts.get('reply-blocked')), ('blocked', 1))

    def test_identity_loss_cancellation_and_general_routing_cannot_interleave(self):
        self.v.history.append({'at': self.now, 'owner': 'private', 'butler': 'private'})
        router = threading.Thread(target=self.hear, args=('Батлер, привет!',), daemon=True)
        original = self.v._cancel
        def racing(*args):
            router.start(); router.join(.2)
            self.assertTrue(router.is_alive(), 'routing ran inside the identity-loss cancellation')
            original(*args)
        with patch.object(self.v, '_cancel', side_effect=racing):
            self.v.cancel(reason='voice-uncertain', private_only=True)
        router.join(3)
        task = self.v.jobs.get_nowait()
        self.assertEqual(task[2], GENERAL)
        self.assertFalse(task[1].is_set(), 'identity loss cancelled the freshly routed general turn')
        self.assertEqual(list(self.v.history), [])

    def test_general_preference_validates_type(self):
        with self.assertRaises(ValueError):
            self.s.control('preferences', {'general_dialogue': 'yes'})


class LocalGeneralModelTests(unittest.TestCase):
    """The general prompt on the real installed local model, synthetic text only."""
    def setUp(self):
        from butler_ambient.analysis import DEFAULT_DIALOGUE_MODEL, LocalAnalyzer
        model = LocalAnalyzer(DEFAULT_DIALOGUE_MODEL)
        try: model.check_local()
        except RuntimeError: self.skipTest('installed local model required')
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)); self.now = 100.
        self.inputs = Mock(audio_status={}, playback={'state': 'idle'})
        self.s = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.s.voice = Conversation(self.s); self.s.listening = Listening(self.s)
        self.v = self.s.voice; self.v.model = model; self.v.speak = Mock(); self.s.visual = Mock()
        self.store.set('continuous_archive', True); self.store.set('voice_enabled', True)
        self.s.gate.enable(True, self.now)

    def tearDown(self):
        self.s.stop(); self.store.close(); self.temp.cleanup()

    def turn(self, text):
        self.v.speak.reset_mock()
        self.v.token = threading.Event(); self.v.general_token = self.v.token; self.v.visible_mode = GENERAL
        self.v._general_turn(b'\0' * 32000, self.v.token, GENERAL, False, 'synthetic-text', time.monotonic(), False, text)
        return ' '.join(call.args[0] for call in self.v.speak.call_args_list)

    def test_named_question_and_follow_up_are_answered_without_owner_name(self):
        first = self.turn('Батлер, сколько будет семь умножить на восемь?')
        self.assertTrue(first, 'named general question got no spoken reply')
        self.now += 20
        follow = self.turn('А сколько будет, если умножить ещё на два?')
        self.assertTrue(follow, 'contextual general follow-up got no spoken reply')
        for reply in (first, follow): self.assertNotIn('Борис', reply)

    def test_speech_to_another_person_during_general_conversation_is_silent(self):
        self.v.general_last = self.now
        self.assertEqual(self.turn('Маша, передай мне, пожалуйста, соль.'), '')

    def test_named_visual_requests_get_only_the_fixed_owner_required_reply(self):
        for text in ('Батлер, опиши, что лежит на столе.', 'Батлер, покажи мне экран.', 'Батлер, нарисуй мне кота.'):
            with self.subTest(text=text):
                self.assertEqual(self.turn(text), GENERAL_OWNER_REQUIRED)
        self.s.visual.voice_request.assert_not_called()


if __name__ == '__main__': unittest.main()

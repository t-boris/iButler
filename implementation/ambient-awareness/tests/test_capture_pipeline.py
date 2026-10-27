"""Slow identity inference must not stop capture/hearing or grant old authority."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient.conversation import Conversation
from butler_ambient.listening import Listening
from butler_ambient.policy import Evidence
from butler_ambient.sensing import Batch
from butler_ambient.service import Service
from butler_ambient.store import Store


class CapturePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.inputs = Mock(audio_status={}, playback={'state':'idle'})
        self.verifier = Mock(unavailable=False, last_failure='unverified-presence', last_voice_failure='unverified-voice')
        self.s = Service(self.store, self.inputs, self.verifier, Mock(), clock=time.time, transport=Mock())
        self.s.voice = Conversation(self.s)
        self.s.listening = Listening(self.s)
        self.s.voice.model = Mock()
        self.s.listening.model = Mock()
        self.s.capture_screen = Mock(return_value=None)
        self.store.set('continuous_archive', True)
        self.store.set('face_wake_dialogue', True)
        self.store.set('voice_enabled', True)
        self.store.set('voice_profile', 'baritone')
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.s.gate.enable(True, time.time())
        now = time.time()
        for offset in (.2,.1,0):
            at = now-offset
            self.s.dialogue_gate.check(Evidence(at, True, True, voice_ids=frozenset({'owner'}),
                camera_ids=frozenset({'owner'}),camera_certain=True,voice_certain=True,speech_detected=True),at,True)
        self.verifying = threading.Event()
        self.release = threading.Event()
        self.hearing = threading.Event()
        def verify(batch, references):
            self.verifying.set()
            self.release.wait(4)
            return Evidence(batch.end, False, True, voice_certain=False, speech_detected=True)
        def transcribe(*args, **kwargs):
            self.hearing.set()
            return 'Сколько минут прошло?'
        self.verifier.verify.side_effect = verify
        self.s.listening.model.transcribe.side_effect = transcribe
        self.speech = patch.object(Listening,'speech',return_value=True)
        self.tail = patch.object(Conversation,'_silence_tail',return_value=1)
        self.speech.start();self.tail.start()

    def tearDown(self):
        self.s.stop_event.set();self.release.set()
        self.s.stop()
        self.speech.stop();self.tail.stop()
        self.store.close();self.temp.cleanup()

    def launch(self, target):
        thread=threading.Thread(target=target,daemon=True)
        self.s.threads.append(thread);thread.start()
        return thread

    @staticmethod
    def batch():
        now=time.time()
        return Batch(now-1,now,[],b'\x12\0'*16000)

    def test_asr_overlaps_identity_and_unchecked_speech_waits_for_its_check_then_stays_private(self):
        self.launch(self.s.listening.loop)
        check=self.launch(lambda:self.s.accept_batch(self.batch()))
        self.assertTrue(self.verifying.wait(1))
        try:
            self.assertTrue(self.hearing.wait(1),'ASR was blocked behind biometric inference')
            self.assertTrue(self.s.voice.jobs.empty(),'old owner permission released unchecked speech')
        finally:self.release.set()
        check.join(2)
        deadline=time.monotonic()+3            # the routing thread may be slow under a full-suite load
        while self.s.listening.phase not in {'routed','routed-general','blocked','error'} and time.monotonic()<deadline:time.sleep(.01)
        # DEC-066: speech the check cannot attribute is not company; the owner session holds and the words are his.
        self.assertEqual(self.s.listening.phase,'routed')
        self.assertFalse(self.s.voice.jobs.empty())
        self.assertTrue(self.s.voice_gate.permits(time.time()))

    def test_matching_checked_utterance_is_released_only_after_the_current_check(self):
        def owner(batch,references):
            self.verifying.set();self.release.wait(4)
            return Evidence(batch.end,True,True,frozenset({'owner'}),True,
                voice_ids=frozenset({'owner'}),camera_ids=frozenset({'owner'}),
                camera_certain=True,voice_certain=True,speech_detected=True)
        self.verifier.verify.side_effect=owner
        self.launch(self.s.listening.loop)
        check=self.launch(lambda:self.s.accept_batch(self.batch()))
        self.assertTrue(self.verifying.wait(1));self.assertTrue(self.hearing.wait(1))
        self.assertTrue(self.s.voice.jobs.empty())
        self.release.set();check.join(2)
        task=self.s.voice.jobs.get(timeout=1)
        self.assertTrue(task[-2]);self.assertEqual(task[-1],'Сколько минут прошло?')

    def test_full_pause_rejects_inflight_capture_and_hearing_after_fast_resume(self):
        self.launch(self.s.listening.loop)
        check=self.launch(lambda:self.s.accept_batch(self.batch()))
        self.assertTrue(self.verifying.wait(1));self.assertTrue(self.hearing.wait(1))
        epoch=self.s.capture_epoch
        self.s.control('enabled',{'value':False})
        self.s.control('enabled',{'value':True})
        self.assertGreater(self.s.capture_epoch,epoch)
        self.release.set();check.join(2)
        self.assertTrue(self.s.voice.jobs.empty())
        self.assertEqual(self.s.listening.last_heard,'')
        self.assertFalse(self.s.voice_gate.permits(time.time()))

    def test_backpressure_preserves_all_recordings_but_breaks_private_check_sequence(self):
        first=self.s._stage_batch(self.batch())
        self.launch(lambda:self.s.accept_batch(first.batch,prepared=first))
        self.assertTrue(self.verifying.wait(1))
        pending=self.s._stage_batch(self.batch());self.s._queue_check(pending)
        latest=self.s._stage_batch(self.batch());self.s._queue_check(latest)
        self.assertTrue(pending.hearing.done.is_set());self.assertFalse(pending.hearing.checked)
        self.assertIs(self.s.check_pending.get_nowait(),latest)
        self.assertEqual(self.s.check_skipped,1)
        self.assertTrue(self.s.voice_gate.permits(time.time()))     # a skipped check is not company (DEC-066)
        self.assertEqual(len(self.store.execute("SELECT id FROM media WHERE source='microphone'")),3)
        self.release.set()

    def test_participant_inference_failure_keeps_hardware_and_passive_hearing_available(self):
        self.verifier.verify.side_effect=RuntimeError('synthetic failure with no raw data')
        stage=self.s._stage_batch(self.batch());self.s._queue_check(stage)
        self.launch(self.s._check_loop)
        self.assertTrue(stage.hearing.done.wait(1))
        deadline=time.monotonic()+1
        while self.s.check_error is None and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.s.check_error,'RuntimeError')
        self.assertTrue(self.s.listening.enabled)
        self.assertTrue(self.s.voice_gate.permits(time.time()))     # a broken check is not company; absence closes later (DEC-066)
        self.inputs.stop.assert_not_called()

    def test_secondary_database_failure_cannot_kill_check_worker_or_restore_old_permission(self):
        self.store.execute("CREATE TRIGGER reject_check_log BEFORE INSERT ON events BEGIN SELECT RAISE(FAIL,'controlled'); END")
        self.verifier.verify.side_effect=RuntimeError('controlled verifier error')
        stage=self.s._stage_batch(self.batch());self.s._queue_check(stage)
        worker=self.launch(self.s._check_loop)
        self.assertTrue(stage.hearing.done.wait(1))
        deadline=time.monotonic()+1
        while self.s.check_error is None and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(worker.is_alive())
        with self.s.lock:self.assertFalse(self.s.voice_gate.permits(time.time()))
        self.store.execute('DROP TRIGGER reject_check_log')
        self.verifier.verify.side_effect=lambda b,r:Evidence(b.end,False,True)
        recovered=self.s._stage_batch(self.batch());self.s._queue_check(recovered)
        self.assertTrue(recovered.hearing.done.wait(1))
        self.assertTrue(recovered.hearing.checked)
        self.assertTrue(worker.is_alive())
        self.inputs.stop.assert_not_called()

    def test_every_spoken_second_waits_for_its_check_and_an_incomplete_check_is_not_company(self):
        from butler_ambient.listening import MicrophoneCheck,UtteranceChecks
        at=time.time()
        checks=UtteranceChecks();first=MicrophoneCheck(at-.1);last=MicrophoneCheck(at)
        first.resolve(Evidence(at-.1,False,True,voice_ids=frozenset({'owner'}),voice_certain=True),now=at)
        checks.items.extend([first,last])
        self.launch(lambda:self.s.listening.process(b'\0'*64000,self.s.listening.token,at,checks,time.monotonic()))
        self.assertTrue(self.hearing.wait(1))
        self.assertTrue(self.s.voice.jobs.empty())
        last.resolve()
        deadline=time.monotonic()+1
        while self.s.listening.phase not in {'routed','blocked'} and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.s.listening.phase,'routed')           # DEC-066: alone by default
        self.assertFalse(self.s.voice.jobs.empty())

    def test_expired_pending_evidence_reports_blocked_instead_of_waiting_forever(self):
        from butler_ambient.listening import MicrophoneCheck,UtteranceChecks
        at=time.time();checks=UtteranceChecks();checks.items.append(MicrophoneCheck(at))
        task=self.launch(lambda:self.s.listening.process(b'\0'*32000,self.s.listening.token,at,checks,time.monotonic()))
        self.assertTrue(self.hearing.wait(1))
        deadline=time.monotonic()+1
        while self.s.listening.phase!='participant-check' and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.s.listening.phase,'participant-check')
        self.s.clock=lambda:at+9
        task.join(1)
        self.assertFalse(task.is_alive())
        self.assertEqual(self.s.listening.phase,'blocked')
        self.assertTrue(self.s.voice.jobs.empty())

    def test_asr_result_past_the_freshness_window_reports_expiry_without_dispatch(self):
        at=time.time()
        def expired(*args,**kwargs):
            self.s.clock=lambda:at+9
            return 'Stale synthetic result'
        self.s.listening.model.transcribe.side_effect=expired
        self.s.listening.process(b'\0'*32000,self.s.listening.token,at,True,time.monotonic())
        self.assertEqual(self.s.listening.phase,'blocked')
        self.assertEqual(self.s.listening.last_heard,'')
        self.assertTrue(self.s.voice.jobs.empty())

    def test_capture_timestamp_expires_permission_even_when_inference_finishes_late(self):
        from butler_ambient.policy import Gate,DialogueGate
        gate=Gate(self.store);gate.enable(True,100)
        dialogue=DialogueGate()
        for captured in (101,102,103):
            evidence=Evidence(captured,True,True,frozenset({'owner'}),True,
                voice_ids=frozenset({'owner'}),voice_certain=True,speech_detected=True)
            gate.check(evidence,captured+1.8)
            dialogue.check(evidence,captured+1.8,True)
        self.assertTrue(gate.permits(105.49));self.assertTrue(dialogue.permits(105.49))
        self.assertFalse(gate.permits(105.51));self.assertTrue(dialogue.permits(105.51))   # recording expires; the owner session holds (DEC-066)

    def test_capture_and_passive_archive_continue_while_identity_model_is_blocked(self):
        second=threading.Event();calls=[]
        def read():
            calls.append(time.monotonic())
            if len(calls)>=2:second.set()
            return self.batch()
        self.inputs.batch.side_effect=read
        if hasattr(self.s,'_check_loop'):self.launch(self.s._check_loop)
        self.launch(self.s.listening.loop)
        self.launch(self.s._capture_loop)
        self.assertTrue(self.verifying.wait(1))
        self.assertTrue(second.wait(1.5),'capture stopped while the identity model was busy')
        deadline=time.monotonic()+.5
        while len(self.store.execute("SELECT id FROM media WHERE source='microphone'"))<2 and time.monotonic()<deadline:time.sleep(.01)
        self.assertGreaterEqual(len(self.store.execute("SELECT id FROM media WHERE source='microphone'")),2)
        self.assertTrue(self.s.voice.jobs.empty())

    def test_capture_does_not_add_a_timer_between_already_queued_pcm_seconds(self):
        self.s.input_running = True
        calls = []
        def read():
            calls.append(time.monotonic())
            if len(calls) == 2: self.s.stop_event.set()
            return self.batch()
        self.inputs.batch.side_effect = read
        with patch.object(self.s, '_stage_batch', return_value=None):
            self.s._capture_loop()
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1]-calls[0], .2, 'capture inserted a second clock after the audio source')


if __name__=='__main__':unittest.main()

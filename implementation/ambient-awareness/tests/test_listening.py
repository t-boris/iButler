"""A working microphone must remain observable when identity blocks a reply."""
import tempfile
import threading
import time
import errno
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from butler_ambient.conversation import Conversation
from butler_ambient.listening import Listening
from butler_ambient.local_ui import snapshot
from butler_ambient.policy import Evidence
from butler_ambient.sensing import Batch
from butler_ambient.service import Service
from butler_ambient.store import Store


class ListeningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name));self.now=100.
        self.inputs=Mock(audio_status={},playback={'state':'idle'})
        self.s=Service(self.store,self.inputs,Mock(),Mock(),clock=lambda:self.now,transport=Mock())
        self.s.voice=Conversation(self.s);self.s.listening=Listening(self.s);self.l=self.s.listening
        self.l.model=Mock();self.s.voice.model=Mock()
        self.store.set('continuous_archive',True);self.store.set('face_wake_dialogue',True)
        self.store.set('voice_enabled',True);self.store.set('voice_profile','baritone')
        self.s.gate.enable(True,self.now)

    def tearDown(self):
        self.s.stop();self.store.close();self.temp.cleanup()

    def process(self,text,owner=False):
        self.l.model.transcribe.return_value=text
        self.l.process(b'\0'*32000,self.l.token,self.now,owner,time.monotonic())

    def test_speech_that_recognizes_to_nothing_is_shown_when_it_lasted_a_second(self):
        self.process('')
        self.assertEqual(self.l.counts.get('asr-empty'),1)
        self.assertEqual(self.l.phase,'waiting')
        self.l.model.transcribe.return_value=''
        self.l.process(b'\0'*16000,self.l.token,self.now,False,time.monotonic())
        self.assertEqual(self.l.counts.get('asr-empty'),1)                      # half a second of noise stays silent

    def test_visible_drafts_reconcile_with_final_identity_and_cannot_dispatch_twice(self):
        self.admit()
        identifier = self.l._begin(self.now-2)
        self.l._draft(identifier, 'Эй Батлер помоги')
        self.assertEqual(self.l.snapshot()['transcript']['state'], 'draft')
        self.assertTrue(self.s.voice.jobs.empty())
        self.l._draft(identifier, 'Эй Батлер помоги почистить диск')
        self.l.utterance = None
        self.l.model.transcribe.return_value = 'Эй, Батлер, помоги почистить диск.'
        args = (b'\0'*64000,self.l.token,self.now,True,time.monotonic(),False,identifier)
        self.l.process(*args)
        self.assertEqual(self.l.snapshot()['transcript']['id'], identifier)
        self.assertEqual(self.l.snapshot()['transcript']['state'], 'final')
        self.assertEqual(self.s.voice.jobs.qsize(),1)
        self.l.process(*args)
        self.assertEqual(self.s.voice.jobs.qsize(),1)

    def test_completed_phrases_keep_fifo_order_and_overflow_is_visible(self):
        with patch.object(Listening,'speech',return_value=True), \
             patch.object(Conversation,'_silence_tail',return_value=1):
            for n in range(4):
                self.now += 1
                self.l.feed(Batch(self.now-1,self.now,[],bytes([n+1,0])*16000))
        self.assertEqual(self.l.jobs.qsize(),3)
        self.assertEqual([self.l.jobs.get_nowait()[0][:2] for _ in range(3)],
                         [b'\1\0',b'\2\0',b'\3\0'])
        self.assertEqual(self.l.counts.get('queue-full'),1)
        self.assertEqual(self.l.snapshot()['transcript']['state'],'queue-full')

    def test_owner_followup_preserves_the_disk_task_still_waiting_for_address_check(self):
        self.admit()
        original = 'Я хочу, чтобы ты помог почистить диск на компьютере.'
        followup = 'И показывай каждый шаг на дисплее, переспрашивай перед удалением.'
        self.process(original,owner=True)
        first = self.s.voice.jobs.get_nowait()
        original_id = self.s.feedback.by_token[first[1]]
        self.s.voice.token = first[1]  # the worker has taken this request
        self.s.voice.state, self.s.voice.phase = 'thinking', 'address'
        self.now += 1
        self.process(followup,owner=True)
        task = self.s.voice.jobs.get_nowait()
        self.assertEqual(task[-1], original+' '+followup)
        self.assertTrue(first[1].is_set())
        self.assertEqual(self.s.feedback.by_token[task[1]],original_id)
        self.assertIn('continuation-joined',[e['event'] for e in self.s.voice.debug_events])
        self.assertFalse(self.s.voice.jobs.qsize())

    def test_sparse_speech_continues_an_admitted_phrase_without_start_threshold(self):
        chunks = [bytes([i, 0])*16000 for i in range(1, 5)]
        with patch.object(Listening, 'speech', side_effect=[True, False, True, False]), \
             patch.object(Listening, 'confirmation_candidate', side_effect=[True, False]), \
             patch.object(Conversation, '_silence_tail', return_value=0):
            for pcm in chunks[:3]:
                self.now += 1
                self.l.feed(Batch(self.now-1, self.now, [], pcm))
                self.assertTrue(self.l.jobs.empty(), 'ongoing speech became a completed turn')
            self.now += 1
            self.l.feed(Batch(self.now-1, self.now, [], chunks[-1]))
        task = self.l.jobs.get_nowait()
        self.assertTrue(task[-1])
        self.assertEqual(task[0], b''.join(chunks))

    def test_long_continuous_speech_is_one_request_with_non_actionable_segments(self):
        self.admit()
        self.l.model.transcribe.side_effect = [
            'Батлер, помоги почистить диск на компьютере',
            'на компьютере, показывай каждый шаг и спрашивай перед удалением.']
        with patch.object(Listening, 'speech', return_value=True), \
             patch.object(Conversation, '_silence_tail', return_value=0):
            for _ in range(20):
                self.now += 1
                self.l.feed(Batch(self.now-1, self.now, [], b'\1\0'*16000))
        identifier = self.l.utterance
        task = self.l.jobs.get_nowait()
        self.assertFalse(task[-1], 'buffer capacity is not end of thought')
        self.l.process(*task)
        self.assertTrue(self.s.voice.jobs.empty())
        self.assertEqual(self.l.snapshot()['transcript']['state'], 'draft')
        self.assertEqual(self.l.utterance, identifier)
        self.assertEqual(len(self.l.audio), 32000)
        with patch.object(Listening, 'speech', return_value=False), \
             patch.object(Listening, 'confirmation_candidate', return_value=False), \
             patch.object(Conversation, '_silence_tail', return_value=1):
            self.now += 1
            self.l.feed(Batch(self.now-1, self.now, [], b'\0'*32000))
        task = list(self.l.jobs.get_nowait()); task[3] = True
        self.l.process(*task)
        self.assertEqual(self.l.snapshot()['transcript']['id'], identifier)
        request = self.s.voice.jobs.get_nowait()
        self.assertEqual(request[-1], 'Батлер, помоги почистить диск на компьютере, показывай каждый шаг и спрашивай перед удалением.')
        self.l.process(*task)
        self.assertTrue(self.s.voice.jobs.empty(), 'same final dispatched twice')

    def test_missing_long_segment_cannot_dispatch_a_truncated_request(self):
        self.admit()
        identifier = self.l._begin(self.now-21)
        self.l.model.transcribe.return_value = ''
        self.l.process(b'\0'*32000, self.l.token, self.now, True, time.monotonic(), False, identifier, False)
        self.l.model.transcribe.return_value = 'и удали всё'
        self.l.process(b'\0'*32000, self.l.token, self.now, True, time.monotonic(), False, identifier, True)
        self.assertTrue(self.s.voice.jobs.empty())
        self.assertEqual(self.l.snapshot()['transcript']['state'], 'unrecognized')

    def test_prefix_noise_cannot_overwrite_the_last_utterance_outcome(self):
        self.admit(); self.process('Батлер, ты здесь?',owner=True)
        row = self.s.feedback.rows[-1]
        previous = dict(row['decision'])
        self.l.mark('speech-candidate','Слышу начало фразы.')
        self.assertEqual(row['decision'],previous)

    def test_pause_and_stale_work_cannot_publish_or_dispatch_a_draft(self):
        identifier = self.l._begin(self.now-10)
        self.l._draft(identifier,'Батлер')
        self.l.process(b'\0'*32000,self.l.token,self.now-9,True,time.monotonic(),False,identifier)
        self.assertEqual(self.l.snapshot()['transcript']['state'],'expired')
        self.l.model.transcribe.assert_not_called()
        self.l.clear()
        self.l._draft(identifier,'late old speech')
        self.assertIsNone(self.l.snapshot()['transcript'])
        self.assertTrue(self.s.voice.jobs.empty())

    def admit(self):
        for _ in range(3):
            self.now+=1
            self.s.dialogue_gate.check(Evidence(self.now,True,True,voice_ids=frozenset({'owner'}),
                camera_ids=frozenset({'owner'}),camera_certain=True,voice_certain=True,speech_detected=True),self.now,True)

    def test_unknown_identity_keeps_complete_transcript_but_cannot_dispatch_private_work(self):
        self.process('Запусти задачу в моём проекте')
        self.assertEqual(self.l.last_heard,'Запусти задачу в моём проекте')
        self.assertEqual(self.l.phase,'blocked')
        self.assertTrue(self.s.voice.jobs.empty())
        self.s.voice.model.generate_json.assert_not_called()
        self.assertFalse(self.s.voice_gate.permits(self.now))

    def test_identity_cancellation_cannot_cancel_passive_asr_or_erase_its_text(self):
        self.process('Сколько времени?')
        self.s.voice.cancel(reason='unverified-presence')
        self.l.model.cancel.assert_not_called()
        self.assertEqual(self.l.snapshot()['last_heard'],'Сколько времени?')
        self.assertIn('reply-blocked',[e['event'] for e in self.l.snapshot()['events']])

    def test_pause_erases_text_and_rejects_old_inflight_result_even_after_resume(self):
        old=self.l.token
        def pause_during_asr(*args,**kwargs):
            self.s.control('enabled',{'value':False})
            self.s.control('enabled',{'value':True})
            return 'Батлер, ты меня слышишь?'
        self.l.model.transcribe.side_effect=pause_during_asr
        self.l.process(b'\0'*32000,old,self.now,True,time.monotonic())
        self.assertTrue(old.is_set());self.assertEqual(self.l.last_heard,'')
        self.assertTrue(self.s.voice.jobs.empty());self.inputs.play.assert_not_called()

    def test_stale_or_gapped_microphone_cannot_supply_a_new_request(self):
        for batch in [Batch(90,91,[],b'\0'*32000),Batch(99,100,[],b'\0'*32000,gaps={'microphone'})]:
            self.l.feed(batch,Evidence(self.now,False,True))
            self.assertTrue(self.l.jobs.empty());self.assertFalse(self.l.audio)
        self.l.process(b'\0'*32000,self.l.token,self.now-9,True,time.monotonic())
        self.l.model.transcribe.assert_not_called()

    def test_complete_asr_is_handed_to_authenticated_dialogue_once_without_second_asr(self):
        self.admit();self.process('Батлер, ты здесь?',owner=True)
        task=self.s.voice.jobs.get_nowait()
        self.assertEqual(task[-1],'Батлер, ты здесь?');self.assertTrue(task[-2])
        self.s.voice.model.generate_json.return_value={'addressed':False,'confidence':.99,'intent':'chat','criteria':'','telegram':False,'reply':''}
        self.s.voice._turn(*task)
        self.s.voice.model.transcribe.assert_not_called()
        self.assertEqual(self.l.model.transcribe.call_count,1)

    def test_authenticated_utterance_yields_background_analysis_before_dialogue(self):
        # Whisper and the Ollama server are shared even when the model names differ (vision vs dialogue).
        self.admit();self.s.voice.model.model='dialogue-fixture';self.s.analyzer.model='vision-fixture'
        self.s.analysis_run_token=threading.Event()
        self.process('Батлер, ты здесь?',owner=True)
        self.s.analyzer.cancel.assert_called_once()
        self.assertTrue(self.s.analysis_run_token.is_set())
        self.assertFalse(self.s.voice.jobs.empty())

    def test_full_sentence_survives_three_checks_and_unverified_first_seconds(self):
        with patch.object(Listening,'speech',side_effect=[True,True,True,False]), \
             patch.object(Conversation,'_silence_tail',return_value=0):
            for i in range(4):
                self.now+=1
                self.l.feed(Batch(self.now-1,self.now,[],bytes([i+1,0])*16000),Evidence(self.now,False,True))
        task=self.l.jobs.get_nowait()
        self.assertEqual(len(task[0]),4*32000)
        self.assertEqual(task[0][:2],b'\x01\x00')
        self.assertFalse(task[3])

    def test_subthreshold_wake_prefix_is_not_overwritten_before_speech_admission(self):
        chunks=[bytes([i,0])*16000 for i in range(1,6)]
        with patch.object(Listening,'speech',side_effect=[False,False,False,True,False]), \
             patch.object(Listening,'confirmation_candidate',side_effect=[False,True,True,False]), \
             patch.object(Conversation,'_silence_tail',return_value=0):
            for pcm in chunks:
                self.now+=1
                self.l.feed(Batch(self.now-1,self.now,[],pcm),Evidence(self.now,False,True))
        task=self.l.jobs.get_nowait()
        self.assertEqual(task[0],b''.join(chunks))
        self.assertEqual(len(task[3].items),5)
        self.assertGreater(self.l.counts.get('speech-candidate',0),0)

    def test_candidate_prefix_is_bounded_and_pause_erases_it(self):
        with patch.object(Listening,'speech',return_value=False),patch.object(Listening,'confirmation_candidate',return_value=True):
            for _ in range(20):
                self.now+=1
                self.l.feed(Batch(self.now-1,self.now,[],b'\1\0'*16000),Evidence(self.now,False,True))
        self.assertEqual(len(self.l.pre_roll),8*32000)
        self.assertEqual(len(self.l.pre_checks),8)
        self.l.clear()
        self.assertFalse(self.l.pre_roll);self.assertFalse(self.l.pre_checks)

    def test_complete_phrase_survives_small_positive_and_negative_pipe_jitter(self):
        # Native PCM is contiguous, but each full second is timestamped when
        # read() finishes. Slightly early completion is not missing speech.
        with patch.object(Listening,'speech',side_effect=[True,True,False]), \
             patch.object(Conversation,'_silence_tail',return_value=0):
            for i,end in enumerate((101.,101.98,103.01)):
                self.now=end
                self.l.feed(Batch(end-1,end,[],bytes([i+1,0])*16000),Evidence(end,False,True))
        task=self.l.jobs.get_nowait()
        self.assertTrue(task[0]==b''.join(bytes([i+1,0])*16000 for i in range(3)),'complete PCM phrase was truncated')
        self.assertEqual(len(task[3].items),3)

    def test_large_gap_duplicate_or_backward_blocks_cannot_extend_an_utterance(self):
        for next_end in (102.4,101.,100.99):
            with self.subTest(next_end=next_end):
                self.l.clear();self.now=101.
                with patch.object(Listening,'speech',side_effect=[True,True]), \
                     patch.object(Conversation,'_silence_tail',return_value=0):
                    self.l.feed(Batch(100.,101.,[],b'\1\0'*16000),Evidence(101.,False,True))
                    self.now=max(101.,next_end)
                    self.l.feed(Batch(next_end-1,next_end,[],b'\2\0'*16000),Evidence(next_end,False,True))
                self.assertNotIn(b'\1\0',bytes(self.l.audio))
                if next_end<=101.:self.assertEqual(len(self.l.audio),0)

    def test_unknown_voice_in_ending_or_prefix_is_not_attributed_to_the_owner(self):
        owner=lambda at:Evidence(at,True,True,voice_ids=frozenset({'owner'}),voice_certain=True,speech_detected=True)
        unknown=lambda at:Evidence(at,True,True,voice_certain=False,speech_detected=True)
        for prefix in (False,True):
            with self.subTest(prefix=prefix):
                self.l.clear();self.now+=2
                with patch.object(Listening,'speech',side_effect=[not prefix,prefix]), \
                     patch.object(Conversation,'_silence_tail',return_value=1 if prefix else 0):
                    self.l.feed(Batch(self.now-1,self.now,[],b'\x10\1'*16000),unknown(self.now) if prefix else owner(self.now))
                    self.now+=1
                    self.l.feed(Batch(self.now-1,self.now,[],b'\x10\1'*16000),owner(self.now) if prefix else unknown(self.now))
                task=self.l.jobs.get_nowait()
                self.assertFalse(task[3],'unknown prefix/tail supplied words attributed to owner')

    def test_verified_silent_ending_preserves_actual_owner_but_cannot_supply_identity_alone(self):
        with patch.object(Listening,'speech',side_effect=[True,False]), \
             patch.object(Conversation,'_silence_tail',return_value=0):
            self.now+=1
            self.l.feed(Batch(self.now-1,self.now,[],b'\x10\1'*16000),
                Evidence(self.now,True,True,voice_ids=frozenset({'owner'}),voice_certain=True,speech_detected=True))
            self.now+=1
            self.l.feed(Batch(self.now-1,self.now,[],b'\0'*32000),Evidence(self.now,True,True,voice_certain=True))
        self.assertTrue(self.l.jobs.get_nowait()[3])
        from butler_ambient.listening import MicrophoneCheck,UtteranceChecks
        quiet=MicrophoneCheck(self.now);quiet.resolve(Evidence(self.now,True,True,voice_certain=True),now=self.now)
        checks=UtteranceChecks();checks.items.append(quiet)
        self.assertFalse(checks,'verified silence cannot itself identify an owner')

    def test_exact_public_hearing_status_needs_no_owner_and_has_no_private_side_effect(self):
        cache=self.store.root/'voice/prepared/baritone';cache.mkdir(parents=True)
        (cache/'disclosure-hearing.wav').write_bytes(b'fixed public phrase')
        self.inputs.play.side_effect=lambda path,key:setattr(self.inputs,'playback',{'state':'done','id':key})
        self.store.set('meeting_mode','quiet')
        with patch.object(Conversation,'ready',new_callable=PropertyMock,return_value=True):
            self.process('Батлер, ты меня слышишь?')
        self.inputs.play.assert_called_once()
        self.assertTrue(str(self.inputs.play.call_args.args[0]).endswith('disclosure-hearing.wav'))
        self.assertEqual(self.l.counts['hearing-replied'],1)
        self.assertTrue(self.s.voice.jobs.empty());self.assertEqual(list(self.s.voice.history),[])
        self.assertFalse(self.s.voice_gate.permits(self.now))

    def test_quoted_hearing_question_is_not_a_public_status_trigger(self):
        self.s.disclosures.play_fixed=Mock()
        self.process('Он спросил: Батлер, ты меня слышишь? Потом ушёл.')
        self.s.disclosures.play_fixed.assert_not_called()
        self.assertEqual(self.l.phase,'blocked')

    def test_asr_sentence_punctuation_does_not_block_exact_public_hearing_status(self):
        self.s.disclosures.play_fixed=Mock(return_value=True)
        self.process('Эй, Батлер! Ты меня слышишь?')
        self.s.disclosures.play_fixed.assert_called_once()
        self.assertEqual(self.l.counts.get('hearing-replied'),1)
        self.assertFalse(self.s.voice_gate.permits(self.now))
        self.assertTrue(self.s.voice.jobs.empty())

    def test_split_vocative_and_hearing_question_get_one_public_reply_without_authority(self):
        self.s.disclosures.play_fixed=Mock(return_value=True)
        self.process('Эй, Батлер!')
        self.now+=2
        self.process('Ты меня слышишь?')
        self.s.disclosures.play_fixed.assert_called_once()
        self.assertEqual(self.l.counts.get('hearing-replied'),1)
        self.assertFalse(self.s.voice_gate.permits(self.now))
        self.assertTrue(self.s.voice.jobs.empty())
        self.s.voice.model.generate_json.assert_not_called()

    def test_split_hearing_attention_cannot_cross_expiry_pause_gaps_or_intervening_speech(self):
        for interruption in ('expired','paused','gap','unrelated','quoted'):
            with self.subTest(interruption=interruption):
                self.l.clear();self.s.disclosures.play_fixed=Mock(return_value=True)
                self.process('«Батлер»' if interruption=='quoted' else 'Батлер')
                if interruption=='expired':self.now+=4.1
                elif interruption=='paused':
                    self.s.control('enabled',{'value':False});self.s.control('enabled',{'value':True})
                elif interruption=='gap':self.l.feed(Batch(self.now-1,self.now,[],b'',gaps=('microphone',)))
                elif interruption=='unrelated':self.process('Это разговор с другим человеком.')
                self.now+=1
                self.process('Ты меня слышишь?')
                self.s.disclosures.play_fixed.assert_not_called()

    def test_public_hearing_status_interrupts_old_private_work_without_changing_pending_review(self):
        cache=self.store.root/'voice/prepared/baritone';cache.mkdir(parents=True)
        (cache/'disclosure-hearing.wav').write_bytes(b'fixed public phrase')
        pending=object();self.s.commands.pending=pending
        self.s.voice.state='thinking';self.s.voice.phase='address'
        old=self.s.voice.token
        self.inputs.play.side_effect=lambda path,key:setattr(self.inputs,'playback',{'state':'done','id':key})
        with patch.object(Conversation,'ready',new_callable=PropertyMock,return_value=True):
            self.process('Батлер, ты меня слышишь?')
        self.inputs.play.assert_called_once()
        self.assertTrue(old.is_set())
        self.assertIs(self.s.commands.pending,pending)
        self.assertFalse(self.s.voice_gate.permits(self.now))
        self.assertTrue(self.s.voice.jobs.empty())

    def test_public_hearing_waits_for_cancelled_output_without_holding_control_lock(self):
        cache=self.store.root/'voice/prepared/baritone';cache.mkdir(parents=True)
        (cache/'disclosure-hearing.wav').write_bytes(b'fixed public phrase')
        self.s.voice.state='speaking';self.s.voice.output_lock.acquire()
        cancel=threading.Event();released=threading.Event()
        self.s.voice.model.cancel.side_effect=cancel.set
        def release_old_output():
            if cancel.wait(1):
                with self.s.lock:
                    released.set();self.s.voice.output_lock.release()
        worker=threading.Thread(target=release_old_output);worker.start()
        self.inputs.play.side_effect=lambda path,key:setattr(self.inputs,'playback',{'state':'done','id':key})
        with patch.object(Conversation,'ready',new_callable=PropertyMock,return_value=True):
            self.process('Батлер, ты меня слышишь?')
        worker.join(1)
        self.assertFalse(worker.is_alive());self.assertTrue(released.is_set())
        self.inputs.play.assert_called_once()

    def test_pause_while_public_hearing_waits_for_output_suppresses_playback(self):
        self.s.voice.state='speaking';self.s.voice.output_lock.acquire()
        cancel=threading.Event();self.s.voice.model.cancel.side_effect=cancel.set
        def pause_then_release():
            if cancel.wait(1):
                self.s.control('enabled',{'value':False})
                self.s.voice.output_lock.release()
        worker=threading.Thread(target=pause_then_release);worker.start()
        with patch.object(Conversation,'ready',new_callable=PropertyMock,return_value=True):
            self.process('Батлер, ты меня слышишь?')
        worker.join(1)
        self.assertFalse(worker.is_alive());self.inputs.play.assert_not_called()

    def test_inflight_vocative_after_input_gap_cannot_recreate_attention(self):
        def interrupted(*args,**kwargs):
            self.l.feed(Batch(self.now-1,self.now,[],b'',gaps=('microphone',)))
            return 'Батлер'
        self.l.model.transcribe.side_effect=interrupted
        self.process('ignored')
        self.l.model.transcribe.side_effect=None
        self.s.disclosures.play_fixed=Mock(return_value=True)
        self.now+=1;self.process('Ты меня слышишь?')
        self.s.disclosures.play_fixed.assert_not_called()

    def test_identity_cancel_does_not_stop_public_output_but_full_pause_does(self):
        self.s.disclosures.active_playback='public'
        self.s.voice.cancel(reason='unverified-presence')
        self.inputs.cancel_playback.assert_not_called()
        self.s.control('enabled',{'value':False})
        self.inputs.cancel_playback.assert_called()

    def test_disable_archive_clears_transcript_and_stale_queue_immediately(self):
        self.process('Что происходит?')
        old=self.l.token
        self.s.control('preferences',{'continuous_archive':False})
        self.assertTrue(old.is_set());self.assertEqual(self.l.snapshot()['last_heard'],'')
        self.assertFalse(self.l.enabled)

    def test_native_local_snapshot_shows_heard_words_and_reason_without_authenticating(self):
        self.process('Какой сегодня день?')
        value=snapshot(self.s)['voice']
        self.assertFalse(value['dialogue_allowed'])
        self.assertEqual(value['last_heard'],'Какой сегодня день?')
        self.assertIn('reply-blocked',[e['event'] for e in value['debug']['events']])
        self.s.control('enabled',{'value':False})
        value=snapshot(self.s)['voice']
        self.assertEqual(value['last_heard'],'');self.assertEqual(value['debug']['events'],[])

    def test_low_disk_space_pauses_recording_but_still_delivers_fresh_microphone_to_listener(self):
        self.s.verifier.verify.return_value=Evidence(self.now,False,True)
        with patch('butler_ambient.archive.shutil.disk_usage',return_value=Mock(free=1024)), \
             patch.object(Listening,'speech',return_value=True), \
             patch.object(Conversation,'_silence_tail',return_value=0):
            self.s.accept_batch(Batch(self.now-1,self.now,[],b'\x10\x01'*16000))
        self.assertEqual(len(self.l.audio),32000)
        self.assertEqual(self.store.execute('SELECT id FROM media'),[])
        self.assertEqual(self.s.archive.storage_error,'low-disk-space')
        self.assertEqual(self.s.archive.snapshot()['sources']['microphone'],'unavailable')
        self.inputs.stop.assert_not_called()

    def test_full_analysis_buffer_blocks_writes_but_keeps_fresh_hearing_and_analysis_permission(self):
        self.store.SPOOL_LIMIT=16
        self.s.verifier.verify.return_value=Evidence(self.now,False,True)
        with patch('butler_ambient.archive.shutil.disk_usage',return_value=Mock(free=10**10)), \
             patch.object(Listening,'speech',return_value=True), \
             patch.object(Conversation,'_silence_tail',return_value=0):
            self.s.accept_batch(Batch(self.now-1,self.now,[],b'\x10\1'*16000))
            self.assertTrue(self.s.archive.permits(self.now),'a full spool must not deadlock its own analysis')
        self.assertEqual(len(self.l.audio),32000)
        self.assertEqual(self.s.archive.storage_error,'analysis-buffer-full')
        self.assertEqual(self.store.execute('SELECT id FROM media'),[])
        self.inputs.stop.assert_not_called()

    def test_archive_write_failure_does_not_abort_hearing_and_recording_recovers(self):
        self.s.verifier.verify.side_effect=lambda b,r:Evidence(b.end,False,True)
        with patch('butler_ambient.archive.shutil.disk_usage',return_value=Mock(free=10**10)), \
             patch.object(Listening,'speech',return_value=True), \
             patch.object(Conversation,'_silence_tail',return_value=0):
            with patch.object(self.store,'media',side_effect=OSError(errno.ENOSPC,'injected full disk')):
                self.s.accept_batch(Batch(self.now-1,self.now,[],b'\x10\x01'*16000))
            self.assertEqual(self.s.archive.storage_error,'write-unavailable')
            self.assertEqual(len(self.l.audio),32000)
            self.now+=1
            self.s.accept_batch(Batch(self.now-1,self.now,[],b'\x10\x01'*16000))
        self.assertIsNone(self.s.archive.storage_error)
        self.assertEqual(len(self.l.audio),64000)
        self.assertEqual(len(self.store.execute('SELECT id FROM media')),1)
        self.inputs.stop.assert_not_called()


if __name__=='__main__':unittest.main()

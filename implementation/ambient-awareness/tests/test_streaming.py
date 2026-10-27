"""Streaming, context lifetime, resident ASR and cancellation contracts."""
import io
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

from butler_ambient.conversation import Conversation, Canceled, silence_requested
from butler_ambient.policy import Evidence
from butler_ambient.sensing import Inputs
from butler_ambient.service import Service
from butler_ambient.store import Store
from butler_ambient.transcriber import ResidentWhisper
from butler_ambient.streaming import object_prefix


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = 1792700000.
        self.inputs = Mock(audio_status={"ready": True, "echo_cancellation": True}, playback={"state": "idle"})
        self.service = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.voice = Conversation(self.service)
        self.service.voice = self.voice
        self.voice.model = Mock()
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        self.service.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .1
            self.service.gate.check(Evidence(self.now, True, True, frozenset({"owner"}), True), self.now)
        self.voice.token = threading.Event()

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def test_single_model_turn_includes_completed_context_and_pause_erases_it(self):
        self.voice.speak = Mock()
        self.voice.model.transcribe.return_value = "Батлер, запомни тему нашего разговора"
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat",
            "criteria": "", "telegram": False, "reply": "Обсудим план рабочего дня."}
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(self.voice.model.generate_json.call_count, 1)
        self.voice.model.generate_json.reset_mock()
        self.voice.model.transcribe.return_value = "А с чего начнём?"
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        prompt = self.voice.model.generate_json.call_args.args[0]
        self.assertIn("Обсудим план рабочего дня.", prompt)
        self.assertEqual(self.voice.model.generate_json.call_count, 1)
        self.assertEqual(len(self.voice.history), 2)
        self.assertNotIn("рабочего дня", json.dumps(self.voice.diagnostics(), ensure_ascii=False))
        self.assertEqual(self.store.execute("SELECT * FROM observations"), [])
        self.service.control("enabled", {"value": False})
        self.assertFalse(self.voice.history)
        self.assertFalse(self.voice.ambient_context)

    def test_interrupted_reply_is_not_claimed_as_completed_dialogue(self):
        self.voice.speak = Mock(side_effect=Canceled())
        self.voice.model.transcribe.return_value = "Батлер, расскажи"
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat",
            "criteria": "", "telegram": False, "reply": "Непроизнесённый ответ."}
        with self.assertRaises(Canceled):
            self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertFalse(self.voice.history)

    def test_first_clause_is_spoken_before_the_model_finishes_its_reply(self):
        started = threading.Event()
        first = "Начнём с самой важной задачи на сегодня. "
        header = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False}
        def plan(*args, **kwargs):
            kwargs["on_partial"]({**header, "reply": first})
            self.assertTrue(started.wait(1), "TTS waited for complete model response")
            return {**header, "reply": first + "Оставим время на отдых."}
        self.voice.model.generate_json.side_effect = plan
        self.voice.model.transcribe.return_value = "Батлер, как спланировать день?"
        self.voice.speak = Mock(side_effect=lambda *args, **kwargs: started.set())
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(self.voice.speak.call_count, 2)
        self.assertIn("Оставим", self.voice.last_reply)
        self.assertEqual(len(self.voice.history), 1)

    def test_streamed_reply_is_one_feed_line_and_speech_is_cut_at_sentences_not_commas(self):
        header = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False}
        first = "Я пока не знаю деталей ваших проектов, так как у меня нет доступа к ним. "
        def plan(*args, **kwargs):
            kwargs["on_partial"]({**header, "reply": "Я пока не знаю деталей ваших проектов, так как у меня "})
            kwargs["on_partial"]({**header, "reply": first})
            return {**header, "reply": first + "Расскажите, чем занимаетесь."}
        self.voice.model.generate_json.side_effect = plan
        self.voice.model.transcribe.return_value = "Батлер, расскажи о моих проектах"
        self.voice.speak = Mock()
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        spoken = [call.args[0] for call in self.voice.speak.call_args_list]
        self.assertEqual(spoken, [first.strip(), "Расскажите, чем занимаетесь."])     # whole sentences, never the clause before a comma
        replies = [e for e in self.voice.debug_events if e["event"] == "reply"]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0]["detail"].endswith("занимаетесь."))

    def test_a_new_utterance_stops_background_analysis_whatever_model_it_runs_on(self):
        self.service.analyzer.model, self.voice.model.model = "vision-fixture", "dialogue-fixture"
        self.service.analysis_run_token = threading.Event()
        self.service.yield_analysis()
        self.assertTrue(self.service.analysis_run_token.is_set())
        self.service.analyzer.cancel.assert_called_once()

    def test_alone_with_a_quiet_desktop_the_hearing_check_and_a_plain_question_need_no_name(self):
        from butler_ambient.dialogue_state import Scene
        quiet = {"foreground_app": "Finder", "foreground_bundle": "com.apple.finder", "call_app": False, "audio_playing": False, "known": True}
        self.voice.speak = Mock()
        self.voice.utterance_scene = Scene(available=True, faces=1, owner_visible=True, owner_facing=True)
        with patch("butler_ambient.conversation.desktop_state", return_value=quiet):
            self.voice.model.transcribe.return_value = "Эй, ты меня слышишь?"
            self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
            self.assertEqual(self.voice.speak.call_count, 1)                 # hearing acknowledgement, no name needed
            self.voice.model.generate_json.assert_not_called()
            self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .7, "intent": "chat",
                "criteria": "", "telegram": False, "reply": "Сегодня суббота."}
            self.voice.model.transcribe.return_value = "Какой сегодня день недели?"
            self.voice.last_direct = float("-inf")
            self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
            self.assertEqual(self.voice.speak.call_count, 2)                 # 0.7 is enough when alone and quiet
            prompt = self.voice.model.generate_json.call_args.args[0]
            self.assertIn('"owner_alone_quiet_desktop": true', prompt)
            self.assertIn('"foreground_app": "Finder"', prompt)
            # In the dark (no scene) or looking away (a phone call), the same question at 0.7 is not for Butler.
            self.voice.utterance_scene = Scene()
            self.voice.last_direct = float("-inf")
            self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
            self.assertEqual(self.voice.speak.call_count, 2)
            self.voice.utterance_scene = Scene(available=True, faces=1, owner_visible=True, owner_facing=False)
            self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
            self.assertEqual(self.voice.speak.call_count, 2)

    def test_any_name_mention_overrides_an_unsure_judge_even_after_company(self):
        self.voice.speak = Mock()
        self.voice.model.generate_json.return_value = {"addressed": False, "confidence": .6, "intent": "chat", "criteria": "", "telegram": False,
                                                       "reply": "С чем помочь?"}
        self.voice.model.transcribe.return_value = "Ты можешь мне помочь с Батлером, моим помощником?"
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(self.voice.speak.call_count, 1)
        self.service.attention.observe(Evidence(self.now, True, True, voice_ids=frozenset({"guest"}), voice_certain=True), self.now)
        self.now += 31                                                       # busy is over, but company was heard a moment ago
        for _ in range(3):
            self.now += .1
            self.service.gate.check(Evidence(self.now, True, True, frozenset({"owner"}), True), self.now)
        self.voice.last_direct = float("-inf")
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(self.voice.speak.call_count, 2)                     # Q-006: name mentions override an unsure negative with company too

    def test_a_call_app_or_playing_audio_restores_the_ordinary_rule(self):
        self.voice.speak = Mock()
        busy = {"foreground_app": "zoom.us", "foreground_bundle": "us.zoom.xos", "call_app": True, "audio_playing": False, "known": True}
        with patch("butler_ambient.conversation.desktop_state", return_value=busy):
            self.voice.model.transcribe.return_value = "Эй, ты меня слышишь?"
            self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .7, "intent": "chat",
                "criteria": "", "telegram": False, "reply": "Да."}
            self.voice.last_direct = float("-inf")
            self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_not_called()                                 # name, follow-up or 0.9 needed in a call

    def test_unnamed_question_and_followup_after_twenty_seconds_can_reply(self):
        self.voice.speak = Mock()
        self.voice.model.generate_json.return_value = {"addressed": True, "confidence": .99, "intent": "chat",
            "criteria": "", "telegram": False, "reply": "Обсудим план рабочего дня."}
        self.voice.model.transcribe.return_value = "Как лучше организовать рабочий день?"
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.last_direct = self.now - 120
        self.voice.model.transcribe.return_value = "А с чего начать?"
        self.voice.model.generate_json.return_value["confidence"] = .92
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertEqual(self.voice.speak.call_count, 2)

    def test_partial_reply_never_speaks_before_complete_confident_address_metadata(self):
        self.voice.speak = Mock()
        self.voice.model.transcribe.return_value = "Это разговор с другим человеком"
        def plan(*args, **kwargs):
            kwargs["on_partial"]({"reply": "Этот текст пока нельзя произносить. "})
            kwargs["on_partial"]({"addressed": False, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False,
                                  "reply": "Этот текст нельзя произносить. "})
            return {"addressed": False, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False, "reply": ""}
        self.voice.model.generate_json.side_effect = plan
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
        self.voice.speak.assert_not_called()

    def test_explicit_request_for_silence_cannot_be_overridden_by_model(self):
        for text in ("Почему ты не говоришь?", "Он молчит", "Почему ты не отвечаешь?"):
            self.assertFalse(silence_requested(text))
        for text in ("Не отвечай, читаю новость", "Это текст из телевизора, отвечать не нужно", "Помолчи, пожалуйста"):
            self.assertTrue(silence_requested(text))
            self.voice.model.transcribe.return_value = text
            self.voice.speak = Mock()
            self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True)
            self.voice.speak.assert_not_called()
        self.voice.model.generate_json.assert_not_called()
        self.assertTrue(self.service.gate.enabled)

    def test_actual_local_model_handles_unnamed_question_followup_and_unrelated_speech(self):
        from butler_ambient.analysis import LocalAnalyzer,DEFAULT_DIALOGUE_MODEL
        model = LocalAnalyzer(DEFAULT_DIALOGUE_MODEL)
        try: model.check_local()
        except RuntimeError: self.skipTest("installed local dialogue model required")
        self.voice.model = model
        self.voice.speak = Mock()
        model.transcribe = Mock(return_value="Как лучше спланировать рабочий день?")
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True, source="synthetic-check")
        self.assertGreater(self.voice.speak.call_count, 0, "real model rejected unnamed sole-owner question")
        self.assertEqual(self.voice.diagnostics()["turns"][-1].get("intent"), "chat")
        self.voice.speak.reset_mock()
        self.voice.last_direct = self.now - 120
        model.transcribe.return_value = "А с чего начать?"
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True, source="synthetic-check")
        self.assertGreater(self.voice.speak.call_count, 0, "real model lost follow-up context")
        self.assertEqual(self.voice.diagnostics()["turns"][-1].get("intent"), "chat")
        self.voice.cancel()
        self.voice.token = threading.Event()
        self.voice.speak.reset_mock()
        model.transcribe.return_value = "Это разговор с другим человеком"
        self.voice._turn(b"\0"*32000, self.voice.token, self.service.voice_gate.generation, True, source="synthetic-check")
        self.voice.speak.assert_not_called()

    def test_actual_local_model_accepts_a_followup_from_the_shared_asr(self):
        from butler_ambient.analysis import LocalAnalyzer, DEFAULT_DIALOGUE_MODEL
        model = LocalAnalyzer(DEFAULT_DIALOGUE_MODEL, dialogue=True)
        try: model.check_local()
        except RuntimeError: self.skipTest('installed local dialogue model required')
        self.voice.model = model
        self.voice.state, self.voice.phase = 'speaking', 'playback'
        self.voice.last_spoken = 'Начните с подготовки важного отчёта, затем разберите почту.'
        self.voice.last_direct = self.now-5
        self.voice.history.append({'at': self.now-5, 'owner': 'Что сначала сделать сегодня?', 'butler': self.voice.last_spoken})
        self.assertTrue(self.voice.observe_input('А почему именно с этой задачи?', 'followup', owner=True))
        self.assertEqual(self.voice.diagnostics()['counts'].get('barge_in_accepted'), 1)
        self.assertTrue(self.voice.jobs.empty(), 'a partial recognition must not dispatch')

    def streaming_fixture(self):
        output = self.store.root / "voice/output"
        output.mkdir(parents=True)
        paths = [output / (f"{i:032x}" + ".wav") for i in range(3)]
        for path in paths: path.write_bytes(b"synthetic")
        worker = Mock(stdin=io.BytesIO())
        worker.poll.return_value = None
        self.voice.worker = worker
        self.voice._ensure_worker = Mock(return_value=worker)
        self.voice._line = Mock(side_effect=[*({"id": "request", "sequence": i, "file": str(path)}
                                           for i, path in enumerate(paths)), {"id": "request", "done": True}])
        self.inputs.begin_stream.side_effect = lambda key: setattr(self.inputs, "playback", {"id": key, "state": "queued", "completed_chunks": 0})
        def append(path, key, sequence):
            self.inputs.playback = {"id": key, "state": "started", "completed_chunks": sequence}
        self.inputs.append_stream.side_effect = append
        self.inputs.finish_stream.side_effect = lambda key: self.inputs.playback.update(state="done")
        self.voice.turn_started = time.monotonic()
        return worker, paths

    def test_audio_starts_before_synthesis_finishes_and_uses_one_bounded_stream(self):
        worker, paths = self.streaming_fixture()
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True), \
             patch("butler_ambient.conversation.uuid.uuid4", return_value=Mock(hex="request")):
            self.voice.speak("Проверочный ответ.", self.voice.token, self.service.voice_gate.generation)
        self.inputs.play.assert_not_called()
        self.inputs.begin_stream.assert_called_once()
        self.assertEqual(self.inputs.append_stream.call_count, 3)
        self.inputs.finish_stream.assert_called_once()
        self.assertLessEqual(self.voice.timings["first_playback"], self.voice.timings["synthesis_done"])
        self.assertTrue(all(not path.exists() for path in paths))
        commands = [json.loads(line) for line in worker.stdin.getvalue().splitlines()]
        self.assertTrue(commands[0]["stream"])
        self.assertEqual(sum(c.get("command") == "credit" for c in commands), 3)
        worker.terminate.assert_not_called()

    def test_real_playback_completion_preserves_committed_reply_gap(self):
        self.streaming_fixture()
        self.voice.turn_active = True
        self.voice.state, self.voice.phase = "thinking", "reply"
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True), \
             patch("butler_ambient.conversation.uuid.uuid4", return_value=Mock(hex="request")):
            self.voice.speak("Проверяю вашу просьбу.", self.voice.token, self.service.voice_gate.generation)
        self.assertEqual((self.voice.state,self.voice.phase),("thinking","reply"))
        self.assertTrue(self.voice.reply_in_progress)
        self.assertTrue(self.voice.turn_committed)
        self.assertEqual(self.voice.diagnostics()['delivery']['state'],'completed')
        self.assertEqual(self.voice.diagnostics()['delivery']['completed_characters'],len('Проверяю вашу просьбу.'))
        generation=self.service.voice_gate.generation
        self.service.gate.invalidate("capture-gap")
        self.voice._permitted(self.voice.token,generation)
        self.assertFalse(self.voice.token.is_set())

    def test_agent_wait_does_not_consume_the_playback_deadline(self):
        self.streaming_fixture()
        self.voice.turn_started = time.monotonic()-200
        def append(path, key, sequence):
            self.inputs.playback = {'id': key, 'state': 'started', 'completed_chunks': 0}
        self.inputs.append_stream.side_effect = append
        self.voice.ended = Mock(is_set=Mock(return_value=False))
        self.voice.ended.wait.side_effect = lambda seconds: self.inputs.playback.update(completed_chunks=1)
        with patch.object(Conversation, 'ready', new_callable=PropertyMock, return_value=True), \
             patch('butler_ambient.conversation.uuid.uuid4', return_value=Mock(hex='request')):
            self.voice.speak('Ответ после долгой проверки.', self.voice.token, self.service.voice_gate.generation)
        self.assertEqual(self.inputs.append_stream.call_count, 3)
        self.assertEqual(self.voice.delivery['state'], 'completed')

    def test_pause_between_chunk_arrival_and_append_never_plays_stale_audio(self):
        worker, paths = self.streaming_fixture()
        def arrive(*args):
            self.service.control("enabled", {"value": False})
            return {"id": "request", "sequence": 0, "file": str(paths[0])}
        self.voice._line.side_effect = arrive
        with patch.object(Conversation, "ready", new_callable=PropertyMock, return_value=True), \
             patch("butler_ambient.conversation.uuid.uuid4", return_value=Mock(hex="request")):
            with self.assertRaises(Canceled):
                self.voice.speak("Проверочный ответ.", self.voice.token, self.service.voice_gate.generation)
        self.inputs.append_stream.assert_not_called()
        self.assertFalse(paths[0].exists())
        worker.terminate.assert_not_called()
        self.assertIn('"command": "cancel"', worker.stdin.getvalue().decode())

    def test_native_stream_protocol_preserves_progress_and_rejects_unready_output(self):
        source = Inputs()
        with self.assertRaises(RuntimeError): source.begin_stream("synthetic")
        source.audio_status.update(ready=True, echo_cancellation=True)
        source._audio_command = Mock()
        source.begin_stream("synthetic")
        source.append_stream(Path("/synthetic.wav"), "synthetic", 0)
        process = Mock(stderr=io.BytesIO(b'BUTLER_AUDIO {"playback":"started","id":"synthetic"}\n'
            b'BUTLER_AUDIO {"playback":"progress","id":"synthetic","completed_chunks":1}\n'))
        source._errors("microphone", process)
        self.assertEqual(source.playback["completed_chunks"], 1)
        self.assertEqual(source.playback["state"], "started")
        source.finish_stream("synthetic")
        self.assertEqual([c.args[0]["command"] for c in source._audio_command.call_args_list], ["begin", "append", "finish"])


class ResidentASRTests(unittest.TestCase):
    def test_client_cancellation_disconnects_decode_without_unloading_weights(self):
        admitted = threading.Event()
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                admitted.set()
                release.wait(3)
                self.send_response(200); self.end_headers()
                try: self.wfile.write(b'{"text":"synthetic"}')
                except OSError: pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        worker = ResidentWhisper("synthetic.bin")
        worker.port = server.server_address[1]
        worker.process = Mock()
        worker.process.poll.return_value = None
        errors = []
        def recognize():
            try: worker.transcribe(b"RIFFsynthetic")
            except RuntimeError: errors.append(True)
        thread = threading.Thread(target=recognize)
        try:
            thread.start()
            self.assertTrue(admitted.wait(2))
            worker.cancel()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            worker.process.terminate.assert_not_called()
        finally:
            release.set(); thread.join(3); worker.stop(); server.shutdown(); server.server_close()

    @unittest.skipUnless(shutil.which("whisper-server") and (Path.home()/".local/share/butler-ambient/models/ggml-large-v3-turbo-q5_0.bin").is_file(), "local Whisper required")
    def test_real_resident_model_reuses_pid_and_recognizes_russian_fixtures(self):
        root = Path(__file__).resolve().parents[1]
        worker = ResidentWhisper(str(Path.home()/".local/share/butler-ambient/models/ggml-large-v3-turbo-q5_0.bin"))
        try:
            worker.warm()
            pid = worker.process.pid
            wake = worker.transcribe((root/"butler_ambient/assets/voice-latency-check.wav").read_bytes()).lower()
            self.assertIn("батлер", wake)
            from butler_ambient.conversation import hearing_check
            hearing=worker.transcribe((root/'butler_ambient/assets/voice-check.wav').read_bytes())
            self.assertTrue(hearing_check(hearing,['Батлер','Бата','Butler']))
            count = worker.transcribe((root/"tests/fixtures/counting-russian.wav").read_bytes()).lower()
            for word in ("раз", "два", "три", "четыре", "пять", "зайчик", "погулять"): self.assertIn(word, count)
            self.assertEqual(worker.process.pid, pid)
        finally: worker.stop()

    def test_dialogue_cancellation_leaves_passive_request_alive_on_same_resident(self):
        admitted={key:threading.Event() for key in ('passive','dialogue')}
        release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                key='passive' if b'RIFFpassive' in body else 'dialogue'
                admitted[key].set();release.wait(3)
                try:
                    self.send_response(200);self.end_headers();self.wfile.write(b'{"text":"synthetic"}')
                except OSError:pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        worker=ResidentWhisper('synthetic.bin');worker.port=server.server_address[1]
        worker.process=Mock();worker.process.poll.return_value=None
        clients={key:worker.client() for key in admitted};results={}
        def run(key):
            try:results[key]=clients[key].transcribe(('RIFF'+key).encode())
            except RuntimeError:results[key]='cancelled'
        threads={key:threading.Thread(target=run,args=(key,)) for key in admitted}
        try:
            for thread in threads.values():thread.start()
            for event in admitted.values():self.assertTrue(event.wait(2))
            clients['dialogue'].cancel();threads['dialogue'].join(1)
            self.assertEqual(results.get('dialogue'),'cancelled')
            self.assertTrue(threads['passive'].is_alive())
            release.set();threads['passive'].join(2)
            self.assertEqual(results['passive'],'synthetic')
            worker.process.terminate.assert_not_called()
        finally:
            release.set();worker.stop()
            for thread in threads.values():thread.join(3)
            server.shutdown();server.server_close()


class PartialJSONTests(unittest.TestCase):
    def test_every_split_preserves_complete_metadata_and_handles_escaped_russian(self):
        value = {"addressed": True, "confidence": .99, "intent": "chat", "criteria": "", "telegram": False,
                 "reply": 'Начнём с задачи «план». Затем скажем "готово".'}
        data = json.dumps(value, ensure_ascii=True)
        for end in range(len(data)+1):
            prefix = object_prefix(data[:end])
            if "reply" in prefix:
                self.assertEqual(prefix["addressed"], True)
                self.assertEqual(prefix["confidence"], .99)
                self.assertTrue(value["reply"].startswith(prefix["reply"]))
        self.assertEqual(object_prefix(data), value)

    def test_unfinished_numbers_and_duplicate_address_fields_are_not_trusted(self):
        self.assertNotIn("confidence", object_prefix('{"addressed":true,"confidence":0.9'))
        with self.assertRaises(ValueError): object_prefix('{"addressed":true,"addressed":false,')


if __name__ == "__main__": unittest.main()

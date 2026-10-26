"""Offline runtime/API checks. All images/audio are synthetic; no device access."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient.mcp import rpc
from butler_ambient.sensing import Batch, Inputs
from butler_ambient.analysis import LocalAnalyzer
from butler_ambient.conversation import explicit_address


ROOT = Path(__file__).resolve().parents[1]
HAS_SENSING = all(importlib.util.find_spec(key) for key in ("cv2", "numpy", "resemblyzer", "webrtcvad"))
HAS_MODELS = all((ROOT / "build/test-models" / name).is_file() for name in ("yunet.onnx", "sface.onnx"))
WHISPER_MODEL = Path(os.environ.get("BUTLER_TEST_WHISPER_MODEL", str(Path.home() / ".local/share/butler-ambient/models/ggml-base.bin")))
TURBO_MODEL = Path.home() / ".local/share/butler-ambient/models/ggml-large-v3-turbo-q5_0.bin"


class RuntimeTests(unittest.TestCase):
    def test_stalled_camera_retries_without_reopening_audio_or_accepting_old_frames(self):
        source = Inputs(camera="2", camera_size="1280x720")
        old, audio, new = Mock(), Mock(), Mock()
        old.poll.return_value = None
        source.processes = [old, audio]
        source.camera_started_at = 80
        source.frames.put((90, b"old-frame"))
        with patch("butler_ambient.sensing.time.time", return_value=100), \
             patch("butler_ambient.sensing.subprocess.Popen", return_value=new) as popen, \
             patch("butler_ambient.sensing.threading.Thread"):
            self.assertTrue(source.recover_camera())
            old.terminate.assert_called_once()
            self.assertIs(source.processes[0], new)
            self.assertIs(source.processes[1], audio)
            audio.terminate.assert_not_called()
            self.assertTrue(source.frames.empty())
            self.assertIsNone(source.last_camera_at)
            self.assertIn("2:none", popen.call_args.args[0])
            self.assertFalse(source.recover_camera())
            popen.assert_called_once()
        source.stopped.set()
        with patch("butler_ambient.sensing.time.time", return_value=200):
            self.assertFalse(source.recover_camera())

    def test_fresh_camera_does_not_restart(self):
        source = Inputs()
        source.processes = [Mock(), Mock()]
        source.camera_started_at, source.last_camera_at = 90, 99
        with patch("butler_ambient.sensing.time.time", return_value=100):
            self.assertFalse(source.recover_camera())
        source.processes[0].terminate.assert_not_called()

    def test_native_video_uses_exact_owner_selected_camera_name(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root)/"butler-audio"
            camera = Path(root)/"butler-camera"
            camera.touch()
            source = Inputs(camera="2", audio_helper=audio, voice_root=root, camera_size="1280x720")
            with patch("butler_ambient.local_settings.devices", return_value={"camera":[{"id":"0","name":"Other"},{"id":"2","name":"Selected USB"}]}), \
                 patch("butler_ambient.sensing.subprocess.Popen") as popen, patch("butler_ambient.sensing.threading.Thread"):
                source.start()
            self.assertEqual(popen.call_args_list[0].args[0], [str(camera), "Selected USB", "1280x720"])

    def test_failed_native_audio_is_retried_without_restarting_camera(self):
        source = Inputs(audio_helper="/local/butler-audio", voice_root="/local/voice")
        camera, old, new = Mock(), Mock(), Mock()
        source.processes = [camera, old]
        source.audio_started_at = 50
        source.audio_status = {"ready":False,"echo_cancellation":False}
        with patch("butler_ambient.sensing.time.time", return_value=100), \
             patch("butler_ambient.sensing.subprocess.Popen", return_value=new) as popen, \
             patch("butler_ambient.sensing.threading.Thread"):
            self.assertTrue(source.recover_audio())
            self.assertIs(source.processes[1], new)
            camera.terminate.assert_not_called()
            self.assertFalse(source.recover_audio())
            popen.assert_called_once()

    def test_native_audio_exit_is_recorded_and_restarted_before_the_aec_less_fallback(self):
        # 2026-10-25: the helper died unnoticed and the voice stayed «unavailable» until a service restart.
        source = Inputs(audio_helper="/local/butler-audio", voice_root="/local/voice")
        camera, old, new = Mock(), Mock(), Mock()
        old.wait.return_value = 1
        source.processes = [camera, old]
        source.audio_started_at = 50
        source.errors["microphone"] = "engine stopped: device gone"
        with patch("butler_ambient.sensing.time.time", return_value=100), \
             patch("butler_ambient.sensing.subprocess.Popen", return_value=new) as popen, \
             patch("butler_ambient.sensing.threading.Thread"):
            source._native_audio_exited(old)
            self.assertIs(source.processes[1], new)                                   # restarted at once, not after 20 s
            popen.assert_called_once()
            self.assertEqual(source.audio_exits[-1]["code"], 1)
            self.assertIn("device gone", source.audio_exits[-1]["stderr_tail"])
            self.assertEqual(source.audio_status["failure"], "recovering-native-audio")
            camera.terminate.assert_not_called()
            for _ in range(3): source.audio_exits.append({"at": 100, "code": 1, "stderr_tail": ""})
            with patch.object(source, "_fallback_audio") as fallback:
                source._native_audio_exited(new)                                       # a fourth exit in two minutes: FFmpeg fallback
                fallback.assert_called_once()

    def test_slow_video_recovery_does_not_block_capture_checks(self):
        import threading
        source = Inputs()
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(1)
        with patch.object(source, "recover_camera", side_effect=slow), patch.object(source, "recover_audio"):
            started = time.monotonic()
            source.recover_sources()
            self.assertLess(time.monotonic()-started, .2)
            self.assertTrue(entered.wait(1))
            source.recover_sources()
            release.set()
            source.recovery_thread.join(2)

    def test_hardware_mute_or_zero_gain_cannot_supply_participant_audio_evidence(self):
        for health,available in [({'input_muted':True,'input_volume':.5},False),
                                 ({'input_muted':False,'input_volume':0},False),
                                 ({'input_verified':False},False),
                                 ({'input_muted':False,'input_volume':.5},True),({},True)]:
            source=Inputs();source.audio_status.update(health)
            now=time.time();source.audio.put((now,b'\0'*32000));source.frames.put((now,b'synthetic-frame'))
            batch=source.batch()
            self.assertEqual(len(batch.pcm)==32000,available)
            self.assertEqual(batch.frames,[b'synthetic-frame'])

    def test_native_audio_health_updates_are_metadata_only(self):
        import io
        source=Inputs()
        lines=[{'state':'ready','echo_cancellation':True,'formats':{'input_rate':16000,'input_channels':1},
                'input_verified':True,'actual_microphone':'Actual USB','voice_processing_enabled':False},
               {'state':'health','input_muted':True,'input_volume':.25},
               {'state':'health','input_muted':False,'input_volume':.5,
                'system_default_microphone':'Headset','microphone_is_system_default':False,
                'system_default_output':'Headset','system_output_muted':False,'system_output_volume':.4}]
        proc=Mock(stderr=io.BytesIO(b''.join(b'BUTLER_AUDIO '+json.dumps(value).encode()+b'\n' for value in lines)))
        source._errors('microphone',proc)
        self.assertFalse(source.audio_status['input_muted'])
        self.assertEqual(source.audio_status['input_volume'],.5)
        self.assertEqual(source.audio_status['formats']['input_rate'],16000)
        self.assertTrue(source.audio_status['input_verified'])
        self.assertEqual(source.audio_status['actual_microphone'],'Actual USB')
        self.assertEqual(source.audio_status['system_default_microphone'],'Headset')
        self.assertFalse(source.audio_status['microphone_is_system_default'])
        self.assertEqual(source.audio_status['system_default_output'],'Headset')
        self.assertFalse(source.audio_status['system_output_muted'])
        self.assertEqual(source.audio_status['system_output_volume'],.4)
        self.assertFalse(source.audio_status['voice_processing_enabled'])
        self.assertEqual(source.errors['microphone'],'')

    @unittest.skipUnless(shutil.which("whisper-cli") and TURBO_MODEL.is_file(), "Local Whisper Turbo model required")
    def test_real_russian_turbo_recognizes_wake_and_counting_without_transcript_files(self):
        analyzer = LocalAnalyzer(whisper_model=str(TURBO_MODEL),whisper_language="ru")
        wake = analyzer.transcribe((ROOT/"butler_ambient/assets/voice-check.wav").read_bytes())
        self.assertTrue(explicit_address(wake,["Батлер","Бата","Butler"]))
        count = analyzer.transcribe((ROOT/"tests/fixtures/counting-russian.wav").read_bytes()).lower()
        for word in ("раз", "два", "три", "четыре", "пять", "зайчик", "погулять"):
            self.assertIn(word,count)

    def test_speech_language_and_cli_selection_reach_actual_argv(self):
        process = Mock()
        process.communicate.return_value = (b"synthetic", b"")
        process.returncode = 0
        with patch("butler_ambient.analysis.subprocess.Popen",return_value=process) as spawn:
            LocalAnalyzer(whisper_model="local.bin",whisper_language="ru",whisper_cli="/local/whisper-cli").transcribe(b"synthetic")
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[0],"/local/whisper-cli")
        self.assertEqual(argv[argv.index("-l")+1],"ru")

    @unittest.skipUnless(shutil.which("whisper-cli") and WHISPER_MODEL.is_file(), "Local multilingual Whisper model required")
    def test_real_whisper_stdin_returns_synthetic_wake_words_without_transcript_files(self):
        original = subprocess.Popen
        with tempfile.TemporaryDirectory() as folder:
            def spawn(*args, **kwargs):
                return original(*args, cwd=folder, **kwargs)
            with patch("butler_ambient.analysis.subprocess.Popen", side_effect=spawn):
                text = LocalAnalyzer(whisper_model=str(WHISPER_MODEL)).transcribe((ROOT / "tests/fixtures/wake-bata.wav").read_bytes()).strip()
            self.assertTrue(explicit_address(text, ["Бата", "Батлер", "Butler"]), "Actual stdin ASR must return a recognizable wake address")
            self.assertEqual(list(Path(folder).iterdir()), [], "Transcripts must remain in memory/stdout")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for the capture transport smoke test")
    def test_real_ffmpeg_streams_produce_bounded_in_memory_video_and_audio(self):
        original = subprocess.Popen
        commands = [
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=5", "-c:v", "mjpeg", "-f", "image2pipe", "pipe:1"],
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-ac", "1", "-f", "s16le", "pipe:1"],
        ]
        def spawn(*args, **kwargs):
            return original(commands.pop(0), **kwargs)
        inputs = Inputs()
        try:
            with patch("butler_ambient.sensing.subprocess.Popen", side_effect=spawn):
                inputs.start()
            sample = inputs.batch()
            self.assertEqual(len(sample.pcm), 32000)
            self.assertTrue(sample.frames)
            self.assertTrue(all(image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9") for image in sample.frames))
            self.assertLessEqual(inputs.frames.qsize(), 15)
            self.assertLessEqual(inputs.audio.qsize(), 2)
            self.assertLess(time.time() - inputs.input_signal["at"], 2.5)
            self.assertGreater(inputs.input_signal["rms_dbfs"], -70)
            self.assertLess(time.time() - inputs.last_camera_at, 2.5)
            self.assertTrue(sample.wav().startswith(b"RIFF"))
        finally:
            inputs.stop()
        self.assertEqual(inputs.processes, [])
    def test_temporary_service_process_starts_paused_without_opening_devices(self):
        with tempfile.TemporaryDirectory() as state:
            process = subprocess.Popen([sys.executable, "-m", "butler_ambient.cli", "--state", state, "run"],
                                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while not (Path(state) / "ambient.sock").exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("temporary service exited before opening its local control socket")
                    time.sleep(.05)
                status = rpc(Path(state), "status")
                self.assertFalse(status["enabled"])
                self.assertFalse(status["recording_allowed"])
                self.assertTrue(all(value == "paused" for value in status["sources"].values()))
                self.assertEqual(list((Path(state) / "raw").iterdir()), [])
            finally:
                process.terminate()
                process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)


@unittest.skipUnless(HAS_SENSING and HAS_MODELS, "optional sensing dependencies and OpenCV test models are required")
class LocalVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        import cv2
        from butler_ambient.sensing import Verifier
        cls.np = np
        cls.verifier = Verifier(ROOT / "build/test-models/yunet.onnx", ROOT / "build/test-models/sface.onnx")
        _, frame = cv2.imencode(".jpg", np.zeros((480, 640, 3), dtype=np.uint8))
        cls.frame = frame.tobytes()
    def test_real_local_models_reject_blank_and_unreadable_camera_data(self):
        now = time.time()
        sample = Batch(now - 1, now, [self.frame], b"\x00" * 32000)
        refs = {"owner": {"face": [1.0] * 128, "voice": [1.0] * 256}}
        self.assertFalse(self.verifier.verify(sample, refs).certain)
        sample.frames = [b"not an image"]
        self.assertFalse(self.verifier.verify(sample, refs).certain)
    def test_real_vad_recognizes_silence_without_inventing_speaker(self):
        self.assertEqual(self.verifier._voices(b"\x00" * 32000), [])
    def test_voice_identity_is_evaluated_when_camera_is_missing_or_invalid(self):
        one = self.np.array([1., 0.])
        refs = {"owner": {"face": one, "voice": one}}
        now = time.time()
        with patch.object(self.verifier, "_voices", return_value=[one]) as voices:
            evidence = self.verifier.verify(Batch(now-1, now, [], b"\0"*32000), refs)
            self.assertTrue(evidence.voice_certain)
            self.assertEqual(evidence.voice_ids, frozenset({"owner"}))
            self.assertFalse(evidence.certain)
            voices.assert_called_once()
        with patch.object(self.verifier, "_voices", return_value=[one]), patch.object(self.verifier, "_faces", side_effect=ValueError("invalid camera frame")):
            evidence = self.verifier.verify(Batch(now-1, now, [b"invalid"], b"\0"*32000), refs)
            self.assertTrue(evidence.voice_certain)
            self.assertFalse(evidence.certain)
    def test_enrollment_voice_uses_speech_after_initial_silence(self):
        pcm = b"\x00" * 64000 + b"\x01\x00" * 4800
        with patch.object(self.verifier, "vad") as vad, patch.object(self.verifier, "voice") as encoder:
            vad.is_speech.side_effect = lambda frame, rate: any(frame)
            encoder.embed_utterance.return_value = self.np.ones(256)
            self.assertTrue(self.verifier._voices(pcm))
            samples = encoder.embed_utterance.call_args.args[0]
            # A VAD frame at the speech boundary may include some silence;
            # the selected window must contain the later speech, not silence only.
            self.assertGreater(float(samples.mean()), 0)
    def test_dropped_capture_evidence_cannot_authorize_a_batch(self):
        np = self.np
        now = time.time()
        sample = Batch(now - 1, now, [self.frame], b"\x00" * 32000, gaps=("microphone",))
        one = np.array([1., 0.])
        with patch.object(self.verifier, "_faces", return_value=[one]), patch.object(self.verifier, "_voices", return_value=[]):
            self.assertFalse(self.verifier.verify(sample, {"owner": {"face": one, "voice": one}}).certain)
    def test_participant_check_uses_freshest_frame_and_cannot_fall_back_to_older_face(self):
        np = self.np
        one, other = np.array([1., 0.]), np.array([0., 1.])
        now = time.time()
        batch = Batch(now - 1, now, [b"earlier", b"freshest"], b"\x00" * 32000)
        refs = {"owner": {"face": one, "voice": one}}
        with patch.object(self.verifier, "_faces", return_value=[other]) as faces, patch.object(self.verifier, "_voices", return_value=[]):
            self.assertFalse(self.verifier.verify(batch, refs).certain)
            faces.assert_called_once_with(b"freshest")
        with patch.object(self.verifier, "_faces", return_value=[one]), patch.object(self.verifier, "_voices", return_value=[]):
            self.assertTrue(self.verifier.verify(batch, refs).certain)
    def test_enrollment_refuses_zero_or_multiple_faces(self):
        with self.assertRaises(ValueError): self.verifier.enroll(self.frame, b"\x00" * 32000)
    def test_unknown_voice_or_ambiguous_face_invalidates_whole_batch(self):
        np = self.np
        one = np.array([1., 0.])
        other = np.array([0., 1.])
        now = time.time()
        sample = Batch(now - 1, now, [self.frame], b"\x00" * 32000)
        refs = {"owner": {"face": one, "voice": one}}
        with patch.object(self.verifier, "_faces", return_value=[one]), patch.object(self.verifier, "_voices", return_value=[other]):
            self.assertFalse(self.verifier.verify(sample, refs).certain)
        with patch.object(self.verifier, "_faces", return_value=[one]), patch.object(self.verifier, "_voices", return_value=[]):
            self.assertTrue(self.verifier.verify(sample, refs).certain)
            refs["other"] = {"face": one, "voice": other}
            self.assertFalse(self.verifier.verify(sample, refs).certain)
    def test_explicit_template_bank_preserves_unknown_and_ambiguity_rejection(self):
        np = self.np
        one, other, unknown = np.eye(3)
        now = time.time()
        batch = Batch(now - 1, now, [self.frame], b"\x00" * 32000)
        refs = {"owner": {"face": one, "voice": one, "face_samples": [one.tolist(), other.tolist()]}}
        with patch.object(self.verifier, "_voices", return_value=[]), patch.object(self.verifier, "_faces", return_value=[other]):
            self.assertTrue(self.verifier.verify(batch, refs).certain)
            refs["another"] = {"face": other, "voice": one}
            self.assertFalse(self.verifier.verify(batch, refs).certain)
        with patch.object(self.verifier, "_voices", return_value=[]), patch.object(self.verifier, "_faces", return_value=[unknown]):
            self.assertFalse(self.verifier.verify(batch, refs).certain)
    def test_nonfinite_reference_similarity_can_never_authorize(self):
        np = self.np
        now = time.time()
        batch = Batch(now - 1, now, [self.frame], b"\x00" * 32000)
        refs = {"owner": {"face": [float("nan"), 0.], "voice": [1., 0.]}}
        with patch.object(self.verifier, "_voices", return_value=[]), patch.object(self.verifier, "_faces", return_value=[np.array([1., 0.])]):
            self.assertFalse(self.verifier.verify(batch, refs).certain)
    def test_person_detector_filters_chairs_and_duplicate_boxes(self):
        from butler_ambient.people import PeopleDetector
        np = self.np
        model = PeopleDetector.__new__(PeopleDetector)
        model.cv, model.np = self.verifier.cv, np
        model.grid, model.scales = np.zeros((3, 2)), np.ones((3, 1))
        predictions = np.zeros((1, 3, 85), dtype=np.float32)
        predictions[:, :, :4] = [100, 100, np.log(80), np.log(120)]
        predictions[:, :, 4] = .9
        predictions[0, 0, 5] = .9
        predictions[0, 1, 5 + 56] = .99
        predictions[0, 2, 5] = .8
        with patch.object(model, "net", create=True) as net:
            net.forward.return_value = predictions
            boxes = model.detect(np.zeros((640, 640, 3), dtype=np.uint8))
        self.assertEqual(len(boxes), 1)
    def test_two_detected_bodies_cannot_share_one_authorized_face(self):
        np = self.np
        face = np.zeros((1, 15), dtype=np.float32)
        face[0, :4] = [250, 10, 50, 50]
        with patch.object(self.verifier, "detector") as detector, patch.object(self.verifier, "people") as people:
            detector.detect.return_value = (None, face)
            people.detect.return_value = [[100, 0, 300, 480], [200, 0, 300, 480]]
            with self.assertRaises(ValueError): self.verifier._faces(self.frame)


if __name__ == "__main__":
    unittest.main()

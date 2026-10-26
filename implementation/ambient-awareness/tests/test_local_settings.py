"""Owner-only settings validation and private restart configuration."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient.local_settings import devices, update
from butler_ambient.service import Service
from butler_ambient.store import Store


class LocalSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.service = Service(self.store, Mock(), Mock(), Mock())
        self.path = self.store.root / "config.json"
        self.path.write_text(json.dumps({"camera": "0", "whisper_model": "/private/local-model.bin"}))

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def test_enumeration_excludes_screen_sources_and_never_captures(self):
        lines = b"[AVFoundation indev @ x] AVFoundation video devices:\n[AVFoundation indev @ x] [0] Camera\n[AVFoundation indev @ x] [1] Capture screen 0\n[AVFoundation indev @ x] AVFoundation audio devices:\n[AVFoundation indev @ x] [0] USB Mic\n"
        with patch("butler_ambient.local_settings.subprocess.run", return_value=Mock(stderr=lines)) as run:
            self.assertEqual(devices(), {"camera": [{"id": "0", "name": "Camera"}], "microphone": [{"id": "0", "name": "USB Mic"}]})
        self.assertIn("-list_devices", run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][-1], "")

    def test_changed_device_is_validated_and_does_not_change_global_preferences(self):
        with patch("butler_ambient.local_settings.devices", return_value={"camera": [], "microphone": [{"id": "2", "name": "Selected USB"}]}):
            result = update(self.service, {"microphone": "2"})
        value = json.loads(self.path.read_text())
        self.assertEqual(value["expected_microphone"], "Selected USB")
        self.assertEqual(value["whisper_model"], "/private/local-model.bin")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(result["restart_required"])
        self.service.inputs.start.assert_not_called()

    def test_unknown_device_model_or_setting_cannot_mutate_config(self):
        original = self.path.read_bytes()
        for values in ({"voice_python": "/other"}, {"camera": "99"}, {"camera": 1}):
            with patch("butler_ambient.local_settings.devices", return_value={"camera": [], "microphone": []}):
                with self.assertRaises(ValueError): update(self.service, values)
        with self.assertRaises(RuntimeError): update(self.service, {"vision_model": "provider:cloud"})
        self.assertEqual(self.path.read_bytes(), original)

    def test_model_change_checks_local_capabilities_before_persisting(self):
        with patch("butler_ambient.local_settings.LocalAnalyzer.check_local") as check:
            update(self.service, {"vision_model": "local-vision:4b", "conversation_model": "local-text:4b"})
        self.assertEqual([c.kwargs for c in check.call_args_list], [{"require_vision": True}, {"require_vision": False}])
        self.assertEqual(json.loads(self.path.read_text())["conversation_model"], "local-text:4b")

    def test_incompatible_recognition_model_does_not_change_config_or_enrollment(self):
        model=self.store.root/'incompatible.onnx';model.write_bytes(b'not an ONNX model')
        original=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'incompatible'):
            update(self.service,{'face_recognizer':str(model)})
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(self.store.execute('SELECT * FROM agreements'),[])

    def test_unsupported_camera_mode_is_rejected_before_restart(self):
        self.service.config={'audio_helper':'metadata-helper','camera':'0'}
        with patch('butler_ambient.local_settings.devices',return_value={'camera':[{'id':'0','name':'Camera'}],'microphone':[]}), \
             patch('butler_ambient.local_settings.camera_modes',return_value=[{'name':'Camera','sizes_at_30fps':['640x480']}]):
            with self.assertRaisesRegex(ValueError,'resolution'):update(self.service,{'camera_size':'1920x1080'})
        self.assertNotIn('camera_size',json.loads(self.path.read_text()))

    def test_hardware_mute_is_not_a_green_working_microphone(self):
        from butler_ambient.local_ui import snapshot
        self.service.inputs.audio_status={'ready':True,'echo_cancellation':True,'input_muted':True}
        self.service.inputs.playback={'state':'idle','id':None}
        self.service.inputs.input_signal={'at':self.service.clock(),'rms_dbfs':-90}
        value=snapshot(self.service)
        self.assertFalse(value['voice']['input']['working'])
        self.assertTrue(value['voice']['input']['muted'])

    def test_settings_distinguish_actual_and_requested_microphone_and_block_mismatch(self):
        from butler_ambient.local_ui import snapshot
        self.service.config['expected_microphone']='Requested USB'
        self.service.inputs.audio_status={'ready':True,'echo_cancellation':True,
            'input_verified':False,'actual_microphone':'Other device'}
        self.service.inputs.playback={'state':'idle','id':None}
        self.service.inputs.input_signal={'at':self.service.clock(),'rms_dbfs':-30}
        value=snapshot(self.service)['voice']['input']
        self.assertEqual(value['name'],'Other device')
        self.assertEqual(value['selected_name'],'Requested USB')
        self.assertFalse(value['selection_verified'])
        self.assertFalse(value['working'])


if __name__ == "__main__": unittest.main()

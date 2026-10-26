"""No owner voice update without local auth, fresh challenge and explicit capture."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np
from butler_ambient.owner_setup import challenge_matches
from butler_ambient.sensing import Batch
from butler_ambient.service import Service
from butler_ambient.store import Store


class OwnerSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.now = 100.
        self.inputs = Mock(input_signal={"at":self.now},audio_status={"input_verified":True})
        self.service = Service(self.store,self.inputs,Mock(),Mock(),clock=lambda:self.now,transport=Mock())
        self.service.voice = Mock()
        self.service.voice.transcriber.available = False
        self.setup = self.service.owner_setup
        self.original = {"face":[0.,1.],"face_samples":[[0.,1.]],"voice":[1.,0.],"voice_samples":[[1.,0.]]}
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,?)",(json.dumps(self.original),))
        self.service.gate.enable(True,self.now)

    def tearDown(self):
        self.service.stop(); self.store.close(); self.temp.cleanup()

    def reference(self): return json.loads(self.store.execute("SELECT reference FROM agreements WHERE id='owner'")[0]["reference"])

    def run_flow(self, *, authorized=True, challenge=True, pause=False, guest=None, owner_voice=False, owner_face=False):
        self.setup.error = None
        self.setup.guest_name=guest
        self.setup.target_id="guest-fixture" if guest else "owner"
        self.service.verifier.FACE_THRESHOLD=.363
        self.service.verifier._faces.return_value=[np.array([0.,1.] if owner_face else [1.,0.])]
        process = Mock(stdin=io.BytesIO(),stdout=io.BytesIO(json.dumps({"authorized":authorized,"start":True}).encode()+b"\n"))
        process.poll.return_value = None
        process.communicate.return_value = (b"",b"")
        model = Mock()
        def transcribe(*args,**kwargs): return "123456" if challenge else "другая фраза"
        model.transcribe.side_effect = transcribe
        def samples(_):
            if pause: self.service.control("enabled",{"value":False})
            if guest and not owner_voice:return [np.array([0.,1.]),np.array([.01,.99])]
            return [np.array([1.,0.]),np.array([.99,.01])]
        self.service.verifier._voices.side_effect = samples
        def feed(_):
            for _ in range(15):
                self.now += 1
                self.setup.feed(Batch(self.now-1,self.now,[b'synthetic guest frame'] if guest else [],b"\1\0"*16000))
            return True
        with patch("butler_ambient.owner_setup.secrets.randbelow",return_value=23456), \
             patch("butler_ambient.owner_setup.subprocess.Popen",return_value=process), \
             patch("butler_ambient.owner_setup.select.select",return_value=([process.stdout],[],[])), \
             patch("butler_ambient.owner_setup.LocalAnalyzer",return_value=model), \
             patch.object(self.setup.complete,"wait",side_effect=feed):
            self.setup.stage = "authenticating"
            self.setup._run(Path("/fixture/native-auth"))

    def test_spoken_digits_require_the_fresh_six_digit_challenge(self):
        self.assertTrue(challenge_matches("один два три четыре пять шесть","123456"))
        self.assertTrue(challenge_matches("Мои цифры 123 456.","123456"))
        self.assertFalse(challenge_matches("123455","123456"))
        self.assertFalse(challenge_matches("один два три","123456"))

    def test_denied_native_auth_cannot_start_capture_or_change_reference(self):
        self.run_flow(authorized=False)
        self.assertEqual(self.setup.stage,"failed")
        self.assertEqual(self.reference(),self.original)
        self.service.verifier._voices.assert_not_called()

    def test_success_preserves_face_and_old_voice_samples_and_saves_no_capture(self):
        self.run_flow()
        self.assertEqual(self.setup.stage,"saved")
        value = self.reference()
        self.assertEqual(value["face_samples"],self.original["face_samples"])
        self.assertEqual(value["voice"],self.original["voice"])
        self.assertEqual(value["voice_samples"][0],self.original["voice_samples"][0])
        self.assertEqual(len(value["voice_samples"]),3)
        self.assertFalse(self.setup.pcm)
        self.assertEqual(self.store.execute("SELECT * FROM media"),[])
        self.assertEqual(len(list((self.root/"backups").glob("owner-voice-*.json"))),1)

    def test_wrong_spoken_challenge_and_pause_do_not_replace_owner(self):
        for options in ({"challenge":False},{"pause":True}):
            self.service.gate.enable(True,self.now)
            self.setup.cancelled.clear(); self.setup.complete.clear()
            self.run_flow(**options)
            self.assertNotEqual(self.setup.stage,"saved")
            self.assertEqual(self.reference(),self.original)
            self.assertFalse(self.setup.pcm)

    def test_capture_discards_buffer_part_preceding_the_start_click(self):
        self.setup.stage = "recording"
        self.setup.started_at = 100.5
        self.now = 101.
        self.setup.feed(Batch(100,101,[],b"\1\0"*8000+b"\2\0"*8000))
        self.assertEqual(self.setup.pcm,b"\2\0"*8000)
        self.service.control("enabled",{"value":False})
        self.assertFalse(self.setup.pcm)
        self.assertEqual(self.setup.stage,"cancelled")

    def test_named_guest_enrolls_only_after_auth_challenge_and_distinct_face_voice(self):
        self.run_flow(guest="Тестовый гость")
        self.assertEqual(self.setup.stage,"saved")
        self.assertEqual(self.reference(),self.original)
        guest=self.store.execute("SELECT name,reference,active FROM agreements WHERE id='guest-fixture'")[0]
        value=json.loads(guest['reference'])
        self.assertEqual(guest['name'],'Тестовый гость');self.assertEqual(guest['active'],1)
        self.assertEqual(len(value['face_samples']),15)
        self.assertEqual(len(value['voice_samples']),2)
        self.assertFalse(self.setup.frames);self.assertFalse(self.setup.pcm)
        self.assertEqual(self.store.execute('SELECT * FROM media'),[])

    def test_guest_cannot_copy_owner_voice_or_owner_face_as_new_identity(self):
        for options in ({'owner_voice':True},{'owner_face':True}):
            self.setup.cancelled.clear();self.setup.complete.clear()
            self.run_flow(guest='Другой человек',**options)
            self.assertNotEqual(self.setup.stage,'saved')
            self.assertEqual(self.store.execute("SELECT id FROM agreements WHERE id!='owner'"),[])
            self.assertEqual(self.reference(),self.original)
            self.assertFalse(self.setup.frames)

    def test_denied_auth_wrong_challenge_and_pause_never_create_guest(self):
        for options in ({'authorized':False},{'challenge':False},{'pause':True}):
            self.service.gate.enable(True,self.now)
            self.setup.cancelled.clear();self.setup.complete.clear()
            self.run_flow(guest='Гость',**options)
            self.assertNotEqual(self.setup.stage,'saved')
            self.assertEqual(self.store.execute("SELECT id FROM agreements WHERE id!='owner'"),[])
            self.assertFalse(self.setup.pcm);self.assertFalse(self.setup.frames)


if __name__ == "__main__": unittest.main()

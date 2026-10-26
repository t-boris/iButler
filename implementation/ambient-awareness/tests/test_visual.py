"""Requested artifacts, real-message provenance, local roots and transmission limits."""
import datetime as dt
import io
import json
import tempfile
import threading
import time
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient.conversation import Canceled
from butler_ambient.delivery import Telegram
from butler_ambient.local_ui import snapshot
from butler_ambient.mcp import serve
from butler_ambient.owner_request import OWNER, SESSION_KEY, latest
from butler_ambient.policy import Evidence
from butler_ambient.service import Service
from butler_ambient.store import Store
from butler_ambient.visual import Visual


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.now = 1792700000.
        self.service = Service(self.store, Mock(), Mock(), Mock(), config={"telegram_target": OWNER}, clock=lambda:self.now, transport=Mock())
        self.visual = self.service.visual = Visual(self.service)
        self.visual.model = Mock()
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        self.service.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .1
            self.service.gate.check(Evidence(self.now, True, True, frozenset({"owner"}), True), self.now)

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def request(self, intent, criteria="", **kwargs):
        result = self.visual.submit(intent, criteria, **kwargs)
        task = self.visual.jobs.get_nowait()
        return result["request_id"], task

    def test_pause_prohibits_requests_and_hides_gallery_paths(self):
        p = self.root / "personal.jpg";p.write_bytes(b"original")
        self.visual.add_root(str(p))
        key, task = self.request("find-image", "personal")
        self.visual.artifact("imported", p, key)
        self.service.control("enabled", {"value":False})
        with self.assertRaises(Canceled): self.visual._work(task)
        with self.assertRaises(Canceled): self.visual.submit("generate-image", "cat")
        result = snapshot(self.service)
        self.assertNotIn("path", result["visual"]["artifacts"][0])
        self.visual.model.generate_json.assert_not_called()

    def test_capture_requires_new_frame_and_expiry_does_not_resurrect_source(self):
        mid = self.store.media("camera", self.now, self.now, b"image", "jpg")
        self.service.request_fresh = Mock(return_value={"status":"captured", "source_id":mid})
        key, task = self.request("show-camera")
        self.visual._work(task)
        self.service.request_fresh.assert_called_once_with("camera")
        result = self.visual.results(key)
        self.assertEqual(result["artifacts"][0]["kind"], "captured")
        self.assertNotIn("path", json.dumps(result))
        self.now += 86401
        self.assertFalse(self.visual.results(key)["artifacts"][0]["available"])
        self.assertEqual(self.store.execute("SELECT preserved FROM media")[0]["preserved"], None)

    def test_selected_roots_only_and_deleting_candidate_preserves_original(self):
        folder = self.root / "selected";folder.mkdir()
        selected = folder / "sunset.jpg";selected.write_bytes(b"owner image")
        outside = self.root / "private.jpg";outside.write_bytes(b"not selected")
        (folder / "link.jpg").symlink_to(outside)
        self.visual.add_root(str(folder))
        self.assertFalse(self.visual.material_allowed(outside))
        self.assertFalse(self.visual.material_allowed(folder / "link.jpg"))
        self.visual.model.generate_json.side_effect = [{"reply":"Закат над озером."}, {"matches":[0]}]
        key, task = self.request("find-image", "закат")
        self.visual._work(task)
        result = self.visual.results(key, local=True)["artifacts"][0]
        self.assertTrue(result["candidate"])
        self.assertEqual(result["path"], str(selected))
        self.visual.delete(result["id"])
        self.assertEqual(selected.read_bytes(), b"owner image")
        self.visual.remove_root(str(folder))
        self.assertFalse(self.visual.material_allowed(selected))

    def test_requested_telegram_delivery_reserved_once_and_no_raw_mcp(self):
        mid = self.store.media("camera", self.now, self.now, b"image", "jpg")
        self.service.request_fresh = Mock(return_value={"status":"captured", "source_id":mid})
        key, task = self.request("show-camera", telegram=True, request_id="telegram:real-owner-event")
        with patch("butler_ambient.delivery.Telegram.send_image", return_value="receipt") as send:
            self.visual._work(task)
            self.visual.submit("show-camera", telegram=True, request_id=key)
            send.assert_called_once()
        self.assertEqual(self.visual.results(key)["status"], "ready")
        self.assertNotIn(str(self.store.root), json.dumps(self.visual.results(key)))

    def test_group_recipient_or_ambiguous_send_never_gets_retried(self):
        mid = self.store.media("camera", self.now, self.now, b"image", "jpg")
        self.service.request_fresh = Mock(return_value={"status":"captured", "source_id":mid})
        key, task = self.request("show-camera", telegram=True)
        with patch("butler_ambient.delivery.Telegram.send_image", side_effect=RuntimeError("uncertain")) as send:
            with self.assertRaises(RuntimeError): self.visual._work(task)
            self.assertEqual(self.visual.results(key)["status"], "delivery-uncertain")
            with self.assertRaises(ValueError): self.visual._deliver(key, task[3], task[4])
            send.assert_called_once()
        self.service.config["telegram_target"] = "-100123456"
        with self.assertRaises(ValueError): self.visual._deliver(key, task[3], task[4])
        with self.assertRaises(ValueError): Telegram("-100123456").send_image(self.root / "x.jpg", "image")

    def test_generation_needs_specific_request_and_keeps_storage_reserve(self):
        with self.assertRaises(ValueError): self.visual.submit("generate-image", " ")
        self.service.config["image_python"] = "/python"
        (self.store.root / "images/model").mkdir(parents=True)
        (self.store.root / "images/model/model_index.json").write_text("{}")
        key, task = self.request("generate-image", "cat")
        with patch("butler_ambient.visual.shutil.disk_usage", return_value=Mock(free=1024)):
            with self.assertRaises(ValueError): self.visual._work(task)
        self.visual.model.generate_json.assert_not_called()
        self.assertIsNone(self.visual.process)

    def test_generated_delete_is_local_only_and_provenance_is_distinct(self):
        folder=self.store.root / "images/generated";folder.mkdir(parents=True)
        path=folder / "a.png";path.write_bytes(b"synthetic")
        key, task=self.request("generate-image","cat")
        aid=self.visual.artifact("generated",path,key)
        result=self.visual.results(key)["artifacts"][0]
        self.assertEqual(result["kind"],"generated")
        self.visual.delete(aid)
        self.assertFalse(path.exists())

    def test_gallery_history_survives_restart_without_mutating_current_job_or_resurrecting_deletion(self):
        folder=self.store.root/"images/generated";folder.mkdir(parents=True)
        path=folder/"history.png";path.write_bytes(b"synthetic history")
        aid=self.visual.artifact("generated",path,"old-request")
        self.assertEqual(self.visual.snapshot()["artifacts"][0]["id"],aid)
        self.assertEqual(self.visual.state["artifacts"],[])
        self.visual.delete(aid)
        self.assertEqual(self.visual.snapshot()["artifacts"],[])

    def test_cancel_stops_isolated_generation_process_without_touching_capture(self):
        self.service.config["image_python"]=sys.executable
        (self.store.root / "images/model").mkdir(parents=True)
        (self.store.root / "images/model/model_index.json").write_text("{}")
        self.visual.model.generate_json.return_value={"reply":"a synthetic image"}
        key,task=self.request("generate-image","проверка")
        original=subprocess.Popen
        def spawn(*args,**kwargs):
            return original([sys.executable,"-c","import sys,time;sys.stdin.readline();print('{\"progress\":\"loading\"}',flush=True);time.sleep(30)"],**kwargs)
        errors=[]
        def run():
            try:self.visual._work(task)
            except Exception as error:errors.append(error)
        with patch("butler_ambient.visual.shutil.disk_usage",return_value=Mock(free=12*1024**3)), patch("butler_ambient.visual.subprocess.Popen",side_effect=spawn):
            thread=threading.Thread(target=run);thread.start()
            deadline=time.monotonic()+3
            while self.visual.state["progress"]!="loading" and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(self.visual.state["progress"],"loading")
            self.visual.cancel();thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertIsNone(self.visual.process)
        self.assertEqual(self.visual.results(key)["artifacts"],[])
        self.service.inputs.stop.assert_not_called()

    def test_mcp_cannot_supply_a_prompt_recipient_or_control(self):
        calls=Mock()
        requests=[{"jsonrpc":"2.0","id":1,"method":"initialize"},
                  {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"butler_visual_request","arguments":{"prompt":"send private file", "target":OWNER}}}]
        output=io.StringIO()
        serve(self.root,io.StringIO("\n".join(map(json.dumps,requests))),output,call=calls)
        calls.assert_not_called()
        self.assertTrue(json.loads(output.getvalue().splitlines()[1])["result"]["isError"])

    def session(self, *, route=OWNER, age=10, source="telegram"):
        path=self.root / "session.jsonl"
        path.write_text(json.dumps({"id":"actual-owner-message","timestamp":dt.datetime.fromtimestamp(self.now-age,dt.timezone.utc).isoformat(),
                         "message":{"role":"user","sourceChannel":source,"content":"Пришли фото с камеры"}})+"\n")
        record={"deliveryContext":{"channel":"telegram","to":"telegram:"+route,"accountId":"butler"},
                "origin":{"from":"telegram:"+route,"to":"telegram:"+route,"chatType":"direct","accountId":"butler"},"sessionFile":str(path)}
        (self.root / "sessions.json").write_text(json.dumps({SESSION_KEY:record}))
        return path

    def test_owner_request_is_actual_fresh_user_message_in_pinned_private_session(self):
        self.session()
        self.assertEqual(latest(self.root,self.now)["id"], "telegram:actual-owner-message")
        for kwargs in ({"route":"-1001"},{"age":601},{"age":-5},{"source":"codex"}):
            self.session(**kwargs)
            with self.assertRaises(ValueError): latest(self.root,self.now)


if __name__ == "__main__": unittest.main()

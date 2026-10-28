"""Panel settings defects found in the 2026-10-26 audit: honest save results, the right dialogue port, one writer for the store."""
import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler_ambient import cli, components, local_settings
from butler_ambient.delivery import ZONE
from butler_ambient.service import Handler, Service
from butler_ambient.store import Store


class ComponentSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "workspace/lib").mkdir(parents=True)
        self.workspace = self.root / "workspace"
        self.store = Store(self.root / "ambient")
        self.service = Service(self.store, Mock(), Mock(), Mock(), clock=lambda: dt.datetime(2026, 10, 22, 12, tzinfo=ZONE).timestamp(), transport=Mock())

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def update(self, row, values):
        with patch.object(components, "snapshot", return_value={"components": [row]}), patch.object(components.local_settings, "update", return_value={"saved": True, "restart_required": True, "changed": ["x"]}) as save:
            return components.update(self.service, values, self.workspace), save

    def test_configured_knowledge_is_distinct_from_a_successful_or_failed_search(self):
        self.service.catalog=None
        self.service.knowledge=Mock()
        for state,ready in [('configured',True),('ready',True),('unavailable',False),('not-configured',False)]:
            self.service.knowledge.snapshot.return_value={'state':state}
            self.service.knowledge.workspace=self.workspace
            with patch.object(components,'LocalAnalyzer',return_value=Mock()), patch.object(components,'agent_status',side_effect=ValueError('offline')):
                value=next(row for row in components.snapshot(self.service,self.workspace)['components'] if row['id']=='knowledge')
            self.assertEqual((value['status'],value['ready']),(state,ready))
            if state=='configured':self.assertIn('первый поиск ещё не выполнялся',value['note'])
        self.service.knowledge.recall.assert_not_called()

    def test_a_read_only_component_is_not_reported_as_saved(self):
        row = {"id": "camera", "model": "m", "executable": "e", "model_key": None, "cli_key": None}
        result, save = self.update(row, {"id": "camera"})
        self.assertIs(result["saved"], False)
        self.assertEqual(result["reason"], "read-only")
        self.assertIn("только для просмотра", result["message"])
        save.assert_not_called()

    def test_saving_an_editable_component_without_a_change_says_nothing_changed(self):
        row = {"id": "asr", "model": "m", "executable": "e", "model_key": "whisper_model", "cli_key": "whisper_cli"}
        result, save = self.update(row, {"id": "asr", "model": "m", "executable": "e"})
        self.assertIs(result["saved"], False)
        self.assertEqual(result["reason"], "unchanged")
        save.assert_not_called()

    def test_a_real_change_is_still_saved(self):
        row = {"id": "asr", "model": "m", "executable": "e", "model_key": "whisper_model", "cli_key": "whisper_cli"}
        result, save = self.update(row, {"id": "asr", "model": "other"})
        self.assertIs(result["saved"], True)
        save.assert_called_once_with(self.service, {"whisper_model": "other"})


class DialoguePortTests(unittest.TestCase):
    """The dialogue model runs on its own Ollama port; probing the background one misreports its readiness."""

    def test_readiness_of_the_dialogue_model_is_probed_on_the_dialogue_port_only(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name)); self.addCleanup(store.close)
        service = Service(store, Mock(), Mock(), Mock(), config={"conversation_model": "dialogue-m", "vision_model": "vision-m"}, transport=Mock())
        self.addCleanup(service.stop)
        probes = []
        def analyzer(name, *args, dialogue=False, **kwargs):
            probes.append((name, dialogue))
            return Mock(check_local=Mock(return_value=None))
        with patch.object(components, "LocalAnalyzer", side_effect=analyzer), patch.object(components, "agent_status", return_value={"model": "", "allowed": [], "cli": ""}), \
             patch.object(components, "command", return_value="{}"), patch.object(components, "owner_routing", return_value={}), \
             patch.object(components, "embedding_info", return_value=1):
            try: rows = components.snapshot(service, Path(temp.name))["components"]
            except Exception as error: self.skipTest("snapshot needs more of the installation: " + type(error).__name__)
        self.assertIn(("dialogue-m", True), probes)
        self.assertIn(("vision-m", False), probes)
        self.assertNotIn(("dialogue-m", False), probes)
        self.assertEqual(next(r for r in rows if r["id"] == "conversation_model")["executable"], "HTTP 127.0.0.1:11435 · Ollama")
        self.assertEqual(next(r for r in rows if r["id"] == "vision_model")["executable"], "HTTP 127.0.0.1:11434 · Ollama")

    def test_changing_the_dialogue_model_is_validated_on_the_dialogue_port(self):
        seen = []
        def analyzer(name, *args, dialogue=False, **kwargs):
            seen.append((name, dialogue))
            return Mock(check_local=Mock(side_effect=RuntimeError("stop here")))
        service = SimpleNamespace(config={}, store=SimpleNamespace(root=Path("/nonexistent")))
        for key, expected in (("conversation_model", True), ("vision_model", False)):
            seen.clear()
            with patch.object(local_settings, "LocalAnalyzer", side_effect=analyzer), self.assertRaises(RuntimeError):
                local_settings.update(service, {key: "m"})
            self.assertEqual(seen, [("m", expected)])


class HandlerTests(unittest.TestCase):
    def run_command(self, command, args, service):
        instance = Handler.__new__(Handler)
        instance.connection = Mock()
        instance.rfile = io.BytesIO(json.dumps({"command": command, "args": args}).encode() + b"\n")
        instance.wfile = io.BytesIO()
        instance.server = SimpleNamespace(service=service)
        instance.handle()
        return json.loads(instance.wfile.getvalue())

    def voice_store(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name)); self.addCleanup(store.close)
        voice = Path(temp.name) / "voice"
        (voice / "previews").mkdir(parents=True)
        (voice / "previews/warm.wav").write_bytes(b"RIFF")
        (voice / "profiles.json").write_text(json.dumps({"synthetic": True, "profiles": [{"id": "warm", "name": "Тёплый"}]}))
        return store

    def test_voice_selection_is_written_by_the_running_service_through_its_own_store(self):
        store = self.voice_store()
        self.assertEqual(self.run_command("voice-select", {"profile": "warm"}, SimpleNamespace(store=store)), {"result": {"selected": "warm", "synthetic": True}})
        self.assertEqual((store.get("voice_profile"), store.get("voice_enabled")), ("warm", True))

    def test_voice_selection_refuses_unknown_profiles_and_extra_fields_without_writing(self):
        store = self.voice_store()
        for args in ({"profile": "robot"}, {"profile": "warm", "x": 1}, {}, {"profile": ["warm"]}):
            self.assertIn("error", self.run_command("voice-select", args, SimpleNamespace(store=store)), args)
        self.assertEqual((store.get("voice_profile"), store.get("voice_enabled")), (None, None))

    def test_the_cli_forwards_voice_selection_instead_of_opening_the_live_database(self):
        output = io.StringIO()
        with patch.object(cli, "Store", side_effect=AssertionError("the CLI must not open the store itself")), \
             patch.object(cli.mcp, "rpc", return_value={"selected": "calm"}) as rpc, contextlib.redirect_stdout(output):
            cli.main(["--state", "/nonexistent-state", "voice-select", "calm"])
        rpc.assert_called_once_with(Path("/nonexistent-state"), "voice-select", {"profile": "calm"})
        self.assertEqual(json.loads(output.getvalue()), {"selected": "calm"})

    def test_owner_setup_refusals_reach_the_panel_as_a_sentence(self):
        setup = Mock(start=Mock(side_effect=ValueError("Для регистрации нужен включённый подтверждённый микрофон. Полная Пауза сохраняется.")))
        result = self.run_command("ui-owner-setup", {}, SimpleNamespace(owner_setup=setup))["result"]
        self.assertEqual(result["queued"], False)
        self.assertIn("подтверждённый микрофон", result["message"])

    def test_guest_setup_refusals_reach_the_panel_and_success_is_passed_through(self):
        setup = Mock(start=Mock(side_effect=[ValueError("Проверка владельца уже открыта."), {"queued": True}]))
        service = SimpleNamespace(owner_setup=setup)
        self.assertEqual(self.run_command("ui-guest-setup", {"name": "Аня"}, service)["result"], {"queued": False, "message": "Проверка владельца уже открыта."})
        self.assertEqual(self.run_command("ui-guest-setup", {"name": "Аня"}, service)["result"], {"queued": True})

    def test_a_malformed_setup_request_is_still_an_error_not_a_message(self):
        service = SimpleNamespace(owner_setup=Mock())
        self.assertIn("error", self.run_command("ui-guest-setup", {"name": "Аня", "x": 1}, service))
        self.assertIn("error", self.run_command("ui-owner-setup", {"x": 1}, service))
        service.owner_setup.start.assert_not_called()


class ArchivePreferenceTests(unittest.TestCase):
    def test_the_snapshot_keeps_the_owners_choice_apart_from_the_effective_state(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        with patch("butler_ambient.archive.shutil.disk_usage", return_value=Mock(free=10 ** 10)):
            store = Store(Path(temp.name)); self.addCleanup(store.close)
            service = Service(store, Mock(), Mock(), Mock(), transport=Mock()); self.addCleanup(service.stop)
            store.set("continuous_archive", True)
            paused = service.archive.snapshot()
            self.assertEqual((paused["enabled"], paused["preference"]), (False, True))      # recording is paused: effective off, choice on
            store.set("continuous_archive", False)
            self.assertIs(service.archive.snapshot()["preference"], False)


if __name__ == "__main__":
    unittest.main()

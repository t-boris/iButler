"""Confirmed voice controls exercise real SQLite/filesystem/visual adapters."""
import json
import errno
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient.analysis import validate_facts
from butler_ambient.policy import Evidence
from butler_ambient.service import Service
from butler_ambient.store import Store
from butler_ambient.visual import Visual
from butler_ambient.voice_commands import control_request


class MemoryControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.now = time.time()
        self.service = Service(self.store, Mock(), Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.service.visual = Visual(self.service)
        self.service.visual.model = Mock()
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.service.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .1
            self.service.gate.check(Evidence(self.now, True, True, frozenset({"owner"}), True), self.now)
        self.commands = self.service.commands
        self.generation = self.service.voice_gate.generation

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def recording(self, *, start=None, end=None):
        mid = self.store.media("microphone", self.now - 60 if start is None else start,
            self.now - 30 if end is None else end, b"PRIVATE RAW AUDIO", "wav")
        facts = validate_facts([{"kind": "activity", "activity": "work", "confidence": .9,
            "detail": "PRIVATE RAW TRANSCRIPT", "subject": "appearance-1", "identity": "uncertain",
            "topic": "software development"}])
        oid = self.store.save_observation(mid, facts)
        return mid, oid

    def review(self, capability, args):
        reply = self.commands.request(capability, args, owner=True, generation=self.generation)
        self.assertIsNotNone(self.commands.pending)
        return reply

    def confirm(self):
        return self.commands.respond("подтверждаю", owner=True, generation=self.generation)

    def test_preserve_expire_and_two_explicit_deletion_scopes_use_real_files_and_results(self):
        mid, oid = self.recording()
        self.review("preserve", {"id": mid})
        self.assertIsNone(self.store.execute("SELECT preserved FROM media WHERE id=?", (mid,))[0]["preserved"])
        self.assertIn("сохранён", self.confirm())
        saved = Path(self.store.execute("SELECT preserved FROM media WHERE id=?", (mid,))[0]["preserved"])
        self.assertEqual(saved.read_bytes(), b"PRIVATE RAW AUDIO")
        self.store.expire(self.now + 86401)
        self.assertTrue(saved.is_file())
        self.review("delete", {"id": mid, "results": False})
        self.assertIn("анализ", self.confirm())
        self.assertFalse(saved.exists())
        self.assertEqual(len(self.store.execute("SELECT id FROM observations WHERE id=?", (oid,))), 1)
        self.review("delete", {"id": mid, "results": True})
        self.confirm()
        self.assertEqual(self.store.execute("SELECT id FROM observations WHERE id=?", (oid,)), [])
        self.assertIsNone(self.confirm())

    def test_correction_updates_retrieval_without_changing_agreements(self):
        self.recording()
        before = self.store.execute("SELECT * FROM agreements")
        self.review("correct", {"subject": "appearance-1", "name": "Owner supplied name"})
        self.assertEqual(self.store.execute("SELECT * FROM corrections"), [])
        self.confirm()
        fact = self.store.history(self.now - 120, self.now)[0]["facts"][0]
        self.assertEqual(fact["identity"], "Owner supplied name")
        self.assertEqual(fact["identity_basis"], "owner-confirmed")
        self.assertEqual(self.store.execute("SELECT * FROM agreements"), before)

    def test_failed_preservation_copy_or_database_update_leaves_no_partial_saved_original(self):
        mid, _ = self.recording()
        def no_space(source, target):
            Path(target).write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "controlled full disk")
        self.review("preserve", {"id": mid})
        with patch("butler_ambient.store.shutil.copyfile", side_effect=no_space):
            self.assertIn("недостаточно места", self.confirm())
        self.assertEqual(self.commands.last_result, "not-completed")
        self.assertEqual(list((self.store.root / "preserved").iterdir()), [])
        self.assertIsNone(self.store.execute("SELECT preserved FROM media WHERE id=?", (mid,))[0]["preserved"])
        self.store.execute("CREATE TRIGGER reject_preservation BEFORE UPDATE OF preserved ON media BEGIN SELECT RAISE(FAIL,'controlled'); END")
        self.review("preserve", {"id": mid})
        self.assertIn("ошибка локальной базы", self.confirm())
        self.assertEqual(self.commands.last_result, "not-completed")
        self.assertEqual(list((self.store.root / "preserved").iterdir()), [])
        self.store.execute("DROP TRIGGER reject_preservation")
        self.review("preserve", {"id": mid})
        self.assertIn("сохранён", self.confirm())
        saved = Path(self.store.execute("SELECT preserved FROM media WHERE id=?", (mid,))[0]["preserved"])
        self.assertEqual(saved.read_bytes(), b"PRIVATE RAW AUDIO")

    def test_real_summary_reports_coverage_gaps_and_uncertainty_without_raw_words(self):
        self.recording()
        reply = self.commands.request("summary", {"minutes": 2}, owner=True, generation=self.generation)
        self.assertIn("25 процентов", reply)
        self.assertIn("1.5 минут", reply)
        self.assertIn("приблизительные", reply)
        self.assertIn("software development", reply)
        self.assertNotIn("PRIVATE", reply)
        self.assertIsNone(self.commands.pending)
        absolute = self.commands.request("summary", {"start": "2026-10-23T10:00:00-05:00", "end": "2026-10-23T11:00:00-05:00"}, owner=True, generation=self.generation)
        self.assertIn("23.10.2026 10:00:00", absolute)

    def test_guest_cannot_read_personal_metadata_or_confirm_a_library_change(self):
        self.recording()
        for capability, args in (("summary", {"minutes": 2}), ("recordings", {}), ("materials", {}), ("images", {})):
            reply = self.commands.request(capability, args, owner=False, generation=self.generation)
            self.assertIn("подтвердить", reply)
            self.assertNotIn("PRIVATE", reply)
        folder = self.root / "pictures"; folder.mkdir()
        self.review("material-add", {"path": str(folder)})
        self.commands.respond("да", owner=False, generation=self.generation)
        self.assertEqual(self.service.visual.roots(), [])

    def test_stale_generation_or_expired_lease_cannot_read_owner_summary(self):
        self.recording()
        for generation in (self.generation + 1, self.generation):
            if generation == self.generation: self.now += 3
            reply = self.commands.request("summary", {"minutes": 2}, owner=True, generation=generation)
            self.assertIn("подтвердить", reply)
            self.assertNotIn("software development", reply)
            self.assertIsNone(self.commands.pending)

    def test_root_add_and_remove_preserve_originals_and_review_canonical_path(self):
        folder = self.root / "pictures"; folder.mkdir()
        original = folder / "photo.jpg"; original.write_bytes(b"owner original")
        reply = self.review("material-add", {"path": str(folder / ".." / "pictures")})
        self.assertIn(str(folder), reply)
        self.assertEqual(self.service.visual.roots(), [])
        self.confirm()
        self.assertEqual(self.service.visual.roots(), [str(folder)])
        self.review("material-remove", {"path": str(folder)})
        self.confirm()
        self.assertEqual(self.service.visual.roots(), [])
        self.assertEqual(original.read_bytes(), b"owner original")

    def test_changed_symlink_target_and_missing_media_are_rejected_after_review(self):
        folder = self.root / "selected"; folder.mkdir()
        outside = self.root / "other"; outside.mkdir()
        self.review("material-add", {"path": str(folder)})
        folder.rmdir(); folder.symlink_to(outside)
        with self.assertRaises(ValueError): self.confirm()
        self.assertEqual(self.service.visual.roots(), [])
        mid, _ = self.recording()
        self.review("preserve", {"id": mid})
        self.store.delete(mid)
        with self.assertRaises(ValueError): self.confirm()
        self.assertIsNone(self.commands.pending)

    def test_generated_image_deletion_and_imported_candidate_have_distinct_real_effects(self):
        generated = self.store.root / "images/generated/result.png"
        generated.parent.mkdir(parents=True); generated.write_bytes(b"generated")
        imported = self.root / "owner.jpg"; imported.write_bytes(b"original")
        self.service.visual.add_root(str(imported))
        for kind, path in (("generated", generated), ("imported", imported)):
            key = self.service.visual.artifact(kind, path, "fixture-request")
            review = self.review("image-delete", {"id": key})
            self.assertIn("будет удалён" if kind == "generated" else "исходный файл останется", review)
            self.assertTrue(path.exists())
            self.confirm()
            self.assertEqual(path.exists(), kind == "imported")
            self.assertEqual(self.store.execute("SELECT id FROM visual_artifacts WHERE id=?", (key,)), [])

    def test_image_cancellation_cannot_switch_to_a_new_task_after_review(self):
        visual = self.service.visual
        visual.state.update(status="working", request_id="image-one")
        self.review("image-cancel", {"id": "image-one"})
        visual.state["request_id"] = "image-two"
        with self.assertRaises(ValueError): self.confirm()
        self.assertFalse(visual.token.is_set())
        self.review("image-cancel", {"id": "image-two"})
        self.confirm()
        self.assertTrue(visual.token.is_set())
        self.assertEqual(visual.state["status"], "cancelled")

    def test_target_revalidation_after_spoken_execution_notice_preserves_replacement_task(self):
        visual = self.service.visual
        visual.state.update(status="working", request_id="image-one")
        self.review("image-cancel", {"id": "image-one"})
        with self.assertRaises(ValueError):
            self.commands.respond("да", owner=True, generation=self.generation,
                before_execute=lambda: visual.state.update(request_id="image-two"))
        self.assertFalse(visual.token.is_set())
        self.assertEqual(visual.state["request_id"], "image-two")

    def test_expired_or_permission_lost_confirmation_cannot_delete_real_recording(self):
        mid, _ = self.recording()
        path = Path(self.store.execute("SELECT path FROM media WHERE id=?", (mid,))[0]["path"])
        for reason in ("expired", "permission"):
            self.review("delete", {"id": mid, "results": True})
            if reason == "expired":
                old = self.now; self.now += 46
                self.confirm(); self.now = old
            else:
                self.service.gate.invalidate(self.now)
                self.confirm()
            self.assertTrue(path.is_file())
            self.assertEqual(len(self.store.execute("SELECT id FROM observations")), 1)

    def test_invalid_ambiguous_targets_and_periods_never_create_a_confirmation(self):
        mid, _ = self.recording()
        invalid = [("delete", {"id": mid}), ("delete", {"id": mid, "results": "yes"}),
            ("preserve", {"id": "invented"}), ("correct", {"subject": "invented", "name": "Name"}),
            ("summary", {"minutes": True}), ("summary", {"minutes": 0}),
            ("summary", {"start": "2026-10-23T10:00:00", "end": "2026-10-23T11:00:00"}),
            ("image-cancel", {}), ("image-delete", {"id": "invented"})]
        for capability, args in invalid:
            with self.subTest(capability=capability, args=args):
                with self.assertRaises(ValueError): self.review(capability, args)
                self.assertIsNone(self.commands.pending)

    def test_planner_context_has_real_ids_but_no_raw_media_paths_or_transcripts(self):
        mid, _ = self.recording()
        context = self.commands.memory.context()
        self.assertEqual(context["recordings"][0]["id"], mid)
        self.assertEqual(context["subjects"][0]["subject"], "appearance-1")
        self.assertNotIn("PRIVATE", json.dumps(context))
        self.assertNotIn(str(self.store.root / "raw"), json.dumps(context))
        for text in ("Покажи записи", "Составь сводку за последний час", "Дай мне сводку за сегодня", "Перечисли папки изображений", "Что ты умеешь?"):
            self.assertTrue(control_request(text), text)


if __name__ == "__main__": unittest.main()

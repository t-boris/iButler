"""Feature evidence: privacy/expiry, exact feedback identity, rule transactions and evaluation."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from butler_core.learning import LearningStore
from butler_core.rules import RuleStore, Conflict


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"; self.vault.mkdir()
        self.ws = self.root / "workspace"; self.ws.mkdir()
        self.store = RuleStore(self.vault, self.ws, clock=lambda: 100)

    def tearDown(self): self.tmp.cleanup()

    def put(self, **kwargs):
        return self.store.put("rule-1", "Ответы", "Отвечай кратко.", operation="event-1", authorized=True, **kwargs)

    def test_idempotent_operation_and_conflict_preserve_manual_changes(self):
        receipt = self.put()
        self.assertEqual(receipt, self.put())
        self.assertTrue(receipt["saved"] and receipt["applied"])
        path = self.vault / "Butler/Rules/rule-1.md"
        path.write_text(path.read_text().replace("Отвечай кратко.", "Отвечай подробно."))
        profile = self.store.publish()
        self.assertEqual(profile["rules"][0]["revision"], 2)
        with self.assertRaises(Conflict):
            self.store.put("rule-1", "Ответы", "Перезапись", operation="event-2", authorized=True, expected_hash=receipt["content_hash"])
        self.assertIn("подробно", path.read_text())

    def test_candidates_retirement_and_deletion_cannot_return_from_restored_source(self):
        self.put()
        source = (self.vault / "Butler/Rules/rule-1.md").read_text()
        self.store.change("rule-1", operation="retire", authorized=True, status="retired")
        self.assertEqual(self.store.profile()["rules"], [])
        result = self.store.change("rule-1", operation="delete", authorized=True, delete=True)
        self.assertTrue(result["deleted"])
        path = self.vault / "Butler/Rules/renamed.md"; path.write_text(source)
        self.store.publish()
        self.assertFalse(path.exists())
        self.assertEqual(self.store.profile()["rules"], [])
        self.assertFalse((self.vault / "Butler/Rules/.history/rule-1").exists())
        with self.assertRaises(Conflict): self.put()

    def test_migration_preserves_identifiers_and_readonly_derived_view(self):
        legacy = "# Lessons\n\n## L-2026-01-01-1 — Кратко\n\nКратко отвечать.\n\n## L-2026-01-01-2 — Урок\n\nПроверять результат.\n"
        (self.ws / "LESSONS.md").write_text(legacy)
        self.assertEqual(self.store.migrate(), 2)
        self.assertEqual(self.store.migrate(), 0)
        self.assertEqual(set(self.store.scan()), {"L-2026-01-01-1", "L-2026-01-01-2"})
        self.assertEqual((self.ws / "LESSONS.md").stat().st_mode & 0o222, 0)
        self.assertEqual((self.store.state / "migration/LESSONS.before.md").read_text(), legacy)

    def test_invalid_manual_profile_keeps_previous_generation_and_rename_keeps_identity(self):
        self.put()
        old = self.store.profile()["version"]
        path = self.vault / "Butler/Rules/rule-1.md"
        renamed = path.with_name("short-answers.md"); path.rename(renamed)
        self.assertEqual(self.store.publish()["version"], old)
        renamed.write_text(renamed.read_text().replace('"active"', '"broken"'))
        selected = self.store.matching(mode="private")
        self.assertEqual(selected["version"], old)
        self.assertEqual(selected["error"], "ValueError")

    def test_unavailable_vault_and_untrusted_source_never_claim_saved(self):
        with self.assertRaises(PermissionError): self.put(source="playback")
        self.vault.rmdir()
        with self.assertRaises(OSError): self.put()
        self.assertFalse(self.store.profile_path.exists())

    def test_manual_scope_is_versioned_even_after_source_grant_revocation(self):
        receipt = self.put()
        self.store.block_context("source-revoked")
        path = self.vault / "Butler/Rules/rule-1.md"
        path.write_text(path.read_text().replace('"private"', '"general"'))
        self.assertEqual(self.store.matching(mode="private")["selected"], [])
        row = self.store.matching(mode="general")["selected"][0]
        self.assertEqual(row["revision"], 2)
        self.assertNotEqual(row["hash"], receipt["content_hash"])
        self.assertEqual(self.store.decode(path.parent / ".history/rule-1/1.md")["scope"], {"mode": "private"})
        with self.assertRaises(Conflict):
            self.store.put("rule-1", "Ответы", "Иначе", operation="stale", authorized=True, expected_hash=receipt["content_hash"])

    def test_interrupted_write_repairs_history_before_returning_receipt(self):
        self.put()
        (self.store.state / "receipts").joinpath(next((self.store.state / "receipts").iterdir()).name).unlink()
        history = self.vault / "Butler/Rules/.history/rule-1/1.md"
        history.unlink()
        result = self.put()
        self.assertTrue(result["saved"] and result["applied"])
        self.assertTrue(history.exists())


class LearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); self.now = 100.
        self.store = LearningStore(self.root / "metadata.sqlite3", self.root / "private", clock=lambda: self.now)

    def tearDown(self): self.store.close(); self.tmp.cleanup()

    def episode(self, identifier="u1", session="s1", accepted=False):
        self.store.episode(identifier, "t-" + identifier, session, 100, "секретная реплика")
        return self.store.decide(identifier, {"accepted": accepted, "wake_name": False, "scene": "alone", "first_word_ms": 1000})

    def test_retention_uses_original_time_and_expired_text_cannot_be_resurrected(self):
        revision = self.episode()
        self.now += 23 * 3600
        self.store.annotate("a", "u1", revision, "recognition", corrected="исправленный секрет", authorized=True)
        self.now = 100 + 29 * 86400
        self.assertEqual(self.store.text("u1")["corrected"], "исправленный секрет")
        self.now = 100 + 30 * 86400
        self.assertIsNone(self.store.text("u1"))
        self.store.annotate("a2", "u1", revision, "label_addressed", authorized=True)
        self.assertIsNone(self.store.text("u1"))
        self.episode("u2")
        self.assertIsNone(self.store.text("u2"))

    def test_private_text_never_enters_metadata_or_anonymized_summary(self):
        revision = self.episode()
        self.store.annotate("a", "u1", revision, "recognition", corrected="private-correction", authorized=True)
        self.store.db.commit()
        content = self.store.path.read_bytes()
        self.assertNotIn("секретная".encode(), content)
        self.assertNotIn(b"private-correction", content)
        summary = json.dumps(self.store.anonymized_summary())
        self.assertNotIn("u1", summary); self.assertNotIn("private", summary)

    def test_duplicate_correction_and_immutable_revisions(self):
        revision = self.episode()
        first = self.store.annotate("a", "u1", revision, "label_addressed", authorized=True)
        self.assertEqual(first, self.store.annotate("a", "u1", revision, "label_addressed", authorized=True))
        with self.assertRaises(ValueError): self.store.annotate("a", "u1", revision, "label_other", authorized=True)
        with self.assertRaises(ValueError): self.store.decide("u1", {"accepted": True}, revision=revision)
        self.store.annotate("retract", "u1", revision, "retract", authorized=True, retract=first["id"])
        self.assertIsNone(self.store.dataset()[0]["label"])

    def test_ambiguous_binding_does_not_label_or_extend_private_retention(self):
        revision = self.episode()
        annotation = self.store.annotate("a", "u1", revision, "label_addressed", authorized=True, binding="ambiguous")
        self.assertEqual(annotation["state"], "unresolved")
        self.now += 86400
        self.assertIsNone(self.store.text("u1"))
        self.assertIsNone(self.store.dataset()[0]["label"])

    def test_compliance_requires_exact_application_and_applicable_evidence(self):
        revision = self.episode()
        receipt = self.store.receipt("u1", revision, "profile-1", rules=[{"id": "r1", "revision": 2, "hash": "abc"}])
        self.assertEqual(self.store.assess(receipt, "r1", 2, "complied"), "unknown")
        self.assertEqual(self.store.assess(receipt, "r1", 2, "violated", applicable=False), "not_applicable")
        self.assertEqual(self.store.assess(receipt, "r1", 2, "complied", applicable=True, evidence="check-1"), "complied")
        with self.assertRaises(ValueError): self.store.assess(receipt, "r1", 1, "complied", applicable=True, evidence="x")

    def test_rates_have_separate_denominators_unknowns_and_session_splits(self):
        r1 = self.episode("u1", "session", accepted=True)
        r2 = self.episode("u2", "session", accepted=False)
        self.episode("u3", "another", accepted=True)
        self.store.annotate("a", "u1", r1, "label_other", authorized=True)
        self.store.annotate("b", "u2", r2, "label_addressed", authorized=True)
        report = self.store.report()
        self.assertEqual(report["false_interventions"], {"errors": 1, "denominator": 1, "rate": 1.})
        self.assertEqual(report["missed_addresses"]["rate"], 1.)
        self.assertEqual(report["unknown_outcomes"], 1)
        data = self.store.dataset()
        self.assertEqual(data[0]["split"], data[1]["split"])

    def test_evaluation_rejects_leakage_regressions_and_unmeasured_activation(self):
        before = {"false_interventions": {"rate": .02, "denominator": 100},
                  "missed_addresses": {"rate": .05, "denominator": 100}, "median_first_word_ms": 1000}
        self.store.record_baseline("v1", before, data_version="set1")
        with self.assertRaises(ValueError): self.store.evaluate("v2", "v1", before, before, data_version="set1", training_sessions=["a"], control_sessions=["a"])
        with self.assertRaises(ValueError): self.store.activate("unmeasured")
        self.assertEqual(self.store.evaluate("v2", "v1", before, before, data_version="set1", training_sessions=["a"], control_sessions=["b"]), "evaluated")
        self.store.activate("v2")
        self.assertEqual(self.store.rollback("v2", "misses increased"), "v1")
        after = {**before, "missed_addresses": {"rate": .1, "denominator": 100}}
        self.store.record_baseline("v1", before, data_version="set2")
        self.assertEqual(self.store.evaluate("v3", "v1", before, after, data_version="set2", training_sessions=["a"], control_sessions=["b"]), "rejected")

    def test_repair_does_not_erase_original_missed_address_from_metrics(self):
        revision = self.episode(accepted=False)
        self.store.annotate("fix", "u1", revision, "missed_address", authorized=True)
        self.store.decide("u1", {"accepted": True, "outcome": "replied"})
        report = self.store.report()
        self.assertEqual(report["missed_addresses"]["errors"], 1)
        self.assertEqual(report["missed_addresses"]["denominator"], 1)

    def test_deletion_manifest_clears_restored_private_text(self):
        self.episode()
        self.store.delete("u1")
        self.store.private.execute("INSERT INTO texts VALUES(?,?,?,?,?)", ("u1", "restored", None, 100, 9999999))
        self.store.private.commit()
        self.assertIsNone(self.store.text("u1"))
        self.assertEqual(self.store.dataset(), [])

    def test_private_fragments_and_correction_notes_expire_with_original_episode(self):
        revision = self.episode()
        self.store.save_fragments("u1", [{"id": "m1", "hash": "abc", "snippet": "private-fragment", "path": "private-path"}])
        self.now += 23 * 3600
        self.store.annotate("a", "u1", revision, "stale_memory", corrected="private-fact-fix", authorized=True, binding="memory:m1")
        self.assertIsNone(self.store.text("u1")["corrected"])
        self.now = 100 + 29 * 86400
        self.assertEqual(self.store.fragments("u1")[0]["snippet"], "private-fragment")
        self.assertNotIn(b"private-fragment", self.store.path.read_bytes())
        self.assertNotIn(b"private-fact-fix", self.store.path.read_bytes())
        self.now = 100 + 30 * 86400
        self.assertEqual(self.store.fragments("u1"), [])
        self.assertEqual(self.store.private.execute("SELECT COUNT(*) FROM correction_texts").fetchone()[0], 0)


if __name__ == "__main__": unittest.main()

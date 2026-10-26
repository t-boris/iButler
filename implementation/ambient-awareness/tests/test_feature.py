from __future__ import annotations

import concurrent.futures
import datetime as dt
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient.analysis import LocalAnalyzer, export_observations, validate_facts
from butler_ambient.delivery import Delivery, Telegram, ZONE, advice_candidate, quiet
from butler_ambient.cli import parser
from butler_ambient.enrollment import enroll_live
from butler_ambient.mcp import rpc, serve
from butler_ambient.policy import Evidence, Gate
from butler_ambient.sensing import Batch
from butler_ambient.service import RPCServer, Service
from butler_ambient.store import Store
from butler_ambient.summaries import summarize, summary_text


def facts(activity="work", **extra):
    return validate_facts([{"kind": "activity", "activity": activity, "confidence": .8,
                            "detail": "Private raw words never exported", "subject": "appearance-1",
                            "identity": "Unconfirmed name", "topic": "software development", **extra}])


class Clock:
    def __init__(self):
        self.now = time.time()
    def __call__(self):
        return self.now
    def tick(self, seconds=1):
        self.now += seconds
        return self.now


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.clock = Clock()
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def agreement(self, pid="owner"):
        self.store.execute("INSERT OR REPLACE INTO agreements VALUES (?,?,1,?)", (pid, pid, '{}'))
    def observation(self, start, end, source="camera", activity="work", values=None):
        mid = self.store.media(source, start, end, b"raw", "jpg")
        oid = self.store.save_observation(mid, values or facts(activity))
        return mid, oid


class ParticipantTests(Fixture):
    def setUp(self):
        super().setUp()
        self.agreement()
        self.gate = Gate(self.store)
        self.gate.enable(True, self.clock())
    def check(self, **kw):
        now = self.clock.tick()
        defaults = dict(captured_at=now, camera_ok=True, microphone_ok=True,
                        verified=frozenset({"owner"}), certain=True)
        defaults.update(kw)
        return self.gate.check(Evidence(**defaults), now)
    def test_three_fresh_checks_before_start_and_auto_resumption(self):
        self.assertFalse(self.check())
        self.assertFalse(self.check())
        self.assertTrue(self.check())
        self.assertFalse(self.check(certain=False))
        self.assertFalse(self.check())
        self.assertFalse(self.check())
        self.assertTrue(self.check())
        gaps = self.store.execute("SELECT * FROM events")
        self.assertEqual(len(gaps), 2)
        self.assertTrue(all(x["end"] is not None for x in gaps))
    def test_inferred_name_missing_audio_or_unconsented_person_cannot_authorize(self):
        for kwargs in ({"verified": frozenset({"Inferred owner name"})}, {"microphone_ok": False},
                       {"camera_ok": False}, {"verified": frozenset({"owner", "unknown"})},
                       {"certain": False}, {"verified": frozenset()}):
            for _ in range(3):
                self.assertFalse(self.check(**kwargs))
    def test_owner_pause_is_authoritative(self):
        self.gate.enable(False, self.clock())
        for _ in range(4):
            self.assertFalse(self.check())
    def test_revocation_pauses_even_on_otherwise_valid_evidence(self):
        for _ in range(3): self.check()
        self.store.execute("UPDATE agreements SET active=0")
        self.assertFalse(self.check())
    def test_stale_replayed_future_or_delayed_checks_do_not_authorize(self):
        self.assertFalse(self.check(captured_at=self.clock() - 10))
        self.assertFalse(self.check(captured_at=self.clock() + 10))
        at = self.clock.tick()
        e = Evidence(at, True, True, frozenset({"owner"}), True)
        self.gate.check(e, at)
        self.assertFalse(self.gate.check(e, at + .1))
        for _ in range(3): self.check()
        self.assertFalse(self.gate.permits(self.clock.tick(3)))
        self.assertFalse(self.check())
    def test_restart_never_reuses_permit(self):
        for _ in range(3): self.check()
        other = Gate(self.store)
        self.assertFalse(other.permits(self.clock()))


class RetentionTests(Fixture):
    def test_live_enrollment_retains_only_references_after_explicit_owner_setup(self):
        sample = Batch(0, 1, [b"jpeg"], b"\x00" * 32000)
        with patch("butler_ambient.enrollment.Inputs") as inputs, patch("butler_ambient.enrollment.Verifier") as verifier:
            inputs.return_value.batch.return_value = sample
            verifier.return_value._faces.return_value = [[1]]
            verifier.return_value.enroll.return_value = {"face": [1], "voice": [2]}
            result = enroll_live(self.store, {"face_detector": "local", "face_recognizer": "local"}, "owner", "Boris", 1)
            self.assertFalse(result["reference_media_retained"])
            inputs.return_value.stop.assert_called_once()
        self.assertEqual(list((self.root / "raw").iterdir()), [])
        self.assertEqual(self.store.execute("SELECT id,active FROM agreements"), [{"id": "owner", "active": 1}])
    def test_live_enrollment_refuses_enabled_collection_or_multiple_people(self):
        self.store.set("enabled", True)
        with self.assertRaises(ValueError): enroll_live(self.store, {}, "owner", "Boris")
        self.store.set("enabled", False)
        sample = Batch(0, 1, [b"jpeg"], b"\x00" * 32000)
        with patch("butler_ambient.enrollment.Inputs") as inputs, patch("butler_ambient.enrollment.Verifier") as verifier:
            inputs.return_value.batch.return_value = sample
            verifier.return_value._faces.return_value = [[1], [2]]
            with self.assertRaises(ValueError): enroll_live(self.store, {"face_detector": "local", "face_recognizer": "local"}, "owner", "Boris", 1)
        self.assertEqual(self.store.execute("SELECT id FROM agreements"), [])
    def test_multiple_reference_templates_require_explicit_setup_and_retain_no_media(self):
        sample = Batch(0, 1, [b"jpeg"], b"\x00" * 32000)
        face, voice = Mock(), Mock()
        face.tolist.return_value, voice.tolist.return_value = [1], [2]
        with patch("butler_ambient.enrollment.Inputs") as inputs, patch("butler_ambient.enrollment.Verifier") as verifier:
            inputs.return_value.batch.return_value = sample
            verifier.return_value._faces.return_value = [face]
            verifier.return_value._voices.return_value = [voice]
            verifier.return_value.enroll.return_value = {"face": [1], "voice": [2]}
            enroll_live(self.store, {"face_detector": "local", "face_recognizer": "local"}, "owner", "Boris", 2, multiple_samples=True)
        saved = json.loads(self.store.execute("SELECT reference FROM agreements")[0]["reference"])
        self.assertEqual(saved["face_samples"], [[1], [1]])
        self.assertEqual(saved["voice_samples"], [[2]])
        self.assertEqual(list((self.root / "raw").iterdir()), [])
    def test_all_raw_types_expire_after_24h_regardless_of_analysis_state(self):
        now = self.clock()
        ids = []
        for i, source in enumerate(("camera", "screen", "microphone")):
            mid = self.store.media(source, now - 86400, now - 86399, b"raw", "bin")
            self.store.execute("UPDATE media SET analyzed=? WHERE id=?", (i - 1, mid))
            ids.append(mid)
        self.assertEqual(self.store.expire(now), 3)
        self.assertEqual(list((self.root / "raw").iterdir()), [])
    def test_preservation_is_explicit_separate_and_survives_raw_expiry(self):
        now = self.clock()
        mid = self.store.media("camera", now - 86399, now - 86398, b"raw", "jpg")
        self.assertEqual(list((self.root / "preserved").iterdir()), [])
        self.store.preserve(mid, now)
        self.store.expire(now + 2)
        row = self.store.execute("SELECT * FROM media WHERE id=?", (mid,))[0]
        self.assertIsNone(row["path"])
        self.assertEqual(Path(row["preserved"]).read_bytes(), b"raw")
        self.assertEqual(self.store.preserve(mid, now + 2), mid)
        self.store.delete(mid)
        self.assertEqual(list((self.root / "preserved").iterdir()), [])
    def test_expired_media_cannot_be_resurrected_by_preserve(self):
        mid = self.store.media("screen", self.clock() - 90000, self.clock() - 90000, b"raw", "png")
        with self.assertRaises(ValueError): self.store.preserve(mid)
    def test_derived_results_keep_source_references_after_expiration_and_can_be_deleted(self):
        now = time.time()
        mid, oid = self.observation(now - 90001, now - 90000)
        rows = self.store.history(now - 91000, now)
        self.assertEqual(rows[0]["id"], oid)
        self.assertFalse(rows[0]["original_available"])
        self.assertEqual(rows[0]["source_ids"], [mid])
        self.store.delete(mid, results=True)
        self.assertEqual(self.store.history(now - 91000, now), [])
    def test_identity_correction_applies_to_history_without_rewriting_inferences(self):
        now = self.clock()
        self.observation(now - 20, now - 10)
        self.store.execute("INSERT INTO corrections VALUES ('appearance-1','Boris')")
        row = self.store.history(now - 30, now)[0]
        self.assertEqual(row["facts"][0]["identity"], "Boris")
        self.assertEqual(row["facts"][0]["identity_basis"], "owner-confirmed")
        self.assertIn("Unconfirmed name", self.store.execute("SELECT facts FROM observations")[0]["facts"])
        self.assertEqual(self.store.execute("SELECT * FROM identities"), [])
    def test_directory_and_database_are_private(self):
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "ambient.sqlite3").stat().st_mode & 0o777, 0o600)
    def test_grouped_evidence_preserves_all_original_ids(self):
        now = self.clock()
        ids = [self.store.media("camera", now + i, now + i + 1, b"raw", "mjpeg") for i in range(3)]
        self.store.save_observation(ids[0], facts(), ids)
        row = self.store.history(now - 1, now + 4)[0]
        self.assertEqual(set(row["source_ids"]), set(ids))
        self.assertEqual(row["end"] - row["start"], 3)
        self.store.delete(ids[1], results=True)
        self.assertEqual(self.store.history(now - 1, now + 4), [])


class BoundaryAndSummaryTests(Fixture):
    def test_supported_person_dog_appearance_book_and_activity_facts_remain_inferences(self):
        result = validate_facts([{"kind": kind, "confidence": 0.0} for kind in ("person", "dog", "appearance", "book", "activity", "work-topic")])
        self.assertTrue(all(x["basis"] == "model-inference" for x in result))
        self.assertTrue(all(x["activity"] == "unknown" and x["identity"] == "" for x in result))
    def test_export_excludes_raw_details_and_inferred_names_but_keeps_provenance(self):
        now = self.clock()
        mid, _ = self.observation(now - 10, now - 5)
        output = json.dumps(export_observations(self.store.history(now - 20, now)))
        self.assertNotIn("Private raw words", output)
        self.assertNotIn("Unconfirmed name", output)
        self.assertIn(mid, output)
        self.assertIn("uncertain model inference", output)
        self.assertIn("software development", output)
    def test_facts_reject_raw_fields_nonfinite_confidence_and_unknown_enums(self):
        for item in ({"kind": "activity", "raw_transcript": "secret"},
                     {"kind": "activity", "confidence": float("nan")},
                     {"kind": "activity", "activity": "invented"},
                     {"kind": "activity", "topic": "verbatim secret"},
                     {"kind": "person", "identity": "a\nb"}):
            with self.assertRaises(ValueError): validate_facts([item])
    def test_work_browsing_youtube_durations_no_overlap_or_gap_invention(self):
        now = self.clock()
        self.observation(now - 100, now - 70, "camera", "work")
        self.observation(now - 100, now - 70, "microphone", "work")
        self.observation(now - 60, now - 40, "camera", "browsing")
        self.observation(now - 40, now - 20, "camera", "youtube")
        report = summarize(self.store, now - 100, now)
        self.assertEqual(report["covered_seconds"], 70)
        self.assertEqual(report["approximate_seconds"]["work"], 30)
        self.assertEqual(report["approximate_seconds"]["browsing"], 20)
        self.assertEqual(report["approximate_seconds"]["youtube"], 20)
        self.assertEqual(sum(x["end"] - x["start"] for x in report["gaps"]), 30)
        self.assertIn("software development", summary_text(report))
    def test_screenshot_only_informs_observed_time_and_never_covers_a_gap(self):
        now = self.clock()
        self.observation(now - 100, now - 70, "camera", "unknown")
        self.observation(now - 100, now - 100, "screen", "youtube")
        report = summarize(self.store, now - 100, now)
        self.assertEqual(report["covered_seconds"], 30)
        self.assertEqual(report["approximate_seconds"]["youtube"], 30)
    def test_summary_rejects_invalid_periods(self):
        for start, end in ((1, 0), (float("nan"), 1), (1, float("inf")), (0, 400 * 86400)):
            with self.assertRaises(ValueError): summarize(self.store, start, end)
    def test_analyzer_connects_only_to_numeric_loopback_and_never_follows_remote_redirect(self):
        analyzer = LocalAnalyzer()
        with patch("butler_ambient.analysis.http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value.status = 302
            with self.assertRaises(RuntimeError): analyzer.analyze("screen", images=[b"image"])
        connection.assert_called_once_with("127.0.0.1", 11434, timeout=5)
    def test_local_failure_has_no_external_raw_fallback(self):
        analyzer = LocalAnalyzer()
        with patch("butler_ambient.analysis.http.client.HTTPConnection", side_effect=OSError):
            with self.assertRaises(RuntimeError): analyzer.analyze("camera", images=[b"image"])
    def test_local_ollama_cloud_proxy_is_rejected_before_raw_media_is_sent(self):
        for data in ({"remote_host": "https://ollama.com", "model_info": {"general.architecture": "x"}, "capabilities": ["completion", "vision"]},
                     {"model_info": {"general.architecture": "x"}, "capabilities": ["embedding"]}):
            with patch("butler_ambient.analysis.http.client.HTTPConnection") as connection:
                response = connection.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = json.dumps(data).encode()
                with self.assertRaises(RuntimeError): LocalAnalyzer().analyze("camera", images=[b"private image"])
                calls = connection.return_value.request.call_args_list
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].args[1], "/api/show")
                self.assertNotIn("private image", calls[0].args[2])
        with patch("butler_ambient.analysis.http.client.HTTPConnection") as connection:
            with self.assertRaises(RuntimeError): LocalAnalyzer("model:cloud").analyze("screen", images=[b"private"])
            connection.assert_not_called()
    def test_canceled_analysis_never_starts_model_or_transcriber(self):
        token = threading.Event()
        token.set()
        analyzer = LocalAnalyzer(whisper_model="local")
        with patch("butler_ambient.analysis.subprocess.Popen") as process:
            with self.assertRaises(RuntimeError): analyzer.transcribe(b"wav", token=token)
            process.assert_not_called()


def local_time(day=22, hour=9, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp()


class DeliveryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.transport = Mock()
        self.transport.send.return_value = "receipt"
        self.delivery = Delivery(self.store, self.transport)
    def send(self, key, now, **kw):
        return self.delivery.send(key, "advice", "text", now, topic=kw.get("topic", "work"), created_at=kw.get("created_at", now))
    def test_switched_transport_hands_over_to_the_core_once_and_releases_on_unavailability(self):
        from butler_ambient.delivery import SwitchedTransport
        calls = []
        class Unavailable(ConnectionError): pass
        class Refused(RuntimeError):
            def __init__(self, code): super().__init__(code); self.code = code
        class Module:
            CoreUnavailable, CoreError = Unavailable, Refused
            on, mode = True, "ok"
            @classmethod
            def switched(cls, name): return cls.on and name == "delivery"
            class Client:
                def notify(self, text, idempotency_key, kind="status", persona="butler", **kw):
                    calls.append((text, idempotency_key, kind, persona))
                    if Module.mode == "down": raise Unavailable("down")
                    return {"id": "dl-1", "state": "pending"}
        self.delivery = Delivery(self.store, SwitchedTransport(Module, self.transport))
        now = local_time(hour=9)
        self.assertEqual(self.send("a1", now), "sent")
        self.assertEqual(calls, [("text", "ambient:a1", "advice", "butler")])
        self.assertEqual(self.store.db.execute("SELECT state FROM deliveries WHERE id='a1'").fetchone()[0], "sent:core:dl-1")
        self.transport.send.assert_not_called()
        Module.mode = "down"
        self.assertEqual(self.send("a2", now + 3601), "unavailable")
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM deliveries WHERE id='a2'").fetchone())
        Module.mode, Module.on = "ok", False
        self.assertEqual(self.send("a2", now + 3602), "sent")
        self.transport.send.assert_called_once_with("text", token=None, key="a2", kind="advice")
        self.assertEqual(len(calls), 2)
    def test_quiet_hour_boundaries(self):
        self.assertTrue(quiet(local_time(hour=7, minute=59)))
        self.assertFalse(quiet(local_time(hour=8)))
        self.assertFalse(quiet(local_time(hour=21, minute=59)))
        self.assertTrue(quiet(local_time(hour=22)))
    def test_three_per_local_day_and_hour_spacing_survive_restart(self):
        now = local_time()
        self.assertEqual(self.send("one", now), "sent")
        self.assertEqual(self.send("early", now + 3599), "quota")
        self.delivery = Delivery(self.store, self.transport)
        self.assertEqual(self.send("one", now + 3600), "duplicate")
        self.assertEqual(self.send("two", now + 3601), "sent")
        self.assertEqual(self.send("three", now + 7202), "sent")
        self.assertEqual(self.send("four", now + 10800), "quota")
        self.assertEqual(self.send("next-day", local_time(day=23)), "sent")
    def test_spacing_does_not_reset_at_calendar_midnight(self):
        # Seed an unusual prior reservation near the next permitted window to test
        # that the limiter reads the last timestamp globally, not just today's rows.
        now = local_time(day=23, hour=8)
        self.store.execute("INSERT INTO deliveries VALUES ('previous','advice','2026-10-22',?,'uncertain')", (now - 3599,))
        self.assertEqual(self.send("new", now), "quota")
    def test_mute_topic_controls_and_stale_candidates_do_not_queue_a_burst(self):
        now = local_time()
        self.store.set("muted", True)
        self.assertEqual(self.send("one", now), "muted")
        self.store.set("muted", False)
        self.store.set("disabled_topics", ["work"])
        self.assertEqual(self.send("two", now), "muted")
        self.assertEqual(self.send("old", now, topic="reading", created_at=now - 121), "stale")
        self.assertEqual(self.store.execute("SELECT * FROM deliveries"), [])
    def test_daily_is_excluded_from_advice_quota(self):
        now = local_time()
        for i in range(3): self.send(str(i), now + 3601 * i)
        self.assertEqual(self.delivery.send("daily", "daily", "summary", now + 7203), "sent")
    def test_ambiguous_delivery_is_reserved_and_never_retried(self):
        self.transport.send.side_effect = TimeoutError()
        now = local_time()
        self.assertEqual(self.send("one", now), "uncertain")
        self.delivery = Delivery(self.store, self.transport)
        self.assertEqual(self.send("one", now + 3600), "duplicate")
        self.transport.send.assert_called_once()
    def test_concurrent_services_cannot_both_deliver_same_message(self):
        other = Store(self.root)
        second = Delivery(other, self.transport)
        now = local_time()
        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                results = list(pool.map(lambda d: d.send("same", "advice", "text", now, topic="work", created_at=now), (self.delivery, second)))
            self.assertEqual(sorted(results), ["duplicate", "sent"])
            self.transport.send.assert_called_once()
        finally:
            other.close()
    def test_all_advice_domains_explain_benefit_grounding_and_uncertainty(self):
        now = self.clock()
        topics = set()
        for activity, duration in (("work", 100), ("work", 3601), ("reading", 100), ("youtube", 100)):
            rows = [{"id": activity, "start": now - duration, "end": now, "facts": facts(activity)}]
            candidate = advice_candidate(rows, now)
            topics.add(candidate["topic"])
            for word in ("Expected benefit:", "Evidence:", "uncertain"):
                self.assertIn(word, candidate["text"])
            self.assertNotIn("reduce", candidate["text"].lower())
        self.assertEqual(topics, {"work", "rest", "reading", "entertainment"})
    def test_telegram_uses_only_existing_openclaw_account_and_requires_receipt(self):
        with patch("butler_ambient.delivery.subprocess.Popen") as spawn:
            proc = spawn.return_value
            proc.returncode = 0
            proc.poll.return_value = 0
            proc.communicate.return_value = (b'{"payload":{"messageId":"receipt"}}', None)
            self.assertEqual(Telegram("owner-chat").send("derived summary"), "receipt")
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[:3], ["openclaw", "message", "send"])
            self.assertEqual(argv[argv.index("--account") + 1], "butler")
            self.assertEqual(argv[argv.index("--target") + 1], "owner-chat")
            proc.communicate.return_value = (b'{"ok":true}', None)
            with self.assertRaises(RuntimeError): Telegram("owner-chat").send("derived summary")


class ServiceTests(Fixture):
    def test_screen_permission_setup_never_captures_during_pause(self):
        with patch("butler_ambient.service.subprocess.run") as run:
            run.return_value.stdout = b'{"screen_permission":false}'
            self.assertEqual(self.service.request_screen_permission(), {"screen_permission": False})
            self.assertEqual(run.call_args.args[0][-1], "--request-permission")
        self.service.capture_screen.assert_not_called()
        self.assertFalse(self.service.gate.allowed)
        self.assertEqual(self.store.execute("SELECT * FROM media"), [])

    def setUp(self):
        super().setUp()
        self.agreement()
        self.verifier = Mock()
        self.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, True, True, frozenset({"owner"}), True)
        self.analyzer = Mock()
        self.analyzer.analyze.return_value = facts()
        self.inputs = Mock()
        self.service = Service(self.store, self.inputs, self.verifier, self.analyzer, clock=self.clock, transport=Mock())
        self.service.capture_screen = Mock(return_value=None)
        self.service.control("enabled", {"value": True})
    def tearDown(self):
        self.service.stop()
        super().tearDown()
    def batch(self):
        at = self.clock.tick()
        return Batch(at - 1, at, [b"\xff\xd8frame\xff\xd9"] * 5, b"\x00\x00" * 16000)
    def authorize(self):
        for _ in range(3): self.service.accept_batch(self.batch())
    def test_permission_pause_keeps_zero_media_and_only_anonymous_gaps(self):
        self.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, True, True)
        for _ in range(5): self.service.accept_batch(self.batch())
        self.assertEqual(self.store.execute("SELECT * FROM media"), [])
        self.assertEqual(self.store.execute("SELECT * FROM observations"), [])
        self.assertEqual(list((self.root / "raw").iterdir()), [])
        self.service.capture_screen.assert_not_called()
        self.analyzer.analyze.assert_not_called()
        with self.assertRaises(ValueError): self.service.control("preserve", {"id": "anything"})
        self.assertNotIn('"owner"', json.dumps(self.store.execute("SELECT * FROM events")))
    def test_continuous_verified_media_and_minute_schedule(self):
        self.authorize()
        self.assertEqual(len(self.store.execute("SELECT * FROM media")), 2)
        self.service.screen_worker.submit(lambda: None).result()
        self.assertEqual(self.service.capture_screen.call_count, 1)
        for _ in range(59): self.service.accept_batch(self.batch())
        self.assertEqual(len(self.store.execute("SELECT * FROM media")), 120)
        self.service.screen_worker.submit(lambda: None).result()
        self.assertEqual(self.service.capture_screen.call_count, 1)
        self.service.accept_batch(self.batch())
        self.service.screen_worker.submit(lambda: None).result()
        self.assertEqual(self.service.capture_screen.call_count, 2)
    def test_mcp_cannot_start_capture_while_paused(self):
        result = self.service.observe("camera")
        self.assertEqual(result["status"], "paused")
        self.assertEqual(self.service.requests.qsize(), 0)
        self.service.capture_screen.assert_not_called()
    def test_owner_pause_and_revoke_cancel_analysis_and_require_new_checks(self):
        self.authorize()
        token = self.service.analysis_token
        self.service.control("revoke", {"id": "owner"})
        self.assertTrue(token.is_set())
        self.assertFalse(self.service.gate.allowed)
        self.analyzer.cancel.assert_called()
        for _ in range(3): self.service.accept_batch(self.batch())
        self.assertFalse(self.service.gate.allowed)
        self.service.control("enabled", {"value": False})
        self.assertFalse(self.service.gate.enabled)
    def test_late_analysis_after_permission_change_is_discarded(self):
        self.authorize()
        mid = self.store.execute("SELECT id FROM media WHERE source='camera'")[0]["id"]
        generation = self.service.gate.generation
        def revoke(*args, **kwargs):
            self.service.control("revoke", {"id": "owner"})
            return facts()
        self.analyzer.analyze.side_effect = revoke
        self.assertFalse(self.service.analyze_one(mid, generation))
        self.assertEqual(self.store.execute("SELECT * FROM observations"), [])
    def test_single_video_segment_uses_one_representative_image_for_analysis(self):
        self.authorize()
        row = self.store.execute("SELECT id,path FROM media WHERE source='camera'")[0]
        self.assertEqual(Path(row["path"]).read_bytes().count(b"\xff\xd8"), 5)
        self.assertTrue(self.service.analyze_one(row["id"], self.service.gate.generation))
        self.assertEqual(self.analyzer.analyze.call_args.kwargs["images"], [b"\xff\xd8frame\xff\xd9"])
        self.assertIsNone(self.store.execute('SELECT path FROM media WHERE id=?',(row['id'],))[0]['path'])
    def test_analysis_failure_is_recorded_and_cannot_extend_raw_lifetime(self):
        self.authorize()
        mid = self.store.execute("SELECT id FROM media WHERE source='camera'")[0]["id"]
        self.analyzer.analyze.side_effect = RuntimeError("private raw data")
        self.assertFalse(self.service.analyze_one(mid, self.service.gate.generation))
        events = json.dumps(self.store.execute("SELECT * FROM events"))
        self.assertIn("analysis-failed", events)
        self.assertNotIn("private raw data", events)
        self.store.expire(self.clock.tick(86401))
        self.assertFalse(list((self.root / "raw").iterdir()))
    def test_fresh_camera_request_waits_for_new_verified_frame(self):
        self.authorize()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(self.service.observe, "camera")
            deadline = time.monotonic() + 1
            while self.service.requests.empty() and time.monotonic() < deadline:
                time.sleep(.001)
            self.service.accept_batch(self.batch())
            result = future.result(timeout=2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observations"][0]["captured_at"], self.clock())
        self.assertNotIn("Private raw words", json.dumps(result))
    def test_preferences_reject_quiet_time_and_support_each_topic(self):
        with self.assertRaises(ValueError): self.service.control("preferences", {"daily_time": "22:00"})
        self.service.control("preferences", {"daily_time": "08:00", "disabled_topics": ["work", "rest", "reading", "entertainment"], "muted": True})
        self.assertTrue(self.store.get("muted"))
    def test_maintenance_expires_media_while_paused_and_cannot_transmit(self):
        mid = self.store.media("screen", self.clock() - 90000, self.clock() - 90000, b"raw", "png")
        self.service.maintenance()
        self.assertIsNone(self.store.execute("SELECT path FROM media WHERE id=?", (mid,))[0]["path"])
        self.service.delivery.transport.send.assert_not_called()
    def test_daily_summary_time_mute_exemption_and_restart_deduplication(self):
        self.clock.now = local_time(hour=20)
        self.service.store.set("daily_time", "20:00")
        self.service.store.set("muted", True)
        self.service.delivery.transport.send.return_value = "receipt"
        self.authorize()
        self.service.maintenance()
        self.service.maintenance()
        self.assertEqual(self.service.delivery.transport.send.call_count, 1)
        self.assertEqual(self.store.execute("SELECT kind FROM deliveries"), [{"kind": "daily"}])
        self.service.delivery = Delivery(self.store, self.service.delivery.transport)
        self.service.maintenance()
        self.assertEqual(self.service.delivery.transport.send.call_count, 1)
    def test_biometric_work_does_not_block_owner_revocation(self):
        entered, release = threading.Event(), threading.Event()
        def slow(batch, references):
            entered.set()
            release.wait(2)
            return Evidence(batch.end, True, True, frozenset({"owner"}), True)
        self.verifier.verify.side_effect = slow
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(self.service.accept_batch, self.batch())
            self.assertTrue(entered.wait(1))
            self.service.control("revoke", {"id": "owner"})
            release.set()
            self.assertFalse(result.result(timeout=1))
    def test_minute_analysis_never_bridges_gaps_or_stretches_screenshots(self):
        self.authorize()
        self.service.delivery.transport.send.return_value = "receipt"
        self.store.execute("DELETE FROM media")
        now = self.clock()
        for start, end in ((now - 50, now - 40), (now - 20, now - 10)):
            self.store.media("camera", start, end, b"\xff\xd8frame\xff\xd9", "mjpeg")
        for at in (now - 55, now - 15):
            self.store.media("screen", at, at, b"png", "png")
        self.service.analysis_tick(self.service.gate.generation)
        rows = self.store.history(now - 100, now)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["start"] == r["end"] for r in rows if r["source"] == "screen"))
        report = summarize(self.store, now - 100, now)
        self.assertEqual(report["covered_seconds"], 20)
        self.assertEqual(sum(x["end"] - x["start"] for x in report["gaps"]), 80)
    def test_short_permission_window_does_not_starve_audio_and_screen_behind_camera_groups(self):
        self.authorize()
        self.store.execute("DELETE FROM media")
        now = self.clock()
        for start in (now - 40, now - 10):
            self.store.media("camera", start, start + 1, b"\xff\xd8frame\xff\xd9", "mjpeg")
            self.store.media("microphone", start, start + 1, Batch(start, start + 1, [], b"\x00" * 32000).wav(), "wav")
            self.store.media("screen", start, start, b"png", "png")
        calls = []
        def analyze(source, **kwargs):
            calls.append(source)
            if len(calls) == 4:
                self.service.control("enabled", {"value": False})
            return facts()
        self.analyzer.analyze.side_effect = analyze
        self.service.analysis_tick(self.service.gate.generation)
        self.assertEqual({row["source"] for row in self.store.execute("SELECT source FROM observations")},
                         {"camera", "microphone", "screen"})
    def test_old_untreated_media_are_marked_as_missed_without_catchup_analysis(self):
        self.authorize()
        self.service.delivery.transport.send.return_value = "receipt"
        old = self.store.media("camera", self.clock() - 1000, self.clock() - 999, b"raw", "mjpeg")
        self.service.analysis_tick(self.service.gate.generation)
        row = self.store.execute("SELECT analyzed FROM media WHERE id=?", (old,))[0]
        self.assertEqual(row["analyzed"], -1)
        self.assertIn("analysis-missed", json.dumps(self.store.execute("SELECT * FROM events")))
    def test_prior_inferences_and_owner_context_are_available_without_becoming_canonical(self):
        self.authorize()
        now = self.clock()
        self.observation(now - 10, now - 5)
        self.store.execute("INSERT INTO identities VALUES ('person-boris','Boris','existing appearance context','person')")
        self.store.execute("INSERT INTO corrections VALUES ('appearance-1','Boris')")
        mid = self.store.execute("SELECT id FROM media WHERE source='camera' ORDER BY start DESC")[0]["id"]
        self.assertTrue(self.service.analyze_one(mid, self.service.gate.generation))
        context = self.analyzer.analyze.call_args.kwargs["context"]
        self.assertTrue(any(x.get("basis") == "prior-model-inferences" for x in context))
        self.assertTrue(any(x.get("name") == "Boris" and x["basis"] == "owner-supplied-context" for x in context))
        prior = next(x for x in context if x.get("basis") == "prior-model-inferences")
        self.assertEqual(prior["facts"][0]["identity"], "Boris")
        self.assertEqual(prior["facts"][0]["identity_basis"], "owner-confirmed")
        self.assertEqual(len(self.store.execute("SELECT * FROM identities")), 1)
    def test_screen_helper_uses_only_memory_and_discards_capture_on_permission_change(self):
        self.authorize()
        self.service.screen_worker.submit(lambda: None).result()
        capture = Service.capture_screen.__get__(self.service)
        with patch("butler_ambient.service.subprocess.Popen") as spawn:
            process = spawn.return_value
            process.returncode = 0
            process.poll.return_value = 0
            process.communicate.return_value = (b"\x89PNG\r\n\x1a\nfixture", None)
            mid = capture(self.clock(), self.service.gate.generation)
            self.assertIsNotNone(mid)
            self.assertEqual(spawn.call_args.kwargs["stdout"], -1)
            def pause(**kwargs):
                self.service.control("revoke", {"id": "owner"})
                return (b"\x89PNG\r\n\x1a\nprivate-late", None)
            process.communicate.side_effect = pause
            self.assertIsNone(capture(self.clock(), self.service.gate.generation))
            self.assertFalse(any(b"private-late" in p.read_bytes() for p in (self.root / "raw").iterdir()))
    def test_unavailable_sources_and_recovery_have_explicit_intervals(self):
        self.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, False, True)
        self.service.accept_batch(self.batch())
        self.assertEqual(self.service.status()["sources"]["camera"], "unavailable")
        rows = self.store.execute("SELECT * FROM events WHERE source='camera' AND reason='source-unavailable'")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["end"])
        self.verifier.verify.side_effect = lambda batch, refs: Evidence(batch.end, True, True, frozenset({"owner"}), True)
        self.authorize()
        self.assertEqual(self.service.status()["sources"]["camera"], "active")
        self.assertIsNotNone(self.store.execute("SELECT end FROM events WHERE source='camera'")[0]["end"])
    def test_real_local_rpc_and_mcp_surface_have_no_raw_or_control_tools(self):
        server = RPCServer(self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual((self.root / "ambient.sock").stat().st_mode & 0o777, 0o600)
            result = rpc(self.root, "status")
            self.assertFalse(result["recording_allowed"])
            requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "butler_observe", "arguments": {"source": "camera"}}}]
            writer = io.StringIO()
            serve(self.root, io.StringIO("\n".join(json.dumps(r) for r in requests)), writer)
            replies = [json.loads(line) for line in writer.getvalue().splitlines()]
            self.assertEqual(len(replies), 3)
            names = {t["name"] for t in replies[1]["result"]["tools"]}
            self.assertEqual(names, {"butler_observe", "butler_observation_status", "butler_activity_summary", "butler_visual_request", "butler_display"})
            self.assertNotIn('"type": "image"', writer.getvalue())
            self.assertIn("paused", replies[2]["result"]["content"][0]["text"])
            with self.assertRaises(RuntimeError): RPCServer(self.service)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class MCPProtocolTests(unittest.TestCase):
    def test_owner_enrollment_requires_explicit_prior_agreement(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit): parser().parse_args(["enroll", "owner", "Boris", "--photo", "a", "--voice", "b"])
        args = parser().parse_args(["enroll", "owner", "Boris", "--photo", "a", "--voice", "b", "--prior-agreement"])
        self.assertTrue(args.prior_agreement)
    def test_parse_errors_notifications_and_invalid_tools(self):
        requests = ["not json", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize"}),
                    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "enable_recording"}}),
                    json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "butler_observe", "arguments": {"source": "camera", "raw": True}}})]
        output = io.StringIO()
        call = Mock()
        serve(Path("/unused"), io.StringIO("\n".join(requests)), output, call)
        replies = [json.loads(x) for x in output.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["error"]["code"], -32600)
        self.assertEqual(replies[3]["error"]["code"], -32602)
        self.assertTrue(replies[4]["result"]["isError"])
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()

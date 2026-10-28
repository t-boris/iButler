"""Browsing what Butler concluded, day by day, with the gaps between: local, read-only, facts only."""
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from butler_ambient import mcp
from butler_ambient.local_ui import snapshot
from butler_ambient.observation_browser import MAX_LIMIT, STALLED_AFTER, analysis_state, browse, day_bounds
from butler_ambient.service import Handler, Service
from butler_ambient.store import Store
from butler_ambient.policy import Evidence

def authorize(service):
    now = service.clock()
    service.store.execute("INSERT OR REPLACE INTO agreements VALUES ('owner','Owner',1,'{}')")
    service.gate.enable(True, now-.3)
    for offset in (.2,.1,0):
        evidence = Evidence(now-offset,True,True,frozenset({'owner'}),True,voice_ids=frozenset({'owner'}),voice_certain=True,speech_detected=True)
        service.gate.check(evidence,now-offset)
        service.dialogue_gate.check(evidence,now-offset,True)


CHICAGO = ZoneInfo("America/Chicago")


def at(day, hour, minute=0, second=0):
    year, month, date = map(int, day.split("-"))
    return dt.datetime(year, month, date, hour, minute, second, tzinfo=CHICAGO).timestamp()


NOW = at("2026-10-26", 20)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.service = Service(self.store, Mock(), Mock(), Mock(), clock=lambda: NOW, transport=Mock())
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.service.stop)
        self.store.execute("DELETE FROM events")             # a new Service logs its own open «owner-paused» event; tests add theirs

    def observe(self, source, start, facts, seconds=10):
        media = self.store.media(source, start, start + seconds, b"original", "jpg")
        self.store.save_observation(media, facts)
        return media

    def fact(self, detail, **extra):
        return {"kind": "activity", "basis": "model-inference", "confidence": .8, "activity": "", "topic": "", "detail": detail,
                "subject": "", "identity": "", **extra}

    # -- request validation -----------------------------------------------------------------

    def test_requests_are_validated_exactly(self):
        for args in ({"x": 1}, {"day": "26.10.2026"}, {"day": "2026-13-40"}, {"day": 20261026}, {"source": "radio"},
                     {"empty": 1}, {"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": MAX_LIMIT + 1}, {"limit": "5"},
                     {"offset": 100_001}, [], None):
            with self.subTest(args), self.assertRaises(ValueError):
                browse(self.service, args)
        self.assertEqual(browse(self.service, {})["limit"], 100)
        self.assertEqual(browse(self.service, {"source": "all"})["source"], None)

    # -- days -------------------------------------------------------------------------------

    def test_days_are_local_chicago_days_newest_first_with_counts_and_today_always_listed(self):
        self.observe("camera", at("2026-10-25", 23, 30), [self.fact("поздно вечером")])     # 04:30 UTC on the 26th
        self.observe("camera", at("2026-10-26", 0, 30), [self.fact("после полуночи")])
        self.observe("screen", at("2026-10-26", 9), [])
        self.store.event("microphone", "capture-gap", at("2026-10-24", 12), at("2026-10-24", 12, 0, 1))
        days = browse(self.service, {})["days"]
        self.assertEqual([d["day"] for d in days], ["2026-10-26", "2026-10-25", "2026-10-24"])
        self.assertEqual(days[0], {"day": "2026-10-26", "informative": 1, "empty": 1, "gaps": 0})
        self.assertEqual(days[1], {"day": "2026-10-25", "informative": 1, "empty": 0, "gaps": 0})
        self.assertEqual(days[2], {"day": "2026-10-24", "informative": 0, "empty": 0, "gaps": 1})

    def test_a_day_without_any_data_is_still_offered_and_opens_empty(self):
        self.observe("camera", at("2026-10-25", 12), [self.fact("вчера")])
        view = browse(self.service, {})
        self.assertEqual(view["days"][0], {"day": "2026-10-26", "informative": 0, "empty": 0, "gaps": 0})
        self.assertEqual(view["day"], "2026-10-25")                      # opens on the newest day that has anything
        today = browse(self.service, {"day": "2026-10-26"})
        self.assertEqual((today["items"], today["total"]), ([], 0))

    def test_nothing_stored_at_all_opens_today(self):
        view = browse(self.service, {})
        self.assertEqual((view["day"], view["items"], view["total"]), ("2026-10-26", [], 0))

    def test_day_picker_includes_observations_older_than_sixty_days(self):
        for days_ago in range(75):
            start = NOW - days_ago * 86400
            self.store.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                               (f"obs-{days_ago}", f"media-{days_ago}", "camera", start, start + 1, "[]"))
        days = browse(self.service, {})["days"]
        self.assertEqual(len(days), 75)
        self.assertEqual(days[-1]["day"], "2026-08-13")

    def test_a_dst_day_is_not_24_hours(self):
        start, end = day_bounds("2026-11-01")                            # clocks go back at 02:00
        self.assertEqual((end - start) / 3600, 25)
        start, end = day_bounds("2026-03-08")                            # clocks go forward
        self.assertEqual((end - start) / 3600, 23)

    # -- items ------------------------------------------------------------------------------

    def test_empty_observations_are_hidden_until_asked_and_counted_either_way(self):
        self.observe("camera", at("2026-10-26", 9), [self.fact("за столом")])
        self.observe("camera", at("2026-10-26", 10), [])
        self.observe("microphone", at("2026-10-26", 11), [])
        shown = browse(self.service, {"day": "2026-10-26"})
        self.assertEqual([i["facts"][0]["detail"] for i in shown["items"]], ["за столом"])
        self.assertEqual(shown["summary"]["informative"], 1)
        self.assertEqual(shown["summary"]["empty"], 2)
        everything = browse(self.service, {"day": "2026-10-26", "empty": True})
        self.assertEqual([i["start"] for i in everything["items"]], [at("2026-10-26", 11), at("2026-10-26", 10), at("2026-10-26", 9)])
        self.assertEqual(everything["total"], 3)

    def test_item_carries_facts_source_times_and_whether_the_original_still_exists(self):
        media = self.observe("camera", at("2026-10-26", 9), [self.fact("пьёт кофе", kind="activity", confidence=.9)])
        item = browse(self.service, {"day": "2026-10-26"})["items"][0]
        self.assertEqual((item["type"], item["source"], item["start"], item["end"]), ("observation", "camera", at("2026-10-26", 9), at("2026-10-26", 9, 0, 10)))
        self.assertEqual(item["facts"][0]["detail"], "пьёт кофе")
        self.assertTrue(item["original"])
        self.store.execute("UPDATE media SET path=NULL WHERE id=?", (media,))
        self.assertFalse(browse(self.service, {"day": "2026-10-26"})["items"][0]["original"])

    def test_owner_corrections_are_applied_to_identities(self):
        self.observe("camera", at("2026-10-26", 9), [self.fact("человек у стола", subject="person-1", identity="неизвестный")])
        self.store.execute("INSERT INTO corrections VALUES ('person-1','Таня')")
        fact = browse(self.service, {"day": "2026-10-26"})["items"][0]["facts"][0]
        self.assertEqual((fact["identity"], fact["identity_basis"]), ("Таня", "owner-confirmed"))

    def test_source_filter_applies_to_observations_and_keeps_global_gaps(self):
        self.observe("camera", at("2026-10-26", 9), [self.fact("камера")])
        self.observe("screen", at("2026-10-26", 9, 5), [self.fact("экран")])
        self.store.event("all", "permission-paused", at("2026-10-26", 10), at("2026-10-26", 10, 5))
        self.store.event("microphone", "source-unavailable", at("2026-10-26", 11), at("2026-10-26", 11, 5))
        view = browse(self.service, {"day": "2026-10-26", "source": "screen", "empty": True})
        self.assertEqual([(i["type"], i["source"]) for i in view["items"]], [("gap", "all"), ("observation", "screen")])
        self.assertEqual([g["reason"] for g in view["summary"]["gaps"]], ["permission-paused"])

    def test_paging_is_newest_first_stable_and_bounded(self):
        for minute in range(30):
            self.observe("camera", at("2026-10-26", 9, minute), [self.fact(f"минута {minute}")])
        first = browse(self.service, {"day": "2026-10-26", "limit": 10})
        second = browse(self.service, {"day": "2026-10-26", "limit": 10, "offset": 10})
        last = browse(self.service, {"day": "2026-10-26", "limit": 10, "offset": 25})
        self.assertEqual((first["total"], len(first["items"]), len(second["items"]), len(last["items"])), (30, 10, 10, 5))
        self.assertEqual(first["items"][0]["facts"][0]["detail"], "минута 29")
        self.assertEqual(second["items"][0]["facts"][0]["detail"], "минута 19")
        self.assertEqual({i["id"] for i in first["items"]} & {i["id"] for i in second["items"]}, set())
        self.assertEqual(browse(self.service, {"day": "2026-10-26", "offset": 40})["items"], [])

    # -- gaps -------------------------------------------------------------------------------

    def test_adjoining_gap_events_of_one_reason_become_one_run_and_others_stay_apart(self):
        for second in range(0, 20, 2):                                    # a ragged run of 1-second capture gaps
            self.store.event("microphone", "capture-gap", at("2026-10-26", 9, 0, second), at("2026-10-26", 9, 0, second + 1))
        self.store.event("microphone", "capture-gap", at("2026-10-26", 9, 30), at("2026-10-26", 9, 30, 1))   # far away: its own run
        self.store.event("microphone", "source-unavailable", at("2026-10-26", 9, 0, 4), at("2026-10-26", 9, 0, 5))
        view = browse(self.service, {"day": "2026-10-26", "empty": True})
        gaps = [i for i in view["items"] if i["type"] == "gap"]
        self.assertEqual(len(gaps), 3)
        run = next(g for g in gaps if g["reason"] == "capture-gap" and g["count"] > 1)
        self.assertEqual((run["count"], run["start"], run["end"]), (10, at("2026-10-26", 9), at("2026-10-26", 9, 0, 19)))
        self.assertEqual({g["reason"] for g in gaps}, {"capture-gap", "source-unavailable"})

    def test_pause_runs_merge_across_short_breaks_but_not_long_ones(self):
        self.store.event("all", "permission-paused", at("2026-10-26", 9), at("2026-10-26", 9, 1))
        self.store.event("all", "permission-paused", at("2026-10-26", 9, 2), at("2026-10-26", 9, 3))      # 60 s later: same run
        self.store.event("all", "permission-paused", at("2026-10-26", 9, 30), at("2026-10-26", 9, 31))    # 27 min later: new run
        gaps = [i for i in browse(self.service, {"day": "2026-10-26", "empty": True})["items"] if i["type"] == "gap"]
        self.assertEqual([(g["count"], g["start"]) for g in gaps], [(1, at("2026-10-26", 9, 30)), (2, at("2026-10-26", 9))])

    def test_an_event_still_open_lasts_until_now_and_is_marked_open(self):
        self.store.event("all", "permission-paused", at("2026-10-26", 19, 30), None)
        gap = [i for i in browse(self.service, {"day": "2026-10-26", "empty": True})["items"] if i["type"] == "gap"][0]
        self.assertEqual((gap["end"], gap["open"]), (NOW, True))

    def test_gaps_are_clipped_to_the_day_and_counted_on_both(self):
        self.store.event("analysis", "analysis-busy", at("2026-10-25", 23), at("2026-10-26", 1))
        late = browse(self.service, {"day": "2026-10-25"})["summary"]["gaps"]
        early = browse(self.service, {"day": "2026-10-26"})["summary"]["gaps"]
        self.assertEqual((late[0]["seconds"], early[0]["seconds"]), (3600.0, 3600.0))

    def test_coverage_totals_seconds_and_events_per_reason_largest_first(self):
        self.store.event("all", "permission-paused", at("2026-10-26", 9), at("2026-10-26", 10))
        self.store.event("microphone", "capture-gap", at("2026-10-26", 11), at("2026-10-26", 11, 0, 1))
        self.store.event("microphone", "capture-gap", at("2026-10-26", 12), at("2026-10-26", 12, 0, 1))
        coverage = browse(self.service, {"day": "2026-10-26"})["summary"]["gaps"]
        self.assertEqual([(c["reason"], c["seconds"], c["count"]) for c in coverage], [("permission-paused", 3600.0, 1), ("capture-gap", 2.0, 2)])

    def test_gap_rows_only_appear_in_the_list_when_empty_ones_are_requested(self):
        self.observe("camera", at("2026-10-26", 9), [self.fact("камера")])
        self.store.event("all", "permission-paused", at("2026-10-26", 10), at("2026-10-26", 10, 5))
        self.assertEqual([i["type"] for i in browse(self.service, {"day": "2026-10-26"})["items"]], ["observation"])
        self.assertEqual([i["type"] for i in browse(self.service, {"day": "2026-10-26", "empty": True})["items"]], ["gap", "observation"])

    # -- privacy and side effects ------------------------------------------------------------

    def test_a_pause_hides_key_frame_paths_and_a_missing_file_is_not_offered(self):
        media = self.observe("camera", at("2026-10-26", 9), [self.fact("кадр")])
        frame = Path(self.temp.name) / "preserved-frame.jpg"
        frame.write_bytes(b"jpeg")
        self.store.execute("UPDATE media SET preserved=? WHERE id=?", (str(frame), media))
        self.service.gate.enable(False, NOW)
        self.assertIsNone(browse(self.service, {"day": "2026-10-26"})["items"][0]["frame"])
        self.service.gate.enable(True, NOW)
        self.assertEqual(browse(self.service, {"day": "2026-10-26"})["items"][0]["frame"], str(frame))
        frame.unlink()
        self.assertIsNone(browse(self.service, {"day": "2026-10-26"})["items"][0]["frame"])

    def test_browsing_never_deletes_anything(self):
        old = self.store.media("camera", NOW - 3 * 86400, NOW - 3 * 86400 + 5, b"old original", "jpg")
        path = Path(self.store.execute("SELECT path FROM media WHERE id=?", (old,))[0]["path"])
        before = self.store.execute("SELECT COUNT(*) AS n FROM events")[0]["n"]
        browse(self.service, {})
        browse(self.service, {"day": "2026-10-23", "empty": True})
        self.assertTrue(path.exists())                                   # expire() would have removed it
        self.assertEqual(self.store.execute("SELECT COUNT(*) AS n FROM events")[0]["n"], before)

    def test_the_response_has_no_media_content_or_transcripts(self):
        self.observe("microphone", at("2026-10-26", 9), [self.fact("разговор на кухне")])
        text = json.dumps(browse(self.service, {"day": "2026-10-26", "empty": True}), ensure_ascii=False)
        self.assertNotIn("original", text.replace('"original"', ""))     # no bytes; only the boolean flag exists
        self.assertNotIn("transcript", text)

    # -- analysis state ---------------------------------------------------------------------

    def test_analysis_is_stalled_only_when_originals_wait_and_nothing_new_was_concluded(self):
        self.assertEqual(analysis_state(self.store, NOW), {"last_at": None, "pending": 0, "stalled": False})
        self.store.media("camera", NOW - 5, NOW, b"waiting", "jpg")
        self.assertTrue(analysis_state(self.store, NOW)["stalled"])      # waiting and never concluded anything
        self.observe("camera", NOW - 60, [self.fact("свежее")])
        self.assertFalse(analysis_state(self.store, NOW)["stalled"])
        self.assertTrue(analysis_state(self.store, NOW + STALLED_AFTER + 120)["stalled"])
        self.assertEqual(analysis_state(self.store, NOW, pending=0)["stalled"], False)

    def test_the_local_snapshot_lists_only_informative_recent_rows_and_reports_analysis(self):
        self.observe("camera", NOW - 100, [self.fact("есть вывод")])
        for n in range(6):
            self.observe("microphone", NOW - 50 + n, [])
        authorize(self.service)
        value = snapshot(self.service)
        self.assertEqual([r["details"] for r in value["recent"]], [["есть вывод"]])
        self.assertEqual(set(value["analysis"]), {"last_at", "pending", "stalled"})


class SurfaceTests(unittest.TestCase):
    def test_the_command_is_registered_on_the_local_socket_only(self):
        self.assertNotIn("ui-observations", json.dumps(mcp.TOOLS))
        self.assertNotIn("observation", " ".join(t["name"] for t in mcp.TOOLS if "browse" in t["name"] or "history" in t["name"]))

    def test_handler_runs_the_browser_and_refuses_bad_requests(self):
        def call(args):
            instance = Handler.__new__(Handler)
            instance.connection = Mock()
            instance.rfile = io.BytesIO(json.dumps({"command": "ui-observations", "args": args}).encode() + b"\n")
            instance.wfile = io.BytesIO()
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            store = Store(Path(temp.name))
            self.addCleanup(store.close)
            service = Service(store, Mock(), Mock(), Mock(), clock=lambda: NOW, transport=Mock())
            self.addCleanup(service.stop)
            authorize(service)
            instance.server = SimpleNamespace(service=service)
            instance.handle()
            return json.loads(instance.wfile.getvalue())
        good = call({"day": "2026-10-26"})
        self.assertEqual(good["result"]["day"], "2026-10-26")
        self.assertIn("error", call({"day": "bad"}))
        self.assertIn("error", call({"surprise": True}))


if __name__ == "__main__":
    unittest.main()

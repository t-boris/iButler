"""Dialogue state rules (REQ-029…035, DEC-001): session, silence, scene cues, turn end, interruption classes."""
import unittest

from butler_ambient.dialogue_state import (LipMotion, Scene, SceneAggregate, Session, TurnMetrics, backchannel,
                                           face_geometry, outcome, parse_silence, scene_from, unfinished, unintelligible)


class SessionTests(unittest.TestCase):
    def test_answer_to_butlers_question_is_expected_from_the_same_partner_within_the_window(self):
        session = Session()
        session.touch("owner", 1000.0)
        session.ask("На какое время поставить напоминание?", 1000.0, expects="time")
        self.assertTrue(session.answering("owner", 1030.0))
        self.assertFalse(session.answering("guest", 1030.0))
        self.assertFalse(session.answering("owner", 1070.0))
        session.answered()
        self.assertFalse(session.answering("owner", 1031.0))

    def test_silence_has_a_scope_and_an_end_and_the_name_resumes(self):
        session = Session()
        until, scope, resume = parse_silence("помолчи", 100.0)
        self.assertEqual((until, scope), (700.0, "ten minutes"))
        session.silence(100.0, until, scope, resume)
        self.assertTrue(session.quiet(500.0))
        self.assertFalse(session.quiet(701.0))
        session.resume("name")
        self.assertFalse(session.quiet(500.0))
        self.assertEqual(parse_silence("помолчи на 30 минут", 0.0)[0], 1800.0)
        self.assertEqual(parse_silence("Батлер, помолчи час", 0.0)[0], 3600.0)
        self.assertEqual(parse_silence("не отвечай, пока я на звонке", 0.0)[1], "until the call ends")
        snapshot = session.snapshot(500.0)
        self.assertEqual((snapshot["quiet"], snapshot["partner"]), (False, None))

    def test_session_restarts_after_a_long_gap(self):
        session = Session()
        session.touch("owner", 0.0)
        session.touch("owner", 100.0)
        self.assertEqual(session.started_at, 0.0)
        session.touch("owner", 500.0)
        self.assertEqual(session.started_at, 500.0)
        session.end()
        self.assertFalse(session.active)


def yunet(x, y, w, h, nose_dx, mouth_width):
    """A face row the way YuNet reports it: box, right eye, left eye, nose, right and left mouth corners."""
    eye_y = y + h * 0.35
    return [x, y, w, h, x + w * 0.3, eye_y, x + w * 0.7, eye_y, x + w * 0.5 + nose_dx, y + h * 0.55,
            x + w * 0.5 - mouth_width / 2, y + h * 0.75, x + w * 0.5 + mouth_width / 2, y + h * 0.75, 0.9]


class SceneTests(unittest.TestCase):
    def test_head_turn_and_lip_motion_come_from_the_landmarks(self):
        frontal = face_geometry(yunet(100, 100, 200, 200, nose_dx=0, mouth_width=60))
        turned = face_geometry(yunet(100, 100, 200, 200, nose_dx=45, mouth_width=60))
        self.assertAlmostEqual(frontal["yaw"], 0.0)
        self.assertGreater(abs(turned["yaw"]), 0.35)
        lips = LipMotion()
        for width in (60, 60, 60):
            still = lips.observe("owner", face_geometry(yunet(100, 100, 200, 200, 0, width))["mouth_ratio"])
        self.assertFalse(still)
        for width in (40, 70, 45, 75):
            moving = lips.observe("owner", face_geometry(yunet(100, 100, 200, 200, 0, width))["mouth_ratio"])
        self.assertTrue(moving)

    def test_scene_names_owner_orientation_and_company(self):
        owner = face_geometry(yunet(100, 100, 200, 200, nose_dx=45, mouth_width=60))
        guest = face_geometry(yunet(400, 120, 180, 180, nose_dx=0, mouth_width=50))
        scene = scene_from([owner, guest], {0: "owner", 1: None})
        self.assertEqual((scene.faces, scene.owner_visible, scene.owner_facing, scene.others_present), (2, True, False, True))
        alone = scene_from([face_geometry(yunet(100, 100, 200, 200, 0, 60))], {0: "owner"})
        self.assertEqual((alone.owner_facing, alone.others_present), (True, False))
        self.assertEqual(scene_from([], {}).faces, 0)
        self.assertFalse(Scene().available)
        self.assertEqual(Scene().data()["image_available"], False)

    def test_aggregate_uses_fractions_over_the_utterance(self):
        agg = SceneAggregate()
        agg.add(Scene())                                    # no image: ignored
        for facing in (False, False, True):
            agg.add(Scene(available=True, faces=2, owner_visible=True, owner_facing=facing, owner_talking=True, others_present=True))
        summary = agg.summary()
        self.assertEqual((summary.owner_facing, summary.others_present, summary.owner_talking), (False, True, True))
        self.assertFalse(SceneAggregate().summary().available)


class JudgementTests(unittest.TestCase):
    def test_unfinished_thoughts_and_complete_requests(self):
        for text in ("Напомни мне завтра…", "Напомни мне завтра,", "Напомни мне завтра в", "Позвонить Саше и", "Send it to"):
            self.assertTrue(unfinished(text), text)
        for text in ("Напомни мне завтра позвонить Саше.", "Который час?", "Да", "Поставь будильник на восемь"):
            self.assertFalse(unfinished(text), text)

    def test_backchannel_versus_interruption(self):
        for text in ("угу", "Ага.", "да-да", "понятно", "ок", "продолжай", "uh-huh"):
            self.assertTrue(backchannel(text), text)
        for text in ("Стоп", "Нет, я имел в виду вторник", "Подожди, не то", "да, но почему?"):
            self.assertFalse(backchannel(text), text)

    def test_unintelligible_is_the_name_alone_or_one_short_word(self):
        aliases = ["Бата", "Батлер", "Butler"]
        self.assertTrue(unintelligible("Батлер", aliases))
        self.assertTrue(unintelligible("Батлер, э", aliases))
        self.assertFalse(unintelligible("Батлер, который час?", aliases))
        self.assertFalse(unintelligible("Батлер, выключи", aliases))

    def test_five_outcomes(self):
        self.assertEqual(outcome(addressed=False, understood=True, may_speak=True), "silent")
        self.assertEqual(outcome(addressed=True, understood=False, may_speak=True), "repeat")
        self.assertEqual(outcome(addressed=True, understood=True, may_speak=False), "wait")
        self.assertEqual(outcome(addressed=True, understood=True, may_speak=True, waiting=True), "wait")
        self.assertEqual(outcome(addressed=True, understood=True, may_speak=True, superseded=True), "revise")
        self.assertEqual(outcome(addressed=True, understood=True, may_speak=True), "answer")

    def test_metrics_report_percentiles_and_counts(self):
        metrics = TurnMetrics()
        for latency in (0.2, 0.4, 0.3, 1.0):
            metrics.stopped(latency)
        metrics.record("answer", vision=True)
        metrics.record("silent")
        snapshot = metrics.snapshot()
        self.assertEqual((snapshot["stop_latency_p50"], snapshot["stop_latency_p95"], snapshot["interruptions"]), (0.4, 1.0, 4))
        self.assertEqual((snapshot["outcomes"], snapshot["vision_used"]), ({"answer": 1, "silent": 1}, 1))


if __name__ == "__main__":
    unittest.main()

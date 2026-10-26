from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
NEWS_VIDEO_ROOT = ROOT.parents[0] / "news-video"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(NEWS_VIDEO_ROOT))

from lib import bamberg_news, news_video  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "valid-bamberg-episode.json"


class BambergNewsPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.episode = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_passes_generic_and_bamberg_validation(self) -> None:
        self.assertEqual([], news_video.validate_episode(self.episode, strict=True))
        self.assertEqual([], bamberg_news.validate_bamberg_episode(self.episode))

    def test_ukraine_story_needs_two_independent_sources(self) -> None:
        episode = copy.deepcopy(self.episode)
        story = next(scene for scene in episode["scenes"] if scene["id"] == "ukraine-1")
        story["source_ids"] = ["iaea-ukraine-1"]
        story["editorial"]["evidence"]["method"] = "primary"
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "Ukraine/Russia claim"):
            bamberg_news.validate_bamberg_episode(episode)

    def test_unknown_source_cannot_claim_primary_tier(self) -> None:
        episode = copy.deepcopy(self.episode)
        episode["sources"][0]["url"] = "https://unverified.example/story"
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "cannot self-declare primary"):
            bamberg_news.validate_bamberg_episode(episode)

    def test_garden_advice_requires_dwd_and_garden_authority(self) -> None:
        episode = copy.deepcopy(self.episode)
        story = next(scene for scene in episode["scenes"] if scene["id"] == "garden-1")
        story["source_ids"] = ["lwg-garden-1"]
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "Deutscher Wetterdienst"):
            bamberg_news.validate_bamberg_episode(episode)

    def test_weather_is_mandatory_and_requires_dwd_metadata(self) -> None:
        episode = copy.deepcopy(self.episode)
        episode["coverage"]["category_status"]["weather"]["status"] = "no_verified_update"
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "weather section must be covered"):
            bamberg_news.validate_bamberg_episode(episode)

        episode = copy.deepcopy(self.episode)
        weather = next(scene for scene in episode["scenes"] if scene["id"] == "weather-1")
        weather["editorial"]["weather"]["warnings_checked"] = False
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "warnings_checked"):
            bamberg_news.validate_bamberg_episode(episode)

    def test_health_is_mandatory_and_never_individualised(self) -> None:
        episode = copy.deepcopy(self.episode)
        health = next(scene for scene in episode["scenes"] if scene["id"] == "health-1")
        health["editorial"]["health"]["medication_advice"] = True
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "medication_advice"):
            bamberg_news.validate_bamberg_episode(episode)

    def test_covered_category_cannot_have_no_story(self) -> None:
        episode = copy.deepcopy(self.episode)
        episode["scenes"] = [scene for scene in episode["scenes"] if scene["id"] != "world-1"]
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "world is covered"):
            bamberg_news.validate_bamberg_episode(episode)


class BambergTvFormatTests(unittest.TestCase):
    """Boris 2026-08-01: the edition is watched on a television."""

    def setUp(self) -> None:
        self.episode = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_portrait_frames_are_rejected(self) -> None:
        episode = copy.deepcopy(self.episode)
        episode["format"] = {"width": 1080, "height": 1920, "fps": 30}
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "landscape"):
            bamberg_news.validate_bamberg_episode(episode)

    def test_a_map_must_be_shown_whole(self) -> None:
        episode = copy.deepcopy(self.episode)
        scene = next(scene for scene in episode["scenes"] if scene["kind"] == "story")
        scene["visual"] = {"mode": "broll", "broll_path": "assets/maps/luitpoldstrasse.jpg"}
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "contain"):
            bamberg_news.validate_bamberg_episode(episode)

        scene["visual"]["fit"] = "contain"
        self.assertEqual([], bamberg_news.validate_bamberg_episode(episode))

    def test_a_factual_photo_scene_must_change_its_picture(self) -> None:
        episode = copy.deepcopy(self.episode)
        scene = next(scene for scene in episode["scenes"] if scene["kind"] == "story")
        scene["visual"] = {"mode": "broll", "broll_path": "assets/broll/dom-1.jpg"}
        with self.assertRaisesRegex(bamberg_news.EditorialPolicyError, "picture changes"):
            bamberg_news.validate_bamberg_episode(episode)

        scene["visual"] = {
            "mode": "broll",
            "broll_paths": ["assets/broll/dom-1.jpg", "assets/broll/dom-2.jpg"],
        }
        self.assertEqual([], bamberg_news.validate_bamberg_episode(episode))


class BambergEpisodeTitleTests(unittest.TestCase):
    """Boris 2026-08-01: «в названии видео должна быть дата и время»."""

    def _title_for(self, episode: dict) -> str:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "episode.json"
            path.write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
            return bamberg_news.episode_title(path)

    def test_title_carries_the_bamberg_date_and_time(self) -> None:
        episode = json.loads(FIXTURE.read_text(encoding="utf-8"))
        episode["as_of"] = "2026-08-01T18:20:00+02:00"
        episode["window"]["timezone"] = "Europe/Berlin"
        self.assertEqual(
            "Бамберг: главное к этому часу — 1 августа 2026, 18:20",
            self._title_for(episode),
        )

    def test_stamp_uses_the_episode_timezone_not_the_host(self) -> None:
        episode = json.loads(FIXTURE.read_text(encoding="utf-8"))
        # Same instant, written with a UTC offset: still 06:30 in Bamberg.
        episode["as_of"] = "2026-08-01T04:30:00+00:00"
        episode["window"]["timezone"] = "Europe/Berlin"
        self.assertIn("1 августа 2026, 06:30", self._title_for(episode))

    def test_unusable_timestamp_never_blocks_a_publication(self) -> None:
        episode = json.loads(FIXTURE.read_text(encoding="utf-8"))
        episode["as_of"] = "не дата"
        self.assertEqual("Бамберг: главное к этому часу", self._title_for(episode))

    def test_publish_passes_the_dated_title_to_the_uploader(self) -> None:
        episode = json.loads(FIXTURE.read_text(encoding="utf-8"))
        episode["as_of"] = "2026-08-01T18:20:00+02:00"
        episode["window"]["timezone"] = "Europe/Berlin"
        with tempfile.TemporaryDirectory() as temp:
            runs = Path(temp) / "runs"
            run_dir = runs / episode["episode_id"]
            run_dir.mkdir(parents=True)
            (run_dir / "episode.json").write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(bamberg_news, "runs_root", return_value=runs.resolve()):
                with mock.patch.object(bamberg_news, "_run_youtube", return_value={"status": "dry-run"}) as uploader:
                    bamberg_news.publish_to_youtube(run_dir, approval="boris:test", dry_run=True)
        args = uploader.call_args.args[0]
        self.assertIn("--title", args)
        self.assertEqual(
            "Бамберг: главное к этому часу — 1 августа 2026, 18:20",
            args[args.index("--title") + 1],
        )

    def test_title_stays_within_the_youtube_limit(self) -> None:
        episode = json.loads(FIXTURE.read_text(encoding="utf-8"))
        episode["title"] = "Б" * 95
        episode["as_of"] = "2026-08-01T18:20:00+02:00"
        title = self._title_for(episode)
        self.assertLessEqual(len(title), 100)
        self.assertIn("18:20", title)


class BambergNewsYouTubeTests(unittest.TestCase):
    def test_configure_destination_persists_channel_id_without_a_secret(self) -> None:
        old_path = bamberg_news.os.environ.get("BAMBERG_NEWS_YOUTUBE_DESTINATION")
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "destination.json"
                bamberg_news.os.environ["BAMBERG_NEWS_YOUTUBE_DESTINATION"] = str(path)
                result = bamberg_news.configure_youtube_destination("UC1234567890abcdef")
                self.assertEqual("configured", result["status"])
                self.assertEqual("UC1234567890abcdef", bamberg_news.configured_youtube_channel_id())
        finally:
            if old_path is None:
                bamberg_news.os.environ.pop("BAMBERG_NEWS_YOUTUBE_DESTINATION", None)
            else:
                bamberg_news.os.environ["BAMBERG_NEWS_YOUTUBE_DESTINATION"] = old_path

    def test_destination_check_refuses_wrong_youtube_channel(self) -> None:
        status = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ready": True, "channel": {"channel_id": "wrong", "title": "Other"}}),
            stderr="",
        )
        with mock.patch.object(bamberg_news, "youtube_cli", return_value=Path("/tmp/butler-youtube")):
            with mock.patch.object(Path, "is_file", return_value=True):
                with mock.patch.object(subprocess, "run", return_value=status):
                    with self.assertRaisesRegex(bamberg_news.BambergNewsError, "does not match"):
                        bamberg_news.youtube_status(expected_channel_id="expected")

    def test_publish_validates_before_it_can_call_uploader(self) -> None:
        old_root = bamberg_news.os.environ.get("BAMBERG_NEWS_RUNS")
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "runs"
                run_dir = root / self._episode_id()
                run_dir.mkdir(parents=True)
                (run_dir / "episode.json").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
                bamberg_news.os.environ["BAMBERG_NEWS_RUNS"] = str(root)
                with mock.patch.object(
                    bamberg_news,
                    "_run_youtube",
                    return_value={"status": "dry-run", "title": "preview"},
                ) as uploader:
                    result = bamberg_news.publish_to_youtube(
                        run_dir,
                        approval="test",
                        dry_run=True,
                    )
                uploader.assert_called_once()
                self.assertEqual("bamberg-news/v1", result["editorial_profile"])
        finally:
            if old_root is None:
                bamberg_news.os.environ.pop("BAMBERG_NEWS_RUNS", None)
            else:
                bamberg_news.os.environ["BAMBERG_NEWS_RUNS"] = old_root

    @staticmethod
    def _episode_id() -> str:
        return "bamberg-news-2026-07-19-morning"


class BambergNewsCliTests(unittest.TestCase):
    def test_validate_command(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "bamberg-news"), "validate", str(FIXTURE)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("valid", json.loads(result.stdout)["status"])


if __name__ == "__main__":
    unittest.main()

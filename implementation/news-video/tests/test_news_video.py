from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import news_video


class NewsVideoValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.episode = news_video.make_demo_episode("daily", compact=True)

    def test_narration_script_understands_pause_markers(self) -> None:
        script = news_video._narration_script("Hello.[pause:0.5] Next line.[breath] Done.")
        self.assertEqual(
            [
                {"kind": "text", "text": "Hello."},
                {"kind": "pause", "seconds": 0.5},
                {"kind": "text", "text": "Next line."},
                {"kind": "pause", "seconds": 0.28},
                {"kind": "text", "text": "Done."},
            ],
            script,
        )

    def test_scene_theme_prefers_semantic_topic(self) -> None:
        scene = {
            "headline": "Fed minutes push rates higher",
            "body": "Markets are reacting to the new inflation read.",
            "visual": {"mode": "template"},
        }
        theme = news_video._scene_theme(scene)
        self.assertEqual("Markets / economy", theme["label"])
        self.assertEqual("chart", theme["icon"])

    def test_demo_is_strictly_valid(self) -> None:
        self.assertEqual([], news_video.validate_episode(self.episode, strict=True))

    def test_display_narration_strips_voice_markers(self) -> None:
        self.assertEqual(
            "Первое. Второе — и третье.",
            news_video._display_narration("Первое.[pause:0.28] Второе —[breath] и третье."),
        )

    def test_edge_rate_percent_maps_words_per_minute(self) -> None:
        self.assertEqual("+0%", news_video._edge_rate_percent(175))
        self.assertEqual("+20%", news_video._edge_rate_percent(210))
        self.assertEqual("-50%", news_video._edge_rate_percent(60))
        self.assertEqual("+50%", news_video._edge_rate_percent(400))

    def test_visual_points_must_be_short_strings(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["scenes"][1]["visual"]["points"] = [123]
        with self.assertRaisesRegex(news_video.ValidationError, "visual.points must be an array of strings"):
            news_video.validate_episode(broken, strict=True)
        broken["scenes"][1]["visual"]["points"] = ["x" * 91]
        with self.assertRaisesRegex(news_video.ValidationError, "at most 90 characters"):
            news_video.validate_episode(broken, strict=True)

    def test_visual_stat_requires_compact_value(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["scenes"][1]["visual"]["stat"] = {"label": "no value"}
        with self.assertRaisesRegex(news_video.ValidationError, "non-empty value"):
            news_video.validate_episode(broken, strict=True)
        broken["scenes"][1]["visual"]["stat"] = {"value": "x" * 17}
        with self.assertRaisesRegex(news_video.ValidationError, "at most 16 characters"):
            news_video.validate_episode(broken, strict=True)

    def test_episode_id_is_deterministic(self) -> None:
        first = news_video.deterministic_episode_id(self.episode)
        second = news_video.deterministic_episode_id(copy.deepcopy(self.episode))
        self.assertEqual(first, second)

    def test_factual_scene_requires_source(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["scenes"][1]["source_ids"] = []
        with self.assertRaisesRegex(news_video.ValidationError, "require at least one source"):
            news_video.validate_episode(broken, strict=True)

    def test_confirmed_scene_requires_primary_or_internal_source(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["sources"][0].pop("trust_tier", None)
        with self.assertRaisesRegex(news_video.ValidationError, "primary or internal source"):
            news_video.validate_episode(broken, strict=True)

    def test_unknown_source_is_rejected(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["scenes"][1]["source_ids"] = ["src-missing"]
        with self.assertRaisesRegex(news_video.ValidationError, "unknown source ids"):
            news_video.validate_episode(broken, strict=True)

    def test_veo_is_restricted_to_intro_outro(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["scenes"][1]["visual"] = {
            "mode": "veo",
            "veo_prompt": "Decorative particles, no text",
            "veo_duration": 4,
        }
        with self.assertRaisesRegex(news_video.ValidationError, "restricted to intro/outro"):
            news_video.validate_episode(broken, strict=True)

    def test_asset_cannot_escape_episode_directory(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["scenes"][1]["visual"] = {"mode": "image", "image_path": "../secret.png"}
        with tempfile.TemporaryDirectory() as temp:
            episode_path = Path(temp) / "episode.json"
            episode_path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(news_video.ValidationError, "escapes the episode directory"):
                news_video.validate_episode(broken, episode_path=episode_path, strict=True)

    def test_non_finite_numbers_are_rejected(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["scenes"][1]["duration_hint"] = float("nan")
        with self.assertRaisesRegex(news_video.ValidationError, "non-finite"):
            news_video.validate_episode(broken, strict=True)

    def test_profile_total_narration_limit_is_enforced(self) -> None:
        broken = copy.deepcopy(self.episode)
        broken["scenes"][1]["kind"] = "context"
        broken["scenes"][1]["source_ids"] = []
        broken["scenes"][1]["status"] = "context"
        broken["scenes"][1]["duration_hint"] = 90
        extra = copy.deepcopy(broken["scenes"][1])
        extra["id"] = "context-2"
        extra["headline"] = "Extra context"
        extra["narration"] = "Neutral supporting context."
        extra["duration_hint"] = 90
        extra["source_ids"] = []
        extra["status"] = "context"
        extra["kind"] = "context"
        broken["scenes"].extend([extra, copy.deepcopy(extra)])
        with self.assertRaisesRegex(news_video.ValidationError, "limits estimated total narration"):
            news_video.validate_episode(broken, strict=True)

    def test_veo_plan_is_costed_but_makes_no_call(self) -> None:
        requests = news_video.veo_plan(
            self.episode,
            model="veo-3.1-fast-generate-preview",
            cost_per_second=0.10,
        )
        self.assertEqual(["intro", "outro"], [request.scene_id for request in requests])
        self.assertEqual(0.8, sum(request.estimated_cost_usd for request in requests))

    def test_veo_generation_requires_cost_approval_before_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            episode_path = Path(temp) / "episode.json"
            episode_path.write_text(json.dumps(self.episode), encoding="utf-8")
            with self.assertRaises(news_video.PaidAPIApprovalRequired):
                news_video.generate_veo_assets(
                    episode_path,
                    output_dir=Path(temp) / "run",
                    max_usd=0.0,
                    paid_api_approval=None,
                )

    def test_veo_pricing_uses_resolution_and_rejects_undercut(self) -> None:
        episode = copy.deepcopy(self.episode)
        episode["scenes"][0]["visual"]["veo_duration"] = 8
        episode["scenes"][0]["visual"]["veo_resolution"] = "4k"
        episode["scenes"][2]["visual"] = {"mode": "template"}
        requests = news_video.veo_plan(episode, model="veo-3.1-fast-generate-preview")
        self.assertEqual(2.4, requests[0].estimated_cost_usd)
        with self.assertRaisesRegex(news_video.ValidationError, "below the published"):
            news_video.veo_plan(
                episode,
                model="veo-3.1-fast-generate-preview",
                cost_per_second=0.10,
            )
        with self.assertRaisesRegex(news_video.ValidationError, "finite positive"):
            news_video.veo_plan(episode, cost_per_second=float("nan"))

    def test_nan_paid_cap_is_rejected_before_veo_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            episode_path = Path(temp) / "episode.json"
            episode_path.write_text(json.dumps(self.episode), encoding="utf-8")
            with self.assertRaises(news_video.PaidAPIApprovalRequired):
                news_video.generate_veo_assets(
                    episode_path,
                    output_dir=Path(temp) / "run",
                    max_usd=float("nan"),
                    paid_api_approval="user:test",
                )


class NewsVideoRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._motion_temp = tempfile.TemporaryDirectory()
        self._original_motion_cache = news_video.MOTION_CACHE_DIR
        news_video.MOTION_CACHE_DIR = Path(self._motion_temp.name)

    def tearDown(self) -> None:
        news_video.MOTION_CACHE_DIR = self._original_motion_cache
        self._motion_temp.cleanup()

    def test_motion_style_follows_scene_theme(self) -> None:
        self.assertEqual("ocean", news_video._motion_style({"kind": "outro", "headline": "x"}))
        self.assertEqual("stars", news_video._motion_style({"kind": "finding", "headline": "Открытие в науке о космосе"}))
        self.assertEqual("smoke", news_video._motion_style({"kind": "story", "headline": "Война: новый удар по фронту"}))
        self.assertEqual("aurora", news_video._motion_style({"kind": "story", "headline": "Новая модель GPT вышла"}))

    def test_motion_loop_is_generated_and_cached(self) -> None:
        loop = news_video._ensure_motion_loop("aurora", width=360, height=640, fps=12)
        self.assertTrue(loop.is_file())
        self.assertGreaterEqual(news_video._duration(loop), news_video.MOTION_LOOP_SECONDS - 0.5)
        first_mtime = loop.stat().st_mtime_ns
        again = news_video._ensure_motion_loop("aurora", width=360, height=640, fps=12)
        self.assertEqual(first_mtime, again.stat().st_mtime_ns)
        with self.assertRaisesRegex(news_video.RenderError, "unknown motion style"):
            news_video._ensure_motion_loop("lava", width=360, height=640, fps=12)

    def test_overwrite_refuses_unowned_nonempty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            episode = news_video.make_demo_episode("daily", compact=True)
            episode_path = root / "episode.json"
            episode_path.write_text(json.dumps(episode), encoding="utf-8")
            unrelated = root / "unrelated"
            unrelated.mkdir()
            sentinel = unrelated / "keep.txt"
            sentinel.write_text("do not delete", encoding="utf-8")
            with self.assertRaisesRegex(news_video.RenderError, "refusing to modify"):
                news_video.render_episode(
                    episode_path,
                    output_dir=unrelated,
                    tts="none",
                    overwrite=True,
                )
            self.assertEqual("do not delete", sentinel.read_text(encoding="utf-8"))

    def test_offline_caption_only_render_and_idempotent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            episode = news_video.make_demo_episode("daily", compact=True)
            episode_path = root / "episode.json"
            episode_path.write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
            run_dir = root / "run"
            first = news_video.render_episode(
                episode_path,
                output_dir=run_dir,
                tts="none",
            )
            self.assertEqual("rendered", first["status"])
            video = Path(first["video"])
            self.assertTrue(video.is_file())
            self.assertEqual(episode["episode_id"], json.loads((run_dir / "episode.json").read_text(encoding="utf-8"))["episode_id"])
            self.assertTrue((run_dir / "SUCCESS").is_file())
            self.assertTrue((run_dir / "captions.srt").is_file())
            self.assertTrue((run_dir / "credits.md").is_file())
            streams = first["media"]["streams"]
            video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
            audio_stream = next(stream for stream in streams if stream["codec_type"] == "audio")
            self.assertEqual("h264", video_stream["codec_name"])
            self.assertEqual((360, 640), (video_stream["width"], video_stream["height"]))
            self.assertEqual("aac", audio_stream["codec_name"])
            self.assertGreater(first["duration_seconds"], 3.0)

            second = news_video.render_episode(
                episode_path,
                output_dir=run_dir,
                tts="none",
            )
            self.assertEqual(first["sha256"], second["sha256"])

            changed = copy.deepcopy(episode)
            changed["scenes"][1]["headline"] = "Changed content with the same episode id"
            changed_path = root / "changed.json"
            changed_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(news_video.RenderError, "different content"):
                news_video.render_episode(changed_path, output_dir=run_dir, tts="none")

    def test_overwrite_preserves_generated_veo_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            episode = news_video.make_demo_episode("daily", compact=True)
            episode_path = root / "episode.json"
            episode_path.write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
            run_dir = root / "run"
            first = news_video.render_episode(
                episode_path,
                output_dir=run_dir,
                tts="none",
            )
            video = Path(first["video"])
            assets_dir = run_dir / "assets" / "veo"
            assets_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(video, assets_dir / "intro.mp4")
            shutil.copy2(video, assets_dir / "outro.mp4")

            rerendered = news_video.render_episode(
                episode_path,
                output_dir=run_dir,
                tts="none",
                overwrite=True,
            )
            self.assertTrue((assets_dir / "intro.mp4").is_file())
            self.assertTrue((assets_dir / "outro.mp4").is_file())
            self.assertFalse(any("Veo asset absent" in warning for warning in rerendered["warnings"]))

    def test_diagram_validation_catches_malformed_specs(self) -> None:
        broken = news_video.make_demo_episode("daily", compact=True)
        visual = broken["scenes"][1]["visual"]
        visual["diagram"] = {"type": "sparkline"}
        with self.assertRaisesRegex(news_video.ValidationError, "diagram.type"):
            news_video.validate_episode(broken, strict=True)
        visual["diagram"] = {"type": "bars", "items": [{"label": "one", "value": 1.0}]}
        with self.assertRaisesRegex(news_video.ValidationError, "array of 2-6"):
            news_video.validate_episode(broken, strict=True)
        visual["diagram"] = {"type": "line", "points": [{"label": "a", "value": 1.0}, {"label": "b", "value": "x"}]}
        with self.assertRaisesRegex(news_video.ValidationError, "finite number"):
            news_video.validate_episode(broken, strict=True)
        visual["diagram"] = {"type": "compare", "left": {"title": "A", "items": ["one"]}, "right": {"title": ""}}
        with self.assertRaisesRegex(news_video.ValidationError, "right needs a non-empty title"):
            news_video.validate_episode(broken, strict=True)
        visual["diagram"] = {
            "type": "bars",
            "unit": "%",
            "items": [{"label": "Fable 5", "value": 8}, {"label": "Kimi K3", "value": 6}],
        }
        self.assertEqual([], news_video.validate_episode(broken, strict=True))

    def test_all_diagram_types_render_cards(self) -> None:
        episode = news_video.make_demo_episode("daily", compact=True)
        diagrams = [
            {"type": "bars", "unit": "%", "items": [{"label": "Nvidia", "value": -2.4}, {"label": "Kioxia", "value": -14.0}, {"label": "Micron", "value": -6.1}]},
            {"type": "line", "unit": "%", "points": [{"label": "апр", "value": 74}, {"label": "май", "value": 72}, {"label": "июн", "value": 71}, {"label": "июл", "value": 66}]},
            {"type": "donut", "items": [{"label": "Copilot", "value": 51}, {"label": "Cursor", "value": 27}, {"label": "прочие", "value": 22}]},
            {"type": "timeline", "steps": [{"label": "16 июля", "detail": "релиз Kimi K3"}, {"label": "27 июля", "detail": "обещаны полные веса"}]},
            {"type": "flow", "steps": [{"label": "Эксперимент"}, {"label": "Внедрение"}, {"label": "Измерение"}, {"label": "Фон"}]},
            {"type": "compare", "left": {"title": "Kimi K3", "items": ["терминал", "long-run"]}, "right": {"title": "Fable 5", "items": ["репозитории", "vision"]}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            for index, diagram in enumerate(diagrams):
                scene = copy.deepcopy(episode["scenes"][1])
                scene["visual"]["diagram"] = diagram
                out = Path(temp) / f"card-{diagram['type']}.png"
                news_video.create_scene_card(episode, scene, episode_dir=Path(temp), out_path=out)
                self.assertGreater(out.stat().st_size, 5000, diagram["type"])

    def test_covered_research_collects_reports_from_rendered_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runs_root = Path(temp)
            run_dir = runs_root / "daily-2026-07-17-manual"
            run_dir.mkdir()
            (run_dir / "episode.json").write_text(
                json.dumps(
                    {
                        "coverage": {
                            "inputs_checked": [
                                "News/Daily Digests/2026-07-17-morning.md",
                                "Research/AI Generated Research/2026-07-17-kimi.md",
                                "Daily Notes/2026-07-17.md (раздел Events отсутствует)",
                            ]
                        },
                        "sources": [
                            {"id": "a", "title": "t", "url": "vault://Research/AI Generated Research/2026-07-17-stampcam.md"},
                            {"id": "b", "title": "t", "url": "https://example.com"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (runs_root / "drafts").mkdir()
            (runs_root / "stray.txt").write_text("x", encoding="utf-8")
            covered = news_video.covered_research(days=14, runs_root=runs_root)
            self.assertEqual(
                [
                    "Research/AI Generated Research/2026-07-17-kimi.md",
                    "Research/AI Generated Research/2026-07-17-stampcam.md",
                ],
                covered,
            )
            self.assertEqual([], news_video.covered_research(days=14, runs_root=runs_root / "missing"))

    def test_lecture_profile_allows_long_chaptered_episodes(self) -> None:
        lecture = news_video.make_demo_episode("daily", compact=True)
        lecture["profile"] = "lecture"
        scene = copy.deepcopy(lecture["scenes"][1])
        lecture["scenes"] = [lecture["scenes"][0]]
        for index in range(20):
            chapter = copy.deepcopy(scene)
            chapter["id"] = f"chapter-{index:02d}"
            chapter["headline"] = f"Глава {index + 1}"
            chapter["narration"] = "Разбираем один тезис лекции подробно и с примерами из базы знаний."
            chapter["duration_hint"] = 45
            lecture["scenes"].append(chapter)
        lecture["episode_id"] = news_video.deterministic_episode_id(lecture)
        self.assertEqual([], news_video.validate_episode(lecture, strict=True))
        as_daily = copy.deepcopy(lecture)
        as_daily["profile"] = "daily"
        with self.assertRaisesRegex(news_video.ValidationError, "at most 10 factual scenes"):
            news_video.validate_episode(as_daily, strict=True)

    def test_edge_tts_failure_falls_back_to_macos_voice(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out_path = Path(temp) / "scene.wav"
            scene = {"id": "s1", "narration": "Тестовая фраза.", "duration_hint": 2.0}

            def fake_macos(text: str, path: Path, *, voice: str, rate: int, tempo: float) -> None:
                self.assertEqual(news_video.MACOS_FALLBACK_VOICE, voice)
                self.assertEqual(news_video.TTS_TEMPO, tempo)
                news_video._silence(path, 1.0)

            with mock.patch.object(news_video, "_tts_edge", side_effect=news_video.RenderError("offline")):
                with mock.patch.object(news_video, "_tts_macos", side_effect=fake_macos) as macos:
                    seconds, warnings = news_video.make_scene_audio(
                        scene,
                        out_path,
                        tts="edge",
                        voice=news_video.EDGE_TTS_DEFAULT_VOICE,
                        rate=175,
                        approval=None,
                        max_usd=None,
                    )
            macos.assert_called_once()
            self.assertTrue(out_path.is_file())
            self.assertGreaterEqual(seconds, 2.0)
            self.assertTrue(any("fell back to macOS" in warning for warning in warnings))

    def test_edge_tts_rejects_non_edge_voice_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(news_video.RenderError, "not an edge-tts voice"):
                news_video._tts_edge("Привет.", Path(temp) / "x.wav", voice="Milena", rate=175)

    def test_openai_tts_nan_cap_is_rejected_before_any_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            episode = news_video.make_demo_episode("daily", compact=True)
            episode_path = root / "episode.json"
            episode_path.write_text(json.dumps(episode), encoding="utf-8")
            with mock.patch.object(news_video, "_run") as runner:
                with self.assertRaises(news_video.PaidAPIApprovalRequired):
                    news_video.render_episode(
                        episode_path,
                        output_dir=root / "run",
                        tts="openai",
                        max_usd=float("nan"),
                        paid_api_approval="user:test",
                    )
            runner.assert_not_called()

    def test_publish_is_approval_gated_quiet_hour_safe_and_idempotent(self) -> None:
        original_runs_root = news_video.RUNS_ROOT
        original_staging = news_video.OPENCLAW_MEDIA_STAGING
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                news_video.RUNS_ROOT = root / "runs"
                news_video.OPENCLAW_MEDIA_STAGING = root / "media-staging"
                episode = news_video.make_demo_episode("daily", compact=True)
                episode_path = root / "episode.json"
                episode_path.write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
                run_dir = news_video.RUNS_ROOT / episode["episode_id"]
                news_video.render_episode(episode_path, output_dir=run_dir, tts="none")
                daytime = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc)
                nighttime = dt.datetime(2026, 7, 18, 4, 0, tzinfo=dt.timezone.utc)

                with self.assertRaises(news_video.DeliveryApprovalRequired):
                    news_video.publish_run(run_dir, approval=None, now=daytime)
                with self.assertRaisesRegex(news_video.DeliveryApprovalRequired, "quiet hours"):
                    news_video.publish_run(run_dir, approval="user:test", now=nighttime)
                with self.assertRaisesRegex(news_video.DeliveryApprovalRequired, "approve-delivery"):
                    # The bypass never waives the approval requirement itself.
                    news_video.publish_run(run_dir, approval=None, allow_quiet_hours=True, now=nighttime)

                preview = news_video.publish_run(run_dir, approval=None, dry_run=True, now=nighttime)
                self.assertEqual("dry-run-no-delivery", preview["status"])
                self.assertFalse((run_dir / "publish" / "telegram.json").exists())

                ok = subprocess.CompletedProcess([], 0, stdout='{"ok":true}', stderr="")
                with mock.patch.object(news_video, "_run", side_effect=[ok, ok]) as runner:
                    receipt = news_video.publish_run(
                        run_dir,
                        approval="cron:news-video-morning (Boris scheduled 05:00 delivery)",
                        allow_quiet_hours=True,
                        now=nighttime,
                    )
                self.assertEqual("published", receipt["status"])
                self.assertTrue(receipt["quiet_hours_bypassed"])
                self.assertEqual(2, runner.call_count)

                with mock.patch.object(news_video, "_run") as duplicate_runner:
                    duplicate = news_video.publish_run(
                        run_dir,
                        approval="cron:news-video-morning",
                        now=daytime,
                    )
                self.assertEqual("published", duplicate["status"])
                duplicate_runner.assert_not_called()
                self.assertFalse((root / "media-staging" / episode["episode_id"]).exists())
        finally:
            news_video.RUNS_ROOT = original_runs_root
            news_video.OPENCLAW_MEDIA_STAGING = original_staging


class LandscapeFormatTests(unittest.TestCase):
    """Bamberg News is watched on a TV: 16:9 frame, real-time speech, a media
    window that shows maps whole and changes its picture (Boris 2026-08-01)."""

    def test_format_and_tempo_follow_the_profile(self) -> None:
        self.assertEqual((1080, 1920, 30), news_video.episode_format({"profile": "daily"}))
        self.assertEqual((1920, 1080, 30), news_video.episode_format({"profile": "bamberg-daily"}))
        self.assertEqual(news_video.TTS_TEMPO, news_video.episode_tempo({"profile": "daily"}))
        self.assertEqual(1.0, news_video.episode_tempo({"profile": "bamberg-daily"}))

    def test_episode_overrides_beat_profile_defaults(self) -> None:
        episode = {"profile": "bamberg-daily", "format": {"width": 1280, "height": 720, "fps": 25}}
        self.assertEqual((1280, 720, 25), news_video.episode_format(episode))
        self.assertEqual(1.15, news_video.episode_tempo({"profile": "daily", "audio": {"tempo": 1.15}}))
        # Out of range values fall back to the profile instead of breaking audio.
        self.assertEqual(1.0, news_video.episode_tempo({"profile": "bamberg-daily", "audio": {"tempo": 9}}))

    def test_landscape_blocks_do_not_overlap(self) -> None:
        layout = news_video._card_layout(1920, 1080)
        self.assertTrue(layout.landscape)
        self.assertLess(layout.text_right, layout.stage[0], "text column must clear the media window")
        self.assertLess(layout.stage[3], layout.chips_y, "source chips sit below the media window")
        self.assertLess(layout.chips_y + 40, layout.caption_box[1], "chips must clear the caption band")
        self.assertLess(layout.caption_box[3], layout.footer_y)
        for value in (layout.stage[0], layout.stage[1], layout.stage[2], layout.stage[3]):
            self.assertEqual(0, value % 2, "media window doubles as an encoder frame size")
        wave_x, wave_y, _wave_w, wave_h = news_video._caption_wave_geometry(1920, 1080)
        self.assertGreater(wave_x, layout.caption_box[0])
        self.assertGreater(wave_y, layout.caption_box[1])
        self.assertLess(wave_y + wave_h, layout.caption_box[3])

    def test_landscape_card_renders_at_frame_size(self) -> None:
        from PIL import Image

        episode = news_video.make_demo_episode("bamberg-daily", compact=True)
        self.assertGreater(episode["format"]["width"], episode["format"]["height"])
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "card.png"
            for scene in episode["scenes"]:
                for kwargs in ({"overlay_only": False}, {"overlay_only": True}, {"background": "transparent"}):
                    news_video.create_scene_card(episode, scene, episode_dir=Path(temp), out_path=out, **kwargs)
                    with Image.open(out) as image:
                        self.assertEqual((episode["format"]["width"], episode["format"]["height"]), image.size)

    def test_slide_plan_fills_the_scene_without_flicker(self) -> None:
        slides, seconds = news_video._slide_plan(3, 24.0)
        self.assertEqual(3, slides)
        timeline = slides * seconds - (slides - 1) * news_video.SLIDE_CROSSFADE_SECONDS
        self.assertAlmostEqual(24.0, timeline, places=3)
        self.assertGreaterEqual(seconds, news_video.MIN_SLIDE_SECONDS)
        # A short scene drops slides rather than flashing them.
        self.assertEqual(1, news_video._slide_plan(4, 3.0)[0])
        self.assertEqual(news_video.MAX_SLIDESHOW_IMAGES, news_video._slide_plan(9, 120.0)[0])

    def test_map_keeps_the_window_but_a_decorative_photo_yields_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "assets" / "maps").mkdir(parents=True)
            (root / "assets" / "broll").mkdir(parents=True)
            map_path = root / "assets" / "maps" / "street.jpg"
            photo_path = root / "assets" / "broll" / "city.jpg"
            for path in (map_path, photo_path):
                path.write_bytes(b"stub")

            map_scene = {"id": "m", "visual": {"mode": "broll", "broll_path": "assets/maps/street.jpg"}}
            assets, fit, in_window = news_video._landscape_media_plan(map_scene, root)
            self.assertEqual([map_path], assets)
            self.assertEqual("contain", fit, "a cropped map is useless")
            self.assertTrue(in_window)

            charted = {
                "id": "c",
                "visual": {
                    "mode": "broll",
                    "broll_path": "assets/broll/city.jpg",
                    "diagram": {"type": "bars", "series": []},
                },
            }
            _assets, fit, in_window = news_video._landscape_media_plan(charted, root)
            self.assertEqual("cover", fit)
            self.assertFalse(in_window, "a real diagram outranks a decorative photo")

            # A map keeps its window even next to a diagram.
            charted["visual"]["broll_path"] = "assets/maps/street.jpg"
            _assets, fit, in_window = news_video._landscape_media_plan(charted, root)
            self.assertEqual("contain", fit)
            self.assertTrue(in_window)

    def test_short_panel_never_produces_an_invalid_diagram(self) -> None:
        from PIL import Image, ImageDraw

        canvas = Image.new("RGBA", (900, 400), (0, 0, 0, 255))
        draw = ImageDraw.Draw(canvas, "RGBA")
        weather = {
            "type": "weather",
            "current": {"temperature": 20.4, "condition": "облачно", "humidity": 79, "pressure": 1014, "wind": 7.2},
            "days": [{"label": "Сегодня", "min": 14, "max": 24}, {"label": "Вс", "min": 13, "max": 29}],
        }
        # The landscape side panel is wide but short; this used to raise.
        news_video._draw_diagram(draw, (20, 20, 860, 220), weather, (93, 225, 255), 1.0)

    def test_slideshow_and_fit_are_validated(self) -> None:
        episode = news_video.make_demo_episode("bamberg-daily", compact=True)
        scene = next(scene for scene in episode["scenes"] if scene["kind"] == "story")
        scene["visual"]["mode"] = "broll"
        scene["visual"]["broll_path"] = "a.jpg"
        scene["visual"]["broll_paths"] = ["a.jpg", "b.jpg"]
        with self.assertRaisesRegex(news_video.ValidationError, "not both"):
            news_video.validate_episode(episode, strict=True)

        scene["visual"].pop("broll_path")
        scene["visual"]["broll_paths"] = [f"{index}.jpg" for index in range(5)]
        with self.assertRaisesRegex(news_video.ValidationError, "at most 4 images"):
            news_video.validate_episode(episode, strict=True)

        scene["visual"]["broll_paths"] = ["a.jpg", "b.mp4"]
        with self.assertRaisesRegex(news_video.ValidationError, "stills only"):
            news_video.validate_episode(episode, strict=True)

        scene["visual"]["broll_paths"] = ["a.jpg", "b.jpg"]
        scene["visual"]["fit"] = "stretch"
        with self.assertRaisesRegex(news_video.ValidationError, "visual.fit"):
            news_video.validate_episode(episode, strict=True)

        scene["visual"]["fit"] = "contain"
        episode["audio"] = {"tempo": 4}
        with self.assertRaisesRegex(news_video.ValidationError, "audio.tempo"):
            news_video.validate_episode(episode, strict=True)


if __name__ == "__main__":
    unittest.main()

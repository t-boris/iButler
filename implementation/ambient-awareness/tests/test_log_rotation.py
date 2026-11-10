"""BUG-027: the dialogue Ollama logs (written by a launchd job) grew without a limit: 9.4 MB and 4 MB in 16 days.

launchd keeps the file open for the whole life of the job, so a rotation cannot rename it: the job would keep writing to the
renamed file. The file is copied to a numbered generation and then truncated in place. launchd opens its output files for
appending (checked with a throwaway job on 2026-11-10), so the job's next write lands at the start of the emptied file.
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient import logfiles
from butler_ambient.service import Service
from butler_ambient.store import Store


class RotateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.log = self.dir / "dialogue-ollama.log"

    def test_a_log_below_the_cap_is_left_alone(self):
        self.log.write_text("x" * 100)
        self.assertFalse(logfiles.rotate(self.log, max_bytes=100))
        self.assertEqual(self.log.read_text(), "x" * 100)
        self.assertFalse((self.dir / "dialogue-ollama.log.1").exists())

    def test_a_missing_log_is_not_an_error(self):
        self.assertFalse(logfiles.rotate(self.log, max_bytes=10))

    def test_a_log_over_the_cap_moves_to_generation_one_and_is_emptied(self):
        self.log.write_text("a" * 101)
        self.assertTrue(logfiles.rotate(self.log, max_bytes=100))
        self.assertEqual((self.dir / "dialogue-ollama.log.1").read_text(), "a" * 101)
        self.assertEqual(self.log.stat().st_size, 0)

    def test_generations_shift_and_the_oldest_is_dropped(self):
        for letter in "abc":
            self.log.write_text(letter * 20)
            logfiles.rotate(self.log, max_bytes=10, keep=2)
        self.assertEqual((self.dir / "dialogue-ollama.log.1").read_text(), "c" * 20)
        self.assertEqual((self.dir / "dialogue-ollama.log.2").read_text(), "b" * 20)
        self.assertFalse((self.dir / "dialogue-ollama.log.3").exists())

    def test_the_writer_that_holds_the_file_open_keeps_writing_at_its_start(self):
        with open(self.log, "ab", buffering=0) as writer:               # what launchd hands the job as stdout
            writer.write(b"old line\n" * 20)
            self.assertTrue(logfiles.rotate(self.log, max_bytes=50))
            writer.write(b"new line\n")
        self.assertEqual(self.log.read_bytes(), b"new line\n")           # no gap of NUL bytes
        self.assertEqual((self.dir / "dialogue-ollama.log.1").read_bytes(), b"old line\n" * 20)

    def test_nothing_is_emptied_when_the_copy_fails(self):
        self.log.write_text("keep me" * 20)
        with patch.object(logfiles.shutil, "copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): logfiles.rotate(self.log, max_bytes=10)
        self.assertEqual(self.log.read_text(), "keep me" * 20)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["dialogue-ollama.log"])   # no half-written generation

    def test_the_generation_is_no_more_readable_than_the_log(self):
        self.log.write_text("x" * 20)
        self.log.chmod(0o600)
        logfiles.rotate(self.log, max_bytes=10)
        self.assertEqual(stat.S_IMODE((self.dir / "dialogue-ollama.log.1").stat().st_mode), 0o600)

    def test_the_default_cap_is_read_when_called_so_it_can_be_changed(self):
        self.log.write_text("x" * 20)
        with patch.object(logfiles, "MAX_BYTES", 10):
            self.assertTrue(logfiles.rotate(self.log))

    def test_only_the_launchd_owned_logs_are_rotated(self):
        for name in (*logfiles.LAUNCHD_LOGS, "launcher.log", "service.log"):
            (self.dir / name).write_text("x" * 20)
        with patch.object(logfiles, "MAX_BYTES", 10):
            rotated = logfiles.rotate_all(self.dir)
        self.assertEqual(sorted(rotated), sorted(logfiles.LAUNCHD_LOGS))
        self.assertEqual((self.dir / "launcher.log").read_text(), "x" * 20)    # not launchd-held by this module
        self.assertEqual((self.dir / "service.log").read_text(), "x" * 20)

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(logfiles.rotate_all(self.dir / "absent"), [])


class ServiceRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.service = Service(self.store, Mock(), Mock(), Mock(), clock=lambda: 100., transport=Mock())
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.service.stop)
        self.logs = self.store.root / "logs"
        self.logs.mkdir(exist_ok=True)
        self.big = self.logs / "dialogue-ollama.log"
        self.big.write_text("x" * 20)
        cap = patch.object(logfiles, "MAX_BYTES", 10)
        cap.start()
        self.addCleanup(cap.stop)

    def run_loop(self, iterations):
        """Run the maintenance loop for a few iterations without sleeping."""
        passes = []

        class Stop:
            def is_set(stop): return len(passes) >= iterations
            def wait(stop, seconds): pass
            def set(stop): pass
        self.service.stop_event = Stop()
        self.service.maintenance = lambda: passes.append(1)
        self.service._maintenance_loop()

    def test_the_maintenance_loop_rotates_an_oversized_log(self):
        self.run_loop(1)
        self.assertEqual((self.logs / "dialogue-ollama.log.1").read_text(), "x" * 20)
        self.assertEqual(self.big.stat().st_size, 0)

    def test_the_size_is_checked_every_few_minutes_not_every_second(self):
        with patch.object(logfiles, "rotate_all", wraps=logfiles.rotate_all) as rotate_all:
            self.run_loop(5)
        self.assertEqual(rotate_all.call_count, 1)

    def test_a_failure_is_recorded_and_does_not_stop_maintenance(self):
        with patch.object(logfiles, "rotate_all", side_effect=OSError("disk full")):
            self.run_loop(2)
        reasons = [row["reason"] for row in self.store.execute("SELECT reason FROM events WHERE source='service'")]
        self.assertEqual(reasons, ["log-rotation-failed"])


if __name__ == "__main__":
    unittest.main()

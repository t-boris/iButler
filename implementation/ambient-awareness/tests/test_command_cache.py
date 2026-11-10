"""BUG-023: the panel polled `openclaw cron list` and `openclaw models status` every 11-16 s, about 6,700 CLI starts a day.

Each start loads the whole CLI and logs a line in the gateway log, so the few lines that matter were pushed out of the
two-day retention. The listing reads are now served from a short cache; anything else the service runs through the same
function (a pause, a run, a model change) drops the cache first, so the panel never shows state older than its own action.
"""
import json
import stat
import tempfile
import unittest
from pathlib import Path

from butler_ambient import components


class CommandCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.0
        original = components.CLOCK
        components.CLOCK = lambda: self.now
        self.addCleanup(setattr, components, "CLOCK", original)
        components.forget_cached_reads()
        self.cli = self.fake("cli")

    def fake(self, name, exit_code=0):
        """A stand-in CLI that counts its starts in <name>.calls and answers with that count."""
        path = self.root / name
        path.write_text(f"#!/bin/sh\necho \"$@\" >> '{path}.calls'\n"
                        f"printf '{{\"start\": %s}}' \"$(wc -l < '{path}.calls' | tr -d ' ')\"\nexit {exit_code}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def starts(self, cli=None):
        calls = Path((cli or self.cli) + ".calls")
        return len(calls.read_text().splitlines()) if calls.exists() else 0

    def test_repeated_cron_listings_start_one_process(self):
        first = components.command(self.cli, "cron", "list", "--agent", "butler", "--all", "--json")
        for _ in range(8):
            self.now += 14                                              # the panel's poll interval
            self.assertEqual(components.command(self.cli, "cron", "list", "--agent", "butler", "--all", "--json"), first)
        self.assertEqual(self.starts(), 1)

    def test_the_listing_is_refreshed_after_its_lifetime(self):
        components.command(self.cli, "cron", "list", "--json")
        self.now += components.CRON_LIST_TTL + 1
        self.assertEqual(json.loads(components.command(self.cli, "cron", "list", "--json")), {"start": 2})
        self.assertEqual(self.starts(), 2)

    def test_models_status_is_cached_for_longer_than_the_cron_listing(self):
        components.command(self.cli, "models", "--agent", "butler", "status", "--json")
        self.now += components.CRON_LIST_TTL + 1
        components.command(self.cli, "models", "--agent", "butler", "status", "--json")
        self.assertEqual(self.starts(), 1)
        self.now += components.MODELS_STATUS_TTL
        components.command(self.cli, "models", "--agent", "butler", "status", "--json")
        self.assertEqual(self.starts(), 2)

    def test_any_other_command_drops_the_cached_reads(self):
        components.command(self.cli, "cron", "list", "--json")
        components.command(self.cli, "models", "--agent", "butler", "status", "--json")
        components.command(self.cli, "cron", "disable", "job-1", "--timeout", "8000")     # a control action
        self.assertEqual(json.loads(components.command(self.cli, "cron", "list", "--json")), {"start": 4})
        components.command(self.cli, "models", "--agent", "butler", "status", "--json")
        self.assertEqual(self.starts(), 5)

    def test_a_failed_write_still_drops_the_cache(self):
        components.command(self.cli, "cron", "list", "--json")
        broken = self.fake("broken", exit_code=1)
        with self.assertRaises(ValueError):
            components.command(broken, "cron", "disable", "job-1")
        components.command(self.cli, "cron", "list", "--json")
        self.assertEqual(self.starts(), 2)

    def test_a_failed_read_is_not_cached(self):
        broken = self.fake("down", exit_code=1)
        for _ in range(2):
            with self.assertRaises(ValueError):
                components.command(broken, "cron", "list", "--json")
        self.assertEqual(self.starts(broken), 2)                         # the next poll tries again

    def test_different_executables_do_not_share_an_answer(self):
        other = self.fake("other")
        components.command(self.cli, "cron", "list", "--json")
        components.command(other, "cron", "list", "--json")
        self.assertEqual((self.starts(), self.starts(other)), (1, 1))

    def test_a_read_that_was_running_during_a_write_is_not_kept(self):
        # The listing starts, a control action lands, the listing then finishes: its answer predates the action.
        calls = []
        real_run = components._run

        def slow_run(cli, *args):
            text = real_run(cli, *args)
            if not calls:
                calls.append(1)
                components.command(self.cli, "cron", "disable", "job-1")   # the write lands while the read is running
            return text

        components._run = slow_run
        self.addCleanup(setattr, components, "_run", real_run)
        components.command(self.cli, "cron", "list", "--json")
        components._run = real_run
        components.command(self.cli, "cron", "list", "--json")
        self.assertEqual(self.starts(), 3)                                # list, disable, list again


if __name__ == "__main__":
    unittest.main()

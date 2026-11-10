"""BUG-016: a Telegram channel that stays `stopped` is noticed within minutes, reported, and healed by a gateway restart.

On 2026-11-09 the account tetyana-dyck sat in `stopped` for 14 h 40 min: the gateway's own health monitor logged
"restarting (reason: stopped)" 76 times without starting it, and only a manual `openclaw gateway restart` healed it.
The owner's rule (2026-11-10): after 10 minutes tell the owner and restart the gateway, but only while no cron job runs
and at most once per hour.
"""
import json

from butler_core.adapters import gateway
from butler_core.channels import ChannelWatchdog
from support import CoreCase


def account(channel, ident, running=True, enabled=True, configured=True):
    return {"channel": channel, "accountId": ident, "enabled": enabled, "configured": configured, "running": running}


def snapshot(*accounts):
    grouped = {}
    for item in accounts:
        row = {k: v for k, v in item.items() if k != "channel"}
        grouped.setdefault(item["channel"], []).append(row)
    return {"channelAccounts": grouped}


class FakeGateway:
    def __init__(self):
        self.accounts = [account("telegram", "butler"), account("telegram", "tetyana-dyck"), account("whatsapp", "default")]
        self.down = False
        self.jobs = []
        self.restarts = 0
        self.heals = True
        self.restart_ok = True

    def status(self):
        if self.down:
            raise gateway.GatewayUnavailable("gateway closed before ready")
        return snapshot(*self.accounts)

    def running_jobs(self):
        if isinstance(self.jobs, Exception):
            raise self.jobs
        return list(self.jobs)

    def restart(self):
        self.restarts += 1
        if self.restart_ok and self.heals:
            for item in self.accounts:
                item["running"] = True
        return {"ok": self.restart_ok, "detail": "restarted" if self.restart_ok else "openclaw exit 1"}

    def stop(self, ident):
        for item in self.accounts:
            if item["accountId"] == ident:
                item["running"] = False


class WatchdogCase(CoreCase):
    def setUp(self):
        super().setUp()
        self.clock.now = 1_800_000_000.0
        self.gateway = FakeGateway()
        self.dog = ChannelWatchdog(self.core, control=self.gateway)

    def tick(self, seconds=0):
        self.clock.now += seconds
        return self.dog.tick()

    def alerts(self):
        return self.core.store.query("SELECT * FROM deliveries WHERE idempotency_key LIKE 'channel-watchdog:%'"
                                     " ORDER BY created_at, idempotency_key")


class DetectionTest(WatchdogCase):
    def test_healthy_channels_cause_no_message_and_no_restart(self):
        for _ in range(5):
            self.tick(120)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.gateway.restarts, 0)

    def test_a_blip_shorter_than_ten_minutes_is_ignored(self):
        self.gateway.stop("tetyana-dyck")
        self.tick()
        self.tick(300)
        self.gateway.accounts[1]["running"] = True
        self.tick(120)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.gateway.restarts, 0)

    def test_disabled_or_unconfigured_accounts_are_not_failures(self):
        self.gateway.accounts.append(account("telegram", "spare", running=False, enabled=False))
        self.gateway.accounts.append(account("telegram", "new", running=False, configured=False))
        self.tick()
        self.tick(1200)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.gateway.restarts, 0)

    def test_a_channel_stopped_for_ten_minutes_is_reported_and_the_gateway_restarted(self):
        self.gateway.stop("tetyana-dyck")
        self.tick()
        self.tick(540)
        self.assertEqual((self.alerts(), self.gateway.restarts), ([], 0))      # 9 minutes: still waiting
        outcome = self.tick(60)
        (alert,) = self.alerts()
        self.assertEqual(alert["kind"], "alert")
        self.assertIn("telegram:tetyana-dyck", alert["content"])
        self.assertIn("10 мин", alert["content"])
        self.assertIn("Перезапускаю gateway", alert["content"])
        self.assertEqual(self.gateway.restarts, 1)
        self.assertEqual(outcome["restarted"], True)

    def test_whatsapp_counts_too(self):
        self.gateway.accounts[2]["running"] = False
        self.tick()
        self.tick(600)
        self.assertIn("whatsapp:default", self.alerts()[0]["content"])


class RestartRuleTest(WatchdogCase):
    def stuck(self):
        self.gateway.heals = False
        self.gateway.stop("tetyana-dyck")
        self.tick()
        return self.tick(600)

    def test_no_restart_while_a_cron_job_is_running_but_the_owner_is_told_why(self):
        self.gateway.jobs = ["bamberg-news"]
        outcome = self.stuck()
        self.assertEqual(self.gateway.restarts, 0)
        self.assertEqual(outcome["restarted"], False)
        (alert,) = self.alerts()
        self.assertIn("cron-задача", alert["content"])
        self.assertNotIn("Перезапускаю", alert["content"])

    def test_the_restart_happens_on_the_first_tick_after_the_job_ends(self):
        self.gateway.jobs = ["bamberg-news"]
        self.stuck()
        self.gateway.jobs = []
        self.tick(120)
        self.assertEqual(self.gateway.restarts, 1)
        self.assertEqual(len(self.alerts()), 2)                  # the alert, then the restart notice

    def test_an_unreadable_cron_state_blocks_the_restart(self):
        self.gateway.jobs = gateway.GatewayUnavailable("cron state unreadable")
        self.stuck()
        self.assertEqual(self.gateway.restarts, 0)

    def test_at_most_one_restart_per_hour(self):
        self.stuck()
        self.assertEqual(self.gateway.restarts, 1)
        for _ in range(20):                                       # 40 minutes of ticks, the channel never heals
            self.tick(120)
        self.assertEqual(self.gateway.restarts, 1)
        self.tick(1200)
        self.assertEqual(self.gateway.restarts, 2)

    def test_a_failed_restart_command_counts_as_an_attempt(self):
        self.gateway.restart_ok = False
        self.stuck()
        self.tick(120)
        self.assertEqual(self.gateway.restarts, 1)

    def test_the_restart_can_be_switched_off_in_the_config(self):
        content = json.loads(json.dumps(self.core.config.active()[1]))
        content["channel_watchdog"] = {"restart": False}
        self.activate(content)
        self.stuck()
        self.assertEqual(self.gateway.restarts, 0)
        (alert,) = self.alerts()
        self.assertIn("автоматический перезапуск выключен", alert["content"])


class ReportingTest(WatchdogCase):
    def test_each_incident_is_announced_once_not_every_two_minutes(self):
        self.gateway.heals = False
        self.gateway.stop("tetyana-dyck")
        self.tick()
        self.tick(600)
        for _ in range(10):
            self.tick(120)
        keys = [row["idempotency_key"] for row in self.alerts()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(sum("не помог" in row["content"] for row in self.alerts()), 1)    # one follow-up per restart

    def test_recovery_after_the_restart_is_reported_with_the_downtime(self):
        self.gateway.stop("tetyana-dyck")
        self.tick()
        self.tick(600)
        self.tick(120)
        texts = [row["content"] for row in self.alerts()]
        self.assertEqual(len(texts), 2)
        self.assertIn("снова работает", texts[1])
        self.assertIn("12 мин", texts[1])

    def test_recovery_without_an_alert_is_silent(self):
        self.gateway.stop("tetyana-dyck")
        self.tick()
        self.tick(120)
        self.gateway.accounts[1]["running"] = True
        self.tick(120)
        self.assertEqual(self.alerts(), [])

    def test_a_dead_gateway_is_reported_after_ten_minutes_without_a_restart_attempt(self):
        self.gateway.down = True
        self.tick()
        self.tick(600)
        (alert,) = self.alerts()
        self.assertIn("Gateway не отвечает", alert["content"])
        self.assertEqual(self.gateway.restarts, 0)
        self.tick(600)
        self.assertEqual(len(self.alerts()), 1)                   # not repeated
        self.gateway.down = False
        self.tick(120)
        self.assertIn("Gateway снова отвечает", self.alerts()[-1]["content"])

    def test_an_alarm_is_raised_while_the_channel_is_stuck_and_cleared_after(self):
        self.gateway.heals = False
        self.gateway.stop("tetyana-dyck")
        self.tick()
        self.tick(600)
        row = self.core.store.one("SELECT cleared_at FROM alarms WHERE key=?", ("channel:telegram:tetyana-dyck",))
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])
        self.gateway.accounts[1]["running"] = True
        self.tick(120)
        row = self.core.store.one("SELECT cleared_at FROM alarms WHERE key=?", ("channel:telegram:tetyana-dyck",))
        self.assertIsNotNone(row[0])

    def test_the_state_survives_a_core_restart(self):
        self.gateway.heals = False
        self.gateway.stop("tetyana-dyck")
        self.tick()
        self.tick(400)
        again = ChannelWatchdog(self.core, control=self.gateway)          # a new instance reads the stored state
        self.clock.now += 200
        again.tick()
        self.assertEqual(self.gateway.restarts, 1)


class ConfigTest(CoreCase):
    def test_the_section_is_validated(self):
        from butler_core.config import ConfigError, validate_content
        from support import base_config
        good = base_config()
        good["channel_watchdog"] = {"restart": True, "grace_minutes": 5, "cooldown_minutes": 30}
        validate_content(good)
        for bad in ({"restart": "yes"}, {"grace_minutes": 0}, {"cooldown_minutes": 5000}, "off"):
            broken = base_config()
            broken["channel_watchdog"] = bad
            with self.assertRaises(ConfigError):
                validate_content(broken)


class GatewayControlTest(CoreCase):
    class Result:
        def __init__(self, code=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    def control(self, result=None, error=None, state=None):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if error:
                raise error
            return result
        path = self.tmp / "jobs-state.json"
        if state is not None:
            path.write_text(state)
        return gateway.GatewayControl(executable="openclaw", runner=runner, cron_state=path), calls

    def test_status_parses_json_that_follows_progress_lines(self):
        control, calls = self.control(self.Result(out="Checking channel status…\n" + json.dumps(snapshot(account("telegram", "a")))))
        self.assertEqual(control.status()["channelAccounts"]["telegram"][0]["accountId"], "a")
        self.assertEqual(calls[0], ["openclaw", "channels", "status", "--json"])

    def test_status_failures_are_one_exception(self):
        for result, error in ((self.Result(code=1, err="gateway closed"), None), (self.Result(out="not json"), None),
                              (None, FileNotFoundError())):
            control, _ = self.control(result, error)
            with self.assertRaises(gateway.GatewayUnavailable):
                control.status()

    def test_restart_reports_instead_of_raising(self):
        control, calls = self.control(self.Result())
        self.assertEqual(control.restart()["ok"], True)
        self.assertEqual(calls[0], ["openclaw", "gateway", "restart"])
        control, _ = self.control(self.Result(code=1))
        self.assertEqual(control.restart()["ok"], False)

    def test_running_jobs_reads_runningAtMs_wherever_it_is(self):
        state = json.dumps({"jobs": {"a1": {"state": {"runningAtMs": 5}}, "b2": {"state": {"runningAtMs": None}}}})
        control, _ = self.control(state=state)
        self.assertEqual(control.running_jobs(), ["a1"])            # the job id, not the `state` key
        control, _ = self.control(state=json.dumps({"a1": {"runningAtMs": 5}, "b2": {"lastRunAtMs": 4}}))
        self.assertEqual(control.running_jobs(), ["a1"])

    def test_missing_state_means_nothing_runs_and_broken_state_is_an_error(self):
        control, _ = self.control()
        self.assertEqual(control.running_jobs(), [])
        control, _ = self.control(state="{broken")
        with self.assertRaises(gateway.GatewayUnavailable):
            control.running_jobs()


if __name__ == "__main__":
    import unittest
    unittest.main()

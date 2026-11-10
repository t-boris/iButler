"""Channel watchdog (BUG-016): a stopped chat channel is reported and healed, not left deaf for hours.

The gateway's own health monitor logs "restarting (reason: stopped)" every few minutes yet cannot start a channel that
went to ``stopped`` after a network blip; on 2026-11-09 the Telegram account tetyana-dyck was deaf for 14 h 40 min and
only a manual ``openclaw gateway restart`` healed it. This watchdog polls the channel status from the outside.

Rule (the owner's, 2026-11-10):

* an enabled, configured account that is not running for ``grace_minutes`` (10) is reported to the owner;
* the gateway is then restarted, but only while no cron job is running (a restart kills it) and at most once per
  ``cooldown_minutes`` (60); a postponed restart says why in the same message, and the restart happens on the first poll
  after the obstacle is gone;
* if the channel is still stopped after a restart, the owner is told once per restart;
* a recovered channel is reported with its downtime; a blip shorter than the grace period is never mentioned;
* a gateway that does not answer for the grace period is reported, never restarted from here (launchd owns that).

The gateway is reached through three callables on ``control`` (``status``, ``restart``, ``running_jobs``), so this module
knows nothing about the CLI. State lives in the ``meta`` table and survives a restart of the core.
"""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

STATE_KEY = "channel_watchdog"
DEFAULTS = {"restart": True, "grace_minutes": 10, "cooldown_minutes": 60}
RECHECK_AFTER = 180          # seconds after a restart before "it did not help" may be said


def settings(core):
    _, content = core.config.active()
    merged = dict(DEFAULTS)
    merged.update((content or {}).get("channel_watchdog", {}))
    return merged


def unhealthy(snapshot):
    """Accounts that should be running and are not: ``{"telegram:tetyana-dyck": "telegram:tetyana-dyck"}``."""
    found = {}
    for channel, accounts in ((snapshot or {}).get("channelAccounts") or {}).items():
        for item in accounts or []:
            if item.get("enabled") and item.get("configured") and not item.get("running"):
                key = f"{channel}:{item.get('accountId', '?')}"
                found[key] = key
    return found


def minutes(seconds):
    return max(1, round(seconds / 60))


class ChannelWatchdog:
    def __init__(self, core, control=None):
        self.core = core
        self.control = control

    # -- state -----------------------------------------------------------------------

    def _load(self):
        try:
            state = json.loads(self.core.store.meta(STATE_KEY, "{}"))
        except ValueError:
            state = {}
        for name in ("since", "alerted", "follow_up"):
            state.setdefault(name, {})
        return state

    def _save(self, state):
        self.core.store.set_meta(STATE_KEY, json.dumps(state, sort_keys=True))

    def _say(self, text, key):
        result = self.core.notify("butler", text, "alert", f"channel-watchdog:{key}")
        self.core.diagnostics.record("watchdog.channel.message", {"key": key, "sent": bool(result)})
        return result

    def _clock_text(self, moment):
        zone = ZoneInfo(self.core.delivery.rule()["zone"])
        return dt.datetime.fromtimestamp(moment, zone).strftime("%H:%M")

    # -- one poll --------------------------------------------------------------------

    def tick(self):
        now = self.core.clock()
        state = self._load()
        config = settings(self.core)
        grace = config["grace_minutes"] * 60
        try:
            snapshot = self.control.status()
        except Exception as error:
            result = self._gateway_silent(state, now, grace, str(error))
            self._save(state)
            return result
        self._gateway_answers(state, now)
        bad = unhealthy(snapshot)
        self._recovered(state, bad, now)
        for key in bad:
            state["since"].setdefault(key, now)
        stuck = sorted(key for key in bad if now - state["since"][key] >= grace)
        result = {"unhealthy": sorted(bad), "stuck": stuck, "restarted": False}
        if stuck:
            result["restarted"] = self._handle_stuck(state, stuck, now, config)
        self._save(state)
        return result

    # -- stuck channels --------------------------------------------------------------

    def _restart_verdict(self, state, now, config):
        """Whether the gateway may be restarted now; otherwise the reason, in the owner's words."""
        if not config["restart"]:
            return False, "автоматический перезапуск выключен"
        last = state.get("last_restart")
        wait = config["cooldown_minutes"] * 60
        if last is not None and now - last < wait:
            return False, f"последний перезапуск был {minutes(now - last)} мин назад, следующий не раньше {self._clock_text(last + wait)}"
        try:
            jobs = self.control.running_jobs()
        except Exception:
            return False, "не удалось проверить, идут ли cron-задачи"
        if jobs:
            return False, f"сейчас выполняется cron-задача ({', '.join(str(j)[:12] for j in jobs[:3])}), перезапуск её прервёт"
        return True, None

    def _handle_stuck(self, state, stuck, now, config):
        new = [key for key in stuck if key not in state["alerted"]]
        allowed, reason = self._restart_verdict(state, now, config)
        names = ", ".join(stuck)
        waited = minutes(now - min(state["since"][key] for key in stuck))
        if allowed:
            lead = (f"⚠️ Канал не работает уже {waited} мин: {names}. Встроенный монитор gateway его не поднимает. "
                    if new else f"⚠️ Канал всё ещё не работает ({waited} мин): {names}. ")
            self._say(lead + "Перезапускаю gateway.", f"restart:{int(now)}")
        elif new:
            self._say(f"⚠️ Канал не работает уже {waited} мин: {names}. Встроенный монитор gateway его не поднимает. "
                      f"Автоперезапуск отложен: {reason}.", f"alert:{int(min(state['since'][k] for k in new))}:{'+'.join(new)}")
        for key in new:
            state["alerted"][key] = now
            self.core.diagnostics.raise_alarm(f"channel:{key}", f"{key} stopped for {waited} min")
        self.core.diagnostics.record("watchdog.channel.stuck", {"channels": stuck, "restart": allowed, "reason": reason})
        if allowed:
            outcome = self.control.restart()
            state["last_restart"] = now
            state["follow_up"] = {}
            self.core.diagnostics.record("watchdog.channel.restart", outcome)
            return True
        self._did_not_help(state, stuck, now, config)
        return False

    def _did_not_help(self, state, stuck, now, config):
        last = state.get("last_restart")
        if last is None or now - last < RECHECK_AFTER:
            return
        mark = str(int(last))
        if state["follow_up"].get(mark):
            return
        state["follow_up"][mark] = True
        again = self._clock_text(last + config["cooldown_minutes"] * 60)
        self._say(f"⚠️ Перезапуск gateway не помог: {', '.join(stuck)} всё ещё не работает. "
                  f"Следующая попытка не раньше {again}.", f"no-help:{mark}")

    # -- recovery --------------------------------------------------------------------

    def _recovered(self, state, bad, now):
        for key in [k for k in state["since"] if k not in bad]:
            since = state["since"].pop(key)
            if state["alerted"].pop(key, None) is not None:
                self._say(f"✅ Канал {key} снова работает (простой {minutes(now - since)} мин).",
                          f"recovered:{key}:{int(since)}")
                self.core.diagnostics.clear_alarm(f"channel:{key}")

    # -- the gateway itself ------------------------------------------------------------

    def _gateway_silent(self, state, now, grace, detail):
        state.setdefault("down_since", now)
        silent = now - state["down_since"]
        if silent >= grace and not state.get("down_alerted"):
            state["down_alerted"] = True
            self._say(f"⚠️ Gateway не отвечает уже {minutes(silent)} мин ({detail[:80]}). Сам не перезапускаю: "
                      "его поднимает launchd.", f"down:{int(state['down_since'])}")
            self.core.diagnostics.raise_alarm("channel:gateway", f"gateway silent for {minutes(silent)} min")
        return {"gateway": "silent", "silent_for": silent}

    def _gateway_answers(self, state, now):
        since = state.pop("down_since", None)
        if state.pop("down_alerted", False) and since is not None:
            self._say(f"✅ Gateway снова отвечает (простой {minutes(now - since)} мин).", f"up:{int(since)}")
            self.core.diagnostics.clear_alarm("channel:gateway")

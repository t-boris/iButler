"""Bounded workers, scheduling and watchdogs (REQ-025).

Each adapter has its own bounded pool, so a stalled executor occupies only its
own slots; dialogue (admission, status, approvals) never runs on these pools.
"""
from __future__ import annotations

import threading
import time
import traceback


class Pool:
    def __init__(self, name, size):
        self.name, self.size = name, size
        self.slots = threading.BoundedSemaphore(size)
        self.active = {}
        self.lock = threading.Lock()

    def submit(self, key, fn, *args):
        """Start ``fn`` in a slot unless the key is already running or no slot is free."""
        with self.lock:
            if key in self.active:
                return False
            if not self.slots.acquire(blocking=False):
                return False
            self.active[key] = time.monotonic()

        def run():
            try:
                fn(*args)
            except Exception:
                traceback.print_exc()
            finally:
                with self.lock:
                    self.active.pop(key, None)
                self.slots.release()

        threading.Thread(target=run, name=f"{self.name}:{key}", daemon=True).start()
        return True

    def busy(self):
        with self.lock:
            return dict(self.active)


class Scheduler:
    def __init__(self, core, interval=0.25, clock=time.time):
        self.core, self.interval, self.clock = core, interval, clock
        self.wakeup = threading.Event()
        self.stopping = threading.Event()
        self.pools = {}
        self.thread = None
        self.periodic = []          # [interval seconds, next run, name, fn]
        self.stalls = set()

    def pool(self, name):
        if name not in self.pools:
            _, content = self.core.config.active()
            workers = (content or {}).get("limits", {}).get("workers", {})
            self.pools[name] = Pool(name, workers.get(name, workers.get("default", 2)))
        return self.pools[name]

    def every(self, seconds, name, fn, delay=0):
        self.periodic.append([seconds, time.monotonic() + delay, name, fn])

    def wake(self):
        self.wakeup.set()

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="core-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wakeup.set()
        if self.thread:
            self.thread.join(5)

    def loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            self.wakeup.wait(self.interval)
            self.wakeup.clear()

    def tick(self):
        core = self.core
        for step in core.tasks.ready_steps("operation"):
            core.start_step(step)
        for op_id in core.operations.ready():
            row = core.operations.get(op_id)
            rule = core.operations.rule(row["kind"]) or {}
            self.pool(rule.get("adapter", "default")).submit(op_id, core.operations.run, op_id)
        for delivery_id in core.delivery.ready():
            self.pool("delivery").submit(delivery_id, core.delivery.send, delivery_id)
        now = time.monotonic()
        for job in self.periodic:
            if now >= job[1]:
                job[1] = now + job[0]
                self.pool("periodic").submit(job[2], job[3])

    def watchdog(self):
        """Signal cancellation to executors past their timeout and raise a stall alarm."""
        core = self.core
        now = self.clock()
        for row in core.store.query("SELECT id, kind, dispatched_at FROM operations WHERE state='dispatching'"):
            rule = core.operations.rule(row["kind"]) or {}
            limit = rule.get("timeout", 600) + 30
            if row["dispatched_at"] and now - row["dispatched_at"] > limit:
                with core.operations.lock:
                    event = core.operations.cancels.get(row["id"])
                if event is not None:
                    event.set()
                if row["id"] not in self.stalls:
                    self.stalls.add(row["id"])
                    core.diagnostics.raise_alarm(f"stall:{row['id']}", f"{row['kind']} executor exceeded its timeout")
                if event is None:
                    # No live executor in this process: the response is lost, reconcile it.
                    self.pool("reconcile").submit(row["id"], core.operations.reconcile, row["id"])
        for op_id in list(self.stalls):
            state = core.store.one("SELECT state FROM operations WHERE id=?", (op_id,))
            if state is None or state[0] != "dispatching":
                self.stalls.discard(op_id)
                core.diagnostics.clear_alarm(f"stall:{op_id}")

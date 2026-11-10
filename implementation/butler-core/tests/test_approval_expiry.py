"""BUG-021: an operation that waits for approval for more than seven days is expired, never executed.

On 2026-11-10, 124 operations had waited since 2026-10-24 and the approvals table had never held a row. A wait without an
end is not a safe default: an old request approved by accident days later would act on a world that has moved on.
"""
from butler_core.approvals import ApprovalError
from butler_core.config import ConfigError, validate_content
from support import OWNER, CoreCase, base_config

DAY = 86400


class ApprovalExpiryTest(CoreCase):
    def waiting(self, key):
        op = self.core.submit(self.request(kind="test.gated", key=key), "system2")
        self.assertEqual(op["state"], "awaiting_approval")
        return op

    def state(self, op):
        return self.core.operations.get(op["id"])["state"]

    def test_an_operation_waiting_more_than_seven_days_expires_without_running(self):
        old = self.waiting("old")
        self.clock.advance(3 * DAY)
        fresh = self.waiting("fresh")
        self.clock.advance(5 * DAY)                                    # old: 8 days, fresh: 5 days
        self.assertEqual(self.core.expire_stale_approvals(), {"expired": 1})
        row = self.core.operations.get(old["id"])
        self.assertEqual(row["state"], "cancelled")
        self.assertTrue(row["reason"].startswith("expired:"), row["reason"])
        self.assertIn("7 days", row["reason"])
        self.assertEqual(self.state(fresh), "awaiting_approval")
        self.assertEqual(self.adapter.calls, [])                       # nothing was executed
        kinds = [r["kind"] for r in self.core.store.query("SELECT kind FROM diagnostics")]
        self.assertIn("approval.expired", kinds)

    def test_expiry_is_idempotent(self):
        self.waiting("old")
        self.clock.advance(8 * DAY)
        self.assertEqual(self.core.expire_stale_approvals(), {"expired": 1})
        self.assertEqual(self.core.expire_stale_approvals(), {"expired": 0})

    def test_an_approved_operation_is_not_expired(self):
        op = self.waiting("approved")
        code = self.core.operations.get(op["id"]) and self.core.operations.list(["awaiting_approval"])[0]["review_code"]
        self.core.approvals.grant(op["id"], code, OWNER, "telegram:butler")
        self.clock.advance(8 * DAY)
        self.assertEqual(self.core.expire_stale_approvals(), {"expired": 0})
        self.assertEqual(self.state(op), "requested")

    def test_an_expired_operation_can_no_longer_be_approved(self):
        op = self.waiting("late")
        code = self.core.operations.list(["awaiting_approval"])[0]["review_code"]
        self.clock.advance(8 * DAY)
        self.core.expire_stale_approvals()
        with self.assertRaises(ApprovalError):
            self.core.approvals.grant(op["id"], code, OWNER, "telegram:butler")
        self.assertEqual(self.adapter.calls, [])

    def test_the_task_follows_the_expired_operation(self):
        op = self.waiting("task")
        task = self.core.operations.get(op["id"])["task"]
        self.clock.advance(8 * DAY)
        self.core.expire_stale_approvals()
        self.assertEqual(self.core.status(task)["state"], "cancelled")

    def test_the_limit_is_configurable_and_validated(self):
        self.activate(base_config(approval={"ttl": 900, "expire_days": 2}))
        op = self.waiting("short")
        self.clock.advance(3 * DAY)
        self.assertEqual(self.core.expire_stale_approvals(), {"expired": 1})
        self.assertEqual(self.state(op), "cancelled")
        for bad in (0, 91, "7", -1):
            with self.assertRaises(ConfigError, msg=bad):
                validate_content(base_config(approval={"ttl": 900, "expire_days": bad}), {"test"})

    def test_the_core_checks_periodically(self):
        self.core.start(workers=False)
        self.assertIn("approval-expiry", [entry[2] for entry in self.core.scheduler.periodic])

"""REQ-021/022/023: the common operation path, approvals, retries, reconciliation, resolution, cancellation."""
import threading
import unittest

from butler_core.adapters.testing import ScriptedAdapter
from butler_core.approvals import ApprovalError
from butler_core.operations import OperationError

from support import CoreCase


class SubmissionTest(CoreCase):
    def test_duplicate_key_returns_existing_operation(self):
        first = self.core.submit(self.request(key="same"), "system2")
        second = self.core.submit(self.request(key="same"), "system2")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.core.store.one("SELECT COUNT(*) FROM operations")[0], 1)

    def test_key_reuse_with_other_parameters_is_refused(self):
        self.core.submit(self.request(key="same", value=1), "system2")
        with self.assertRaises(OperationError):
            self.core.submit(self.request(key="same", value=2), "system2")

    def test_rules_system1_and_system2_use_the_same_path(self):
        allowed = self.core.submit(self.request(key="r"), "rules")
        self.assertEqual(allowed["state"], "requested")
        denied = [self.core.submit(self.request(kind="test.write", key=f"d-{source}", persona="tetyana-dyck",
                                                principal="agent:butler"), source)
                  for source in ("rules", "system2")]
        self.assertEqual({d["state"] for d in denied}, {"denied"})
        with self.assertRaises(OperationError):
            self.core.submit(self.request(key="s1"), "system1")      # shadow mode cannot act
        content = self.core.config.active()[1]
        content["routing"] = {"system1": {"mode": "active", "criteria_evidence": "test-criteria"}}
        self.activate(content)
        through = self.core.submit(self.request(key="s1"), "system1")
        self.assertEqual(through["state"], "requested")
        refused = self.core.submit(self.request(kind="test.write", key="s1-x", persona="tetyana-dyck",
                                                principal="agent:butler"), "system1")
        self.assertEqual(refused["state"], "denied")
        sources = {r[0] for r in self.core.store.query("SELECT DISTINCT boundary FROM policy_decisions")}
        self.assertIn("execute", sources)

    def test_queue_limit_rejects_explicitly(self):
        content = self.core.config.active()[1]
        content["limits"] = {"queue": 2, "workers": {"default": 1}}
        self.activate(content)
        self.core.submit(self.request(key="q1"), "cli")
        self.core.submit(self.request(key="q2"), "cli")
        with self.assertRaises(OperationError):
            self.core.submit(self.request(key="q3"), "cli")


class ExecutionTest(CoreCase):
    def run_op(self, key="k", kind="test.write"):
        op = self.core.submit(self.request(kind=kind, key=key), "system2")
        return op, self.core.operations.run(op["id"])

    def test_success_records_receipt_and_task(self):
        op, state = self.run_op()
        self.assertEqual(state, "succeeded")
        row = self.core.operations.get(op["id"])
        self.assertEqual(row["receipt"], "test:k")
        self.assertEqual(self.core.tasks.get(op["task"])["state"], "succeeded")

    def test_not_dispatched_retries_within_bounds(self):
        self.adapter.script = ["not_dispatched", "not_dispatched", "not_dispatched"]
        op, state = self.run_op()
        self.assertEqual(state, "requested")
        self.assertEqual(self.core.operations.ready(), [])            # backoff holds it
        self.clock.advance(3)
        self.assertEqual(self.core.operations.run(op["id"]), "requested")
        self.clock.advance(11)
        self.assertEqual(self.core.operations.run(op["id"]), "failed")  # max_attempts=3
        self.assertEqual(self.adapter.effects, {})
        self.assertEqual(self.core.store.one("SELECT COUNT(*) FROM operation_attempts")[0], 3)

    def test_rejected_never_retries(self):
        self.adapter.script = ["rejected"]
        _, state = self.run_op()
        self.assertEqual(state, "failed")
        self.assertEqual(len(self.adapter.calls), 1)

    def test_lost_response_reconciles_to_done_without_repeating(self):
        self.adapter.script = ["ambiguous"]
        op, state = self.run_op()
        self.assertEqual(state, "succeeded")
        self.assertEqual(self.adapter.effects, {"k": 1})
        self.assertEqual(self.core.store.one("SELECT outcome FROM reconciliations")[0], "done")

    def test_lost_response_without_effect_retries(self):
        self.adapter.script = ["ambiguous_none", "ok"]
        op, state = self.run_op()
        self.assertEqual(state, "requested")
        self.clock.advance(3)
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual(self.adapter.effects, {"k": 1})

    def test_unknown_outcome_blocks_repetition_until_owner_resolves(self):
        adapter = ScriptedAdapter("test", script=["ambiguous"], reconcile_supported=False)
        self.core.adapters["test"] = adapter
        op, state = self.run_op()
        self.assertEqual(state, "needs_reconciliation")
        self.assertEqual(self.core.operations.run(op["id"]), "needs_reconciliation")  # never re-run
        self.assertEqual(self.core.operations.ready(), [])
        notice = self.core.delivery.list()[0]
        self.assertEqual(notice["kind"], "status")
        with self.assertRaises(OperationError):
            self.core.operations.resolve(op["id"], "done", "checked", "agent:butler", "cli")
        result = self.core.operations.resolve(op["id"], "done", "found the record in the service", "terminal:owner",
                                              "terminal")
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(adapter.calls), 1)                         # resolution dispatched nothing
        self.assertEqual(self.core.store.one("SELECT COUNT(*) FROM resolutions")[0], 1)
        self.assertEqual(self.core.store.one("SELECT outcome FROM reconciliations")[0], "unknown")
        with self.assertRaises(OperationError):
            self.core.operations.resolve(op["id"], "not_done", "again", "terminal:owner", "terminal")

    def test_owner_resolution_not_done_fails_without_retry(self):
        self.core.adapters["test"] = ScriptedAdapter("test", script=["ambiguous"], reconcile_supported=False)
        op, _ = self.run_op()
        self.core.operations.resolve(op["id"], "not_done", "nothing in the service", "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.get(op["id"])["state"], "failed")
        self.assertEqual(self.core.operations.ready(), [])


class ApprovalTest(CoreCase):
    def gated(self, key="g", value=1):
        return self.core.submit(self.request(kind="test.gated", key=key, value=value), "system2")

    def test_approval_is_bound_and_single_use(self):
        op = self.gated()
        self.assertEqual(op["state"], "awaiting_approval")
        self.assertEqual(self.core.operations.run(op["id"]), "awaiting_approval")
        with self.assertRaises(ApprovalError):
            self.core.approvals.grant(op["id"], "000000", "terminal:owner", "terminal")    # wrong parameters
        with self.assertRaises(ApprovalError):
            self.core.approvals.grant(op["id"], op["review_code"], "agent:butler", "cli")   # not an approver
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        used = self.core.store.one("SELECT used_at FROM approvals")[0]
        self.assertIsNotNone(used)
        other = self.gated(key="g2")
        self.assertNotEqual(other["review_code"], None)
        row = self.core.store.one("SELECT id FROM approvals")
        self.core.store.db.execute("UPDATE operations SET approval=? WHERE id=?", (row[0], other["id"]))
        self.core.store.db.execute("UPDATE operations SET state='requested' WHERE id=?", (other["id"],))
        self.assertEqual(self.core.operations.run(other["id"]), "failed")   # reuse cannot authorize
        self.assertIn("approval invalid", self.core.operations.get(other["id"])["reason"])

    def test_changed_parameters_invalidate(self):
        op = self.gated(value=1)
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.core.store.db.execute("UPDATE operations SET params_hash=? WHERE id=?", ("b" * 64, op["id"]))
        self.assertEqual(self.core.operations.run(op["id"]), "failed")
        self.assertEqual(self.adapter.calls, [])

    def test_expired_approval_cannot_authorize(self):
        op = self.gated()
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.clock.advance(901)
        self.assertEqual(self.core.operations.run(op["id"]), "failed")
        self.assertIn("expired", self.core.operations.get(op["id"])["reason"])

    def test_revocation_blocks_previously_approved_work(self):
        op = self.gated()
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        content = self.core.config.active()[1]
        content["personas"]["butler"]["actions"].remove("test.gated")
        self.activate(content)
        self.assertEqual(self.core.operations.run(op["id"]), "failed")
        self.assertIn("revoked", self.core.operations.get(op["id"])["reason"])
        self.assertEqual(self.adapter.calls, [])

    def test_approval_request_is_delivered_to_owner(self):
        op = self.gated()
        notice = self.core.store.one("SELECT content, recipient FROM deliveries WHERE kind='approval'")
        self.assertIn(f"/approve_{op['id'][3:11]}_{op['review_code']}", notice["content"])
        self.assertEqual(notice["recipient"], "telegram:1000001")

    def test_reject(self):
        op = self.gated()
        self.core.approvals.reject(op["id"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.get(op["id"])["state"], "cancelled")


class CancelTest(CoreCase):
    def test_cancel_wait(self):
        op = self.core.submit(self.request(kind="test.gated", key="c"), "system2")
        result = self.core.operations.cancel(op["id"], "owner")
        self.assertEqual((result["mode"], result["effect"]), ("cancel_wait", "none"))

    def test_stop_executor(self):
        self.adapter.script = ["stall"]
        op = self.core.submit(self.request(key="s"), "system2")
        worker = threading.Thread(target=self.core.operations.run, args=(op["id"],))
        worker.start()
        self.assertTrue(self.adapter.started.wait(5))
        result = self.core.operations.cancel(op["id"], "owner")
        worker.join(5)
        self.assertEqual(result["mode"], "stop_executor")
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.core.operations.get(op["id"])["state"], "cancelled")

    def test_effect_already_done(self):
        op = self.core.submit(self.request(key="e"), "system2")
        self.core.operations.run(op["id"])
        result = self.core.operations.cancel(op["id"], "owner")
        self.assertEqual((result["mode"], result["effect"]), ("effect_done", "done"))

    def test_tracked_work_is_stopped(self):
        tracked = ScriptedAdapter("test", tracked=True)
        self.core.adapters["test"] = tracked
        op = self.core.submit(self.request(key="t"), "system2")
        self.assertEqual(self.core.operations.run(op["id"]), "running")
        result = self.core.operations.cancel(op["id"], "owner")
        self.assertEqual((result["mode"], result["state"]), ("stop_executor", "cancelled"))


if __name__ == "__main__":
    unittest.main()

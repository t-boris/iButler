"""W4: Todoist writes are idempotent core operations; unknown outcomes are reconciled, never repeated blindly."""
import json
import unittest

from butler_core.adapters.base import NotDispatched
from butler_core.adapters.todoist import TodoistAdapter, TodoistClient, command_uuid
from butler_core.service import Core
from fake_todoist import FakeTodoist
from support import CoreCase, base_config


def todoist_config():
    config = base_config()
    kinds = {"todoist.create": "none", "todoist.update": "none", "todoist.close": "none", "todoist.reopen": "none",
             "todoist.comment": "none", "todoist.delete": "required", "todoist.import": "required"}
    config["personas"]["butler"]["actions"] += list(kinds)
    config["exports"].append({"persona": "butler", "category": "task", "recipient": "todoist"})
    for kind, approval in kinds.items():
        config["operations"][kind] = {"adapter": "todoist", "approval": approval, "reconcile": "auto", "max_attempts": 2,
                                      "timeout": 60, "export": [{"category": "task", "recipient": "todoist"}]}
    config["limits"]["workers"]["todoist"] = 1
    return config


class TodoistCase(CoreCase):
    def make_core(self, **kwargs):
        self.todoist = FakeTodoist()
        self.token = "tok-secret"
        client = TodoistClient(token=lambda: self.token, opener=self.todoist, base="https://todoist.test")
        self.todoist_adapter = TodoistAdapter(client)
        return Core(self.tmp / "state", adapters={"test": self.adapter, "todoist": self.todoist_adapter},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        self.activate(todoist_config())

    def submit(self, kind, key, **params):
        return self.core.submit({"kind": kind, "persona": "butler", "principal": "agent:butler", "target": "todoist:inbox",
                                 "params": params, "idempotency_key": key}, "system2")


class CreateTest(TodoistCase):
    def test_create_runs_one_sync_command_and_returns_the_task(self):
        op = self.submit("todoist.create", "c1", content="Купить молоко", due_string="tomorrow", priority=4, labels=["home"])
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        record = self.core.operations.get(op["id"])
        self.assertEqual(record["receipt"], "todoist:task:101")
        self.assertEqual(json.loads(record["result"]), {"id": "101", "content": "Купить молоко"})
        self.assertEqual(self.todoist.tasks["101"]["labels"], ["home"])
        self.assertEqual(self.todoist.requests[0][:2], ("POST", "/sync"))
        self.assertEqual(len(self.todoist.tasks), 1)
        # The token travelled only in the request header.
        self.assertNotIn("tok-secret", json.dumps(dict(record)))

    def test_unknown_outcome_after_the_effect_is_reconciled_without_a_second_task(self):
        self.todoist.fail = "timeout-after"
        op = self.submit("todoist.create", "c2", content="Позвонить в банк")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        record = self.core.operations.get(op["id"])
        self.assertEqual(record["reason"], "reconciled: effect found")
        self.assertEqual(json.loads(record["result"])["id"], "101")
        self.assertEqual(len(self.todoist.tasks), 1)
        self.assertEqual(sum(1 for r in self.todoist.requests if r[1] == "/sync"), 1)

    def test_unknown_outcome_before_the_effect_retries_with_the_same_uuid(self):
        self.todoist.fail = "timeout-before"
        op = self.submit("todoist.create", "c3", content="Проверить почту")
        self.assertEqual(self.core.operations.run(op["id"]), "requested")      # reconciled not done, retry scheduled
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        syncs = [r for r in self.todoist.requests if r[1] == "/sync"]
        self.assertEqual(len(syncs), 2)
        self.assertEqual(len(self.todoist.tasks), 1)
        self.assertEqual(self.todoist.seen, {command_uuid(op["id"])})

    def test_repeated_command_is_never_executed_twice_even_when_the_core_forgot_the_id(self):
        op = self.submit("todoist.create", "c4", content="Дважды")
        self.core.operations.run(op["id"])
        # A second execute of the same operation (e.g. after a restart mid-finish) finds the task by search.
        contract = {"id": op["id"], "kind": "todoist.create", "params": {"content": "Дважды"}, "idempotency_key": "c4"}
        outcome = self.todoist_adapter.execute(contract, None)
        self.assertEqual(outcome["result"]["id"], "101")
        self.assertEqual(len(self.todoist.tasks), 1)

    def test_refusals_fail_without_retry_and_missing_token_is_not_dispatched(self):
        self.todoist.fail = "http400"
        op = self.submit("todoist.create", "c5", content="Плохой запрос")
        self.assertEqual(self.core.operations.run(op["id"]), "failed")
        self.assertIn("rejected", self.core.operations.get(op["id"])["reason"])
        self.todoist.fail = "refuse-command"
        op = self.submit("todoist.create", "c6", content="Отказ команды")
        self.assertEqual(self.core.operations.run(op["id"]), "failed")
        self.assertEqual(len(self.todoist.tasks), 0)
        self.token = ""
        from butler_core.adapters.telegram import TokenUnavailable
        client = TodoistClient(token=lambda: (_ for _ in ()).throw(TokenUnavailable("no token")), opener=self.todoist, base="https://todoist.test")
        with self.assertRaises(NotDispatched):
            TodoistAdapter(client).execute({"id": "op-x", "kind": "todoist.create", "params": {"content": "x"}}, None)


class ChangeTest(TodoistCase):
    def setUp(self):
        super().setUp()
        self.core.operations.run(self.submit("todoist.create", "seed", content="Задача")["id"])

    def test_close_reopen_update_and_comment_reconcile_from_the_task(self):
        for kind, key, params in (("todoist.close", "k1", {}), ("todoist.reopen", "k2", {}),
                                  ("todoist.update", "k3", {"content": "Задача!", "labels": ["work"]}),
                                  ("todoist.comment", "k4", {"content": "заметка"})):
            self.todoist.fail = "timeout-after"
            op = self.submit(kind, key, task_id="101", **params)
            self.assertEqual(self.core.operations.run(op["id"]), "succeeded", kind)
            self.assertEqual(self.core.operations.get(op["id"])["reason"], "reconciled: effect found", kind)
        self.assertEqual(self.todoist.tasks["101"]["content"], "Задача!")
        self.assertEqual(self.todoist.comments, [{"task_id": "101", "content": "заметка"}])

    def test_delete_needs_the_owner_and_is_reconciled_by_absence(self):
        op = self.submit("todoist.delete", "d1", task_id="101")
        self.assertEqual(op["state"], "awaiting_approval")
        self.assertEqual(self.core.operations.run(op["id"]), "awaiting_approval")
        self.assertEqual(len(self.todoist.tasks), 1)
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.todoist.fail = "timeout-after"
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual(self.todoist.tasks, {})

    def test_bulk_import_needs_the_owner_and_creates_each_item_once(self):
        items = [{"content": "Старая задача 1", "project_id": "p9"}, {"content": "Старая задача 2", "due_string": "friday"}]
        op = self.submit("todoist.import", "i1", items=items)
        self.assertEqual(op["state"], "awaiting_approval")
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual(json.loads(self.core.operations.get(op["id"])["result"])["ids"], ["102", "103"])
        self.assertEqual(self.todoist.tasks["102"]["project_id"], "p9")
        self.assertEqual(len(self.todoist.tasks), 3)


if __name__ == "__main__":
    unittest.main()

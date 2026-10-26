"""REQ-023 / DEC-037 / DEC-050: persona isolation, owner grants, export policy, disclosure."""
import unittest

from butler_core.config import ConfigError

from support import TANYA, CoreCase, base_config


class IsolationTest(CoreCase):
    def check(self, persona, boundary, resource, principal=None):
        return self.core.policy.check(persona, boundary, resource, principal=principal)["outcome"]

    def test_default_isolation_across_sources_tasks_and_actions(self):
        self.assertEqual(self.check("butler", "search", "source:vault"), "allow")
        self.assertEqual(self.check("tetyana-dyck", "search", "source:vault"), "deny")
        self.assertEqual(self.check("tetyana-dyck", "execute", "action:test.gated"), "deny")
        self.assertEqual(self.check("butler", "search", "source:tetyana-repo"), "deny")
        task = self.core.admit_message({"account": "tetyana-dyck", "sender": TANYA[9:], "chat": TANYA[9:],
                                        "message_id": "1", "text": "сделай баннер"})["task"]
        self.assertEqual(self.check("tetyana-dyck", "disclose", f"task:{task}"), "allow")
        self.assertEqual(self.check("butler", "disclose", f"task:{task}"), "deny")

    def test_shared_project_association_grants_nothing(self):
        content = base_config()
        content["personas"]["butler"]["sources"].append("project:familybuilder")
        content["personas"]["tetyana-dyck"]["sources"].append("project:familybuilder")
        self.activate(content)
        self.assertEqual(self.check("tetyana-dyck", "search", "source:vault"), "deny")
        task = self.core.admit_message({"account": "butler", "sender": "1000001", "chat": "1000001",
                                        "message_id": "2", "text": "про familybuilder"})["task"]
        self.assertEqual(self.check("tetyana-dyck", "disclose", f"task:{task}"), "deny")

    def test_explicit_owner_grant_opens_exactly_what_it_names(self):
        content = base_config()
        content["grants"] = [{"persona": "tetyana-dyck", "kind": "source", "resource": "vault", "by": "owner"}]
        self.activate(content)
        self.assertEqual(self.check("tetyana-dyck", "search", "source:vault"), "allow")
        self.assertEqual(self.check("tetyana-dyck", "search", "source:memory"), "deny")
        self.assertIn("vault", self.core.policy.sources("tetyana-dyck"))

    def test_grants_by_anyone_but_the_owner_are_invalid(self):
        content = base_config()
        content["grants"] = [{"persona": "tetyana-dyck", "kind": "source", "resource": "vault", "by": "agent:butler"}]
        with self.assertRaises(ConfigError):
            self.core.config.propose(content, "test")

    def test_principals_are_scoped_to_their_persona(self):
        self.assertEqual(self.check("butler", "execute", "action:test.write", principal="agent:tetyana-dyck"), "deny")
        self.assertEqual(self.check("butler", "execute", "action:test.write", principal=TANYA), "deny")
        self.assertEqual(self.check("tetyana-dyck", "execute", "action:test.write", principal=TANYA), "allow")

    def test_every_check_is_recorded(self):
        self.check("butler", "search", "source:vault")
        self.check("butler", "export", "export:task-prompt@openai")
        rows = self.core.store.query("SELECT boundary, outcome FROM policy_decisions ORDER BY at")
        self.assertEqual([tuple(r) for r in rows][-2:], [("search", "allow"), ("export", "allow")])


class ExportTest(CoreCase):
    def test_export_needs_category_and_recipient(self):
        self.assertEqual(self.core.policy.check("butler", "export", "export:task-prompt@openai")["outcome"], "allow")
        self.assertEqual(self.core.policy.check("butler", "export", "export:task-prompt@google")["outcome"], "deny")
        self.assertEqual(self.core.policy.check("tetyana-dyck", "export", "export:task-prompt@openai")["outcome"],
                         "deny")

    def test_raw_observation_materials_stay_local(self):
        self.assertEqual(self.core.policy.check("butler", "export", "export:raw-observation@openai")["outcome"],
                         "deny")
        content = base_config()
        content["exports"].append({"persona": "butler", "category": "raw-media", "recipient": "openai"})
        with self.assertRaises(ConfigError):
            self.core.config.propose(content, "test")

    def test_operation_exception_requires_operation_specific_approval(self):
        content = base_config()
        content["operations"]["test.export"] = {"adapter": "test", "exception": True,
                                                "export": [{"category": "summary", "recipient": "web"}]}
        self.activate(content)
        op = self.core.submit(self.request(kind="test.export", key="x"), "system2")
        self.assertEqual(op["state"], "awaiting_approval")
        content["operations"]["test.export"]["exception"] = False
        self.activate(content)
        denied = self.core.submit(self.request(kind="test.export", key="y"), "system2")
        self.assertEqual(denied["state"], "denied")

    def test_approved_export_runs_and_revocation_blocks_it(self):
        op = self.core.submit(self.request(kind="test.export", key="z"), "system2")
        self.assertEqual(op["state"], "requested")
        content = base_config()
        content["exports"] = []
        self.activate(content)
        self.assertEqual(self.core.operations.run(op["id"]), "failed")
        self.assertIn("export authorization revoked", self.core.operations.get(op["id"])["reason"])


if __name__ == "__main__":
    unittest.main()

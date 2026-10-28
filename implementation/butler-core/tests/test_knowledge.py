"""W6: vault writes and recalls on the operation path; the search boundary; the installed vault library applying writes.

The vault harness copies the installed ``lib/vault.py`` and its write policy into
a temporary workspace with a temporary vault root, so the core's adapter runs
the real apply and check commands against real files, never the owner's vault.
"""
from support import require_files
import json
import os
import re
import shutil
import unittest
from pathlib import Path

from butler_core.adapters import butler as workspace_adapters
from butler_core.adapters.vault import KnowledgeAdapter, VaultAdapter
from butler_core.service import Core
from support import CoreCase, base_config
from test_dispatch import substitute

INSTALLED = Path.home() / ".openclaw/workspace-butler"
PYTHON = workspace_adapters.WORKSPACE_PYTHON


def knowledge_config():
    config = base_config()
    write = {"adapter": "vault", "reconcile": "auto", "max_attempts": 2, "timeout": 120, "sources": ["vault"]}
    config["operations"]["vault.write"] = {**write, "approval": "none"}
    config["operations"]["vault.write_confirmed"] = {**write, "approval": "required"}
    config["operations"]["knowledge.recall"] = {"adapter": "knowledge", "approval": "none", "max_attempts": 1,
                                                "sources": ["vault", "memory", "sessions"]}
    config["personas"]["butler"]["actions"] += ["vault.write", "vault.write_confirmed", "knowledge.recall"]
    config["personas"]["butler"]["sources"] = ["vault", "memory", "sessions"]
    config["personas"]["tetyana-dyck"]["actions"] += ["knowledge.recall", "vault.write"]
    config["limits"]["workers"].update({"vault": 1, "knowledge": 1})
    return config


def fake_workspace(root):
    require_files(INSTALLED / "lib/vault.py", INSTALLED / "policies/write.yaml")
    ws = (root / "ws").resolve()
    vault_root = (root / "vault").resolve()
    (ws / "lib").mkdir(parents=True)
    (ws / "policies").mkdir()
    (ws / "state").mkdir()
    (vault_root / "Daily Notes").mkdir(parents=True)
    source = substitute((INSTALLED / "lib/vault.py").read_text(), 'WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")',
                        f'WORKSPACE = Path("{ws}")')
    (ws / "lib/vault.py").write_text(source)
    policy = (INSTALLED / "policies/write.yaml").read_text()
    policy = substitute(policy, "  path: /Users/boris/Documents/KnowledgeDB", f"  path: {vault_root}")
    (ws / "policies/write.yaml").write_text(policy)
    (ws / "lib/__init__.py").write_text("")
    return ws, vault_root


class VaultCase(CoreCase):
    def make_core(self, **kwargs):
        self.ws, self.vault_root = fake_workspace(self.tmp)
        self.vault = VaultAdapter(self.ws, PYTHON, os.environ.get("PATH", "/usr/bin:/bin"), {"TMPDIR": "/tmp"})
        return Core(self.tmp / "state", adapters={"test": self.adapter, "vault": self.vault, "knowledge": KnowledgeAdapter()},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        self.activate(knowledge_config())

    def spec(self, kind, rel_path, content, action="allow", ensure_newline=True):
        """Write a spec the way the installed library does, through that library."""
        import subprocess
        code = (f"import json\nfrom lib import vault\n"
                f"spec, digest = vault.write_spec({kind!r}, {rel_path!r}, {content!r}, action={action!r}, reason='test', "
                f"ensure_newline={ensure_newline!r}, meta={{'source': 'test'}})\nprint(json.dumps({{'spec': str(spec), 'sha256': digest}}))")
        done = subprocess.run([str(PYTHON), "-c", code], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(self.ws)},
                              cwd=self.ws, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def submit(self, kind, rel_path, content, key, op_kind="vault.write", persona="butler", action="allow"):
        spec = self.spec(kind, rel_path, content, action=action)
        target = "vault:" + re.sub(r"[^A-Za-z0-9._/-]", "_", rel_path)
        return self.core.submit({"kind": op_kind, "persona": persona, "principal": f"agent:{persona}", "target": target,
                                 "params": {**spec, "kind": kind, "rel_path": rel_path, "action": action},
                                 "idempotency_key": key}, "system2")


class VaultWriteTest(VaultCase):
    def test_allowed_write_is_applied_by_the_installed_library_with_its_audit(self):
        content = "---\ntype: daily\n---\n# 2026-10-24\n- запись\n"
        op = self.submit("create", "Daily Notes/2026-10-24.md", content, "v1")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual((self.vault_root / "Daily Notes/2026-10-24.md").read_text(), content)
        record = self.core.operations.get(op["id"])
        self.assertTrue(record["receipt"].startswith("vault:create:Daily Notes/2026-10-24.md@"))
        audit = (self.ws / "logs/obsidian-writes.jsonl").read_text()
        self.assertIn(op["id"], audit)                                   # the audit names the core operation

    def test_lost_answer_is_reconciled_from_the_vault_and_never_applied_twice(self):
        content = "# note\n"
        op = self.submit("create", "Daily Notes/2026-10-25.md", content, "v2")
        # The write happened but the answer was lost: the next attempt finds the content.
        (self.vault_root / "Daily Notes/2026-10-25.md").write_text(content)
        self.core.store.db.execute("UPDATE operations SET state='dispatching', attempts=1 WHERE id=?", (op["id"],))
        self.assertEqual(self.core.operations.reconcile(op["id"]), "succeeded")
        self.assertEqual(self.core.operations.get(op["id"])["reason"], "reconciled: effect found")
        missing = self.submit("create", "Daily Notes/2026-10-26.md", "# other\n", "v3")
        self.core.store.db.execute("UPDATE operations SET state='dispatching', attempts=1 WHERE id=?", (missing["id"],))
        self.assertEqual(self.core.operations.reconcile(missing["id"]), "requested")   # not applied: retry

    def test_confirm_rule_waits_for_the_owner_and_a_changed_spec_is_refused(self):
        op = self.submit("create", "Projects/Demo/plan.md", "# plan\n", "v4", op_kind="vault.write_confirmed", action="stage_then_confirm")
        self.assertEqual(op["state"], "awaiting_approval")
        self.assertFalse((self.vault_root / "Projects/Demo/plan.md").exists())
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual((self.vault_root / "Projects/Demo/plan.md").read_text(), "# plan\n")
        spec = self.spec("create", "Projects/Demo/other.md", "# other\n")
        tampered = self.core.submit({"kind": "vault.write", "persona": "butler", "principal": "agent:butler", "target": "vault:x",
                                     "params": {"spec": spec["spec"], "sha256": "0" * 64, "kind": "create", "rel_path": "Projects/Demo/other.md"},
                                     "idempotency_key": "v5"}, "system2")
        self.assertEqual(self.core.operations.run(tampered["id"]), "failed")
        self.assertIn("checksum", self.core.operations.get(tampered["id"])["reason"])

    def test_source_scope_is_checked_at_the_search_boundary(self):
        denied = self.submit("create", "Daily Notes/x.md", "# x\n", "v6", persona="tetyana-dyck")
        self.assertEqual(denied["state"], "denied")
        self.assertIn("outside persona scope", denied["reason"])
        recall = self.core.submit({"kind": "knowledge.recall", "persona": "tetyana-dyck", "principal": "agent:tetyana-dyck",
                                   "target": "knowledge:recall", "params": {"scopes": ["vault"], "query_sha256": "ab", "k": 5},
                                   "idempotency_key": "r1"}, "system2")
        self.assertEqual(recall["state"], "denied")
        allowed = self.core.submit({"kind": "knowledge.recall", "persona": "butler", "principal": "voice:owner",
                                    "target": "knowledge:recall", "params": {"scopes": ["vault", "sessions"], "query_sha256": "ab", "k": 5},
                                    "idempotency_key": "r2"}, "system2")
        self.assertEqual(self.core.operations.run(allowed["id"]), "succeeded")
        decisions = [d for d in self.core.store.query("SELECT boundary, resource, outcome FROM policy_decisions WHERE boundary='search'")]
        self.assertTrue(any(d["resource"] == "source:sessions" and d["outcome"] == "allow" for d in decisions))


if __name__ == "__main__":
    unittest.main()

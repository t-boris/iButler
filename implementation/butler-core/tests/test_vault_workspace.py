"""W6 compatibility: the installed vault and memory libraries on the core path, with the core faked.

The libraries are copied with their workspace and vault pointed at temporary
folders; a stand-in core client records what they submit and answers as told.
"""
import importlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from test_dispatch import substitute

INSTALLED = Path.home() / ".openclaw/workspace-butler"

FAKE_CLIENT = '''
import json
from pathlib import Path
HERE = Path(__file__).parent
class CoreUnavailable(ConnectionError): pass
class CoreError(RuntimeError):
    def __init__(self, code, message): super().__init__(code); self.code, self.message = code, message
def switched(name): return name in (HERE / "switches").read_text().split()
class Client:
    def execute(self, kind, persona, target, params, key, principal=None, timeout=60, task=None, wait=None):
        with (HERE / "calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"kind": kind, "persona": persona, "target": target, "params": params, "key": key, "principal": principal}) + "\\n")
        mode = (HERE / "mode").read_text().strip()
        if mode == "down": raise CoreUnavailable("down")
        if mode == "deny": return {"id": "op-d", "state": "denied", "reason": "source is outside persona scope"}
        if kind == "vault.write_confirmed": return {"id": "op-c", "state": "awaiting_approval", "review_code": "123456"}
        if kind == "vault.write":
            # The real core would run `lib.vault core-apply`; do the same here so the file appears.
            import subprocess, sys
            done = subprocess.run([sys.executable, "-m", "lib.vault", "core-apply", params["spec"], params["sha256"], "--operation", "op-1"],
                                  capture_output=True, text=True, cwd=str(HERE.parent), env={"PYTHONPATH": str(HERE.parent), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
            if done.returncode: return {"id": "op-1", "state": "failed", "reason": done.stderr.strip()[-200:]}
            return {"id": "op-1", "state": "succeeded", "result": json.loads(done.stdout)}
        return {"id": "op-r", "state": "succeeded"}
'''


def workspace(root, switches="vault knowledge"):
    ws = (root / "ws").resolve()
    vault_root = (root / "vault").resolve()
    (ws / "lib").mkdir(parents=True)
    (ws / "policies").mkdir()
    (ws / "state").mkdir()
    for folder in ("Daily Notes", "Knowledge/fact", "Projects/Demo", "Tasks", "Research"):
        (vault_root / folder).mkdir(parents=True)
    source = substitute((INSTALLED / "lib/vault.py").read_text(), 'WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")',
                        f'WORKSPACE = Path("{ws}")')
    (ws / "lib/vault.py").write_text(source)
    # memory.py finds the vault through lib.vault and keeps no workspace path of its own.
    sources = Path(__file__).resolve().parents[1] / "workspace/lib"
    (ws / "lib/memory.py").write_text((sources / "memory.py").read_text())
    (ws / "lib/knowledge_sources.py").write_text((sources / "knowledge_sources.py").read_text())
    policy = substitute((INSTALLED / "policies/write.yaml").read_text(), "  path: /Users/boris/Documents/KnowledgeDB", f"  path: {vault_root}")
    (ws / "policies/write.yaml").write_text(policy)
    (ws / "lib/__init__.py").write_text("")
    (ws / "lib/butler_core_client.py").write_text(FAKE_CLIENT)
    (ws / "lib/switches").write_text(switches)
    (ws / "lib/mode").write_text("ok")
    return ws, vault_root


def load(ws, name):
    sys.path.insert(0, str(ws))
    for key in [k for k in sys.modules if k == "lib" or k.startswith("lib.")]:
        del sys.modules[key]
    return importlib.import_module(f"lib.{name}")


class VaultLibraryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vault-ws-"))
        self.ws, self.vault_root = workspace(self.root)
        self.vault = load(self.ws, "vault")

    def tearDown(self):
        for key in [k for k in sys.modules if k == "lib" or k.startswith("lib.")]:
            del sys.modules[key]
        if str(self.ws) in sys.path:
            sys.path.remove(str(self.ws))
        shutil.rmtree(self.root, ignore_errors=True)

    def calls(self):
        path = self.ws / "lib/calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_allowed_write_goes_through_the_core_and_lands_with_the_audit(self):
        result = self.vault.append("Daily Notes/2026-10-24.md", "- заметка\n", source="test")
        self.assertTrue(result.ok)
        self.assertEqual((self.vault_root / "Daily Notes/2026-10-24.md").read_text(), "- заметка\n")
        call = self.calls()[0]
        self.assertEqual((call["kind"], call["persona"], call["principal"]), ("vault.write", "butler", "agent:butler"))
        self.assertEqual(call["params"]["action"], "allow")
        self.assertTrue(Path(call["params"]["spec"]).is_file())
        self.assertEqual(call["key"], "vault:" + call["params"]["sha256"][:24])
        audit = (self.ws / "logs/obsidian-writes.jsonl").read_text()
        self.assertIn("op-1", audit)

    def test_confirm_rule_stages_and_waits_for_the_owner(self):
        result = self.vault.create("Projects/Demo/design.md", "# design\n", source="test")
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "stage_then_confirm")
        self.assertFalse((self.vault_root / "Projects/Demo/design.md").exists())
        call = self.calls()[0]
        self.assertEqual(call["kind"], "vault.write_confirmed")
        self.assertTrue(call["params"]["staged_id"])
        self.assertEqual(len(self.vault.list_staged()), 1)
        # What the core applies after approval is exactly the staged content.
        applied = self.vault.core_apply(call["params"]["spec"], call["params"]["sha256"], "op-c")
        self.assertTrue(applied.ok)
        self.assertEqual((self.vault_root / "Projects/Demo/design.md").read_text(), "# design\n")
        self.assertTrue(self.vault.core_check(call["params"]["spec"], call["params"]["sha256"])["applied"])

    def test_block_rule_and_core_down_write_nothing(self):
        with self.assertRaises(self.vault.PolicyError):
            self.vault.edit("Research/2026-10-21-tea-market.md", "overwrite", source="test")   # no rule: default deny
        self.assertEqual([c["kind"] for c in self.calls() if c["kind"].startswith("vault")], [])
        (self.ws / "lib/mode").write_text("down")
        with self.assertRaises(self.vault.CoreRefused):
            self.vault.append("Daily Notes/2026-10-24.md", "- x\n", source="test")
        self.assertFalse((self.vault_root / "Daily Notes/2026-10-24.md").exists())

    def test_switch_off_keeps_the_direct_path(self):
        (self.ws / "lib/switches").write_text("")
        result = self.vault.append("Daily Notes/2026-10-24.md", "- прямо\n", source="test")
        self.assertTrue(result.ok)
        self.assertEqual(self.calls(), [])


class MemoryLibraryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="memory-ws-"))
        self.ws, self.vault_root = workspace(self.root)
        (self.vault_root / "Knowledge/fact/2026-10-20-boris-loves-tea.md").write_text(
            "---\ntype: memory\nkind: fact\nsource: boris-said\ncaptured_at: 2026-10-20\nconfidence: high\n---\n# Борис любит чай\n")
        (self.vault_root / "Research/2026-10-21-tea-market.md").write_text(
            "---\ntype: research\nsource: research\nmanaged_by: ai-butler\n---\n# Рынок чая\nЧай растёт в цене.\n")
        (self.vault_root / "Projects/Demo/notes.md").write_text("# Demo\nБорис пьёт чай на встречах.\n")
        self.memory = load(self.ws, "memory")

    def tearDown(self):
        for key in [k for k in sys.modules if k == "lib" or k.startswith("lib.")]:
            del sys.modules[key]
        if str(self.ws) in sys.path:
            sys.path.remove(str(self.ws))
        shutil.rmtree(self.root, ignore_errors=True)

    def calls(self):
        path = self.ws / "lib/calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_recall_is_recorded_first_and_hits_carry_provenance(self):
        hits = self.memory.recall("чай", k=5, scope=("all-vault",), semantic=False)
        call = self.calls()[0]
        self.assertEqual((call["kind"], call["params"]["scopes"], call["principal"]), ("knowledge.recall", ["all-vault"], "agent:butler"))
        self.assertNotIn("чай", json.dumps(call))                              # only a checksum of the query leaves the library
        labels = {Path(h.path).name: h.provenance for h in hits}
        self.assertIn("canonical (owner said)", labels["2026-10-20-boris-loves-tea.md"])
        self.assertIn("model inference, unverified", labels["2026-10-21-tea-market.md"])
        self.assertIn("human-authored, canonical", labels["notes.md"])

    def test_refused_or_absent_core_means_no_search(self):
        (self.ws / "lib/mode").write_text("deny")
        with self.assertRaises(self.memory.RecallRefused):
            self.memory.recall("чай", k=5, scope=("all-vault",), semantic=False)
        (self.ws / "lib/mode").write_text("down")
        with self.assertRaises(self.memory.RecallRefused):
            self.memory.recall("чай", k=5, scope=("all-vault",), semantic=False)
        (self.ws / "lib/switches").write_text("")
        hits = self.memory.recall("чай", k=5, scope=("all-vault",), semantic=False)
        self.assertGreaterEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()

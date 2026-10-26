"""REQ-021: module boundaries, versioned contracts and the core starting with test adapters."""
import ast
import json
import sys
import unittest
from pathlib import Path

from butler_core import contracts

PACKAGE = Path(__file__).resolve().parents[1] / "butler_core"
EXAMPLES = PACKAGE / "contracts" / "examples" / "v1"

# Module -> butler_core modules it may import. Anything else is a boundary violation.
ALLOWED = {
    "contracts": set(),
    "store": set(),
    "diagnostics": {"store"},
    "config": {"contracts", "store"},
    "policy": {"contracts", "config", "store"},
    "approvals": {"contracts", "store"},
    "events": {"contracts", "store"},
    "tasks": {"contracts", "store"},
    "operations": {"contracts", "adapters.base", "approvals", "store"},
    "delivery": {"contracts", "adapters.base", "store"},
    "routing": {"approvals", "operations", "store", "tasks"},
    "workers": set(),
    "backup": {"diagnostics", "store"},
    "cron": set(),
    "feedback": {"store"},
    "server": {"approvals", "config", "diagnostics", "feedback", "operations", "store", "tasks"},
    "client": set(),
    "system1": set(),
    "system3": {"store"},
    "service": None,     # composition root
    "cli": None,         # interface; wires the daemon
    "daemon": None,
    "__init__": set(),
}
ADAPTER_ALLOWED = {"contracts", "adapters"}


def imports(path, package_parts):
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[:len(package_parts) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
                if node.module is None:
                    for alias in node.names:
                        found.add(".".join(base + [alias.name]))
                    continue
            else:
                target = node.module or ""
            if target.startswith("butler_core"):
                found.add(target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("butler_core"):
                    found.add(alias.name)
    return {name.removeprefix("butler_core").lstrip(".") for name in found}


class BoundaryTest(unittest.TestCase):
    def test_every_module_has_a_rule(self):
        for path in PACKAGE.glob("*.py"):
            self.assertIn(path.stem, ALLOWED, f"{path.name} has no boundary rule")

    def test_prohibited_imports_are_detected(self):
        violations = []
        for path in PACKAGE.glob("*.py"):
            rule = ALLOWED[path.stem]
            if rule is None:
                continue
            used = {name for name in imports(path, ["butler_core"]) if name and name != path.stem}
            for name in sorted(used):
                top = name.split(".")[0]
                if name not in rule and top not in rule:
                    violations.append(f"{path.stem} -> {name}")
        for path in (PACKAGE / "adapters").glob("*.py"):
            for name in imports(path, ["butler_core", "adapters"]):
                if name.split(".")[0] not in ADAPTER_ALLOWED:
                    violations.append(f"adapters.{path.stem} -> {name}")
        self.assertEqual(violations, [])

    def test_checker_catches_a_violation(self):
        probe = Path(self.id().replace(".", "_") + ".py")
        target = PACKAGE.parent / "tests" / probe
        target.write_text("from butler_core.service import Core\nfrom ..store import Store\n")
        try:
            self.assertEqual(imports(target, ["butler_core", "adapters"]), {"service", "store"})
        finally:
            target.unlink()


class ContractTest(unittest.TestCase):
    def test_retained_examples_remain_compatible(self):
        names = sorted(p.name for p in EXAMPLES.glob("*.json") if not p.name.startswith("incompatible-"))
        self.assertEqual(len(names), len(contracts.SCHEMAS))
        for path in EXAMPLES.glob("*.json"):
            data = json.loads(path.read_text())
            if path.name.startswith("incompatible-"):
                with self.subTest(path.name), self.assertRaises(contracts.ContractError) as caught:
                    contracts.validate(data["data"])
                self.assertEqual(caught.exception.field, data["expect_field"])
            else:
                with self.subTest(path.name):
                    self.assertEqual(contracts.validate(data)["contract"], path.stem)

    def test_incompatible_input_has_a_reason(self):
        with self.assertRaises(contracts.ContractError) as caught:
            contracts.validate({"contract": "task", "version": 9})
        self.assertIn("supported [1]", caught.exception.reason)

    def test_rejections_are_recorded(self):
        sys.path.insert(0, str(Path(__file__).parent))
        from support import CoreCase

        class Probe(CoreCase):
            def runTest(self):
                with self.assertRaises(contracts.ContractError):
                    self.core.events.admit({"source": "test", "kind": "message", "persona": "butler",
                                            "occurred_at": 1.0, "dedup_key": "x", "privacy": "nope"})
                row = self.core.store.one("SELECT reason, field FROM rejections")
                self.assertEqual((row["reason"], row["field"]), ("invalid value", "privacy"))

        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.failures + result.errors, [])


class TestAdapterStartupTest(unittest.TestCase):
    def test_core_starts_with_test_adapters_only(self):
        sys.path.insert(0, str(Path(__file__).parent))
        from support import CoreCase

        class Probe(CoreCase):
            def runTest(self):
                recovery = self.core.start()
                self.assertEqual(recovery["dispatching"], [])
                result = self.core.submit(self.request(), "rules")
                row = self.core.wait(result["id"], 5)
                self.assertEqual(row["state"], "succeeded")
                self.assertEqual(self.core.doctor()["adapters"], ["test"])

        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.failures + result.errors, [])


if __name__ == "__main__":
    unittest.main()

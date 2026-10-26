"""W9: Tatyana's pull requests as core operations — one per branch and commit, settled from GitHub after a lost answer."""
import json
import os
import stat
import unittest

from butler_core.adapters.github import GitHubAdapter
from butler_core.service import Core
from support import CoreCase, base_config

FAKE_CLI = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent.parent
mode = (here / "mode").read_text().strip() if (here / "mode").exists() else "ok"
args = sys.argv[1:]
(here / "calls.jsonl").open("a").write(json.dumps(args) + "\n")
def opt(name): return args[args.index(name) + 1]
if args[0] == "open":
    branch = opt("--branch")
    if not branch.startswith(("feature/", "chore/", "fix/")):
        print(f"error: branch {branch!r} is not feature/*, chore/* or fix/*", file=sys.stderr); sys.exit(1)
    if mode == "die-after-push":
        (here / "pushed").write_text(branch); sys.exit(137)
    print(json.dumps({"number": 214, "url": "https://github.com/t-boris/Tetyana-Dyck-Web/pull/214", "state": "OPEN", "branch": branch})); sys.exit(0)
if args[0] == "find":
    if (here / "pushed").exists() and (here / "pr-appeared").exists():
        print(json.dumps({"number": 215, "url": "https://github.com/t-boris/Tetyana-Dyck-Web/pull/215", "state": "OPEN"}))
    else:
        print("{}")
    sys.exit(0)
sys.exit(2)
'''


def github_config():
    config = base_config()
    config["operations"]["github.pr"] = {"adapter": "github", "approval": "none", "reconcile": "auto", "max_attempts": 2, "timeout": 900,
                                         "export": [{"category": "repository", "recipient": "github"}]}
    config["personas"]["tetyana-dyck"]["actions"] += ["github.pr"]
    config["exports"].append({"persona": "tetyana-dyck", "category": "repository", "recipient": "github"})
    config["limits"]["workers"]["github"] = 1
    return config


class GitHubTest(CoreCase):
    def make_core(self, **kwargs):
        self.ws = self.tmp / "tetyana"
        (self.ws / "bin").mkdir(parents=True)
        cli = self.ws / "bin/tetyana-pr"
        cli.write_text(FAKE_CLI)
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        self.github = GitHubAdapter(self.ws, os.environ.get("PATH", "/usr/bin:/bin"), {"TMPDIR": "/tmp"})
        return Core(self.tmp / "state", adapters={"test": self.adapter, "github": self.github},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        self.activate(github_config())

    def calls(self):
        path = self.ws / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def submit(self, branch="feature/banner", key="pr1", persona="tetyana-dyck", sha="abc123def456"):
        return self.core.submit({"kind": "github.pr", "persona": persona, "principal": f"agent:{persona}",
                                 "target": "github:t-boris/Tetyana-Dyck-Web",
                                 "params": {"branch": branch, "head_sha": sha, "title": "Banner", "repo": "t-boris/Tetyana-Dyck-Web"},
                                 "idempotency_key": f"pr:{branch}:{sha[:12]}" if key == "pr1" else key}, "system2")

    def test_pull_request_is_opened_once_per_branch_and_commit(self):
        op = self.submit()
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual(self.core.operations.get(op["id"])["receipt"], "github:pr:214")
        self.assertEqual(self.calls()[0][:5], ["open", "--branch", "feature/banner", "--title", "Banner"])
        again = self.submit()
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.calls()), 1)

    def test_wrong_branch_is_refused_and_butler_persona_is_denied(self):
        op = self.submit(branch="main", key="pr2")
        self.assertEqual(self.core.operations.run(op["id"]), "failed")
        self.assertIn("not feature/", self.core.operations.get(op["id"])["reason"])
        denied = self.submit(key="pr3", persona="butler")
        self.assertEqual(denied["state"], "denied")

    def test_lost_answer_is_settled_from_github_without_a_second_push(self):
        (self.ws / "mode").write_text("die-after-push")
        op = self.submit(branch="feature/lost", key="pr4")
        self.assertEqual(self.core.operations.run(op["id"]), "requested")          # not found yet: retry allowed
        (self.ws / "pr-appeared").write_text("")
        self.core.store.db.execute("UPDATE operations SET state='dispatching' WHERE id=?", (op["id"],))
        self.assertEqual(self.core.operations.reconcile(op["id"]), "succeeded")
        self.assertEqual(self.core.operations.get(op["id"])["receipt"], "github:pr:215")
        self.assertEqual(len([c for c in self.calls() if c[0] == "open"]), 1)


if __name__ == "__main__":
    unittest.main()

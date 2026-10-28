"""W9 compatibility: the installed ``tetyana-pr`` wrapper with git and GitHub faked and a real core socket.

A temporary git repository stands in for the checkout, a recording script for
``gh``; nothing reaches GitHub. The switched path submits ``github.pr`` to a
real core, which runs the same wrapper with ``--core-operation``.
"""
from support import require_files
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from butler_core.adapters.github import GitHubAdapter
from butler_core.server import Server
from butler_core.service import Core
from support import CoreCase
from test_github import github_config
from test_runtime import short_dir

INSTALLED = Path.home() / ".openclaw/workspace-tetyana-dyck/bin/tetyana-pr"
CLIENT = Path(__file__).resolve().parents[1] / "butler_core/client.py"

FAKE_GH = r'''#!/usr/bin/env python3
"""Records every gh call; answers pr list/create/merge/view from a small local state."""
import json, sys
from pathlib import Path
state = Path(__file__).resolve().parent / "gh-state.json"
data = json.loads(state.read_text()) if state.exists() else {"prs": {}, "calls": []}
args = sys.argv[1:]
data["calls"].append(args)
def save(): state.write_text(json.dumps(data))
if args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    pr = data["prs"].get(head)
    print(json.dumps([pr] if pr else [])); save(); sys.exit(0)
if args[:2] == ["pr", "create"]:
    head = args[args.index("--head") + 1]
    number = 300 + len(data["prs"])
    data["prs"][head] = {"number": number, "url": f"https://github.com/t-boris/Tetyana-Dyck-Web/pull/{number}", "state": "OPEN", "mergeStateStatus": "CLEAN"}
    print(data["prs"][head]["url"]); save(); sys.exit(0)
if args[:2] == ["pr", "merge"]:
    assert "--squash" in args and "--auto" in args and "--delete-branch" in args and "--admin" not in args
    save(); sys.exit(0)
if args[:2] == ["pr", "view"]:
    number = int(args[2])
    for pr in data["prs"].values():
        if pr["number"] == number:
            pr["state"] = "MERGED"; print(json.dumps(pr)); save(); sys.exit(0)
save(); sys.exit(1)
'''


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def fake_checkout(root):
    repo = root / "checkout"
    remote = root / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# site\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "checkout", "-q", "-b", "feature/banner")
    (repo / "banner.txt").write_text("banner\n")
    git(repo, "add", "banner.txt")
    git(repo, "commit", "-q", "-m", "banner")
    return repo, remote


class TetyanaPRTest(CoreCase):
    def make_core(self, **kwargs):
        require_files(INSTALLED)
        self.ws = self.tmp / "tetyana"
        (self.ws / "bin").mkdir(parents=True)
        shutil.copy(INSTALLED, self.ws / "bin/tetyana-pr")
        (self.ws / "bin/tetyana-pr").chmod(0o755)
        gh = self.ws / "bin/gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.repo, self.remote = fake_checkout(self.tmp)
        self.env_extra = {"TETYANA_CHECKOUT": str(self.repo), "GH_BIN": str(gh), "TETYANA_REPO": "t-boris/Tetyana-Dyck-Web"}
        self.github = GitHubAdapter(self.ws, os.environ.get("PATH", "/usr/bin:/bin"), {"TMPDIR": "/tmp", **self.env_extra})
        return Core(self.tmp / "state", adapters={"test": self.adapter, "github": self.github},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        config = github_config()
        config["scenarios"]["github"] = {"enabled": True}
        self.activate(config)
        self.sockdir = short_dir()
        self.server = Server(self.core, self.sockdir / "core.sock", classify=lambda pid: "local")
        self.server.start()
        self.core.start()

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.sockdir, ignore_errors=True)
        super().tearDown()

    def cli(self, *arguments, switched=True):
        env = {**os.environ, **self.env_extra, "BUTLER_CORE_STATE": str(self.core.store.root),
               "BUTLER_CORE_SOCKET": str(self.sockdir / "core.sock")}
        if not switched:
            env["BUTLER_CORE_STATE"] = str(self.tmp / "no-core")
        return subprocess.run([sys.executable, str(self.ws / "bin/tetyana-pr"), *arguments], capture_output=True, text=True,
                              env=env, cwd=self.ws, timeout=120)

    def gh_calls(self):
        state = self.ws / "bin/gh-state.json"
        return json.loads(state.read_text())["calls"] if state.exists() else []

    def test_switched_open_pushes_creates_and_arms_auto_merge_once(self):
        done = self.cli("open", "--branch", "feature/banner", "--title", "Banner")
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout)
        self.assertEqual(receipt["number"], 300)
        self.assertEqual(git(self.remote, "rev-parse", "feature/banner"), git(self.repo, "rev-parse", "feature/banner"))
        kinds = [c[:2] for c in self.gh_calls()]
        self.assertEqual(kinds, [["pr", "list"], ["pr", "create"], ["pr", "list"], ["pr", "merge"]])
        ops = self.core.operations.list(limit=5)
        self.assertEqual([(o["kind"], o["state"], o["persona"]) for o in ops], [("github.pr", "succeeded", "tetyana-dyck")])
        again = self.cli("open", "--branch", "feature/banner", "--title", "Banner")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len([c for c in self.gh_calls() if c[:2] == ["pr", "create"]]), 1)

    def test_main_and_odd_branches_are_refused_before_anything_is_pushed(self):
        for branch in ("main", "hotfix/x"):
            done = self.cli("open", "--branch", branch, "--title", "Nope")
            self.assertEqual(done.returncode, 1, branch)
            self.assertIn("not feature/", done.stderr)
        self.assertEqual(self.gh_calls(), [])
        self.assertEqual(self.core.operations.list(limit=5), [])

    def test_core_down_pushes_nothing(self):
        self.server.stop()
        done = self.cli("open", "--branch", "feature/banner", "--title", "Banner")
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(self.gh_calls(), [])
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.remote, "rev-parse", "feature/banner")

    def test_switch_off_runs_the_wrapper_directly(self):
        done = self.cli("open", "--branch", "feature/banner", "--title", "Banner", switched=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.core.operations.list(limit=5), [])
        self.assertEqual(len([c for c in self.gh_calls() if c[:2] == ["pr", "create"]]), 1)
        waited = self.cli("wait", "--number", "300", "--minutes", "1", switched=False)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["state"], "MERGED")


if __name__ == "__main__":
    unittest.main()

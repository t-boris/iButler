"""Back Burner researcher, step 1: the confined, silent ``backburner`` dispatch route (REQ-004, REQ-006).

Like test_dispatch, the harness copies the installed ``lib/dispatch.py`` into a temporary workspace and
replaces the coding backend with a script that records how it was started (arguments, working directory).
The notifier and the gateway CLI are recorders too, so a status line or a butler wake would be seen.
Set BUTLER_DISPATCH_UNDER_TEST to a folder holding ``lib/dispatch.py`` and ``lib/cost.py`` to test a
candidate (or a mutant) instead of the installed file.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import test_dispatch
from test_dispatch import INSTALLED, PYTHON, fake_workspace, substitute, wait_for
from test_runtime import short_dir

SOURCE = Path(os.environ.get("BUTLER_DISPATCH_UNDER_TEST") or INSTALLED)
TOOLS = "Read,Edit,Write,Glob,Grep,WebSearch,WebFetch"

RECORDING_BACKEND = """#!/bin/sh
pwd -P > '{root}/cwd.log'
for a in "$@"; do printf '%s\\0' "$a" >> '{root}/argv.log'; done
echo '{{"type":"result","result":"fake result"}}'
"""
RECORDER = """#!/bin/sh
printf '%s\\0' "$*" >> '{root}/{name}.log'
"""


@unittest.skipUnless((SOURCE / "lib/dispatch.py").is_file() and PYTHON.exists(), "Butler workspace not installed")
class BackburnerRouteCase(unittest.TestCase):
    def setUp(self):
        self.root = short_dir()
        installed = test_dispatch.INSTALLED
        test_dispatch.INSTALLED = SOURCE
        self.addCleanup(setattr, test_dispatch, "INSTALLED", installed)
        self.ws = fake_workspace(self.root)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.ws / "fake-backend").write_text(RECORDING_BACKEND.format(root=self.ws))
        (self.ws / "bin").mkdir()
        for name, path in (("notify", self.ws / "bin/butler-notify"), ("wake", self.ws / "bin/openclaw-recorder")):
            path.write_text(RECORDER.format(root=self.ws, name=name))
            path.chmod(0o755)
        dispatch = self.ws / "lib/dispatch.py"
        dispatch.write_text(substitute(dispatch.read_text(), 'OPENCLAW_BIN = "/usr/bin/true"',
                                       f'OPENCLAW_BIN = "{self.ws}/bin/openclaw-recorder"'))
        self.project = self.ws / "projects/demo"

    def python(self, code):
        return subprocess.run([str(PYTHON), "-c", code], cwd=self.ws, capture_output=True, text=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "PYTHONPATH": str(self.ws)})

    def build(self, backend, model, prompt, task_kind, streaming=False):
        code = ("import json; from lib import dispatch; "
                f"cmd = dispatch._command({task_kind!r}, {backend!r}, {model!r}, {prompt!r}, streaming={streaming!r}); "
                "print(json.dumps({'cmd': cmd, 'preamble': dispatch.NOTIFY_PREAMBLE}))")
        done = self.python(code)
        return done, (json.loads(done.stdout) if done.returncode == 0 else None)

    def records(self, name):
        """One entry per call of the notifier or the gateway CLI (a call may span several lines)."""
        path = self.ws / f"{name}.log"
        return path.read_bytes().decode().split("\0")[:-1] if path.exists() else []

    def argv(self):
        return (self.ws / "argv.log").read_bytes().decode().split("\0")[:-1]

    def start(self, kind, dispatch_id):
        done = subprocess.run([str(PYTHON), "-m", "lib.dispatch", "start", kind, "--project", str(self.project),
                               "--prompt", "DO THE WORK", "--id", dispatch_id], cwd=self.ws, capture_output=True,
                              text=True, timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
                                                          "PYTHONPATH": str(self.ws)})
        self.assertEqual(done.returncode, 0, done.stderr)
        directory = self.ws / "state/dispatches" / dispatch_id
        wait_for(lambda: (directory / "result.json").exists())
        pid = json.loads((directory / "meta.json").read_text())["monitor_pid"]
        # The wake runs after the result is written; the monitor is finished only when its process is gone.
        wait_for(lambda: subprocess.run(["/bin/ps", "-p", str(pid)], capture_output=True).returncode != 0)


class BackburnerCommandTest(BackburnerRouteCase):
    def test_route_is_opus_5_5_silent_and_limited_to_the_editing_and_web_tools(self):
        done = self.python("import json, typing; from lib import dispatch; "
                           "print(json.dumps({'route': dispatch.ROUTES['backburner'], "
                           "'kinds': list(typing.get_args(dispatch.TaskKind))}))")
        self.assertEqual(done.returncode, 0, done.stderr)
        found = json.loads(done.stdout)
        self.assertEqual(found["route"], {"backend": "claude", "model": "claude-opus-5-5", "timeout": 2700,
                                          "silent": True, "tools": TOOLS.split(",")})
        self.assertIn("backburner", found["kinds"])

    def test_command_is_confined_with_no_shell_and_no_bypassed_permissions(self):
        done, found = self.build("claude", "claude-opus-5-5", "DO THE WORK", "backburner")
        self.assertEqual(done.returncode, 0, done.stderr)
        cmd = found["cmd"]
        self.assertEqual(cmd[:3], [str(self.ws / "fake-backend"), "-p", "DO THE WORK"])
        self.assertIn("--restricted", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], TOOLS)
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], TOOLS)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-5-5")
        self.assertNotIn("bypassPermissions", cmd)
        self.assertFalse([part for part in cmd if "Bash" in part])

    def test_prompt_carries_no_instruction_to_notify_because_the_worker_has_no_shell(self):
        done, found = self.build("claude", "claude-opus-5-5", "DO THE WORK", "backburner")
        prompt = found["cmd"][2]
        self.assertEqual(prompt, "DO THE WORK")
        self.assertNotIn("butler-notify", prompt)
        self.assertNotIn(found["preamble"], prompt)

    def test_streaming_keeps_the_confinement(self):
        done, found = self.build("claude", "claude-opus-5-5", "DO THE WORK", "backburner", streaming=True)
        cmd = found["cmd"]
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        for flag in ("--restricted", "--strict-mcp-config", "--include-partial-messages", "--verbose"):
            self.assertIn(flag, cmd)
        self.assertNotIn("bypassPermissions", cmd)

    def test_a_confined_route_refuses_a_backend_that_cannot_be_confined(self):
        done, found = self.build("codex", "gpt-6.1-sol", "DO THE WORK", "backburner")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("confined", done.stderr)

    def test_other_routes_keep_the_unchanged_command(self):
        for kind in ("review", "code", "curate", None):
            with self.subTest(kind=kind):
                done, found = self.build("claude", "claude-sonnet-5", "DO THE WORK", kind)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(found["cmd"], [str(self.ws / "fake-backend"), "-p", found["preamble"] + "DO THE WORK",
                                                "--model", "claude-sonnet-5", "--output-format", "json",
                                                "--permission-mode", "bypassPermissions"])

    def test_owner_ai_settings_can_change_the_model_but_never_the_confinement(self):
        (self.ws / "config").mkdir()
        (self.ws / "config/ai-settings.json").write_text(json.dumps(
            {"routes": {"backburner": {"backend": "claude", "model": "claude-opus-4-8"}}}))
        done, found = self.build("claude", "claude-opus-4-8", "DO THE WORK", "backburner")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--restricted", found["cmd"])
        route = self.python("import json; from lib import dispatch; print(json.dumps(dispatch.ROUTES['backburner']))")
        self.assertEqual(json.loads(route.stdout), {"backend": "claude", "model": "claude-opus-4-8", "timeout": 2700,
                                                    "silent": True, "tools": TOOLS.split(",")})
        (self.ws / "config/ai-settings.json").write_text(json.dumps(
            {"routes": {"backburner": {"backend": "claude", "model": "claude-opus-4-8", "tools": ["Bash"]}}}))
        refused = self.python("from lib import dispatch")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Invalid owner Butler AI Settings", refused.stderr)


class BackburnerRunTest(BackburnerRouteCase):
    def test_synchronous_dispatch_runs_in_the_given_folder_with_the_confined_command(self):
        done = self.python(f"from lib import dispatch; r = dispatch.dispatch('backburner', {str(self.project)!r}, "
                           "'DO THE WORK'); print(r.success, r.exit_code)")
        self.assertEqual(done.stdout.strip(), "True 0", done.stderr)
        self.assertEqual((self.ws / "cwd.log").read_text().strip(), os.path.realpath(self.project))
        argv = self.argv()
        self.assertEqual(argv[1:2], ["DO THE WORK"])
        self.assertIn("--restricted", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_background_worker_of_a_silent_route_posts_no_status_and_wakes_nobody(self):
        self.start("backburner", "20261027-010000-aaaaaa")
        self.assertEqual(self.records("notify"), [])
        self.assertEqual(self.records("wake"), [])
        argv = self.argv()
        self.assertEqual(argv[1], "DO THE WORK")
        self.assertIn("--restricted", argv)
        status = json.loads(subprocess.run([str(PYTHON), "-m", "lib.dispatch", "status", "20261027-010000-aaaaaa",
                                            "--json"], cwd=self.ws, capture_output=True, text=True,
                                           env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(self.ws)}).stdout)
        self.assertEqual(status["status"], "done")

    def test_background_start_on_the_wrong_backend_is_refused_before_any_job_exists(self):
        done = subprocess.run([str(PYTHON), "-m", "lib.dispatch", "start", "backburner", "--project", str(self.project),
                               "--prompt", "DO THE WORK", "--backend", "codex", "--id", "20261027-010002-cccccc"],
                              cwd=self.ws, capture_output=True, text=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "PYTHONPATH": str(self.ws)})
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("confined", done.stderr)
        self.assertFalse((self.ws / "state/dispatches/20261027-010002-cccccc").exists())

    def test_core_start_on_the_wrong_backend_is_refused_before_the_core_is_asked(self):
        done = self.python("from lib import dispatch\n"
                           "def asked():\n    raise SystemExit('core was asked')\n"
                           "dispatch._core = asked\n"
                           f"dispatch.core_start('backburner', {str(self.project)!r}, 'X', backend_override='codex')")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("confined", done.stderr)
        self.assertNotIn("core was asked", done.stderr)

    def test_ordinary_routes_still_call_build_command_with_its_original_signature(self):
        # test_work_tasks (ambient) replaces _build_command with this four-argument form; a route that is
        # not confined must keep reaching it with exactly these arguments.
        dispatch = self.ws / "lib/dispatch.py"
        guard = 'if __name__ == "__main__":'      # the override must exist before _main runs
        text = dispatch.read_text()
        self.assertEqual(text.count(guard), 1)
        dispatch.write_text(text.replace(guard, "def _build_command(backend, model, prompt, *, streaming):\n"
                                                "    import sys\n"
                                                "    return [sys.executable, '-c', 'print(\"replaced command ran\")']\n\n\n"
                                                + guard))
        self.start("review", "20261027-010003-dddddd")
        directory = self.ws / "state/dispatches/20261027-010003-dddddd"
        self.assertEqual(json.loads((directory / "result.json").read_text())["exit_code"], 0)
        self.assertIn("replaced command ran", (directory / "output.log").read_text())

    def test_control_an_ordinary_route_still_posts_status_and_wakes_the_butler(self):
        self.start("review", "20261027-010001-bbbbbb")
        notices = self.records("notify")
        self.assertEqual(len(notices), 2, notices)
        self.assertIn("▶ review", notices[0])
        self.assertIn("✅ review", notices[1])
        wakes = self.records("wake")
        self.assertEqual(len(wakes), 1, wakes)
        self.assertIn("[task-done] dispatch 20261027-010001-bbbbbb", wakes[0])
        self.assertIn("bypassPermissions", self.argv())


if __name__ == "__main__":
    unittest.main()

"""Source-wide vault coverage and canonical compatibility, using only a temporary vault."""
import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"; self.vault.mkdir()
        self.ws = self.root / "workspace"; (self.ws / "lib").mkdir(parents=True)
        (self.ws / "lib/__init__.py").write_text("")
        for name in ("knowledge_sources.py", "lessons.py", "embeddings.py"):
            content = (SOURCE / "workspace/lib" / name).read_text()
            content = content.replace('WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")', f'WORKSPACE = Path({str(self.ws)!r})')
            (self.ws / "lib" / name).write_text(content)
        shutil.copy2(SOURCE / "butler_core/rules.py", self.ws / "lib/butler_rules.py")
        (self.ws / "lib/vault.py").write_text("from pathlib import Path\ndef vault_root(): return Path(" + repr(str(self.vault)) + ")\n")
        for key in list(sys.modules):
            if key == "lib" or key.startswith("lib."): sys.modules.pop(key)
        sys.path.insert(0, str(self.ws))

    def tearDown(self):
        for key in list(sys.modules):
            if key == "lib" or key.startswith("lib."): sys.modules.pop(key)
        sys.path.remove(str(self.ws)); self.tmp.cleanup()

    def test_whole_vault_includes_nondefault_folders_but_excludes_secrets_archives_symlinks(self):
        paths = ["Unlisted/notes.md", "Knowledge/fact.md", "Secrets.md", "Personal/Secrets.md", "Research/Archive/old.md"]
        for name in paths:
            path = self.vault / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("synthetic")
        (self.vault / "link.md").symlink_to(self.vault / "Knowledge/fact.md")
        sources = importlib.import_module("lib.knowledge_sources")
        self.assertEqual({str(p.relative_to(self.vault)) for p in sources.files(self.vault)}, {"Unlisted/notes.md", "Knowledge/fact.md"})

    def test_lessons_cli_and_preferences_share_one_canonical_store(self):
        lessons = importlib.import_module("lib.lessons")
        lessons.add_new_lesson("L-2026-10-25-1", "Кратко", "Говорить кратко.")
        from lib.butler_rules import RuleStore
        rules = RuleStore(self.vault, self.ws)
        rules.put("preference-abc", "Предпочтение", "Кратко.", operation="p1", authorized=True)
        self.assertEqual(len(lessons.list_top()), 2)
        self.assertIn("Говорить кратко", lessons.get("L-2026-10-25-1"))
        lessons.retire("L-2026-10-25-1")
        self.assertIsNone(lessons.get("L-2026-10-25-1"))
        sources = importlib.import_module("lib.knowledge_sources")
        eligible = {p.name for p in sources.files(self.vault)}
        self.assertNotIn("L-2026-10-25-1.md", eligible)
        self.assertIn("preference-abc.md", eligible)
        self.assertEqual(lessons.auto_confirm_from_sessions()["confirmed"], {})

    def test_semantic_windows_bound_a_single_long_paragraph_without_losing_text(self):
        embeddings=importlib.import_module('lib.embeddings')
        text=''.join(f'{i:08d}X' for i in range(3000))
        windows=embeddings._split_long(text,7)
        end=0
        for line,value in windows:
            self.assertEqual(line,7)
            self.assertLessEqual(len(value),embeddings.WINDOW_SIZE)
            start=text.index(value)
            self.assertLessEqual(start,end)
            end=max(end,start+len(value))
        self.assertEqual(end,len(text))

    def test_semantic_model_inputs_bound_both_long_titles_and_body(self):
        e=importlib.import_module('lib.embeddings')
        path=self.vault/'large.md';path.write_text('---\ntitle: '+('T'*5000)+'\n---\n# '+('H'*5000)+'\n'+('word '*3000))
        seen=[]
        class Vector:
            def tobytes(self):return b'\0'*3072
        def embed(texts):
            seen.extend(texts);return [Vector() for _ in texts]
        with e._connect() as con,patch.object(e,'embed_docs',side_effect=embed):
            count=e._index_one(con,path,'large.md','vault',path.stat())
        self.assertGreater(count,1)
        self.assertTrue(all(len(value)<2000 for value in seen))


if __name__ == "__main__": unittest.main()


class QuietWaitTest(SourcesTest):
    """wait_for_quiet asks the ambient service before each embedding batch; one slow answer must not kill a rebuild."""

    def serve(self, replies):
        """A stand-in ambient socket: each entry is a JSON status, or None to hang past the client's timeout."""
        home = Path(tempfile.mkdtemp(prefix="qw-", dir="/tmp"))               # short: AF_UNIX paths are capped at 104 bytes
        self.addCleanup(shutil.rmtree, home, True); (home / ".local/share/butler-ambient").mkdir(parents=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(home / ".local/share/butler-ambient/ambient.sock")); server.listen(8); server.settimeout(10)
        self.addCleanup(server.close)
        seen = []

        def run():
            for reply in replies:
                try: connection, _ = server.accept()
                except OSError: return
                with connection:
                    seen.append(connection.makefile("rb").readline())
                    if reply is None: time.sleep(1.2)
                    else: connection.sendall((json.dumps({"result": reply}) + "\n").encode())
        threading.Thread(target=run, daemon=True).start()
        patcher = patch.object(Path, "home", lambda: home); patcher.start(); self.addCleanup(patcher.stop)
        return seen

    def test_a_single_slow_answer_is_retried_and_the_batch_goes_on(self):
        seen = self.serve([None, {"enabled": True, "analysis_busy": False}])
        sources = importlib.import_module("lib.knowledge_sources")
        started = time.monotonic(); sources.wait_for_quiet()
        self.assertEqual(len(seen), 2)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_service_that_keeps_failing_is_reported_after_a_bounded_wait(self):
        self.serve([None] * 12)
        sources = importlib.import_module("lib.knowledge_sources")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "background-analysis-status-unavailable"): sources.wait_for_quiet()
        self.assertLess(time.monotonic() - started, 12)        # bounded: a few tries, not the 120-step busy loop

    def test_a_paused_service_and_a_busy_one_keep_their_meaning(self):
        self.serve([{"enabled": False}])
        sources = importlib.import_module("lib.knowledge_sources")
        with self.assertRaisesRegex(RuntimeError, "background-analysis-paused"): sources.wait_for_quiet()
        for key in list(sys.modules):
            if key.startswith("lib."): sys.modules.pop(key)
        self.serve([{"enabled": True, "analysis_busy": True}, None, {"enabled": True, "analysis_busy": False}])
        importlib.import_module("lib.knowledge_sources").wait_for_quiet()   # busy, then a blip, then free

    def test_misses_count_only_in_a_row_so_a_long_busy_stretch_with_blips_still_waits(self):
        busy = {"enabled": True, "analysis_busy": True}
        self.serve([None, busy] * 5 + [{"enabled": True, "analysis_busy": False}])   # 5 blips, never 5 in a row
        importlib.import_module("lib.knowledge_sources").wait_for_quiet()

    def test_no_service_means_no_waiting(self):
        home = self.root / "nohome"; home.mkdir()
        with patch.object(Path, "home", lambda: home):
            importlib.import_module("lib.knowledge_sources").wait_for_quiet()


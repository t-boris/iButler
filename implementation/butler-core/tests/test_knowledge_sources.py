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
import types
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

    def test_generated_and_dependency_folders_are_outside_the_vault_boundary_but_ordinary_names_stay_in(self):
        """2026-11-02: 575 of 2660 indexed files were node_modules documentation. Generated folders are not notes."""
        generated = ["App/node_modules/pkg/README.md", "App/node_modules/@scope/pkg/CHANGELOG.md", "Tool/.venv/lib/x.md",
                     "Tool/venv/lib/x.md", "Py/__pycache__/x.md", "Web/.next/cache/x.md", "Repo/.git/info/x.md",
                     "Py/site-packages/pkg/x.md", "Web/.cache/x.md", "Py/.pytest_cache/README.md"]
        ordinary = ["Hobby/Build/plan.md", "Hobby/Dist/notes.md", "Hobby/target/notes.md", "Projects/Cache Ideas/a.md", "Notes/venv-setup.md"]
        for name in generated + ordinary:
            path = self.vault / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("synthetic")
        sources = importlib.import_module("lib.knowledge_sources")
        found = {str(p.relative_to(self.vault)) for p in sources.files(self.vault)}
        self.assertEqual(found, set(ordinary))
        for name in generated: self.assertFalse(sources.eligible(self.vault / name, self.vault), name)

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

    def test_full_reindex_failure_preserves_the_previous_index(self):
        e = importlib.import_module('lib.embeddings')
        for name in ('a.md', 'b.md'):
            (self.vault / name).write_text('# Synthetic\n' + 'example words ' * 60)
        with e._connect() as con:
            e._stamp_meta(con)
            con.execute("INSERT INTO files VALUES ('old.md', 1, 1, 1)")
            con.execute("INSERT INTO chunks (path,scope,heading,start_line,end_line,date,text,embedding) VALUES ('old.md','vault','',1,1,NULL,'synthetic',?)", (b'old',))
            con.commit()
        class Vector:
            def tobytes(self): return b'new'
        with patch.object(e, 'is_available', return_value=True), patch.object(e, 'embed_docs', side_effect=[[Vector()], RuntimeError('synthetic failure')]):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                e.reindex(full=True)
        with e._connect() as con:
            self.assertEqual(con.execute('SELECT path FROM files').fetchall(), [('old.md',)])
            self.assertEqual(con.execute('SELECT path,embedding FROM chunks').fetchall(), [('old.md', b'old')])

    def test_a_note_evicted_to_icloud_keeps_its_index_rows_while_deleted_and_excluded_notes_lose_theirs(self):
        """2026-11-02: the vault is on iCloud and the disk is 99 % full, so macOS evicts notes. An evicted note still exists;
        pruning it as "deleted" shrank the index from 2660 to 1275 files within hours."""
        e = importlib.import_module('lib.embeddings')
        (self.vault / 'kept.md').write_text('# Kept\n' + 'example words ' * 60)
        (self.vault / 'evicted.md').write_text('# Evicted\n' + 'example words ' * 60)
        (self.vault / 'App/node_modules/pkg').mkdir(parents=True); (self.vault / 'App/node_modules/pkg/x.md').write_text('synthetic')
        with e._connect() as con:
            e._stamp_meta(con)
            for rel in ('evicted.md', 'deleted.md', 'App/node_modules/pkg/x.md'):
                con.execute("INSERT INTO files VALUES (?, 1, 1, 1)", (rel,))
                con.execute("INSERT INTO chunks (path,scope,heading,start_line,end_line,date,text,embedding) VALUES (?,'vault','',1,1,NULL,'old',?)", (rel, b'old'))
            con.commit()
        real = Path.stat
        def stat(self_, *args, **kwargs):
            st = real(self_, *args, **kwargs)
            if self_.name == 'evicted.md':
                return types.SimpleNamespace(st_flags=getattr(st, 'st_flags', 0) | 0x40000000, st_mtime=st.st_mtime, st_size=st.st_size, st_mode=st.st_mode)
            return st
        class Vector:
            def tobytes(self): return b'new'
        with patch.object(Path, 'stat', stat), patch.object(e, 'is_available', return_value=True), \
                patch.object(e, 'embed_docs', side_effect=lambda texts: [Vector() for _ in texts]):
            result = e.reindex()
        with e._connect() as con:
            paths = {row[0] for row in con.execute('SELECT path FROM files')}
            kept_chunks = con.execute("SELECT COUNT(*) FROM chunks WHERE path='evicted.md'").fetchone()[0]
        self.assertEqual(paths, {'kept.md', 'evicted.md'})
        self.assertEqual(kept_chunks, 1)                      # its old rows are untouched, never re-embedded or downloaded
        self.assertEqual(result.removed, 2)                   # only the deleted note and the excluded folder's note

    def test_full_reindex_does_not_publish_a_skipped_file(self):
        e = importlib.import_module('lib.embeddings')
        path = self.vault / 'new.md'
        path.write_text('synthetic')
        with e._connect() as con:
            e._stamp_meta(con)
            con.execute("INSERT INTO files VALUES ('old.md', 1, 1, 0)")
            con.commit()
        with patch.object(e, 'is_available', return_value=True), patch.object(e, '_index_one', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'full reindex incomplete'):
                e.reindex(full=True)
        with e._connect() as con:
            self.assertEqual(con.execute('SELECT path FROM files').fetchall(), [('old.md',)])

    def test_full_reindex_publishes_only_after_all_files_succeed(self):
        e = importlib.import_module('lib.embeddings')
        path = self.vault / 'new.md'
        path.write_text('# Synthetic\n' + 'example words ' * 60)
        with e._connect() as con:
            e._stamp_meta(con)
            con.execute("INSERT INTO files VALUES ('old.md', 1, 1, 0)")
            con.commit()
        class Vector:
            def tobytes(self): return b'new'
        def embed(texts):
            import sqlite3
            with sqlite3.connect(e.DB_PATH) as reader:
                self.assertEqual(reader.execute('SELECT path FROM files').fetchall(), [('old.md',)])
            return [Vector() for _ in texts]
        with patch.object(e, 'is_available', return_value=True), patch.object(e, 'embed_docs', side_effect=embed):
            result = e.reindex(full=True)
        self.assertEqual(result.reindexed, 1)
        with e._connect() as con:
            self.assertEqual(con.execute('SELECT path FROM files').fetchall(), [('new.md',)])

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


    def test_embedded_base64_data_never_reaches_the_chunker_but_the_prose_and_line_numbers_do(self):
        """2026-11-02: four pasted-image notes were 92-100 % base64 and produced tens of thousands of useless chunks."""
        e = importlib.import_module('lib.embeddings')
        blob = "iVBORw0KGgo" + "A" * 40000
        content = ("# Plan\n\nReal prose about the pickup winding.\n\n![fig](data:image/png;base64," + blob + ")\n\n"
                   "## Next\n\nMore real prose after the image.\n")
        path = self.vault / "images.md"; path.write_text(content)
        seen = []
        class Vector:
            def tobytes(self): return b'\0' * 3072
        def embed(texts): seen.extend(texts); return [Vector() for _ in texts]
        with e._connect() as con, patch.object(e, 'embed_docs', side_effect=embed):
            count = e._index_one(con, path, 'images.md', 'vault', path.stat())
            rows = con.execute("SELECT text, start_line, end_line FROM chunks WHERE path='images.md' ORDER BY start_line").fetchall()
        self.assertGreaterEqual(count, 1); self.assertLess(count, 6)
        self.assertTrue(all("iVBORw0KGgo" not in t and len(t) < 2000 for t in seen))
        joined = " ".join(t for t, *_ in rows)
        self.assertIn("Real prose about the pickup winding", joined); self.assertIn("More real prose after the image", joined)


if __name__ == "__main__": unittest.main()


class QuietWaitTest(SourcesTest):
    """wait_for_quiet asks the ambient service before each embedding batch; one slow answer must not kill a rebuild."""

    def serve(self, replies):
        """A stand-in ambient socket: each entry is a JSON status, None to hang past a shortened client timeout,
        or ("slow", status) to answer after 1.2 s, the latency of a loaded service."""
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
                    else:
                        if isinstance(reply, tuple): time.sleep(1.2); reply = reply[1]
                        connection.sendall((json.dumps({"result": reply}) + "\n").encode())
        threading.Thread(target=run, daemon=True).start()
        patcher = patch.object(Path, "home", lambda: home); patcher.start(); self.addCleanup(patcher.stop)
        return seen

    def test_a_single_slow_answer_is_retried_and_the_batch_goes_on(self):
        seen = self.serve([None, {"enabled": True, "analysis_busy": False}])
        sources = importlib.import_module("lib.knowledge_sources"); sources.STATUS_TIMEOUT = .5
        started = time.monotonic(); sources.wait_for_quiet()
        self.assertEqual(len(seen), 2)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_service_that_keeps_failing_is_reported_after_a_bounded_wait(self):
        self.serve([None] * 12)
        sources = importlib.import_module("lib.knowledge_sources"); sources.STATUS_TIMEOUT = .5
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
        sources = importlib.import_module("lib.knowledge_sources"); sources.STATUS_TIMEOUT = .5
        sources.wait_for_quiet()   # busy, then a blip, then free

    def test_misses_count_only_in_a_row_so_a_long_busy_stretch_with_blips_still_waits(self):
        busy = {"enabled": True, "analysis_busy": True}
        self.serve([None, busy] * 5 + [{"enabled": True, "analysis_busy": False}])   # 5 blips, never 5 in a row
        sources = importlib.import_module("lib.knowledge_sources"); sources.STATUS_TIMEOUT = .5
        sources.wait_for_quiet()

    def test_a_loaded_service_answering_in_over_half_a_second_is_not_a_miss(self):
        """Regression 2026-11-02: status took ~1 s under load, a 0.5 s timeout made every poll a miss and the nightly reindex failed."""
        seen = self.serve([("slow", {"enabled": True, "analysis_busy": False})])
        sources = importlib.import_module("lib.knowledge_sources")             # production timeout, not shortened
        sources.wait_for_quiet()
        self.assertEqual(len(seen), 1)                                          # answered on the first try, no retry needed

    def test_no_service_means_no_waiting(self):
        home = self.root / "nohome"; home.mkdir()
        with patch.object(Path, "home", lambda: home):
            importlib.import_module("lib.knowledge_sources").wait_for_quiet()


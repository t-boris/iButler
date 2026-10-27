"""Source-wide vault coverage and canonical compatibility, using only a temporary vault."""
import importlib
import json
import shutil
import sys
import tempfile
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

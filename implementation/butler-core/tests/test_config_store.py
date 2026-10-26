"""REQ-025: configuration validation, preview and rollback; durable store behaviour."""
import os
import sqlite3
import unittest

from butler_core.config import ConfigError
from butler_core.store import MIGRATIONS, StorageExhausted, Store

from support import CoreCase, base_config


class ConfigTest(CoreCase):
    def test_invalid_configuration_never_activates(self):
        for broken in ({"schema": 2}, base_config(personas={}), base_config(limits={"queue": 0}),
                       base_config(routing={"system1": {"mode": "active"}}),
                       base_config(operations={"x.y": {"adapter": "missing"}}),
                       base_config(unknown_section={})):
            with self.subTest(broken=list(broken)[:3]), self.assertRaises(ConfigError):
                self.core.config.propose(broken, "test")
        self.assertEqual(self.core.config.active()[0], 1)

    def test_preview_activate_and_rollback(self):
        content = base_config()
        content["scenarios"]["dispatch"] = {"enabled": True}
        proposed = self.core.config.propose(content, "test")
        self.assertEqual(proposed["diff"]["added"], {"scenarios.dispatch.enabled": True})
        self.core.config.activate(proposed["version"], "owner")
        self.assertTrue(self.core.config.enabled("dispatch"))
        result = self.core.config.rollback("owner")
        self.assertEqual((result["version"], result["rolled_back"]), (1, 2))
        self.assertFalse(self.core.config.enabled("dispatch"))
        with self.assertRaises(ConfigError):
            self.core.config.rollback("owner")


class StoreTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp(prefix="butler-store-")

    def test_durability_settings_and_permissions(self):
        store = Store(self.root, reserve=0)
        pragmas = {name: store.db.execute(f"PRAGMA {name}").fetchone()[0]
                   for name in ("journal_mode", "synchronous", "fullfsync")}
        self.assertEqual(pragmas, {"journal_mode": "wal", "synchronous": 2, "fullfsync": 1})
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)
        self.assertEqual(store.schema_version, len(MIGRATIONS))

    def test_reopen_keeps_data_and_schema(self):
        store = Store(self.root, reserve=0)
        store.set_meta("probe", "1")
        store.close()
        again = Store(self.root, reserve=0)
        self.assertEqual(again.meta("probe"), "1")

    def test_newer_schema_is_refused(self):
        store = Store(self.root, reserve=0)
        store.db.execute(f"PRAGMA user_version={len(MIGRATIONS) + 1}")
        store.close()
        with self.assertRaises(RuntimeError):
            Store(self.root, reserve=0)

    def test_full_database_raises_storage_exhausted(self):
        store = Store(self.root, reserve=0)
        pages = store.db.execute("PRAGMA page_count").fetchone()[0]
        store.db.execute(f"PRAGMA max_page_count={pages}")
        with self.assertRaises(StorageExhausted):
            with store.transaction() as db:
                db.execute("INSERT INTO meta VALUES('big', ?)", ("x" * 200000,))
        self.assertIsNone(store.meta("big"))

    def test_online_backup_is_consistent(self):
        store = Store(self.root, reserve=0)
        store.set_meta("probe", "2")
        target = os.path.join(self.root, "copy.sqlite3")
        store.backup_to(target)
        copy = sqlite3.connect(target)
        self.assertEqual(copy.execute("SELECT value FROM meta WHERE key='probe'").fetchone()[0], "2")


if __name__ == "__main__":
    unittest.main()

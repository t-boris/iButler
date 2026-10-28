"""Owner-local UI history. No expiry or row-count pruning; explicit Clear is the only deletion."""
import json

class LocalHistory:
    def __init__(self, store, namespace):
        self.store, self.namespace = store, namespace
        store.execute('CREATE TABLE IF NOT EXISTS local_ui_history(namespace TEXT NOT NULL,id TEXT NOT NULL,at REAL NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(namespace,id))')

    def load(self):
        rows = self.store.execute('SELECT payload FROM local_ui_history WHERE namespace=? ORDER BY at,rowid', (self.namespace,))
        if not isinstance(rows, list): return []  # lightweight non-storage test doubles
        return [json.loads(row['payload']) for row in rows]

    def put(self, row):
        self.store.execute('INSERT INTO local_ui_history VALUES(?,?,?,?) ON CONFLICT(namespace,id) DO UPDATE SET at=excluded.at,payload=excluded.payload',
                           (self.namespace, row['id'], row['at'], json.dumps(row, ensure_ascii=False)))

    def clear(self):
        self.store.execute('DELETE FROM local_ui_history WHERE namespace=?', (self.namespace,))

import sqlite3
import tempfile
import unittest
from pathlib import Path

from butler_core.learning import LearningStore


class TranscriptRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 100
        self.store = LearningStore(self.root/'metadata.db',self.root/'private',clock=lambda:self.now)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_draft_final_revisions_are_private_and_expire_from_original_capture(self):
        self.store.transcript('u',1,'Эй Батлер',born=100,state='draft')
        self.now += 20
        self.store.transcript('u',2,'Эй Батлер помоги почистить диск',born=120,state='final')
        rows = self.store.private.execute('SELECT * FROM transcripts ORDER BY revision').fetchall()
        self.assertEqual([r['expires'] for r in rows],[86500,86500])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM episodes').fetchone()[0],0)
        metadata = '\n'.join(self.store.db.iterdump())
        self.assertNotIn('Батлер',metadata)
        self.assertNotIn('transcripts',metadata)
        self.now = 86500
        self.store.expire()
        self.assertEqual(self.store.private.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0],0)
        self.store.transcript('u',2,'late retry',born=100,state='final')
        self.assertEqual(self.store.private.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0],0)

    def test_label_uses_original_thirty_day_deadline_and_deletion_blocks_restore(self):
        self.store.transcript('u',1,'private draft',born=100,state='draft')
        self.store.episode('u','turn','session',100,'final')
        self.store.decide('u',{'accepted':True},revision=1)
        self.now += 100
        self.store.annotate('event','u',1,'recognition',authorized=True,corrected='corrected')
        self.assertEqual(self.store.private.execute('SELECT expires FROM transcripts').fetchone()[0],100+30*86400)
        self.store.delete('u')
        self.store.transcript('u',1,'restored draft',born=100,state='draft')
        self.assertEqual(self.store.private.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0],0)


if __name__ == '__main__': unittest.main()

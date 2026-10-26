"""Durable derived knowledge must precede bounded temporary-media cleanup."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from butler_ambient.store import AnalysisBufferFull, Store


class StoragePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root)

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def add(self,data=b'complete original',**kwargs):
        return self.store.media('microphone',100,101,data,'wav',temporary=True,**kwargs)

    def test_spool_limit_is_configurable_and_validated(self):
        self.assertEqual(self.store.SPOOL_LIMIT, 512 * 1024 * 1024)                 # default unchanged
        with tempfile.TemporaryDirectory() as other:
            store = Store(Path(other), spool_limit_mb=4096)
            try:
                self.assertEqual(store.SPOOL_LIMIT, 4096 * 1024 * 1024)
                self.assertEqual(store.storage_status()['spool_limit_bytes'], 4096 * 1024 * 1024)
                self.assertEqual(Store.SPOOL_LIMIT, 512 * 1024 * 1024)               # the class default is not mutated
            finally:
                store.close()
            for bad in (0, 63, 32769, "4096", 4096.0, True):
                with self.assertRaises(ValueError):
                    Store(Path(other), spool_limit_mb=bad)

    def test_success_reclaims_all_group_originals_and_keeps_complete_provenance(self):
        ids=[self.add(),self.add()]
        self.store.save_observation(ids[0],[{'detail':'derived fact'}],ids)
        self.assertEqual(len(list((self.root/'raw').iterdir())),0)
        rows=self.store.history(99,102)
        self.assertEqual(len(rows),1);self.assertEqual(set(rows[0]['source_ids']),set(ids))
        self.assertFalse(rows[0]['original_available'])
        self.assertEqual(rows[0]['facts'],[{'detail':'derived fact'}])
        self.assertEqual(self.store.spool_bytes(),0)

    def test_real_provenance_insert_failure_rolls_back_facts_and_every_marker_before_cleanup(self):
        ids=[self.add(),self.add()]
        self.store.execute("CREATE TRIGGER deny_link BEFORE INSERT ON observation_media WHEN NEW.media_id='"+ids[1]+"' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.Error):self.store.save_observation(ids[0],[],ids)
        self.assertEqual(self.store.execute('SELECT id FROM observations'),[])
        self.assertEqual(self.store.execute('SELECT * FROM observation_media'),[])
        self.assertEqual({r['analyzed'] for r in self.store.execute('SELECT analyzed FROM media')},{0})
        self.assertEqual(len(list((self.root/'raw').iterdir())),2)
        self.store.execute('DROP TRIGGER deny_link')
        self.store.save_observation(ids[0],[],ids)
        self.assertEqual(self.store.spool_bytes(),0)

    def test_quota_refuses_new_capture_without_a_partial_file_then_recovers_after_analysis(self):
        self.store.SPOOL_LIMIT=64
        mid=self.add(b'a'*32)
        with self.assertRaises(AnalysisBufferFull):self.add(b'b'*40)
        self.assertEqual(len(list((self.root/'raw').iterdir())),1)
        self.assertEqual(self.store.spool_bytes(),32)
        self.store.save_observation(mid,[])
        self.add(b'b'*40)
        self.assertEqual(self.store.spool_bytes(),40)

    def test_saturation_waits_for_headroom_while_derived_commits_and_cleanup_continue(self):
        self.store.SPOOL_LIMIT=1024
        first=self.add(b'a'*100);second=self.add(b'b'*800)
        with self.assertRaises(AnalysisBufferFull):self.add(b'c'*200)
        self.assertTrue(self.store.storage_status()['recovering_capacity'])
        self.store.save_observation(first,[])
        self.assertEqual(self.store.spool_bytes(),800)
        # This fits the hard cap but cannot consume the first tiny freed hole.
        with self.assertRaises(AnalysisBufferFull):self.add(b'c'*200)
        self.store.save_observation(second,[])
        self.add(b'c'*200)
        self.assertFalse(self.store.storage_status()['recovering_capacity'])
        self.assertEqual(self.store.spool_bytes(),200)
        self.assertEqual(len(self.store.execute('SELECT id FROM observations')),2)

    def test_single_oversized_request_does_not_block_valid_smaller_capture(self):
        self.store.SPOOL_LIMIT=64
        with self.assertRaises(AnalysisBufferFull):self.add(b'x'*65)
        self.assertFalse(self.store.spool_blocked)
        self.add(b'a'*60)

    def test_cleanup_failure_preserves_durable_facts_and_retries_after_restart(self):
        mid=self.add()
        with patch.object(Path,'unlink',side_effect=PermissionError('injected cleanup denial')):
            self.store.save_observation(mid,[{'detail':'safe derived fact'}])
        self.assertEqual(self.store.cleanup_error,'raw-cleanup-unavailable')
        self.assertEqual(len(self.store.execute('SELECT id FROM observations')),1)
        self.assertEqual(len(list((self.root/'raw').iterdir())),1)
        self.store.close();self.store=Store(self.root)
        self.store.cleanup_analyzed()
        self.assertEqual(self.store.cleanup_error,None)
        self.assertEqual(len(list((self.root/'raw').iterdir())),0)
        self.assertEqual(len(self.store.execute('SELECT id FROM observations')),1)

    def test_preserved_copy_and_requested_photo_survive_automatic_cleanup(self):
        mid=self.add();self.store.preserve(mid,now=101)
        photo=self.store.media('camera',101,101,b'requested photo','jpg')
        self.store.save_observation(mid,[]);self.store.save_observation(photo,[])
        rows=self.store.execute('SELECT * FROM media WHERE id=?',(mid,))
        self.assertIsNone(rows[0]['path']);self.assertTrue(Path(rows[0]['preserved']).is_file())
        self.assertIsNotNone(self.store.execute('SELECT path FROM media WHERE id=?',(photo,))[0]['path'])

    def test_pending_and_failed_temporary_media_still_have_hard_24_hour_expiry(self):
        for analyzed in (0,-1):
            mid=self.add();self.store.execute('UPDATE media SET analyzed=? WHERE id=?',(analyzed,mid))
        self.assertEqual(self.store.expire(100+86400),2)
        self.assertEqual(self.store.spool_bytes(),0)

    def test_crash_orphan_counts_against_quota_until_existing_expiry_reclaims_it(self):
        self.store.close();orphan=self.root/'raw'/'untracked.wav';orphan.write_bytes(b'a'*64)
        self.store=Store(self.root);self.store.SPOOL_LIMIT=64
        with self.assertRaises(AnalysisBufferFull):self.add(b'b')
        self.store.expire(time.time()+86401)
        self.assertFalse(orphan.exists());self.add(b'b')
        self.assertEqual(self.store.spool_bytes(),1)

    def test_legacy_migration_reclaims_analyzed_only_and_keeps_retry_data_outside_new_quota(self):
        self.store.close();(self.root/'ambient.sqlite3').unlink()
        analyzed=self.root/'raw'/'done.wav';analyzed.write_bytes(b'known analyzed')
        pending=self.root/'raw'/'retry.wav';pending.write_bytes(b'known retry')
        db=sqlite3.connect(self.root/'ambient.sqlite3')
        db.execute('CREATE TABLE media(id TEXT PRIMARY KEY,source TEXT,start REAL,end REAL,path TEXT,preserved TEXT,analyzed INTEGER)')
        db.executemany('INSERT INTO media VALUES (?,?,?,?,?,?,?)',[
            ('done','microphone',100,101,str(analyzed),None,1),('retry','microphone',100,101,str(pending),None,-1)])
        db.execute('CREATE TABLE observations(id TEXT PRIMARY KEY,media_id TEXT,source TEXT,start REAL,end REAL,facts TEXT)')
        db.execute("INSERT INTO observations VALUES ('obs','done','microphone',100,101,'[]')")
        db.execute('CREATE TABLE observation_media(observation_id TEXT,media_id TEXT,PRIMARY KEY(observation_id,media_id))')
        db.execute("INSERT INTO observation_media VALUES ('obs','done')")
        db.commit();db.close();self.store=Store(self.root);self.store.SPOOL_LIMIT=5
        self.assertEqual(self.store.cleanup_analyzed(limit=1),1)
        self.assertFalse(analyzed.exists());self.assertTrue(pending.exists())
        self.assertEqual(self.store.execute("SELECT analyzed FROM media WHERE id='retry'")[0]['analyzed'],0)
        self.add(b'new');self.assertEqual(self.store.spool_bytes(),3)

    def test_analyzed_marker_without_durable_provenance_cannot_authorize_early_cleanup(self):
        mid=self.add();self.store.execute('UPDATE media SET analyzed=1 WHERE id=?',(mid,))
        self.assertEqual(self.store.cleanup_analyzed(),0)
        self.assertEqual(len(list((self.root/'raw').iterdir())),1)

    def test_unanalyzed_expiry_records_the_original_source_interval_as_a_gap(self):
        self.add();self.store.expire(100+86400)
        self.assertEqual(self.store.execute("SELECT source,start,end FROM events WHERE reason='analysis-expired'"),
            [{'source':'microphone','start':100.,'end':101.}])

    def test_expiry_log_failure_cannot_extend_raw_retention(self):
        self.add()
        self.store.execute("CREATE TRIGGER deny_gap BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'injected log failure'); END")
        self.store.expire(100+86400)
        self.assertEqual(len(list((self.root/'raw').iterdir())),0)
        self.assertEqual(self.store.cleanup_error,'retention-gap-log-unavailable')

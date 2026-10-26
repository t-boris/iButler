"""DEC-065/REQ-027: automatic key frames of important camera events."""
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from butler_ambient.event_frames import entity_key, important_events, local_day
from butler_ambient.local_ui import snapshot
from butler_ambient.sensing import Batch
from butler_ambient.service import Service
from butler_ambient.store import Store


def fact(kind='person', identity='', confidence=.9, activity='unknown'):
    return {'kind': kind, 'activity': activity, 'topic': 'unknown', 'detail': 'synthetic', 'subject': kind + '-1',
            'identity': identity, 'confidence': confidence, 'basis': 'model-inference'}


class EventSelectionTests(unittest.TestCase):
    def test_new_known_and_unknown_people_dog_and_book_are_events(self):
        events = important_events([fact(), fact(identity='Synthetic Friend'), fact('dog'),
                                   fact('book', identity='Synthetic Title')], [])
        self.assertEqual(events, [('person', 'person:unknown'), ('person', 'person:synthetic friend'),
                                  ('dog', 'dog:unknown'), ('book', 'book:synthetic title')])

    def test_entity_seen_in_previous_ten_minutes_is_not_new(self):
        self.assertEqual(important_events([fact()], [[fact(confidence=.8)]]), [])
        self.assertEqual(important_events([fact(identity='A')], [[fact()]]), [('person', 'person:a')])

    def test_low_confidence_facts_never_trigger_or_suppress_events(self):
        self.assertEqual(important_events([fact(confidence=.69)], []), [])
        self.assertEqual(important_events([fact()], [[fact(confidence=.5)]]), [('person', 'person:unknown')])

    def test_main_activity_is_compared_with_the_immediately_previous_observation(self):
        reading = [fact('activity', activity='reading')]
        work = [fact('activity', activity='work')]
        self.assertEqual(important_events(reading, [], work), [('activity', 'activity:reading')])
        self.assertEqual(important_events(reading, [], [fact('activity', activity='reading', confidence=.8)]), [])
        self.assertEqual(important_events(reading, [], None), [('activity', 'activity:reading')])
        # Only the previous observation counts, not an older one in the window.
        self.assertEqual(important_events(reading, [reading, work], work), [('activity', 'activity:reading')])
        self.assertEqual(important_events([fact('activity', activity='unknown')], [], None), [])

    def test_owner_corrections_apply_to_current_and_previous_facts(self):
        corrected = {'person-1': 'Synthetic Friend'}
        current = [fact(identity='Synthetic Friend')]
        self.assertEqual(important_events(current, [[fact()]], None, corrected), [])
        self.assertEqual(important_events(current, [[fact()]], None), [('person', 'person:synthetic friend')])

    def test_key_normalizes_names_and_local_day_uses_chicago(self):
        self.assertEqual(entity_key(fact(identity='  Synthetic   Friend ')), 'person:synthetic friend')
        self.assertEqual(local_day(1792818000.0), '2026-10-24')  # 05:00 UTC = 00:00 CDT
        self.assertEqual(local_day(1792817999.0), '2026-10-23')


class EventFrameStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def keep(self, at=1000., key='person:unknown', day='2026-10-24'):
        return self.store.keep_event_frame('obs', b'\xff\xd8frame\xff\xd9', at, [('person', key)], day)

    def test_frame_is_a_private_preserved_camera_record_with_provenance(self):
        mid = self.keep()
        row = self.store.execute('SELECT * FROM media WHERE id=?', (mid,))[0]
        self.assertEqual((row['source'], row['storage_class'], row['path'], row['analyzed']),
                         ('camera', 'event-frame', None, 1))
        path = Path(row['preserved'])
        self.assertEqual(path.parent, self.store.root / 'preserved')
        self.assertEqual(path.read_bytes(), b'\xff\xd8frame\xff\xd9')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.store.execute('SELECT observation_id,kind,key,captured_at,day FROM event_frames'),
                         [{'observation_id': 'obs', 'kind': 'person', 'key': 'person:unknown',
                           'captured_at': 1000., 'day': '2026-10-24'}])
        self.assertEqual(list((self.store.root / 'preserved').glob('*.tmp')), [])

    def test_same_event_is_spaced_ten_minutes_and_other_events_are_independent(self):
        self.assertIsNotNone(self.keep(1000.))
        self.assertIsNone(self.keep(1599.))
        self.assertIsNotNone(self.keep(1599., key='dog:unknown'))
        self.assertIsNotNone(self.keep(1600.))

    def test_daily_limit_counts_frames_including_owner_deleted_ones(self):
        mids = [self.keep(1000. + i * 600) for i in range(20)]
        self.assertTrue(all(mids))
        self.store.delete(mids[0])
        self.assertFalse(Path(self.store.execute('SELECT * FROM media WHERE id=?', (mids[0],))[0]['preserved'] or 'x').exists())
        self.assertIsNone(self.keep(99999.))
        self.assertIsNotNone(self.keep(99999., day='2026-10-25'))
        status = self.store.event_frame_status('2026-10-24')
        # 19 kept frames from the full day plus one from the next day.
        self.assertEqual((status['files'], status['today'], status['daily_limit']), (20, 20, 20))

    def test_expiry_and_analysis_cleanup_never_remove_event_frames(self):
        mid = self.keep(1000.)
        self.store.expire(10**9); self.store.cleanup_analyzed()
        row = self.store.execute('SELECT preserved FROM media WHERE id=?', (mid,))[0]
        self.assertTrue(Path(row['preserved']).is_file())

    def test_spacing_and_daily_limit_survive_a_restart(self):
        for i in range(20): self.assertIsNotNone(self.keep(1000. + i * 600))
        self.store.close(); self.store = Store(Path(self.temp.name))
        self.assertIsNone(self.keep(99999.), 'daily limit was not durable')
        self.assertIsNone(self.keep(1000. + 19 * 600 + 1, day='2026-10-25'), 'spacing was not durable')
        self.assertEqual(self.store.event_frame_status('2026-10-24')['files'], 20)

    def test_write_failure_leaves_no_file_or_record(self):
        with patch('butler_ambient.store.os.fsync', side_effect=OSError('injected')):
            with self.assertRaises(OSError): self.keep()
        self.assertEqual(list((self.store.root / 'preserved').iterdir()), [])
        self.assertEqual(self.store.execute('SELECT * FROM event_frames'), [])

    def test_startup_removes_only_uncommitted_event_frame_files(self):
        mid = self.keep()
        kept = Path(self.store.execute('SELECT preserved FROM media WHERE id=?', (mid,))[0]['preserved'])
        preserved = self.store.root / 'preserved'
        for name in ('event-orphan.jpg', 'event-crashed-abc.tmp', 'owner-original.mjpeg'):
            (preserved / name).write_bytes(b'x')
        self.store.close(); self.store = Store(Path(self.temp.name))
        self.assertEqual(sorted(path.name for path in preserved.iterdir()), sorted([kept.name, 'owner-original.mjpeg']))

    def test_database_failure_leaves_no_file_or_record(self):
        real = self.store.db
        class Failing:
            def __getattr__(self, name): return getattr(real, name)
            def execute(self, sql, *args):
                if sql.lstrip().startswith('INSERT INTO event_frames'): raise sqlite3.OperationalError('injected')
                return real.execute(sql, *args)
        self.store.db = Failing()
        with self.assertRaises(sqlite3.Error): self.keep()
        self.store.db = real
        self.assertEqual(list((self.store.root / 'preserved').iterdir()), [])
        self.assertEqual(self.store.execute("SELECT id FROM media WHERE storage_class='event-frame'"), [])
        self.assertEqual(self.store.execute('SELECT * FROM event_frames'), [])


class EventFrameServiceTests(unittest.TestCase):
    def setUp(self):
        disk = patch('butler_ambient.archive.shutil.disk_usage', return_value=Mock(free=10**10))
        disk.start(); self.addCleanup(disk.stop)
        self.temp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.temp.name)); self.now = 1792818000.
        self.service = Service(self.store, Mock(), Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.service.screen_worker.submit = Mock(); self.store.set('continuous_archive', True)
        self.service.control('enabled', {'value': True}); self.archive = self.service.archive

    def tearDown(self):
        self.service.stop(); self.store.close(); self.temp.cleanup()

    def clip(self, seconds=10):
        for i in range(seconds):
            self.now += 1
            ok, data = cv2.imencode('.jpg', np.full((24, 32, 3), (i * 20) % 255, dtype=np.uint8))
            frames = [data.tobytes()] * 5
            times = tuple(self.now - 1 + (k + 1) / 5 for k in range(5))
            self.archive.capture(Batch(self.now - 1, self.now, frames, b'\0' * 32000, self.now, (), times))
        return [r['id'] for r in self.store.execute(
            "SELECT id FROM media WHERE source='camera' AND storage_class='spool' AND complete=1 AND analyzed=0 ORDER BY start")]

    def analyze(self, facts):
        ids = self.clip()
        self.service.analyzer.analyze.return_value = facts
        self.assertTrue(self.service.analyze_group(ids[:1], self.archive.generation, archive=True))
        return self.service.analyzer.analyze.call_args.kwargs['images']

    def frames(self):
        return self.store.execute("""SELECT m.preserved,e.kind,e.key,e.observation_id FROM event_frames e
            JOIN media m ON m.id=e.media_id ORDER BY e.captured_at""")

    def test_committed_camera_observation_keeps_latest_analyzed_frame_for_new_unknown_person(self):
        images = self.analyze([fact()])
        kept = self.frames()
        self.assertEqual([(k['kind'], k['key']) for k in kept], [('person', 'person:unknown')])
        self.assertEqual(Path(kept[0]['preserved']).read_bytes(), images[-1])
        observation = self.store.execute('SELECT id FROM observations')[0]['id']
        self.assertEqual(kept[0]['observation_id'], observation)
        self.assertIsNone(self.store.event_frame_error)
        value = snapshot(self.service)['storage']['event_frames']
        self.assertEqual((value['files'], value['today']), (1, 1))

    def test_same_person_is_new_again_only_after_ten_minutes_without_them(self):
        self.analyze([fact()]); self.now += 300; self.analyze([fact()])
        self.assertEqual(len(self.frames()), 1)
        self.now += 700; self.analyze([fact()])
        self.assertEqual(len(self.frames()), 2)

    def test_activity_after_a_long_gap_is_compared_with_the_previous_observation(self):
        reading = [fact('activity', activity='reading')]
        self.analyze(reading); self.now += 1800; self.analyze(reading)
        self.assertEqual([k['key'] for k in self.frames()], ['activity:reading'])

    def test_unchanged_scene_and_empty_observations_keep_no_new_frame(self):
        self.analyze([fact()]); self.analyze([fact()]); self.analyze([])
        self.assertEqual(len(self.frames()), 1)

    def test_frame_failure_keeps_the_committed_observation(self):
        with patch.object(self.store, 'keep_event_frame', side_effect=OSError('injected')):
            self.analyze([fact('dog')])
        self.assertEqual(len(self.store.execute('SELECT id FROM observations')), 1)
        self.assertEqual(self.store.event_frame_error, 'event-frame-unavailable')

    def test_pause_before_commit_keeps_no_event_frame(self):
        ids = self.clip()
        def pause(*args, **kwargs):
            self.service.control('enabled', {'value': False})
            return [fact()]
        self.service.analyzer.analyze.side_effect = pause
        self.assertFalse(self.service.analyze_group(ids[:1], self.archive.generation, archive=True))
        self.assertEqual(self.frames(), [])
        self.assertEqual(list((self.store.root / 'preserved').iterdir()), [])

    def test_non_camera_sources_never_keep_frames(self):
        self.service.analyzer.transcribe.return_value = 'Controlled audio'
        self.service.analyzer.analyze.return_value = [fact()]
        audio = Batch(self.now - 1, self.now, [], b'\0' * 32000).wav()
        mid = self.store.media('microphone', self.now - 1, self.now, audio, 'wav', temporary=True)
        self.assertTrue(self.service.analyze_group([mid], self.archive.generation, archive=True))
        self.assertEqual(len(self.store.execute("SELECT id FROM observations WHERE source='microphone'")), 1)
        self.assertEqual(self.frames(), [])

    def test_owner_delete_control_removes_an_event_frame(self):
        self.analyze([fact('book', identity='Synthetic Title')])
        mid = self.store.execute("SELECT id FROM media WHERE storage_class='event-frame'")[0]['id']
        path = Path(self.frames()[0]['preserved'])
        self.service.control('delete', {'id': mid})
        self.assertFalse(path.exists())


if __name__ == '__main__': unittest.main()

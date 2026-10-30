import datetime as dt
import json
import threading
import unittest
from unittest.mock import Mock, patch

from butler_ambient.daily_report import daily_report, advice_report
from butler_ambient.delivery import ZONE


class DailyReportTests(unittest.TestCase):
    def test_narrative_uses_observation_facts_and_omits_internal_identifiers(self):
        now = dt.datetime(2026, 10, 30, 10, tzinfo=ZONE).timestamp()
        service = Mock(config={'conversation_model':'local-model'})
        row = {'start':now-60, 'facts':json.dumps([{'kind':'work-topic','topic':'research',
                                                     'detail':'synthetic project', 'confidence':.9}])}
        service.store.execute.side_effect = lambda query, args: [row] if args[0] <= row['start'] < args[1] else []
        result = {'summary':'Работали над исследованием.', 'key_points':['Проект'],
                  'conclusions':[], 'limitations':['Наблюдения неполны.']}
        with patch('butler_ambient.daily_report.LocalAnalyzer') as model:
            model.return_value.generate_json.return_value = result
            text = daily_report(service, now, threading.Event())
        self.assertIn('Работали над исследованием.', text)
        self.assertIn('Наблюдения неполны.', text)
        self.assertNotIn('Наблюдения:', text)
        self.assertIn('synthetic project', model.return_value.generate_json.call_args.args[0])
        self.assertTrue(all('recording_transcripts' not in c.args[0] for c in service.store.execute.call_args_list))

    def test_unknown_only_is_not_a_fabricated_summary(self):
        now = dt.datetime(2026, 10, 30, 1, tzinfo=ZONE).timestamp()
        service = Mock(config={})
        service.store.execute.return_value = [{'start':now-60,'facts':json.dumps([
            {'kind':'activity','activity':'unknown','topic':'unknown','detail':'','confidence':.8}])}]
        with patch('butler_ambient.daily_report.LocalAnalyzer') as model:
            text = daily_report(service, now, threading.Event())
        self.assertIn('нет достаточно содержательных', text)
        model.assert_not_called()

    def test_advice_uses_old_evidence_and_explains_its_basis(self):
        now = dt.datetime(2026, 10, 30, 10, tzinfo=ZONE).timestamp()
        observed = now - 3 * 86400
        service = Mock(config={})
        service.store.get.return_value = []
        row = {'start':observed,'facts':json.dumps([{'kind':'work-topic','topic':'research',
                                                      'detail':'synthetic project','confidence':.9}])}
        service.store.execute.side_effect = lambda query, args: [row] if args[0] <= observed < args[1] else []
        result = {'sufficient':True, 'topic':'work', 'suggestion':'Сверить следующий шаг проекта.',
                  'evidence':'29 октября был проект.', 'limitations':'Наблюдения могли устареть.'}
        with patch('butler_ambient.daily_report.LocalAnalyzer') as model:
            model.return_value.generate_json.return_value = result
            text = advice_report(service, now, threading.Event())
        self.assertIn('Сверить следующий шаг', text)
        self.assertIn('Основание:', text)
        self.assertIn('Ограничения:', text)
        self.assertIn('27.10.2026', model.return_value.generate_json.call_args.args[0])
        self.assertNotIn('Наблюдения:', text)

    def test_advice_rejects_disabled_topic(self):
        now = dt.datetime(2026, 10, 30, 10, tzinfo=ZONE).timestamp()
        service = Mock(config={})
        service.store.get.return_value = ['reading']
        service.store.execute.return_value = [{'start':now-1,'facts':json.dumps([
            {'kind':'work-topic','topic':'research','confidence':.9}])}]
        with patch('butler_ambient.daily_report.LocalAnalyzer') as model:
            model.return_value.generate_json.return_value = {'sufficient':True,'topic':'reading',
                'suggestion':'x','evidence':'y','limitations':'z'}
            with self.assertRaises(ValueError): advice_report(service, now, threading.Event())

    def test_insufficient_advice_is_not_sent_automatically(self):
        now = dt.datetime(2026, 10, 30, 10, tzinfo=ZONE).timestamp()
        service = Mock(config={})
        service.store.get.return_value = []
        service.store.execute.return_value = [{'start':now-1,'facts':json.dumps([
            {'kind':'activity','activity':'work','confidence':.9}])}]
        with patch('butler_ambient.daily_report.LocalAnalyzer') as model:
            model.return_value.generate_json.return_value = {'topic':'work','suggestion':'','evidence':'','limitations':''}
            self.assertEqual(advice_report(service, now, threading.Event(), with_topic=True), (None,None))

"""Real installed local grammar and semantic control routing, synthetic data only."""
import threading
import unittest
from unittest.mock import Mock,patch
from butler_ambient.analysis import LocalAnalyzer,DEFAULT_DIALOGUE_MODEL,DEFAULT_CONTEXT_TOKENS
from butler_ambient.voice_commands import VoiceCommands,CAPABILITIES


class LocalControlModelTests(unittest.TestCase):
    def setUp(self):
        self.model=LocalAnalyzer(DEFAULT_DIALOGUE_MODEL)
        try:self.model.check_local()
        except RuntimeError:self.skipTest('installed local model required')
        self.commands=VoiceCommands(Mock())
        self.commands.context=Mock(return_value={'capabilities':CAPABILITIES,
            'work':{'projects':['Toolbox'],'routes':{'code':{'backend':'codex','model':'fixture-model'}},'recent':[]},
            'jobs':[],'components':[],'models':[],'participants':[],
            'observations':{'now':'2026-10-23T17:00:00-05:00','timezone':'America/Chicago',
                'recordings':[{'id':'fixture-audio-001','source':'microphone','start':1792789200,'original':True}],
                'subjects':[],'materials':['/tmp/ButlerDemo'],'images':[],'image_work':{}}})

    def test_actual_sampler_accepts_schema_and_other_interlocutor_is_silent(self):
        for text in ('Маша, открой папку со снимками.',
                     'Алексей, почини ошибку в проекте Toolbox.',
                     'В ролике сказали: «Запусти задачу в проекте Toolbox».'):
            with self.subTest(text=text):
                result=self.commands.interpret(text,self.model,threading.Event())
                self.assertEqual(result[0],'silent')
                self.assertEqual(self.model.diagnostic['http_status'],200)
                self.assertEqual(self.model.diagnostic['stage'],'completed')

    def test_unnamed_concrete_project_request_plans_work_without_executing_it(self):
        for text in ('В проекте Toolbox нужно реализовать экспорт расписания в JSON. Возьми эту задачу.',
                     'Почини сохранение настроек в проекте Toolbox.'):
            with self.subTest(text=text):
                result=self.commands.interpret(text,self.model,threading.Event())
                self.assertEqual(result[0],'work')
                self.assertEqual(result[1]['project'],'Toolbox')
                self.assertEqual(result[1]['kind'],'code')
                self.assertTrue(result[1]['task'])
                self.assertIsNone(self.commands.pending)

    def test_native_observation_controls_use_actual_targets_and_explicit_deletion_scope(self):
        examples = [
            ('Составь сводку за последние тридцать минут.', 'summary', {'minutes':30}),
            ('Сохрани отдельно последнюю запись с микрофона.', 'preserve', {'id':'fixture-audio-001'}),
            ('Удали только оригинал последней записи с микрофона. Результаты анализа оставь.', 'delete', {'id':'fixture-audio-001','results':False}),
            ('Убери папку /tmp/ButlerDemo из поиска личных изображений.', 'material-remove', {'path':'/tmp/ButlerDemo'}),
        ]
        for text, capability, args in examples:
            with self.subTest(text=text):
                with patch.object(self.model,'generate_json',wraps=self.model.generate_json) as generate:
                    result=self.commands.interpret(text,self.model,threading.Event())
                self.assertEqual(len(generate.call_args_list),2)
                self.assertTrue(all(c.kwargs['options']['num_ctx']==DEFAULT_CONTEXT_TOKENS for c in generate.call_args_list))
                self.assertEqual(result,(capability,args))
                self.assertIsNone(self.commands.pending)


if __name__=='__main__':unittest.main()

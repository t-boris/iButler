"""Continuous dialogue sequences: work, questions, streaming, failure and hard close."""
import threading
import time
import unittest

from butler_ambient.brain import BrainSilent
from butler_ambient.dialogue import Dialogue, TextStream
from butler_ambient.dialogue_state import Session
from butler_ambient.voice_commands import confirmation


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline: time.sleep(.005)
    if not predicate(): raise AssertionError('condition did not complete')


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.ended = threading.Event()
        self.session = Session()
        self.d = Dialogue(self.session, self.ended, clock=lambda: self.now)

    def tearDown(self):
        self.ended.set(); self.d.stop()

    def test_content_is_deliverable_while_agent_is_still_working(self):
        emitted, release = threading.Event(), threading.Event()
        def run(emit, cancelled):
            emit('Первая содержательная часть. ')
            emitted.set(); release.wait(1)
            emit('Продолжение ответа.')
            return 'Первая содержательная часть. Продолжение ответа.'
        request = self.d.submit('Объясни архитектуру', run)
        self.assertTrue(emitted.wait(1))
        output = self.d.claim_output()
        self.assertEqual(output[1], 'Первая содержательная часть.')
        self.assertEqual(request.state, 'running')
        self.d.delivered(request, output[2], output[1])
        release.set(); wait_for(lambda: request.state == 'completed')
        output = self.d.claim_output()
        self.assertEqual(output[1], 'Продолжение ответа.')
        self.d.delivered(request, output[2], output[1])
        self.assertEqual(request.delivery, 'completed')

    def test_interruption_stops_output_but_keeps_accepted_work_and_followup_order(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def first(emit, cancelled):
            calls.append('first'); emit('Начало ответа на исходную просьбу. ')
            entered.set(); release.wait(1)
            self.assertFalse(cancelled.is_set())
            return 'Результат исходной работы.'
        request = self.d.submit('Первоначальная просьба', first)
        self.assertTrue(entered.wait(1)); self.assertIsNotNone(self.d.claim_output())
        self.d.interrupt_output()
        next_request = self.d.submit('Дополнение к просьбе', lambda emit, cancelled: calls.append('followup') or 'Учёл дополнение.')
        self.assertEqual(calls, ['first'])
        release.set(); wait_for(lambda: next_request.state == 'completed')
        self.assertEqual(calls, ['first', 'followup'])
        self.assertEqual(request.delivery, 'interrupted')
        self.assertEqual(request.result, 'Результат исходной работы.')
        self.assertEqual(self.d.claim_output()[0].id, next_request.id)

    def test_late_question_survives_pause_and_answer_is_correlated(self):
        request = self.d.submit('Подготовь изменение', lambda emit, cancelled: 'Применить показанное изменение?')
        wait_for(lambda: request.state == 'awaiting-answer')
        question = self.session.question_id
        self.now += 1800
        self.assertTrue(self.session.answering('owner', self.now))
        answer = self.d.submit('Да, есть моё подтверждение', lambda emit, cancelled: 'Получил ответ на вопрос.')
        self.assertEqual(answer.parent_question, question)
        self.assertEqual(request.state, 'completed')
        self.assertEqual(answer.dialogue, request.dialogue)
        wait_for(lambda: answer.state == 'completed')

    def test_privacy_close_invalidates_work_text_question_and_queued_output(self):
        entered = threading.Event()
        def run(emit, cancelled):
            emit('Приватная часть ответа. '); entered.set(); cancelled.wait(1)
            emit('Старый остаток.'); return 'Приватный результат'
        request = self.d.submit('Приватная просьба', run)
        self.assertTrue(entered.wait(1))
        old = self.d.id
        self.session.ask('Личный вопрос?', self.now, task=request.id)
        self.d.invalidate()
        self.assertTrue(request.cancelled.is_set())
        self.assertNotEqual(old, self.d.id)
        self.assertIsNone(self.session.question)
        self.assertIsNone(self.d.claim_output())
        self.assertEqual((request.text, request.result, request.raw), ('', '', ''))
        followup = self.d.submit('Новая сессия', lambda emit, cancelled: 'Новый ответ.')
        wait_for(lambda: followup.state == 'completed')
        self.assertEqual(self.d.claim_output()[1], 'Новый ответ.')

    def test_delivery_failure_never_reexecutes_work_and_next_request_still_runs(self):
        calls = []
        request = self.d.submit('Работа', lambda emit, cancelled: calls.append('run') or 'Результат работы.')
        wait_for(lambda: request.state == 'completed')
        output = self.d.claim_output()
        self.d.delivered(request, output[2], output[1], RuntimeError('audio stalled'))
        self.assertEqual(request.delivery, 'failed')
        self.assertIsNone(self.d.claim_output())
        next_request = self.d.submit('Следующий вопрос', lambda emit, cancelled: 'Следующий ответ.')
        wait_for(lambda: next_request.state == 'completed')
        self.assertEqual(self.d.claim_output()[0].id, next_request.id)
        self.assertEqual(calls, ['run'])

    def test_reaction_occurs_once_and_names_only_the_actual_request(self):
        clock = [0.0]; self.d.monotonic = lambda: clock[0]
        entered = threading.Event()
        def run(emit, cancelled):
            entered.set(); cancelled.wait(1); return 'Ответ.'
        request = self.d.submit('Почему это происходит?', run)
        self.assertTrue(entered.wait(1))
        self.assertIsNone(self.d.reaction())
        clock[0] = 1.3
        reaction = self.d.reaction()[1]
        self.assertIn(request.text, reaction)
        self.assertNotIn('готово', reaction.lower())
        self.assertIsNone(self.d.reaction())

    def test_busy_withholds_proactive_notice_but_not_requested_result(self):
        self.d.notice('Итоги созвона готовы.')
        request = self.d.submit('Что получилось?', lambda emit, cancelled: 'Вот ответ на вашу просьбу.')
        wait_for(lambda: request.state == 'completed')
        output = self.d.claim_output(include_notices=False)
        self.assertEqual(output[0].id, request.id)
        self.d.delivered(request, output[2], output[1])
        self.assertIsNone(self.d.claim_output(include_notices=False))
        self.assertEqual(self.d.claim_output()[1], 'Итоги созвона готовы.')

    def test_silence_sentinel_never_becomes_spoken_output(self):
        def run(emit, cancelled):
            emit('NO_'); emit('REPLY'); raise BrainSilent()
        request = self.d.submit('Слова другому', run)
        wait_for(lambda: request.state == 'silent')
        self.assertIsNone(self.d.claim_output())

    def test_long_text_is_chunked_without_losing_words(self):
        text = ' '.join(['длинное'] * 210) + '. Затем конец.'
        chunks = TextStream().feed(text, final=True)
        self.assertTrue(all(len(part) <= 600 for part in chunks))
        self.assertEqual(' '.join(chunks).split(), text.split())

    def test_snapshot_without_private_access_contains_no_request_or_result(self):
        request = self.d.submit('Секретная просьба', lambda emit, cancelled: 'Секретный ответ.')
        wait_for(lambda: request.state == 'completed')
        public = str(self.d.snapshot())
        self.assertNotIn('Секрет', public)
        self.assertIn('Секретный ответ', str(self.d.snapshot(private=True)))

    def test_natural_confirmation_does_not_accept_negated_or_quoted_speech(self):
        for phrase in ['Да, есть моё подтверждение', 'да я подтверждаю', 'Мое подтверждение есть']:
            self.assertIs(confirmation(phrase), True)
        for phrase in ['Он сказал да', 'да но не разрешаю', '«да»', 'подтверждаю другую операцию']:
            self.assertIsNone(confirmation(phrase))


if __name__ == '__main__': unittest.main()

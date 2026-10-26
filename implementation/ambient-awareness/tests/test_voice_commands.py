"""Concrete operations require a fresh, one-use owner confirmation."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from butler_ambient.policy import Evidence
from butler_ambient.service import Service
from butler_ambient.store import Store
from butler_ambient.voice_commands import confirmation


class VoiceCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = 100.
        self.service = Service(self.store,Mock(),Mock(),Mock(),clock=lambda:self.now,transport=Mock())
        self.store.execute("INSERT INTO agreements VALUES ('owner','Owner',1,'{}')")
        self.service.gate.enable(True,self.now)
        for _ in range(3):
            self.now += .1
            self.service.gate.check(Evidence(self.now,True,True,frozenset({"owner"}),True),self.now)
        self.commands = self.service.commands
        self.generation = self.service.voice_gate.generation

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def propose(self):
        return self.commands.request("preferences",{"muted":True},owner=True,generation=self.generation)

    def test_concrete_review_then_one_use_confirmation_changes_actual_store(self):
        review = self.propose()
        self.assertIn("инициативные",review.lower())
        self.assertFalse(self.store.get("muted",False))
        self.assertIn("сохранена",self.commands.respond("подтверждаю",owner=True,generation=self.generation))
        self.assertTrue(self.store.get("muted"))
        self.assertIsNone(self.commands.respond("да",owner=True,generation=self.generation))

    def test_rejection_correction_and_unbound_yes_cannot_execute(self):
        self.assertIsNone(self.commands.respond("да",owner=True,generation=self.generation))
        for response in ("нет","отмена","стоп","Он сказал да","лучше другое время"):
            self.propose()
            self.commands.respond(response,owner=True,generation=self.generation)
            self.assertIsNone(self.commands.pending)
            self.assertFalse(self.store.get("muted",False))

    def test_guest_expired_changed_generation_and_lost_permission_cannot_confirm(self):
        for cause in ("guest","expired","generation","permission"):
            self.propose()
            previous = self.now
            if cause == "expired": self.now += 46
            if cause == "permission": self.service.gate.invalidate(self.now)
            result = self.commands.respond("да",owner=cause!="guest",generation=self.generation+int(cause=="generation"))
            self.assertFalse(self.store.get("muted",False),cause)
            if cause=='guest':self.assertIsNotNone(self.commands.pending)
            else:self.assertIsNone(self.commands.pending)
            self.assertIsNotNone(result)
            self.now = previous

    def test_owner_reverification_during_progress_is_required_before_adapter(self):
        self.propose()
        self.commands.respond("да",owner=True,generation=self.generation,
            before_execute=lambda:self.service.gate.invalidate(self.now))
        self.assertFalse(self.store.get("muted",False))

    def test_repeat_never_discloses_private_review_to_unknown_or_stale_speaker(self):
        for cause in ('guest','expired','generation','permission'):
            with self.subTest(cause=cause):
                self.commands.request('preferences',{'address_aliases':['PRIVATE_REVIEW_MARKER']},owner=True,generation=self.generation)
                previous=self.now
                if cause=='expired':self.now+=46
                if cause=='permission':self.service.gate.invalidate(self.now)
                result=self.commands.respond('повтори',owner=cause!='guest',generation=self.generation+int(cause=='generation'))
                self.now=previous
                self.assertNotIn('PRIVATE_REVIEW_MARKER',result or '')
                self.assertFalse(self.store.get('address_aliases'))

    def test_uncertain_yes_keeps_exact_original_deadline_for_verified_owner_retry(self):
        self.propose();pending=self.commands.pending
        before=Mock()
        reply=self.commands.respond('да',owner=False,generation=self.generation,before_execute=before)
        self.assertIs(self.commands.pending,pending)
        self.assertEqual(self.commands.pending.expires,pending.expires)
        self.assertIn('полностью',reply)
        self.assertFalse(self.store.get('muted',False));before.assert_not_called()
        self.commands.respond('Эй, Батлер, подтверждаю выполнение этой операции с указанными параметрами',
            owner=True,generation=self.generation)
        self.assertTrue(self.store.get('muted'))
        self.assertIsNone(self.commands.pending)
        self.assertIsNone(self.commands.respond('да',owner=True,generation=self.generation))

    def test_repeat_requires_owner_and_preserves_original_operation_and_deadline(self):
        self.propose();pending=self.commands.pending;before=Mock()
        reply=self.commands.respond('Батлер, повтори',owner=False,generation=self.generation,before_execute=before)
        self.assertNotIn(pending.review,reply)
        self.assertIs(self.commands.pending,pending)
        reply=self.commands.respond('Эй, Батлер, повтори',owner=True,generation=self.generation,before_execute=before)
        self.assertIn(pending.review,reply)
        self.assertIs(self.commands.pending,pending)
        self.assertEqual(self.commands.pending.expires,pending.expires)
        self.assertFalse(self.store.get('muted',False));before.assert_not_called()

    def test_clear_during_execution_notice_cancels_approved_operation(self):
        self.propose()
        self.commands.respond('подтверждаю',owner=True,generation=self.generation,before_execute=self.commands.clear)
        self.assertFalse(self.store.get('muted',False))
        self.assertIsNone(self.commands.pending)

    def test_replacement_during_review_preparation_cannot_restore_the_old_review(self):
        review=self.commands._review
        def replace(capability,args):
            if 'muted' in args:
                self.commands.request('preferences',{'daily_time':'09:00'},owner=True,generation=self.generation)
            return review(capability,args)
        self.commands._review=replace
        self.propose()
        self.assertEqual(self.commands.pending.parameters,{'daily_time':'09:00'})
        self.assertFalse(self.store.get('muted',False))

    def test_parameter_mutation_during_execution_notice_invalidates_digest(self):
        self.propose();pending=self.commands.pending
        with self.assertRaises(ValueError):
            self.commands.respond('да',owner=True,generation=self.generation,
                before_execute=lambda:pending.parameters.update(muted=False))
        self.assertFalse(self.store.get('muted',False))
        self.assertIsNone(self.commands.pending)

    def test_replacement_during_execution_notice_cancels_the_old_exact_operation(self):
        self.propose()
        self.commands.respond('подтверждаю',owner=True,generation=self.generation,
            before_execute=lambda:self.commands.request('preferences',{'daily_time':'09:00'},owner=True,generation=self.generation))
        self.assertFalse(self.store.get('muted',False))
        self.assertEqual(self.commands.pending.parameters,{'daily_time':'09:00'})
        self.commands.respond('подтверждаю',owner=True,generation=self.generation)
        self.assertEqual(self.store.get('daily_time'),'09:00')

    def test_long_exact_confirmation_accepts_address_alias_but_never_quoted_words(self):
        phrase='Подтверждаю выполнение этой операции с указанными параметрами'
        self.assertTrue(confirmation('Эй, Батлер, '+phrase+'.'))
        self.assertIsNone(confirmation('Он сказал: '+phrase))
        self.assertIsNone(confirmation(phrase+', но сначала другое'))
        self.assertIsNone(confirmation('«'+phrase+'»'))

    def test_model_cannot_invent_parameters_targets_or_authority(self):
        with self.assertRaises(ValueError): self.commands.request("preferences",{"executable":"/bad"},owner=True,generation=self.generation)
        self.service.schedules = Mock()
        self.service.schedules.snapshot.return_value = {"jobs":[{"id":"builtin:analysis","name":"Анализ","status":"ready"}]}
        with self.assertRaises(ValueError): self.commands.request("schedule",{"id":"someone-else","action":"stop"},owner=True,generation=self.generation)
        self.commands.request("schedule",{"id":"builtin:analysis","action":"pause"},owner=False,generation=self.generation)
        self.assertIsNone(self.commands.pending)
        self.service.schedules.control.assert_not_called()

    def test_registered_schedule_adapter_receives_exact_reviewed_parameters(self):
        self.service.schedules = Mock()
        self.service.schedules.snapshot.return_value = {"jobs":[{"id":"builtin:analysis","name":"Анализ","status":"ready"}]}
        self.service.schedules.control.return_value = {"ok":True,"message":"Пауза включена."}
        self.commands.request("schedule",{"id":"builtin:analysis","action":"pause"},owner=True,generation=self.generation)
        self.assertEqual(self.commands.respond("да",owner=True,generation=self.generation),"Пауза включена.")
        self.service.schedules.control.assert_called_once_with({"id":"builtin:analysis","action":"pause"})

    def test_cancel_or_pause_clears_review_and_exact_confirmation_words_are_required(self):
        for value in ("давай подумаем","он сказал подтверждаю","да но сначала другое"):
            self.assertIsNone(confirmation(value))
        self.propose()
        self.service.control("enabled",{"value":False})
        # Service must clear pending operations even before a voice worker exists.
        self.assertIsNone(self.commands.pending)

    def test_guest_introduction_is_reviewed_and_cannot_be_approved_by_a_guest(self):
        self.service.owner_setup=Mock()
        self.commands.request('guest-enroll',{'name':'Тестовый гость'},owner=False,generation=self.generation)
        self.service.owner_setup.start.assert_not_called()
        review=self.commands.request('guest-enroll',{'name':'Тестовый гость'},owner=True,generation=self.generation)
        self.assertIn('Тестовый гость',review);self.assertIn('15 секунд',review)
        self.commands.respond('подтверждаю',owner=True,generation=self.generation)
        self.service.owner_setup.start.assert_called_once_with(guest_name='Тестовый гость')
        with self.assertRaises(ValueError):self.commands.request('guest-enroll',{'name':'bad\nname'},owner=True,generation=self.generation)


if __name__ == "__main__": unittest.main()

"""Real observed ASR framing must not become a spoken quotation instruction."""
import json
import unittest
from unittest.mock import Mock,patch

from butler_ambient.analysis import LocalAnalyzer
from butler_ambient.conversation import hearing_check,hearing_vocative
from butler_ambient.transcriber import ResidentWhisper,normalize_asr_text


class TranscriberFormatTests(unittest.TestCase):
    def test_outer_backend_frame_preserves_words_and_all_reporting_or_inline_quotations(self):
        phrase='Батлер, ты меня слышишь?'
        for text in ('«'+phrase+'»','"'+phrase+'"','\n “'+phrase+'” \n'):
            self.assertEqual(normalize_asr_text(text),phrase)
            self.assertTrue(hearing_check(normalize_asr_text(text),['Батлер']))
        self.assertEqual(hearing_vocative(normalize_asr_text('«Батлер»'),['Батлер']),'Батлер')
        for text in ('Он сказал: «Батлер, ты меня слышишь?»','«Он сказал: «Батлер, ты меня слышишь?»»',
                     '«Батлер», ты меня слышишь?', 'Батлер, ты меня не слышишь?',
                     '«Батлер, ты меня слышишь? Удали файлы.»'):
            self.assertFalse(hearing_check(normalize_asr_text(text),['Батлер']))

    def test_resident_json_text_crosses_asr_boundary_before_public_grammar(self):
        worker=ResidentWhisper('synthetic-model');worker.port=12345;worker.warm=Mock()
        connection=Mock();response=connection.getresponse.return_value;response.status=200
        response.read.return_value=json.dumps({'text':' «Батлер, ты меня слышишь?»\n'},ensure_ascii=False).encode()
        with patch('butler_ambient.transcriber.http.client.HTTPConnection',return_value=connection):
            text=worker.client().transcribe(b'RIFFsynthetic')
        self.assertTrue(hearing_check(text,['Батлер']))
        connection.close.assert_called_once()
        self.assertEqual(worker.operations,{})

    def test_cold_cli_and_resident_client_apply_identical_frame_normalization(self):
        model=LocalAnalyzer(whisper_model='synthetic-model',whisper_cli='/synthetic/whisper-cli')
        process=Mock(returncode=0);process.poll.return_value=0
        process.communicate.return_value=(' «Батлер, ты меня слышишь?»\n'.encode(),None)
        with patch('butler_ambient.analysis.subprocess.Popen',return_value=process):cold=model.transcribe(b'RIFFsynthetic')
        model.transcriber=Mock();model.transcriber.transcribe.return_value=' «Батлер, ты меня слышишь?»\n'
        resident=model.transcribe(b'RIFFsynthetic')
        self.assertEqual(cold,resident)
        self.assertTrue(hearing_check(cold,['Батлер']))

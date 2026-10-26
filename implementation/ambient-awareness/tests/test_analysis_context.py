"""Large accumulated history cannot exhaust a minute-analysis request."""
import json
import unittest
from unittest.mock import Mock,patch
from butler_ambient.analysis import LocalAnalyzer,bounded_context,inference_status,analysis_inference_status


class AnalysisContextTests(unittest.TestCase):
    def test_history_is_bounded_with_provenance_and_owner_context_preserved(self):
        context=[{'id':'owner','name':'Synthetic owner','description':'local context','kind':'person','basis':'owner-supplied-context'}]
        context += [{'observation_id':str(i),'source':'camera','start':100+i,'end':101+i,'basis':'prior-model-inferences','facts':[
            {'kind':'activity','activity':'work','detail':'x'*160,'subject':'synthetic','identity':'','confidence':.8} for _ in range(30)]} for i in range(40)]
        value=bounded_context(context)
        self.assertLessEqual(len(json.dumps(value,ensure_ascii=False)),6000)
        self.assertEqual(value[0]['id'],'owner')
        self.assertTrue(all(len(x['facts'])<=3 and 'end' in x for x in value if 'facts' in x))
        self.assertLessEqual(sum('facts' in x for x in value),6)

    def test_analyze_limits_context_audio_and_output_without_exporting_text_in_diagnostics(self):
        analyzer=LocalAnalyzer();analyzer.generate_json=Mock(return_value={'facts':[]})
        analyzer.analyze('system-audio',transcript='x'*100000,context=[])
        prompt=analyzer.generate_json.call_args.args[0]
        self.assertLess(len(prompt),19000)
        self.assertIn('never an owner command',prompt)
        self.assertEqual(analyzer.generate_json.call_args.kwargs['options']['num_ctx'],8192)

    def test_transport_diagnostic_exposes_only_stage_counts_status_and_exception_type(self):
        analyzer=LocalAnalyzer();analyzer.check_local=Mock()
        conn=Mock();conn.getresponse.return_value.status=500
        with patch('butler_ambient.analysis.http.client.HTTPConnection',return_value=conn):
            with self.assertRaises(RuntimeError):analyzer.generate_json('PRIVATE SECRET',{})
        self.assertEqual(analyzer.diagnostic['http_status'],500)
        self.assertEqual(analyzer.diagnostic['error_type'],'ValueError')
        self.assertNotIn('PRIVATE',json.dumps(analyzer.diagnostic))

    def test_warmup_and_dialogue_use_matching_bounded_context_and_keep_explicit_options(self):
        analyzer=LocalAnalyzer();analyzer.check_local=Mock()
        conn=Mock();response=conn.getresponse.return_value;response.status=200
        response.read.return_value=b'{"done":true}'
        with patch('butler_ambient.analysis.http.client.HTTPConnection',return_value=conn):
            analyzer.warm()
            warm=json.loads(conn.request.call_args.args[2])
            response.readline.side_effect=[b'{"response":"{}","done":true}\n',b'']
            analyzer.generate_json('synthetic',{})
            dialogue=json.loads(conn.request.call_args.args[2])
            self.assertEqual(warm['options']['num_ctx'],8192)
            self.assertEqual(dialogue['options']['num_ctx'],8192)
            response.readline.side_effect=[b'{"response":"{}","done":true}\n',b'']
            analyzer.generate_json('synthetic',{},options={'num_ctx':4096})
            self.assertEqual(json.loads(conn.request.call_args.args[2])['options']['num_ctx'],4096)

    def test_stream_timing_reports_backend_reload_cost_without_prompt_reply_or_thinking(self):
        analyzer=LocalAnalyzer();analyzer.check_local=Mock()
        conn=Mock();response=conn.getresponse.return_value;response.status=200
        response.readline.side_effect=[(json.dumps({'response':'{"reply":"PRIVATE OUTPUT"}',
            'done':True,'done_reason':'stop','load_duration':123000000,'prompt_eval_duration':45000000,
            'eval_duration':67000000,'total_duration':235000000,'prompt_eval_count':19,'eval_count':7,
            'prompt':'PRIVATE PROMPT','thinking':'PRIVATE THOUGHT'})+'\n').encode()]
        with patch('butler_ambient.analysis.http.client.HTTPConnection',return_value=conn):
            self.assertEqual(analyzer.generate_json('PRIVATE PROMPT',{}),{'reply':'PRIVATE OUTPUT'})
        value=inference_status(analyzer.diagnostic)
        self.assertEqual(value['context_tokens'],8192)
        self.assertEqual(value['load_ms'],123)
        self.assertEqual(value['prompt_ms'],45)
        self.assertEqual(value['generation_ms'],67)
        self.assertEqual(value['generated_tokens'],7)
        self.assertGreaterEqual(value['first_token_ms'],0)
        self.assertGreaterEqual(value['elapsed_ms'],value['first_token_ms'])
        self.assertNotIn('PRIVATE',json.dumps(value))

    def test_inference_ui_metadata_rejects_nonfinite_unknown_and_raw_values(self):
        value=inference_status({'stage':['PRIVATE'],'load_ms':float('inf'),'elapsed_ms':float('nan'),
            'http_status':200,'context_tokens':True,'prompt':'PRIVATE','response':'PRIVATE','thinking':'PRIVATE',
            'error_type':'PRIVATE SECRET','finish_reason':{'PRIVATE':True}})
        self.assertEqual(value,{'http_status':200})

    def test_split_analysis_metadata_never_exposes_names_or_raw_context(self):
        value=analysis_inference_status({'perception':{'stage':'completed','images':5,'prompt':'PRIVATE'},
            'association':{'stage':{'PRIVATE':True},'names':['PRIVATE']},'total_ms':float('nan'),'context':'PRIVATE'})
        self.assertEqual(value,{'perception':{'stage':'completed','images':5},'association':{}})
        self.assertNotIn('PRIVATE',json.dumps(value))


if __name__=='__main__':unittest.main()

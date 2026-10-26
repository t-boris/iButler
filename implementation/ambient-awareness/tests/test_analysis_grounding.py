"""History may label observed entities, never create current presence/activity."""
import json
import unittest
from unittest.mock import Mock,patch
from butler_ambient.analysis import LocalAnalyzer


def fact(kind='person',subject='current',**changes):
    return {'kind':kind,'subject':subject,'identity':'guessed name','detail':'Current green jacket',
        'activity':'rest','topic':'unknown','confidence':.97,**changes}


class AnalysisGroundingTests(unittest.TestCase):
    def setUp(self):
        self.model=LocalAnalyzer()
        self.context=[{'id':'known','name':'Synthetic known person','kind':'person','description':'Green jacket'}]
        self.match={'subject':'current','candidate':'ref0','confidence':.95}

    def test_blank_current_input_never_receives_or_repeats_prior_scene_facts(self):
        self.model.generate_json=Mock(return_value={'facts':[]})
        context=[{'observation_id':'old','facts':[fact(detail='PRIVATE_PAST_READING',identity='PRIVATE_NAME')]}]
        self.assertEqual(self.model.analyze('camera',images=[b'synthetic image'],context=context),[])
        self.model.generate_json.assert_called_once()
        prompt=self.model.generate_json.call_args.args[0]
        self.assertNotIn('PRIVATE_',prompt)
        self.assertNotIn('context',json.loads(prompt.split('Evidence follows as JSON data:\n')[1]))

    def test_association_can_only_label_present_subject_without_changing_current_facts(self):
        self.model.generate_json=Mock(side_effect=[{'facts':[fact()]},{'matches':[self.match],
            'facts':[fact(kind='book',detail='INVENTED_PAST_BOOK')]}])
        result=self.model.analyze('camera',images=[b'synthetic image'],context=self.context)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['activity'],'rest');self.assertEqual(result[0]['detail'],'Current green jacket')
        self.assertEqual(result[0]['subject'],'known');self.assertEqual(result[0]['identity'],'Synthetic known person')
        self.assertEqual(result[0]['identity_basis'],'model-inference')
        self.assertEqual(result[0]['confidence'],.95)
        self.assertNotIn('Synthetic known person',self.model.generate_json.call_args_list[0].args[0])
        self.assertNotIn('images',self.model.generate_json.call_args_list[1].kwargs)
        self.assertNotIn('Synthetic known person',json.dumps(self.model.analysis_inference))

    def test_unknown_subject_alias_kind_or_uncertain_and_duplicate_matches_are_rejected(self):
        invalid=[{'subject':'invented','candidate':'ref0','confidence':.99},
            {'subject':'current','candidate':'invented','confidence':.99},
            {'subject':'current','candidate':'ref0','confidence':True},
            {'subject':'current','candidate':'ref0','confidence':.8}]
        for match in invalid:
            with self.subTest(match=match):
                model=LocalAnalyzer();model.generate_json=Mock(side_effect=[{'facts':[fact()]},{'matches':[match]}])
                result=model.analyze('camera',context=self.context)
                self.assertEqual(result[0]['subject'],'current');self.assertEqual(result[0]['identity'],'')
        for matches,context in (([self.match,self.match],self.context),
                ([self.match],[{**self.context[0],'kind':'dog'}])):
            model=LocalAnalyzer();model.generate_json=Mock(side_effect=[{'facts':[fact()]},{'matches':matches}])
            self.assertEqual(model.analyze('camera',context=context)[0]['identity'],'')

    def test_owner_correction_is_a_candidate_not_current_scene_evidence(self):
        context=[{'observation_id':'old','facts':[fact(subject='known',identity='Corrected name',
            identity_basis='owner-confirmed',detail='PRIVATE_OLD_ACTIVITY')]}]
        self.model.generate_json=Mock(side_effect=[{'facts':[fact()]},{'matches':[self.match]}])
        result=self.model.analyze('camera',context=context)
        self.assertEqual(result[0]['identity'],'Corrected name')
        self.assertEqual(result[0]['identity_basis'],'model-inference')
        self.assertNotIn('PRIVATE_',self.model.generate_json.call_args_list[0].args[0])

    def test_association_cache_expires_without_extending_its_original_lifetime(self):
        self.model.generate_json=Mock(side_effect=[{'facts':[fact()]},{'matches':[self.match]},
            {'facts':[fact()]},{'facts':[fact()]},{'matches':[]}])
        with patch('butler_ambient.analysis.time.monotonic',return_value=1000):
            self.model.analyze('camera',context=self.context)
        with patch('butler_ambient.analysis.time.monotonic',return_value=1001):
            self.assertEqual(self.model.analyze('camera',context=self.context)[0]['identity'],'Synthetic known person')
        with patch('butler_ambient.analysis.time.monotonic',return_value=1121):
            self.assertEqual(self.model.analyze('camera',context=self.context)[0]['identity'],'')
        self.assertEqual(self.model.generate_json.call_count,5)

    def test_cancel_clears_cache_and_rejects_inflight_association(self):
        def association(*args,**kwargs):
            self.model.cancel();return {'matches':[self.match]}
        self.model.generate_json=Mock(side_effect=[{'facts':[fact()]},association])
        # A callable within side_effect's list is a return value, so dispatch
        # explicitly to exercise cancellation at the second model boundary.
        calls=[0]
        def generate(*args,**kwargs):
            calls[0]+=1
            return {'facts':[fact()]} if calls[0]==1 else association()
        self.model.generate_json=Mock(side_effect=generate)
        with self.assertRaises(RuntimeError):self.model.analyze('camera',context=self.context)
        self.assertEqual(self.model.association_cache,{})

    def test_cancellation_after_perception_cannot_start_a_new_association_request(self):
        def generate(*args,**kwargs):self.model.cancel();return {'facts':[fact()]}
        self.model.generate_json=Mock(side_effect=generate)
        with self.assertRaises(RuntimeError):self.model.analyze('camera',context=self.context)
        self.model.generate_json.assert_called_once()

    def test_compact_current_facts_keep_typed_defaults_without_a_visual_name_guess(self):
        self.model.generate_json=Mock(return_value={'facts':[{'kind':'person','detail':'Green jacket','subject':'new','confidence':.8}]})
        result=self.model.analyze('camera')
        self.assertEqual(result[0]['activity'],'unknown');self.assertEqual(result[0]['topic'],'unknown')
        self.assertEqual(result[0]['identity'],'')
        schema=self.model.generate_json.call_args.args[1]['properties']['facts']
        self.assertNotIn('identity',schema['items']['properties'])
        self.assertEqual(set(schema['items']['required']),{'kind','detail','subject','confidence'})

    def test_association_cache_has_a_fixed_bound_and_clears_on_cancel(self):
        def generate(prompt,schema,**kwargs):
            if 'facts' in schema['properties']:
                return {'facts':[fact(subject='current'+str(self.serial))]}
            return {'matches':[{'subject':'current'+str(self.serial),'candidate':'ref0','confidence':.95}]}
        self.model.generate_json=Mock(side_effect=generate)
        with patch('butler_ambient.analysis.time.monotonic',return_value=1000):
            for self.serial in range(36):self.model.analyze('camera',context=self.context)
        self.assertEqual(len(self.model.association_cache),32)
        self.model.cancel();self.assertEqual(self.model.association_cache,{})

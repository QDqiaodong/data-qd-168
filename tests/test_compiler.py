import json
from pathlib import Path
import unittest
from locale_bundle.compiler import compile_bundle
from locale_bundle.messages import render, signature
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class LocaleTests(unittest.TestCase):
    def test_fallback_provenance_and_render(self):
        result=compile_bundle(sample());greeting=result['catalogs']['fr-CA']['greeting']
        self.assertEqual(greeting['source_locale'],'fr')
        self.assertEqual(render(greeting,{'name':'Ada'}),'Bonjour Ada')
        self.assertEqual(len(result['fallback_messages']),1)
    def test_implicit_default(self):
        data=sample();data['fallback']={}
        self.assertEqual(compile_bundle(data)['catalogs']['fr-CA']['greeting']['source_locale'],'en')
    def test_empty_translation_is_present(self):
        data=sample();data['catalogs']['en']['empty']={'value':'default','variables':{}}
        data['catalogs']['fr-CA']['empty']={'value':'','variables':{}}
        self.assertEqual(compile_bundle(data)['catalogs']['fr-CA']['empty']['value'],'')
    def test_cycle_rejected_even_unrequested(self):
        data=sample();data['fallback']={'fr-CA':'fr','fr':'fr-CA'};data['requested']=['en']
        with self.assertRaisesRegex(ValueError,'cycle'):compile_bundle(data)
    def test_unknown_key_rejected(self):
        data=sample();data['catalogs']['fr']['unknown']={'value':'x','variables':{}}
        with self.assertRaisesRegex(ValueError,'unknown key'):compile_bundle(data)
    def test_translation_types_must_agree(self):
        data=sample();data['catalogs']['fr']['greeting']['variables']['name']='integer'
        with self.assertRaisesRegex(ValueError,'types'):compile_bundle(data)
    def test_placeholder_mismatch_rejected(self):
        data=sample();data['catalogs']['fr']['greeting']['value']='Bonjour {other}'
        with self.assertRaisesRegex(ValueError,'placeholder'):compile_bundle(data)
    def test_unsafe_and_positional_fields_rejected(self):
        for value in ('{name.upper}','{name[0]}','{}','{name!r}','{name:03}','{'):
            with self.subTest(value=value),self.assertRaises(ValueError):signature(value)
    def test_literal_and_repeated_placeholders(self):
        self.assertEqual(signature('{{ok}} {name} {name}'),{'name'})
    def test_bool_does_not_render_as_integer(self):
        message=compile_bundle(sample())['catalogs']['fr-CA']['cart']
        with self.assertRaises(ValueError):render(message,{'count':True})
    def test_extra_render_variables_rejected(self):
        message=compile_bundle(sample())['catalogs']['fr-CA']['cart']
        with self.assertRaises(ValueError):render(message,{'count':2,'extra':'x'})

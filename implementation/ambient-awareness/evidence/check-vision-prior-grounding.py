"""Synthetic blank input must not inherit a person/activity from prior context."""
from pathlib import Path
import io,json
from PIL import Image
from butler_ambient.analysis import LocalAnalyzer,inference_status

output=io.BytesIO();Image.new('RGB',(640,360),(20,80,220)).save(output,format='PNG')
prior={'observation_id':'synthetic-prior','source':'camera','start':1,'end':2,'basis':'prior-model-inferences',
    'facts':[{'kind':'person','activity':'reading','detail':'A fictional person is reading a book.',
              'subject':'fictional-person','identity':'Fictional person','confidence':.99,'topic':'unknown'}]}
results=[]
for name in ('gemma3:4b','qwen3.5:9b'):
    model=LocalAnalyzer(name)
    facts=model.analyze('camera',images=[output.getvalue()],context=[prior])
    result={'model':name,'facts_count':len(facts),
        'unsupported_person_book_dog_or_work':any(f['kind'] in {'person','book','dog','work-topic'} or f.get('activity','unknown')!='unknown' or f.get('identity','') for f in facts),
        'prior_detail_repeated':any(f.get('detail')==prior['facts'][0]['detail'] for f in facts),
        'diagnostic':inference_status(model.diagnostic)}
    results.append(result);print(json.dumps(result),flush=True)
Path(__file__).with_name('vision-prior-grounding.json').write_text(json.dumps({'synthetic_only':True,'results':results},indent=2)+'\n')

"""Exact-source/current-runtime audit; no live transcript or owner intent inferred."""
from pathlib import Path
import json,hashlib,re,datetime as dt,subprocess
root=Path(__file__).resolve().parents[1];e=root/'evidence';r=json.loads((e/'results.json').read_text());deploy=json.loads((e/'streaming-deployment.json').read_text());offline=json.loads((e/'split-hearing-installed-offline.json').read_text());live=json.loads((e/'current-background-live.json').read_text())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert r['passed'] and all(x['exit']==0 for x in r['jobs']) and all(sha(root/k)==v for k,v in r['source_sha256'].items())
package=Path.home()/'.openclaw/tools/ambient-awareness/.venv/lib/python3.12/site-packages/butler_ambient'
assert all(sha(p)==sha(package/p.relative_to(root/'butler_ambient')) for p in (root/'butler_ambient').glob('*.py'))
assert deploy['before']==deploy['after'] and deploy['stable_parent_config_agreements_preferences_unchanged']
assert offline['passed'] and offline['installed_package'] and not offline['audio_played'] and not offline['private_authority_granted'] and not offline['private_jobs_queued']
focused=int(re.search(r'Ran (\d+) tests',(e/'installed-public-hearing-tests.log').read_text())[1]);assert focused==107
counts={}
for job in r['jobs']:
 text=(e/job['log']).read_text();m=re.search(r'Ran (\d+) tests?',text)
 if m:counts[job['name']]=int(m[1])
 if job['name']=='whatsapp-node-tests':counts[job['name']]=int(re.search(r'ℹ tests (\d+)',text)[1])
assert sum(counts.values())==440
assert all(s['microphone']['working'] and s['camera_working'] for s in live['samples'])
assert all(v=='ready' for v in live['samples'][-1]['models'].values())
app=Path.home()/'Applications/Butler.app/Contents/MacOS/Butler'
assert sha(app)==sha(root/'build/butler-panel')
helpers={name:sha(root/'build'/name)==sha(Path.home()/'.openclaw/tools/ambient-awareness/native'/name) for name in ('butler-audio','butler-panel','butler-camera','butler-owner-auth','butler-system-audio')};assert all(helpers.values())
p=e/'current-runtime-verification.json';d=json.loads(p.read_text());d.update(at=dt.datetime.now(dt.timezone.utc).isoformat(),tests=sum(counts.values()),feature_tests=counts['feature-tests'],native_ui_checks=42,verification_jobs=21,all_checks_passed=True,source_hashes_match=True,installed_sources_match=True,installed_native_hashes_match=True,native_binary_matches=helpers,installed_focused_tests=focused)
d['verification_state']='DEC060/061 public hearing split/busy-output and recognizer framing verified in exact installed440-test build; actual owner dialogue remains unverified'
d['public_hearing_offline']=offline;d['fresh_microphone_camera_samples']=len(live['samples']);d['current_ui_foreground_samples']=sum(s['butler_ui_foreground'] is True for s in live['samples']);d['background_fresh_samples']=sum(s['butler_ui_foreground'] is False for s in live['samples'])
d['prior_background_evidence']='pre-public-hearing-current-background-live.json';d['storage_evidence_scope']='Prior431-test storage lifecycle/capture evidence; storage code unchanged and full440-test regressions pass'
d['models_ready']=live['samples'][-1]['models'];d['goal_complete']=False;d['actual_owner_spoken_conversation_verified']=False
p.write_text(json.dumps(d,indent=2)+'\n')
p=e/'current-completion-status.json';d=json.loads(p.read_text());d.update(timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),tests=sum(counts.values()),feature_tests=counts['feature-tests'],native_ui_checks=42,verification_jobs=21,all_automated_checks_passed=True,source_hashes_match=True,installed_sources_match=True,installed_native_hashes_match=True)
d['required_checklist'][1].update(status='satisfied',evidence='DEC060/061,BUG009 resolved; REQ010/I10/I17/F004 remain in_progress;109feature metadata files/26tasks valid')
d['required_checklist'][3].update(status='satisfied',evidence='results.json:440tests/42UIchecks/21jobs allgreen;107installed-public-hearing-tests;split-hearing-installed-offline.json passed; exact streaming-deployment.json')
d['public_hearing_scope_verified']=True;d['goal_complete']=False;d['actual_owner_spoken_conversation_verified']=False
for name in ('installed-public-hearing-tests.log','split-hearing-installed-offline.json','synthetic-whisper-framing.json','public-hearing-ui-refresh.json'):
 if name not in d['evidence']:d['evidence'].append(name)
p.write_text(json.dumps(d,indent=2)+'\n')
p=root/'VERIFICATION.md';old=p.read_text();header='''# Current verification — 440 tests; public hearing repair installed

All 369 feature +71 project regression tests, 42 native UI checks and21verification jobs pass: [results](evidence/results.json). Exact source/wheel/native code installed; stable permission parent/configuration/agreements/preferences unchanged: [deployment](evidence/streaming-deployment.json), [integrity](evidence/current-runtime-verification.json). Native UI binary is identical and was left running: [UI refresh](evidence/public-hearing-ui-refresh.json).107 installed voice/control tests pass: [log](evidence/installed-public-hearing-tests.log).

DEC-060/061 repair two public-status failures: a short pause between the assistant vocative and the exact hearing question, and refusal while older private work owns the output. A volatile4s capture-bound alias can join only the next exact hearing question; expiry, input gaps, unrelated/reporting/quoted/extended text and full Pause cannot authorize anything. Older private generation/audio is cancelled with context/pending review preserved; output handoff waits≤0.75s without holding the control lock, rechecking Pause/freshness. The reply is the existing fixed nameless public acknowledgement, with no personal knowledge, private job or owner authority.

Actual Whisper adds outer quotes to known unquoted synthetic speech. The candidate literal guard failed before installation; DEC-061 canonicalizes a single outer ASR frame only, preserving words and inline/nested/reporting content. [Framing evidence](evidence/synthetic-whisper-framing.json), [retained failed crops](evidence/split-hearing-first-crops.json). Real **installed** local resident Whisper now recognizes the0.7s split fixture and the isolated listener selects exactly one public route with no authority/private job/no sound played: [installed offline evidence](evidence/split-hearing-installed-offline.json). This does not test an intended live owner or real audible output. BUG-009 resolved at this controlled routing/formatting scope.

[Current runtime metadata](evidence/current-background-live.json) proves10/10fresh mic/camera and all4modelsready. The UI was foreground for this window; no new background claim is inferred. [Prior background proof](evidence/pre-public-hearing-current-background-live.json) remains scoped to the earlier431-test build; native UI/capture code are unchanged. Storage scope remains verified by the prior lifecycle evidence and current full regressions; the historical storage section below retains its timestamps/counts.

**The full goal/F-004 remain incomplete.** Intended owner dialogue/semantic correctness/audibility, real end-to-end latency, voice-only identity, complete real spoken desktop/MCP control and genuine call classification remain open: [status](evidence/current-completion-status.json), [voice metadata only](evidence/current-voice-state.json). Q013/Q014/Q015/Q016 have no new owner answer; ordinary private reply gating is unchanged. No model/reference/threshold/device/volume/version/shared-gateway change, production playback from recorded fixtures, MarkView browser check or manual owner checklist. Git/main/commits/GitHub/PR/CI waived by DEC005; no merge. Earlier evidence below is historical.

---

'''
old=old.replace('# Current verification','# Historical verification',1);p.write_text(header+old)
print(json.dumps({'tests':440,'ui_checks':42,'jobs':21,'installed_voice_tests':107,'offline_installed_split_passed':True,'current_fresh_samples':10,'current_ui_foreground':True,'source_runtime_match':True,'goal_complete':False}))

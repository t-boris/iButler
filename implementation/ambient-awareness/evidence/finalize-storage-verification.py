"""Finalize current evidence from exact artifacts and metadata-only live checks."""
from pathlib import Path
import datetime as dt
import hashlib,json,re,subprocess

root=Path(__file__).resolve().parents[1];e=root/'evidence';project=root.parents[1]
r=json.loads((e/'results.json').read_text());deployment=json.loads((e/'streaming-deployment.json').read_text())
background=json.loads((e/'current-background-live.json').read_text());probe=json.loads((e/'camera-clips-installed-live.json').read_text());lifecycle=json.loads((e/'storage-lifecycle-final.json').read_text())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
counts={}
for job in r['jobs']:
    log=(e/job['log']).read_text();match=re.search(r'Ran (\d+) tests?',log)
    if match:counts[job['name']]=int(match[1])
    if job['name']=='whatsapp-node-tests':counts[job['name']]=int(re.search(r'ℹ tests (\d+)',log)[1])
ui_checks=json.loads((e/'ui-preview/native-panel-layout.json').read_text())['checks']
assert r['passed'] and all(j['exit']==0 for j in r['jobs']) and all(x['passed'] for x in ui_checks)
source_match=all(sha(root/k)==v for k,v in r['source_sha256'].items());assert source_match
runtime=Path.home()/'.openclaw/tools/ambient-awareness';app=Path.home()/'Applications/Butler.app'
installed=runtime/'.venv/lib/python3.12/site-packages/butler_ambient'
installed_match=all(sha(p)==sha(installed/p.relative_to(root/'butler_ambient')) for p in (root/'butler_ambient').glob('*.py'));assert installed_match
native={name:sha(root/'build'/name)==sha(runtime/'native'/name) for name in ('butler-audio','butler-panel','butler-camera','butler-owner-auth','butler-system-audio')};assert all(native.values())
ui_match=sha(root/'build/butler-panel')==sha(app/'Contents/MacOS/Butler');icon_match=sha(root/'butler_ambient/assets/Butler.icns')==sha(app/'Contents/Resources/Butler.icns');assert ui_match and icon_match
assert deployment['stable_parent_config_agreements_preferences_unchanged'] and deployment['before']==deployment['after']
focused=int(re.search(r'Ran (\d+) tests', (e/'installed-camera-storage-tests.log').read_text())[1]);assert focused==98
assert all(s['microphone']['working'] and s['camera_working'] and s['butler_ui_foreground'] is False for s in background['samples'])
assert all(value=='ready' for value in background['samples'][-1]['models'].values())
summary=probe['summary'];assert summary['fresh_microphone']==summary['samples']==summary['fresh_camera'] and summary['fresh_previews']==summary['requested_previews'] and summary['all_spool_bounded'] and not any(summary['errors'].values())
assert all(x['files']==x['analyzed']==x['reclaimed'] for x in lifecycle['probe_inputs']) and lifecycle['spool_now_bytes']<lifecycle['spool_start_bytes']<=lifecycle['limit_bytes']
ui=[];expected=str(app/'Contents/MacOS/Butler')
for line in subprocess.check_output(['ps','-axo','pid=,args='],text=True).splitlines():
    pid,_,args=line.strip().partition(' ')
    if args==expected or args.startswith(expected+' '):ui.append(int(pid))
assert len(ui)==1
current={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'tests':sum(counts.values()),'feature_tests':counts['feature-tests'],'native_ui_checks':len(ui_checks),'verification_jobs':len(r['jobs']),
    'all_checks_passed':True,'source_hashes_match':source_match,'installed_sources_match':installed_match,'native_binary_matches':native,'installed_native_hashes_match':all(native.values()),
    'native_ui_matches':ui_match,'icon_matches':icon_match,'running_ui_pids':ui,'stable_parent_config_agreements_preferences_unchanged':True,'installed_focused_tests':focused,
    'background_fresh_samples':len(background['samples']),'models_ready':background['samples'][-1]['models'],'actual_microphone':background['samples'][-1]['microphone']['name'],
    'system_input':background['samples'][-1]['system_input'],'system_output':background['samples'][-1]['system_output'],
    'voice_processing_enabled':background['samples'][-1]['voice_processing_enabled'],'other_audio_ducking':background['samples'][-1]['other_audio_ducking'],
    'storage_capture_summary':summary,'post_window_lifecycle':lifecycle,'raw_speech_or_media_exported':False,'actual_owner_spoken_conversation_verified':False,
    'open_questions':['Q-013','Q-014','Q-015','Q-016'],'goal_complete':False,
    'verification_state':'DEC056–059 analysis-first storage/current-only perception/continuous clips/volatile preview verified; full live owner voice goal remains open'}
(e/'current-runtime-verification.json').write_text(json.dumps(current,indent=2)+'\n')
waiver='docs/features/butler-vision-and-ambient-awareness/decisions/DEC-005.md'
items=[{'item':i,'status':'waived by DEC-005','evidence':waiver} for i in range(1,8)]
items[1].update(status='satisfied',evidence='implementation/plan.md; REQ003/004/024/025 verified; DEC053–059; BUG008 resolved; F004 and full voice criteria remain open;107feature frontmatters parse')
items[3].update(status='satisfied',evidence=f"evidence/results.json:{sum(counts.values())}tests/{len(ui_checks)}UIchecks/{len(r['jobs'])}jobs exit0; installed-camera-storage-tests.log:{focused}tests; exact streaming-deployment.json")
remaining=[
    'REQ010/I10/F004: intended owner question, name-free follow-up and correct audible reply remain unverified; current-voice-state.json records metadata only. Q013 ordinary-reply policy and Q014 input preference have no new answer.',
    'REQ013/I13: subjective audibility of other applications remains unconfirmed; no global device or volume change.',
    'REQ017/I17: real intended-owner end-of-speech-to-first-audio and sustained dialogue latency remain unverified; background analysis has no90s completion SLA.',
    'REQ018/I19: actual current voice-only identity remains unverified; enrolled references/thresholds unchanged.',
    'REQ021/I22: complete real spoken desktop/MCP operation remains unverified despite controlled adapter coverage.',
    'REQ023/I24: genuine muted/end-call meeting classification remains unverified.',
    'Q015 permanent automatic image selection is unanswered; current explicit-only preservation is implemented. Q016 optional model choice remains unanswered/nonblocking; selected Gemma remains unchanged.'
]
completion={'timestamp':current['at'],'goal_complete':False,'tests':current['tests'],'feature_tests':current['feature_tests'],'native_ui_checks':current['native_ui_checks'],'verification_jobs':current['verification_jobs'],
    'all_automated_checks_passed':True,'source_hashes_match':True,'installed_sources_match':True,'installed_native_hashes_match':True,'stable_permission_parent_unchanged':True,
    'git_repository_present':(project/'.git').exists(),'required_checklist':items,'storage_scope_verified':True,'remaining':remaining,
    'no_merge_version_bump_shared_gateway_restart_global_volume_change_or_manual_owner_checklist':True,'actual_owner_spoken_conversation_verified':False,
    'evidence':['results.json','streaming-deployment.json','installed-camera-storage-tests.log','camera-clips-installed-live.json','storage-lifecycle-final.json','current-background-live.json','vision-prior-grounding.json','spec-metadata-validation.json','current-voice-state.json']}
(e/'current-completion-status.json').write_text(json.dumps(completion,indent=2)+'\n')
p=root/'VERIFICATION.md';old=p.read_text();tail=old[old.index('---\n'):];tail=re.sub(r'^# Current verification', '# Historical verification',tail,flags=re.M)
header=f'''# Current verification — {current['tests']}tests; storage scope verified

All{current['feature_tests']}feature+71project regression tests, {current['native_ui_checks']}native UI checks and{current['verification_jobs']}verification jobs pass: [results](evidence/results.json). The exact source/wheel/native/UI/icon are installed, with stable permission parent/configuration/agreements/preferences unchanged: [deployment](evidence/streaming-deployment.json), [integrity](evidence/current-runtime-verification.json). The installed interpreter passes{focused}focused tests: [log](evidence/installed-camera-storage-tests.log).

DEC-056–059 implement a512MiB temporary analysis spool, atomic facts/provenance before raw cleanup, incremental legacy cleanup, continuous≤10s camera clips with≤3timestamped representatives, constrained current-only perception/name association, fair current/recent-tail scheduling, quota recovery headroom and a fresh volatile native preview independent of disk admission. [107feature records/26-task metadata validation](evidence/spec-metadata-validation.json) passes. REQ003/004/024/025 and I4/I6/I25/I26 are verified in this controlled/installed scope.

[Actual181s scene-changing capture](evidence/camera-clips-installed-live.json):181/181fresh microphone/camera,37/37fresh private previews,zero capture/analysis/participant errors,spool≤512MiB. Analysis is asynchronous: at window end2camera and21system-audio inputs older90s are still pending; no90s completion guarantee is claimed. A [later lifecycle audit](evidence/storage-lifecycle-final.json) at+213s proves all380closed inputs wholly inside that window analyzed/reclaimed (18camera,180microphone,3screenshots,179system-audio), while continuous ingestion continues and spool falls504,682,131→491,477,893bytes. Existing1,920,700,558bytes/15,367legacy originals remain pending under their24h expiry; they are not discarded to meet the new quota. Derived metadata survives raw cleanup, selected copies remain, and unapproved automatic image selection stays off pendingQ015.

[Background evidence](evidence/current-background-live.json):10/10fresh microphone/camera while UI notforeground,all4modelsready,no global audio/device/volume change. Both actual local models pass the synthetic blank-plus-fictional-prior counterexample: [grounding](evidence/vision-prior-grounding.json); BUG008 resolved. Earlier saturated/negative results are retained in camera-clips-saturated-before-hysteresis.json, camera-clips-recovery-live.json and the before regression logs.

**The full goal/F004 remain incomplete.** Intended owner dialogue/semantic correctness/audibility, real end-to-end latency, voice-only identity, complete real spoken desktop/MCP control and genuine call classification remain unverified: [current status](evidence/current-completion-status.json), [metadata-only voice audit](evidence/current-voice-state.json). Q013/Q014/Q015/Q016 have no new owner answer. No model/reference/threshold/device/version/shared-gateway change, MarkView browser check or manual owner checklist. Git/main/commits/GitHub/PR/CI are waived by DEC005; no merge performed. All earlier verification below is historical.

'''
p.write_text(header+tail)
notes=root/'WORKING-NOTES.md';older=notes.read_text();notes.write_text(f'''# Current state — storage complete; full live voice goal still open

{current['tests']}tests={current['feature_tests']}feature+71regressions, {current['native_ui_checks']}native UI checks and21jobs pass. Exact source/wheel/native/UI/icon installed; stable parent/configuration/agreements/preferences unchanged.98installed focused tests pass.10/10background freshmic/camera/all4modelsready. Current-runtime-verification.json has fresh hashes/PID. All prior tool sessions are terminal; no verifier, deployment, benchmark or probe remains running.

DEC056–059 complete:512MiBspool, atomic derived commit before cleanup, continuous≤10sclips/≤3selectedframes withtimestamps, openclipappendrollback/owner-lock crashrecovery, minute/recent180s-tail priority and immutable snapshot cutoff,8MiBresumeheadroom,volatile privatepreviewindependentofquota. No model/config/device/reference/threshold/volumechanges. Actual181s probe:181/181freshsensors,37/37preview,zeroerrors,boundedspool; no90sSLA. At+213s ALL380closedwindowinputs analyzed/reclaimed (18cam/180mic/3screen/179sys), spool504.68→491.48MB despiteingestion. Legacy1.921GB/15,367filesunprocessedkeep24hdeadline. Q015unansweredexplicit-onlypermanentselection;Q016optional/unaskedasyncmodelchoice remainsnonblocking,nochangeGemma. BUG008resolvedafterinstalledactual2modelblank-priorcounterexample.107featurefrontmatters/26taskplanparse; mispositionedupdated/unquotedcolon summaries repaired. REQ003/004/024/025 andI4/I6/I25/I26verified. Latestbeforelogsforprioritytail/snapshotfairnessretained. See VERIFICATION.md andevidence/current-completion-status.json.

FullgoalstillACTIVE/unachieved: REQ010/I10/F004owneractualdialogue/followup/semanticcorrectness/audibility,REQ013/I13,REQ017/I17actualvoiceend-to-endlatency,REQ018/I19voice-onlyidentity,REQ021/I22fullrealvoice-desktop-MCP,REQ023/I24genuinecallclassification. Currentvoice-state.json metadataonlyhasdialoguepermissionfalse; counters/playbackarenotowner-successproof. Q013ordinaryreplypolicy/Q014inputpreferenceunanswered; do NOT reask recorded enrollment/lamp questions, fabricate identity, loosen thresholds or repeat old synthetic tests as live acceptance. No new owner answer beyond proceed (already DEC056). This turn made meaningful storage progress; do not mark complete. Follow blocked-audit rules for future actual no-progress turns, not a budget-driven completion.

DEC005waivesallGit(norepo);DEC007ownlocaldeploymentauthorized. No versionbump/sharedgatewayrestart/MarkViewbrowser/manualownerchecklist/subagents. Whenstoppingprint7requireditems✓/✗withlinks plusfullgoalincomplete; no new permissionneeded for finishedowninstall.

---

'''+older)
print(json.dumps({'tests':current['tests'],'ui_checks':current['native_ui_checks'],'jobs':current['verification_jobs'],'installed_focused_tests':focused,'source_and_runtime_match':True,'running_ui_pids':ui,'storage_scope_verified':True,'goal_complete':False}))

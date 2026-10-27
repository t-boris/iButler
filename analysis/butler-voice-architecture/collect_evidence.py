"""Read-only source/runtime inventory. Never saves audio, transcripts or secrets."""
from pathlib import Path
import datetime
import hashlib
import json
import re
import socket

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
FEATURE = ROOT / 'docs/features/butler-attention-architecture-knowing-whom-the-owner-is'
EVIDENCE = ROOT / 'implementation/ambient-awareness/evidence/attention-implementation'
AMBIENT = ROOT / 'implementation/ambient-awareness/butler_ambient'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rpc(path, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall((json.dumps(request) + '\n').encode())
        return json.loads(client.makefile('rb').readline())['result']


def main():
    rows = []
    for path in sorted(FEATURE.rglob('*')):
        if not path.is_file():
            continue
        text = path.read_text()
        field = lambda key: next(iter(re.findall(r'^' + key + r': (.*)$', text, re.M)), '').strip('"')
        rows.append({'path': str(path.relative_to(ROOT)), 'title': field('title') or path.stem,
                     'type': field('type'), 'status': field('status'),
                     'date': field('updated') or field('created'),
                     'lines': len(text.splitlines()), 'sha256': sha(path)})
    (OUT / 'source_index.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    deployment = json.loads((EVIDENCE / 'deployment.json').read_text())
    verification = json.loads((EVIDENCE / 'release-verification.json').read_text())
    config_path = Path.home() / '.local/share/butler-ambient/config.json'
    config = json.loads(config_path.read_text())
    safe_keys = ['conversation_model', 'vision_model', 'turn_control', 'voice_brain', 'brain_session']
    data = {'observed_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'scope': 'metadata only; no raw media, transcript, task content or credentials copied',
            'feature_files': len(rows), 'git_present': (ROOT / '.git').exists(),
            'source_version': re.search(r'__version__ = "([^"]+)"', (AMBIENT / '__init__.py').read_text())[1],
            'deployment_record': {key: deployment[key] for key in ['started', 'versions', 'verified', 'installed', 'running', 'turn_control']},
            'verification_record': {key: verification[key] for key in ['finished', 'versions', 'passed', 'source_unchanged', 'reused_from', 'changed_sources']},
            'latest_checks': verification['jobs'][-3:],
            'configuration': {key: config.get(key) for key in safe_keys},
            'whisper_model_filename': Path(config.get('whisper_model', '')).name,
            'source_hashes': {str(p.relative_to(ROOT)): sha(p) for p in sorted(AMBIENT.rglob('*')) if p.suffix in {'.py', '.swift'}},
            'source_matches_tested_manifest': all((ROOT / p).is_file() and sha(ROOT / p) == h for p, h in verification['source_sha256'].items()),
            'artifacts_record': verification.get('artifacts')}
    try:
        status = rpc(Path.home() / '.local/share/butler-ambient/ambient.sock', {'command': 'status'})
        data['runtime_status'] = {key: status.get(key) for key in ['version', 'enabled', 'capture_errors', 'participant_check_skipped', 'participant_check_error']}
        core = rpc(Path.home() / '.local/share/butler-core/core.sock', {'id': 'architecture-review', 'method': 'doctor', 'params': {}})
        data['runtime_core_version'] = core.get('core')
    except (OSError, KeyError, ValueError) as error:
        data['runtime_read_error_type'] = type(error).__name__
    installed = Path.home() / '.openclaw/tools/ambient-awareness/.venv/lib/python3.12/site-packages/butler_ambient/conversation.py'
    data['installed_conversation_sha256'] = sha(installed)
    data['installed_old_deadline_present'] = 'time.monotonic() - self.turn_started > 90' in installed.read_text()
    log = Path.home() / '.local/share/butler-ambient/logs/service.log'
    lines = log.read_text().splitlines()
    crash = []
    for i, line in enumerate(lines):
        if line.startswith('Exception in thread') and '(_run)' in line:
            block = lines[i:i+13]
            if any('local audio deadline' in item for item in block):
                crash = block
    data['dialogue_worker_crash_in_service_log'] = bool(crash)
    data['thread_liveness'] = 'Not exposed by status RPC; traceback proves worker termination at the recorded failure, not current recovery'
    (OUT / 'runtime_evidence.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    (OUT / 'playback-crash.txt').write_text('\n'.join(crash) + '\n')
    print(json.dumps({key: data[key] for key in ['observed_utc', 'feature_files', 'source_version', 'source_matches_tested_manifest', 'runtime_status', 'runtime_core_version', 'installed_old_deadline_present', 'dialogue_worker_crash_in_service_log']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""Installer regressions without launchctl, model runners or private configuration."""
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dialogue_server


class BackgroundDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'Library/LaunchAgents/sh.brew.ollama.plist'

    def test_missing_optional_homebrew_service_does_not_launch_or_create_anything(self):
        with patch.object(dialogue_server.subprocess, 'run') as run:
            result = dialogue_server.configure_background(self.home)
        self.assertEqual(result, {'changed': False, 'available': False, 'reason': 'background-launch-agent-not-installed'})
        run.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_existing_configured_background_service_keeps_its_bytes_and_process(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(plistlib.dumps({'ProgramArguments':['/opt/homebrew/bin/ollama','serve'],
            'EnvironmentVariables':{'OLLAMA_FLASH_ATTENTION':'1','OLLAMA_KV_CACHE_TYPE':'q8_0',
                                    'OLLAMA_MAX_LOADED_MODELS':'5','OLLAMA_NUM_PARALLEL':'2'}}))
        before = self.path.read_bytes()
        with patch.object(dialogue_server.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertFalse(dialogue_server.configure_background(self.home)['changed'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(run.call_count, 1)
        self.assertIn('print', run.call_args.args[0])

    def test_existing_unexpected_launch_agent_still_fails_before_launchctl(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(plistlib.dumps({'ProgramArguments':['/other/process']}))
        with patch.object(dialogue_server.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'unexpected background Ollama service'):
                dialogue_server.configure_background(self.home)
        run.assert_not_called()

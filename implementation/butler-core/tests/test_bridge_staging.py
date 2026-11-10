"""The deploy stages the bridge plugin file by file; a file it misses makes the plugin fail to load at the next gateway start.

BUG-025 added `lib/process.js` and `index.js` imports it, but the list of staged files did not name it, so a deploy
would have left a plugin that cannot start.
"""
import re
import unittest
from pathlib import Path

import deploy

BRIDGE = Path(__file__).resolve().parents[1] / "bridge"
IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*["'](\.{1,2}/[^"']+)["']""")


class BridgeStagingTest(unittest.TestCase):
    def test_every_runtime_file_of_the_bridge_is_staged(self):
        on_disk = {str(p.relative_to(BRIDGE)) for p in BRIDGE.rglob("*")
                   if p.is_file() and not {"node_modules", "test"} & set(p.relative_to(BRIDGE).parts)}
        self.assertEqual(on_disk, set(deploy.BRIDGE_FILES))

    def test_every_relative_import_points_at_a_staged_file(self):
        for name in deploy.BRIDGE_FILES:
            path = BRIDGE / name
            if path.suffix != ".js": continue
            for target in IMPORT.findall(path.read_text()):
                resolved = (path.parent / target).resolve().relative_to(BRIDGE.resolve())
                self.assertIn(str(resolved), deploy.BRIDGE_FILES, f"{name} imports {target}")


if __name__ == "__main__":
    unittest.main()

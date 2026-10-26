"""REQ-036: the Mac's own state (front app, audio playing) as metadata for the addressee decision."""
import unittest
from unittest.mock import Mock, patch

from butler_ambient import desktop


class Done:
    def __init__(self, stdout, returncode=0): self.stdout, self.returncode = stdout, returncode


class DesktopTests(unittest.TestCase):
    def test_front_app_is_parsed_from_lsappinfo_without_window_content(self):
        answers = iter([Done("ASN:0x0-0xd3da3cd:\n"), Done('"‎WhatsApp" ASN:0x0-0xd3da3cd: (in front) \n    bundleID="net.whatsapp.WhatsApp"\n')])
        with patch.object(desktop.subprocess, "run", side_effect=lambda *a, **k: next(answers)):
            self.assertEqual(desktop.foreground_app(), {"name": "WhatsApp", "bundle_id": "net.whatsapp.WhatsApp"})
        with patch.object(desktop.subprocess, "run", side_effect=OSError("no lsappinfo")):
            self.assertEqual(desktop.foreground_app(), {})

    def test_quiet_alone_needs_a_known_quiet_desktop(self):
        service = Mock(archive=Mock(last={"system-audio": 100.0}, system_dbfs=-70.0))
        with patch.object(desktop, "foreground_app", return_value={"name": "Finder", "bundle_id": "com.apple.finder"}):
            state = desktop.desktop_state(service, now=101.0)
        self.assertEqual((state["call_app"], state["audio_playing"], state["known"]), (False, False, True))
        self.assertTrue(desktop.quiet_alone(True, state))
        self.assertFalse(desktop.quiet_alone(False, state))
        service.archive.system_dbfs = -20.0                                  # music or a call partner from the speakers
        with patch.object(desktop, "foreground_app", return_value={"name": "Finder", "bundle_id": "com.apple.finder"}):
            self.assertFalse(desktop.quiet_alone(True, desktop.desktop_state(service, now=101.0)))
        service.archive.system_dbfs = -70.0
        with patch.object(desktop, "foreground_app", return_value={"name": "zoom.us", "bundle_id": "us.zoom.xos"}):
            self.assertFalse(desktop.quiet_alone(True, desktop.desktop_state(service, now=101.0)))
        with patch.object(desktop, "foreground_app", return_value={}):       # probe unavailable: ordinary rule
            state = desktop.desktop_state(service, now=101.0)
        self.assertFalse(state["known"]); self.assertFalse(desktop.quiet_alone(True, state))
        with patch.object(desktop, "foreground_app", return_value={"name": "Finder", "bundle_id": "com.apple.finder"}):
            state = desktop.desktop_state(service, now=200.0)                 # stale audio level
        self.assertIsNone(state["audio_playing"]); self.assertFalse(desktop.quiet_alone(True, state))


if __name__ == "__main__":
    unittest.main()

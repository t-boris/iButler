"""The socket handler: failures are logged without their message text, and a vanished client is not a crash."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from butler_ambient.service import Handler


class Writer(io.BytesIO):
    """A reply stream whose peer may already be gone."""

    def __init__(self, gone=False):
        super().__init__()
        self.gone = gone

    def write(self, data):
        if self.gone:
            raise BrokenPipeError(32, "Broken pipe")
        return super().write(data)


def handler(command, control, writer):
    instance = Handler.__new__(Handler)
    instance.connection = Mock()
    instance.rfile = io.BytesIO(json.dumps({"command": command, "args": {}}).encode() + b"\n")
    instance.wfile = writer
    instance.server = SimpleNamespace(service=SimpleNamespace(control=control))
    return instance


class HandlerErrorTest(unittest.TestCase):
    def test_failure_is_logged_without_the_message_and_answered_generically(self):
        secret = "owner said something private"

        def control(command, args):
            raise ValueError(secret)

        writer = Writer()
        with self.assertLogs("butler.ambient", level="WARNING") as logged:
            handler("probe-command", control, writer).handle()
        text = "\n".join(logged.output)
        self.assertIn("probe-command", text)
        self.assertIn("ValueError", text)
        self.assertNotIn(secret, text)
        reply = json.loads(writer.getvalue())
        self.assertEqual(list(reply), ["error"])
        self.assertNotIn(secret, writer.getvalue().decode())

    def test_client_gone_before_the_reply_does_not_raise(self):
        writer = Writer(gone=True)
        with self.assertLogs("butler.ambient", level="INFO") as logged:
            handler("probe-command", lambda command, args: {"ok": True}, writer).handle()
        self.assertTrue(any("client closed before the probe-command reply" in line for line in logged.output))

    def test_client_gone_while_the_error_is_sent_does_not_raise(self):
        def control(command, args):
            raise RuntimeError("boom")

        writer = Writer(gone=True)
        with self.assertLogs("butler.ambient", level="INFO") as logged:
            handler("probe-command", control, writer).handle()
        self.assertTrue(any("failed: RuntimeError" in line for line in logged.output))
        self.assertTrue(any("client closed" in line for line in logged.output))

    def test_unreadable_request_is_answered_and_logged_without_a_command_name(self):
        instance = Handler.__new__(Handler)
        instance.connection = Mock()
        instance.rfile = io.BytesIO(b"not json\n")
        writer = Writer()
        instance.wfile = writer
        instance.server = SimpleNamespace(service=SimpleNamespace())
        with self.assertLogs("butler.ambient", level="WARNING") as logged:
            instance.handle()
        self.assertTrue(any("request None failed" in line for line in logged.output))
        self.assertEqual(list(json.loads(writer.getvalue())), ["error"])


if __name__ == "__main__":
    unittest.main()

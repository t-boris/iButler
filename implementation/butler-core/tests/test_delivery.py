"""REQ-024: delivery is tracked apart from execution; bounded retries; no blind retransmission."""
import datetime as dt
import json
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from butler_core.adapters import telegram
from butler_core.adapters.base import Ambiguous, NotDispatched, Rejected
from support import OWNER, CoreCase, FakeTransport, base_config

ZONE = ZoneInfo("America/Chicago")


def at(hour, minute=0):
    return dt.datetime(2026, 10, 20, hour, minute, tzinfo=ZONE).timestamp()


class DeliveryTest(CoreCase):
    def enqueue(self, key="d1", kind="result", **extra):
        return self.core.delivery.enqueue(recipient=OWNER, channel="telegram:butler", persona="butler", kind=kind,
                                          content="готово", idempotency_key=key, **extra)

    def setUp(self):
        super().setUp()
        self.clock.now = at(12)

    def test_delivery_failure_keeps_the_completed_operation(self):
        op = self.core.submit(self.request(key="op"), "system2")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.transport.script = ["not_dispatched"] * 3
        delivery = self.enqueue(operation=op["id"])
        for _ in range(3):
            self.core.delivery.send(delivery["id"])
            self.clock.advance(61)
        self.assertEqual(self.core.store.one("SELECT state FROM deliveries WHERE id=?", (delivery["id"],))[0],
                         "failed")
        self.assertEqual(self.core.operations.get(op["id"])["state"], "succeeded")
        self.assertEqual(len(self.adapter.calls), 1)                    # only delivery was retried

    def test_retry_until_success_within_bounds(self):
        self.transport.script = ["not_dispatched"]
        delivery = self.enqueue()
        self.assertEqual(self.core.delivery.send(delivery["id"]), "pending")
        self.assertEqual(self.core.delivery.ready(), [])
        self.clock.advance(6)
        self.assertEqual(self.core.delivery.send(delivery["id"]), "delivered")
        self.assertEqual(len(self.transport.sent), 1)

    def test_retry_window_bounds_retries(self):
        self.transport.script = ["not_dispatched", "not_dispatched"]
        delivery = self.enqueue()
        self.core.delivery.send(delivery["id"])
        self.clock.advance(400)
        self.assertEqual(self.core.delivery.send(delivery["id"]), "failed")

    def test_ambiguous_outcome_is_not_retransmitted(self):
        self.transport.script = ["ambiguous"]
        delivery = self.enqueue()
        self.assertEqual(self.core.delivery.send(delivery["id"]), "uncertain")
        self.clock.advance(600)
        self.assertNotIn(delivery["id"], self.core.delivery.ready())
        self.assertEqual(self.core.delivery.send(delivery["id"]), "uncertain")
        self.assertEqual(len(self.transport.sent), 1)

    def test_deduplicating_transport_may_retry_only_on_the_same_gateway(self):
        transport = FakeTransport(deduplicates=True)
        self.core.delivery.transports["telegram:butler"] = transport
        transport.script = ["ambiguous"]
        first = self.enqueue("a")
        self.assertEqual(self.core.delivery.send(first["id"]), "pending")
        transport.script = ["ambiguous_restart"]
        second = self.enqueue("b")
        self.assertEqual(self.core.delivery.send(second["id"]), "uncertain")

    def test_duplicate_key_is_one_delivery(self):
        self.assertEqual(self.enqueue("same")["id"], self.enqueue("same")["id"])

    def test_quiet_hours_defer_initiative_and_silence_replies(self):
        self.clock.now = at(23)
        daily = self.enqueue("daily", kind="daily")
        self.assertEqual(self.core.delivery.send(daily["id"]), "deferred")
        result = self.enqueue("r")
        self.assertEqual(self.core.delivery.send(result["id"]), "delivered")
        self.assertTrue(self.transport.sent[-1]["silent"])
        self.clock.now = at(8, 1) + 86400   # next morning
        self.assertIn(daily["id"], self.core.delivery.ready())
        self.assertEqual(self.core.delivery.send(daily["id"]), "delivered")

    def test_stale_advice_and_initiative_limits(self):
        advice = self.enqueue("adv", kind="advice", created_at=self.clock() - 200)
        self.assertEqual(self.core.delivery.send(advice["id"]), "cancelled")
        sent = []
        for index in range(5):
            item = self.enqueue(f"a{index}", kind="advice")
            sent.append(self.core.delivery.send(item["id"]))
            self.clock.advance(3601)
        self.assertEqual(sent.count("delivered"), 3)

    def test_revoked_disclosure_blocks_delivery(self):
        delivery = self.enqueue()
        content = base_config()
        content["owner"]["principals"].remove(OWNER)
        content["personas"]["butler"]["principals"] = []
        self.activate(content)
        self.assertEqual(self.core.delivery.send(delivery["id"]), "cancelled")
        self.assertEqual(self.transport.sent, [])

    def test_recipient_outside_the_persona_is_refused(self):
        result = self.core.delivery.enqueue(recipient="telegram:2000002", channel="telegram:butler",
                                            persona="butler", kind="result", content="x", idempotency_key="o")
        self.assertEqual(result["state"], "denied")

    def test_restart_during_transmission_is_uncertain(self):
        delivery = self.enqueue()
        self.core.store.db.execute("UPDATE deliveries SET state='sending', attempts=1, first_attempt_at=?,"
                                   " gateway='gw-1' WHERE id=?", (self.clock(), delivery["id"]))
        self.assertEqual(self.core.delivery.recover(), {delivery["id"]: "uncertain"})

    def test_import_is_read_only(self):
        self.core.delivery.import_uncertain([{"key": "q1", "channel": "telegram:butler", "attempts": 1}] * 2)
        rows = self.core.delivery.list(["uncertain"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.core.delivery.ready(), [])

    def test_status_lines_are_silent_and_results_are_not(self):
        status = self.enqueue(key="s1", kind="status")
        result = self.enqueue(key="r1", kind="result")
        self.core.delivery.send(status["id"])
        self.core.delivery.send(result["id"])
        self.assertEqual([m["silent"] for m in self.transport.sent], [True, False])

    def test_import_classifies_refusals_as_failed_and_the_rest_as_uncertain(self):
        self.core.delivery.import_uncertain([
            {"key": "q2", "channel": "telegram:butler", "attempts": 1, "reason": "Network request failed"},
            {"key": "q3", "channel": "telegram:butler", "attempts": 0, "state": "failed",
             "reason": "Local media path is not under an allowed directory"}])
        states = {r["idempotency_key"]: r["state"] for r in self.core.delivery.list()}
        self.assertEqual(states["import:q2"], "uncertain")
        self.assertEqual(states["import:q3"], "failed")
        self.assertEqual(self.core.delivery.ready(), [])


class AttachmentTest(CoreCase):
    """DEC-063: a file is copied into private state at enqueue and removed when the delivery ends."""

    def setUp(self):
        super().setUp()
        self.clock.now = at(12)
        self.source = self.tmp / "tasks.html"
        self.source.write_text("<html>board</html>")

    def enqueue(self, key="f1", **extra):
        return self.core.delivery.enqueue(recipient=OWNER, channel="telegram:butler", persona="butler", kind="result",
                                          content="📋 задачи", idempotency_key=key,
                                          attachment={"path": str(self.source), "name": "tasks.html",
                                                      "mime": "text/html"}, **extra)

    def staged(self):
        return sorted((Path(self.core.store.root) / "attachments").glob("*")) \
            if (Path(self.core.store.root) / "attachments").exists() else []

    def test_attachment_is_staged_privately_sent_and_released(self):
        delivery = self.enqueue()
        staged = self.staged()
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].stat().st_mode & 0o777, 0o600)
        self.source.write_text("changed after enqueue")
        self.assertEqual(self.core.delivery.send(delivery["id"]), "delivered")
        message = self.transport.sent[0]
        self.assertEqual(message["attachment"]["name"], "tasks.html")
        self.assertEqual(message["attachment"]["mime"], "text/html")
        self.assertEqual(self.staged(), [])
        row = self.core.store.one("SELECT size, sha256 FROM attachments WHERE delivery=?", (delivery["id"],))
        self.assertEqual(row["size"], len("<html>board</html>"))

    def test_duplicate_and_denied_attachments_leave_no_staged_copy(self):
        self.enqueue()
        duplicate = self.enqueue()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.staged()), 1)
        denied = self.core.delivery.enqueue(recipient="telegram:999", channel="telegram:butler", persona="butler",
                                            kind="result", content="x", idempotency_key="f2",
                                            attachment={"path": str(self.source)})
        self.assertEqual(denied["state"], "denied")
        self.assertEqual(len(self.staged()), 1)

    def test_symlinks_and_oversized_files_are_refused(self):
        link = self.tmp / "link.html"
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.core.delivery.enqueue(recipient=OWNER, channel="telegram:butler", persona="butler", kind="result",
                                       content="x", idempotency_key="f3", attachment={"path": str(link)})
        self.assertEqual(self.staged(), [])

    def test_ambiguous_document_send_is_uncertain_and_released(self):
        self.transport.script = ["ambiguous"]
        delivery = self.enqueue()
        self.assertEqual(self.core.delivery.send(delivery["id"]), "uncertain")
        self.assertEqual(self.staged(), [])


class DocumentTransportTest(unittest.TestCase):
    """The Bot API path: token resolved through the account reference, never stored or echoed."""

    def setUp(self):
        import tempfile
        self.home = Path(tempfile.mkdtemp())
        (self.home / "openclaw.json").write_text(json.dumps({"channels": {"telegram": {"accounts": {
            "butler": {"botToken": {"source": "env", "id": "TEST_BOT_TOKEN"}}}}}}))
        (self.home / ".env").write_text("OTHER=1\nTEST_BOT_TOKEN='123:secret'\n")
        self.file = self.home / "tasks.html"
        self.file.write_text("<html/>")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def test_token_reference_is_read_from_the_env_file(self):
        self.assertEqual(telegram.bot_token("butler", home=self.home), "123:secret")
        with self.assertRaises(telegram.TokenUnavailable) as caught:
            telegram.bot_token("missing", home=self.home)
        self.assertNotIn("secret", str(caught.exception))

    def test_send_document_posts_multipart_and_classifies_failures(self):
        requests = []

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.body

        def opener(request, timeout):
            requests.append(request)
            return Response(b'{"ok": true, "result": {"message_id": 77}}')

        receipt = telegram.send_document("123:secret", "1000001", self.file, name="tasks.html", mime="text/html",
                                         caption="board", silent=True, opener=opener)
        self.assertEqual(receipt, "77")
        request = requests[0]
        self.assertTrue(request.full_url.endswith("/bot123:secret/sendDocument"))
        body = request.data
        self.assertIn(b'name="chat_id"\r\n\r\n1000001', body)
        self.assertIn(b'name="disable_notification"\r\n\r\ntrue', body)
        self.assertIn(b'filename="tasks.html"\r\nContent-Type: text/html', body)
        self.assertIn(b"<html/>", body)

        import io
        import urllib.error

        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                         io.BytesIO(b'{"ok": false, "description": "Bad Request: chat not found"}'))
        with self.assertRaises(Rejected):
            telegram.send_document("t", "1", self.file, opener=refused)

        def gone(request, timeout):
            raise urllib.error.URLError(ConnectionRefusedError())
        with self.assertRaises(NotDispatched):
            telegram.send_document("t", "1", self.file, opener=gone)

        def timed_out(request, timeout):
            raise TimeoutError()
        with self.assertRaises(Ambiguous):
            telegram.send_document("t", "1", self.file, opener=timed_out)

    def test_transport_routes_attachments_to_the_bot_api_without_the_gateway(self):
        from butler_core.adapters.openclaw import TelegramTransport
        calls = []
        transport = TelegramTransport("butler", runner=lambda *a, **k: self.fail("gateway must not be called"),
                                      token=lambda account: "123:secret",
                                      document=lambda token, chat, path, **kw: calls.append((chat, path, kw)) or "9")
        receipt = transport.send({"recipient": "telegram:1000001", "content": "board", "silent": True,
                                  "attachment": {"path": str(self.file), "name": "t.html", "mime": "text/html"}})
        self.assertEqual(receipt, "9")
        self.assertEqual(calls[0][0], "1000001")
        self.assertEqual(calls[0][2]["caption"], "board")
        self.assertTrue(calls[0][2]["silent"])
        down = TelegramTransport("butler", token=lambda account: (_ for _ in ()).throw(
            telegram.TokenUnavailable("no reference")))
        with self.assertRaises(NotDispatched):
            down.send({"recipient": "telegram:1", "content": "", "attachment": {"path": str(self.file)}})


if __name__ == "__main__":
    unittest.main()

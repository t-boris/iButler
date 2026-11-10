"""BUG-017: the tunnel policy lets exactly one route through, and the probe script judges answers correctly."""
import importlib.util
import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:                      # the policy is plain YAML; without PyYAML only the text checks below run
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_PATHS = {"/plugins/webhooks/tetyana-dyck-ci"}


def load_probe():
    spec = importlib.util.spec_from_file_location("verify_exposure", ROOT / "verify_exposure.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "ngrok-policy.yml").read_text()

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_one_rule_answers_404_to_everything_but_the_allowed_routes(self):
        policy = yaml.safe_load(self.text)
        rules = policy["on_http_request"]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual([a["type"] for a in rule["actions"]], ["custom-response"])
        self.assertEqual(rule["actions"][0]["config"]["status_code"], 404)
        expression = " ".join(rule["expressions"])
        self.assertTrue(expression.startswith("!("), "the rule must fire for everything EXCEPT the allowed request")
        paths = set(re.findall(r"req\.url\.path == '([^']+)'", expression))
        self.assertEqual(paths, ALLOWED_PATHS)
        self.assertIn("req.method == 'POST'", expression)

    def test_no_prefix_match_that_would_open_a_whole_subtree(self):
        self.assertNotIn("startsWith", self.text)
        self.assertNotIn("matches(", self.text)

    def test_the_edge_answer_is_what_the_probe_recognises(self):
        policy = self.text
        self.assertIn('content: "Not found"', policy)
        self.assertIn("status_code: 404", policy)


class ProbeJudgementTest(unittest.TestCase):
    def setUp(self):
        self.probe = load_probe()

    def test_the_edge_404_counts_as_blocked(self):
        self.assertTrue(self.probe.judge("blocked", 404, b"Not found"))

    def test_a_gateway_answer_is_not_blocked(self):
        self.assertFalse(self.probe.judge("blocked", 200, b"<html>"))
        self.assertFalse(self.probe.judge("blocked", 401, b'{"error":"unauthorized"}'))

    def test_the_webhook_must_reach_the_gateway_and_be_refused_there(self):
        self.assertTrue(self.probe.judge("reaches-gateway", 401, b'{"error":"unauthorized"}'))
        self.assertFalse(self.probe.judge("reaches-gateway", 404, b"Not found"))     # still blocked at the edge
        self.assertFalse(self.probe.judge("reaches-gateway", 200, b"ok"))            # accepted without a secret

    def test_every_check_targets_a_known_path(self):
        paths = {path for _, path, _ in self.probe.CHECKS}
        self.assertTrue(ALLOWED_PATHS <= paths)
        self.assertEqual([c for c in self.probe.CHECKS if c[2] == "reaches-gateway"],
                         [("POST", next(iter(ALLOWED_PATHS)), "reaches-gateway")])


if __name__ == "__main__":
    unittest.main()

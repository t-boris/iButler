#!/usr/bin/env python3
"""Probe what the public tunnel of the OpenClaw gateway lets through (BUG-017).

Only unauthenticated requests are sent, and no request can change anything: the one POST goes to
the webhook path WITHOUT its secret, so the gateway answers 401/403 and does nothing.

    verify_exposure.py https://<tunnel-domain>

Exit code 0 when only the webhook path reaches the gateway, 1 otherwise.
"""
import subprocess
import sys

WEBHOOK = "/plugins/webhooks/tetyana-dyck-ci"
# (method, path, what must hold)
CHECKS = [
    ("GET", "/", "blocked"),
    ("GET", "/health", "blocked"),
    ("GET", "/v1/models", "blocked"),
    ("POST", "/v1/chat/completions", "blocked"),
    ("GET", "/plugins/webhooks/other-route", "blocked"),
    ("GET", WEBHOOK, "blocked"),                 # only POST is allowed through
    ("POST", WEBHOOK, "reaches-gateway"),        # no secret: the gateway itself must refuse it
]


def probe(base, method, path, timeout=20):
    """One request through curl: it uses the macOS trust store, which the python.org Python does not."""
    command = ["/usr/bin/curl", "-s", "-m", str(timeout), "-X", method, "-H", "content-type: application/json",
               "-A", "exposure-check", "-o", "-", "-w", "\n%{http_code}"]
    if method == "POST":
        command += ["--data", "{}"]
    done = subprocess.run(command + [base + path], capture_output=True, timeout=timeout + 5)
    if done.returncode != 0:
        raise SystemExit(f"curl failed ({done.returncode}) for {method} {path}: {done.stderr.decode()[:120]}")
    body, _, status = done.stdout.rpartition(b"\n")
    return int(status), body[:200]


def judge(expectation, status, body):
    """'blocked' = answered by the edge (404 "Not found"); 'reaches-gateway' = answered by the gateway."""
    edge = status == 404 and body.strip() == b"Not found"
    if expectation == "blocked":
        return edge
    return not edge and status in (400, 401, 403, 404, 405, 415)   # refused by the gateway, never accepted


def main(argv):
    if len(argv) != 2 or not argv[1].startswith("https://"):
        print(__doc__)
        return 2
    base = argv[1].rstrip("/")
    failed = 0
    for method, path, expectation in CHECKS:
        status, body = probe(base, method, path)
        ok = judge(expectation, status, body)
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {method:4} {path:36} -> {status} (want {expectation})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

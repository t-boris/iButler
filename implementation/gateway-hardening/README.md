# Gateway hardening (BUG-017)

What the OpenClaw gateway exposes to the internet, and the settings that keep it that way.

## Exposure model

- The gateway listens on `127.0.0.1:18789` (`gateway.bind = loopback`, `gateway.auth.mode = token`).
- Launch agent `ai.openclaw.ngrok` publishes that port on a reserved ngrok domain, for one purpose: the CI deploy
  callback `POST /plugins/webhooks/tetyana-dyck-ci` (shared-secret route of the `webhooks` plugin).
- `ngrok-policy.yml` (ngrok Traffic Policy) runs at ngrok's edge and answers `404 Not found` to everything except
  that exact method and path. The Control UI page, `/health`, `/v1/*` and the websocket are not reachable from the
  internet; they still work on loopback.

Before the policy (measured 2026-11-10, no token): `GET /` 200 (Control UI page), `GET /health` 200, `GET /v1/models` 401.

## Check it

```
python3 verify_exposure.py https://<tunnel-domain>      # exit 0 = only the webhook reaches the gateway
python3 -m unittest discover -s tests
```

The only POST it sends goes to the webhook path without its secret, so the gateway refuses it (401) and nothing runs.

## Install (already done on this Mac, 2026-11-10)

1. `install -m 600 ngrok-policy.yml ~/.openclaw/ngrok-policy.yml`
2. Add `--traffic-policy-file /Users/boris/.openclaw/ngrok-policy.yml` to `ProgramArguments` of
   `~/Library/LaunchAgents/ai.openclaw.ngrok.plist` (`plutil -insert`).
3. `launchctl bootout gui/$(id -u)/ai.openclaw.ngrok && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.ngrok.plist`
4. Run `verify_exposure.py`.

Rollback: restore the plist from `ai.openclaw.ngrok.plist.bak-bug017-2026-11-10` (same directory) and repeat step 3.
A new webhook route needs its exact path in the policy expression and in `tests/test_policy.py`.

## Gateway settings (`~/.openclaw/openclaw.json`, changed 2026-11-10, backup `openclaw.json.bak-bug017-2026-11-10`)

- `gateway.controlUi.allowInsecureAuth = false`. OpenClaw documents it as a localhost compatibility toggle for plain-HTTP
  Control UI sessions without device identity. Loopback (`http://127.0.0.1:18789/`) is a secure context for browsers, so
  nothing is lost; opening the UI through a LAN IP over HTTP no longer works.
- `plugins.allow` pins exactly: `acpx, anthropic, bonjour, browser, butler-core-bridge, canvas, codex, device-pair,
  file-transfer, openai, perplexity, phone-control, talk-voice, telegram, webhooks, whatsapp`. These are the 15 plugins
  the gateway loads at start plus `openai` (owner of the `openai` and `openai-codex` providers, enabled in the config).
  The "plugins.allow is empty" warning no longer appears. A new plugin must be added to this list, otherwise it will not load.

Apply or change with `openclaw config patch --file <patch.json5> --dry-run`, then without `--dry-run`; both keys need a
gateway restart (do it when `~/.openclaw/cron/jobs-state.json` shows no job with `runningAtMs`).

## Accepted risk: `plugins.entries.acpx.config.permissionMode = approve-all`

Decision of the owner, 2026-11-10: kept. ACP agents (Codex workers started by Butler) run non-interactively
(`nonInteractivePermissions = deny` means a prompt would fail the task), so every tool call they need is pre-approved.
Limits that make it acceptable: the gateway is on loopback, the only public route is the secret-protected CI webhook, and
workers start only from the owner's own channels. Revisit if a public route that can start an agent is ever added.

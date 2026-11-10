// BUG-025: the bridge belongs to the gateway. Every short-lived `openclaw` command loads plugins too, and there the
// bridge said hello to the core, was refused ("bridge.hello is not available to local callers") or timed out, and logged
// a warning that looked like an outage.
import assert from "node:assert/strict";
import { test } from "node:test";
import { isGatewayProcess } from "../lib/process.js";

const node = "/Users/boris/.nvm/versions/node/v22.22.3/bin/node";
const entry = "/opt/homebrew/lib/node_modules/openclaw/dist/index.js";

test("the launch agent's command line is the gateway", () => {
  assert.equal(isGatewayProcess([node, entry, "gateway", "--port", "18789"]), true);
  assert.equal(isGatewayProcess([node, entry, "gateway"]), true);
  assert.equal(isGatewayProcess([node, entry, "gateway", "run"]), true);
  assert.equal(isGatewayProcess([node, entry, "gateway", "run", "--port", "18789"]), true);
});

test("gateway management commands and every other command are short-lived CLI processes", () => {
  for (const sub of ["status", "start", "stop", "restart", "install", "uninstall", "health", "call", "probe", "discover"]) {
    assert.equal(isGatewayProcess([node, entry, "gateway", sub]), false, sub);
  }
  for (const words of [["message", "send", "--channel", "telegram"], ["cron", "list", "--json"], ["models", "status"],
                       ["agent", "--session-key", "x"], ["channels", "status"], ["config", "get", "plugins"]]) {
    assert.equal(isGatewayProcess([node, entry, ...words]), false, words.join(" "));
  }
});

test("anything unexpected is not the gateway", () => {
  assert.equal(isGatewayProcess([]), false);
  assert.equal(isGatewayProcess(undefined), false);
  assert.equal(isGatewayProcess([node]), false);
  assert.equal(isGatewayProcess([node, entry]), false);
  assert.equal(isGatewayProcess([node, entry, "Gateway"]), false);
});

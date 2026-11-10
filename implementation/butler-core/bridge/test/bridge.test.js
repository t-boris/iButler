import assert from "node:assert/strict";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { createBridge } from "../lib/bridge.js";
import { CoreUnavailable, callCore } from "../lib/core.js";
import { setReaction } from "../lib/telegram.js";

const CONFIG = { accounts: { butler: { receipt: "👌" }, "tetyana-dyck": { receipt: "👌" } }, timeout_ms: 1200 };
const SESSION = "agent:butler:telegram:direct:1000001";

function fixture(admit = () => ({ admitted: true, duplicate: false, task: "tk-1", receipt: "👌" })) {
  const calls = [];
  const reactions = [];
  let clock = 1_790_000_000_000;
  const call = async (method, params) => {
    calls.push({ method, params });
    if (method === "bridge.hello") return { ok: true, config: CONFIG };
    if (method === "bridge.config") return CONFIG;
    if (method === "bridge.admit") return admit(params);
    return { ok: true };
  };
  const react = async (args) => {
    reactions.push(args);
    return { ok: true };
  };
  const bridge = createBridge({ call, react, instance: { id: "1@1", startedAt: 1 }, version: "0.1.0", now: () => clock });
  return { bridge, calls, reactions, advance: (ms) => (clock += ms) };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const ctx = (extra = {}) => ({ channelId: "telegram", accountId: "butler", conversationId: "1000001", sessionKey: SESSION, senderId: "1000001", ...extra });

test("admits before dispatch and reacts only after the core confirmed", async () => {
  const { bridge, calls, reactions } = fixture();
  bridge.messageReceived({ messageId: "55", sessionKey: SESSION }, ctx({ conversationId: "telegram:1000001", messageId: "55" }));
  const result = await bridge.beforeDispatch({ channel: "telegram", content: "Привет", sessionKey: SESSION, senderId: "1000001", timestamp: 1_789_999_999 }, ctx());
  assert.equal(result, undefined);
  const admit = calls.find((c) => c.method === "bridge.admit");
  assert.equal(admit.params.message_id, "55");
  assert.equal(admit.params.chat, "1000001");
  assert.equal(admit.params.text, "Привет");
  assert.equal(calls[0].method, "bridge.hello");
  await flush();
  assert.deepEqual(reactions, [{ account: "butler", chatId: "1000001", messageId: "55", emoji: "👌" }]);
  await flush();
  assert.ok(calls.some((c) => c.method === "bridge.metric" && c.params.name === "receipt.text"));
});

test("no receipt for a duplicate or when the core is unreachable", async () => {
  const duplicate = fixture(() => ({ admitted: true, duplicate: true, task: "tk-1" }));
  duplicate.bridge.messageReceived({ messageId: "9" }, ctx());
  await duplicate.bridge.beforeDispatch({ channel: "telegram", content: "x", sessionKey: SESSION }, ctx());
  await flush();
  assert.equal(duplicate.reactions.length, 0);

  const down = createBridge({
    call: async () => {
      throw new CoreUnavailable("ENOENT");
    },
    react: async () => assert.fail("must not react"),
    instance: { id: "1@1", startedAt: 1 },
    version: "0.1.0",
  });
  const result = await down.beforeDispatch({ channel: "telegram", content: "x", sessionKey: SESSION }, ctx());
  assert.equal(result, undefined);
  assert.ok(down.stats.unreachable >= 1);
});

test("unmanaged accounts and other channels pass untouched", async () => {
  const { bridge, calls } = fixture();
  assert.equal(await bridge.beforeDispatch({ channel: "whatsapp", content: "x" }, { channelId: "whatsapp" }), undefined);
  assert.equal(await bridge.beforeDispatch({ channel: "telegram", content: "x", sessionKey: "s" }, ctx({ accountId: "bamberg-news" })), undefined);
  assert.equal(calls.filter((c) => c.method === "bridge.admit").length, 0);
});

test("rule commands and storage rejection are answered by the bridge", async () => {
  const command = fixture(() => ({ admitted: true, handled: true, text: "Последние задачи: …", receipt: "👌" }));
  command.bridge.messageReceived({ messageId: "1" }, ctx());
  assert.deepEqual(await command.bridge.beforeDispatch({ channel: "telegram", content: "/tasks", sessionKey: SESSION }, ctx()),
    { handled: true, text: "Последние задачи: …" });
  const full = fixture(() => ({ admitted: false, rejected: "storage", text: "хранилище заполнено" }));
  const result = await full.bridge.beforeDispatch({ channel: "telegram", content: "x", sessionKey: SESSION }, ctx());
  assert.deepEqual(result, { handled: true, text: "хранилище заполнено" });
  await flush();
  assert.equal(full.reactions.length, 0);
});

test("lifecycle is reported in order only for admitted sessions", async () => {
  const { bridge, calls } = fixture();
  bridge.agentEvent({ stream: "lifecycle", runId: "cron-run", sessionKey: "agent:butler:cron:x", data: { phase: "start" } });
  bridge.messageReceived({ messageId: "2" }, ctx());
  await bridge.beforeDispatch({ channel: "telegram", content: "x", sessionKey: SESSION }, ctx());
  bridge.agentEvent({ stream: "lifecycle", runId: "r1", sessionKey: SESSION, data: { phase: "start" } });
  bridge.agentEvent({ stream: "assistant", runId: "r1", sessionKey: SESSION, data: {} });
  bridge.agentEvent({ stream: "lifecycle", runId: "r1", sessionKey: SESSION, data: { phase: "error", error: "timeout after 600s" } });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const lifecycle = calls.filter((c) => c.method.startsWith("bridge.turn"));
  assert.deepEqual(lifecycle.map((c) => [c.method, c.params.run_id]), [["bridge.turn_started", "r1"], ["bridge.turn_ended", "r1"]]);
  assert.equal(lifecycle[1].params.outcome, "error");
});

test("sent replies of managed accounts are recorded", async () => {
  const { bridge, calls } = fixture();
  await bridge.refresh();
  bridge.messageSent({ to: "1000001", success: true, messageId: "900" }, { channelId: "telegram", accountId: "butler" });
  bridge.messageSent({ to: "1000001", success: false }, { channelId: "telegram", accountId: "butler" });
  await flush();
  const replies = calls.filter((c) => c.method === "bridge.reply");
  assert.deepEqual(replies.map((c) => c.params), [{ account: "butler", chat: "1000001", message_id: "900" }]);
});

test("core client speaks JSON lines over the socket and times out", async () => {
  const dir = fs.mkdtempSync(path.join("/tmp", "bcb-"));
  const socketPath = path.join(dir, "core.sock");
  const server = net.createServer((connection) => {
    connection.on("data", (data) => {
      const request = JSON.parse(String(data));
      if (request.method === "slow") return;
      connection.write(`${JSON.stringify({ id: request.id, ok: request.method === "ping", result: { pong: true }, error: { code: "forbidden", message: "no" } })}\n`);
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  try {
    assert.deepEqual(await callCore(socketPath, "ping", {}, 1000), { pong: true });
    await assert.rejects(callCore(socketPath, "approve", {}, 1000), /forbidden/);
    await assert.rejects(callCore(socketPath, "slow", {}, 100), CoreUnavailable);
    await assert.rejects(callCore(path.join(dir, "missing.sock"), "ping", {}, 500), CoreUnavailable);
  } finally {
    server.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("reaction request shape and failures", async () => {
  const requests = [];
  const ok = await setReaction({
    token: "123:abc",
    chatId: "1000001",
    messageId: "55",
    emoji: "👌",
    fetchImpl: async (url, init) => {
      requests.push({ url, body: JSON.parse(init.body) });
      return { status: 200, json: async () => ({ ok: true }) };
    },
  });
  assert.deepEqual(ok, { ok: true });
  assert.match(requests[0].url, /\/setMessageReaction$/);
  assert.deepEqual(requests[0].body, { chat_id: "1000001", message_id: 55, reaction: [{ type: "emoji", emoji: "👌" }] });
  assert.equal((await setReaction({ token: "", chatId: "1", messageId: "1", emoji: "👌" })).reason, "no token");
  const failed = await setReaction({ token: "t", chatId: "1", messageId: "1", emoji: "👌", fetchImpl: async () => ({ status: 400, json: async () => ({ ok: false, description: "Bad Request: REACTION_INVALID" }) }) });
  assert.equal(failed.ok, false);
  const offline = await setReaction({ token: "t", chatId: "1", messageId: "1", emoji: "👌", fetchImpl: async () => {
    throw new TypeError("fetch failed");
  } });
  assert.equal(offline.reason, "network");
});

const argv = process.argv;

test("the plugin registers its hooks on the installed OpenClaw host and re-registers idempotently", async (t) => {
  const host = "/opt/homebrew/lib/node_modules/openclaw/package.json";
  if (!fs.existsSync(path.join(path.dirname(new URL(import.meta.url).pathname), "..", "node_modules", "openclaw"))) {
    t.skip("node_modules/openclaw link is created at deployment");
    return;
  }
  assert.equal(JSON.parse(fs.readFileSync(host, "utf8")).version, "2026.5.27");
  const plugin = (await import("../index.js")).default;
  assert.equal(plugin.id, "butler-core-bridge");
  t.after(() => (process.argv = argv));
  process.argv = [argv[0], "/opt/homebrew/lib/node_modules/openclaw/dist/index.js", "gateway", "--port", "18789"];
  const hooks = [];
  let subscriptions = 0;
  let unsubscribed = 0;
  const api = {
    pluginConfig: { socket: path.join(os.tmpdir(), "absent.sock"), timeoutMs: 100 },
    logger: { warn() {}, info() {} },
    runtime: {
      config: { current: () => ({}) },
      events: {
        onAgentEvent() {
          subscriptions += 1;
          return () => {
            unsubscribed += 1;
          };
        },
      },
    },
    on(name, handler, options) {
      hooks.push({ name, options });
    },
  };
  plugin.register(api);
  plugin.register(api);
  assert.deepEqual([...new Set(hooks.map((h) => h.name))].sort(), ["before_dispatch", "message_received", "message_sent"]);
  assert.equal(subscriptions, 2);
  assert.equal(unsubscribed, 1);
  clearInterval(globalThis[Symbol.for("butler-core-bridge")].timer);
});

test("in a short-lived CLI process the plugin stays dormant: no hooks, no subscription, no hello, no timer", async (t) => {
  if (!fs.existsSync(path.join(path.dirname(new URL(import.meta.url).pathname), "..", "node_modules", "openclaw"))) {
    t.skip("node_modules/openclaw link is created at deployment");
    return;
  }
  const plugin = (await import("../index.js")).default;
  t.after(() => (process.argv = argv));
  const shared = Symbol.for("butler-core-bridge");
  delete globalThis[shared];
  for (const words of [["message", "send", "--channel", "telegram"], ["cron", "list"], ["gateway", "status"]]) {
    process.argv = [argv[0], "/opt/homebrew/lib/node_modules/openclaw/dist/index.js", ...words];
    const touched = [];
    const api = {
      pluginConfig: { socket: path.join(os.tmpdir(), "absent.sock"), timeoutMs: 100 },
      logger: { warn: (m) => touched.push(m), info: (m) => touched.push(m), error: (m) => touched.push(m) },
      runtime: { config: { current: () => ({}) }, events: { onAgentEvent: () => touched.push("subscribed") } },
      on: (name) => touched.push(name),
    };
    plugin.register(api);
    assert.deepEqual(touched, [], words.join(" "));
    assert.equal(globalThis[shared], undefined, "no shared state, no refresh timer");
  }
});

function logging(call, now = () => Date.now()) {
  const lines = [];
  const bridge = createBridge({
    now,
    call,
    react: async () => ({ ok: true }),
    instance: { id: "1@1", startedAt: 1 },
    version: "0.1.0",
    log: (level, message) => lines.push(`${level}: ${message}`),
  });
  return { bridge, lines };
}

test("a core outage is logged once when it begins and once when it ends, not on every call", async () => {
  let up = false;
  const { bridge, lines } = logging(async (method) => {
    if (!up) throw new CoreUnavailable("core did not answer within 1200 ms");
    return method === "bridge.hello" ? { ok: true, config: CONFIG } : CONFIG;
  });
  for (let i = 0; i < 5; i += 1) {
    await bridge.beforeDispatch({ channel: "telegram", content: "x", sessionKey: SESSION }, ctx());
  }
  assert.equal(lines.filter((l) => l.startsWith("warn")).length, 1, lines.join("\n"));
  assert.match(lines[0], /^warn: butler-core unavailable/);
  assert.ok(bridge.stats.unreachable >= 5, "every failed call is still counted");
  up = true;
  await bridge.beforeDispatch({ channel: "telegram", content: "x", sessionKey: SESSION }, ctx());
  assert.equal(lines.filter((l) => l.startsWith("info: butler-core reachable again")).length, 1, lines.join("\n"));
  assert.match(lines.at(-1), /failed calls/);
});

test("a second outage is logged again", async () => {
  let up = true;
  const { bridge, lines } = logging(async (method) => {
    if (!up) throw new CoreUnavailable("ENOENT");
    return method === "bridge.hello" ? { ok: true, config: CONFIG } : CONFIG;
  });
  await bridge.refresh();
  up = false;
  await bridge.refresh();
  await bridge.refresh();
  up = true;
  await bridge.refresh();
  up = false;
  await bridge.refresh();
  assert.equal(lines.filter((l) => l.startsWith("warn")).length, 2, lines.join("\n"));
});

test("a long outage is mentioned again every ten minutes so that it is not mistaken for a recovery", async () => {
  let clock = 1_790_000_000_000;
  const { bridge, lines } = logging(async () => {
    throw new CoreUnavailable("bridge.hello is not available to local callers");
  }, () => clock);
  await bridge.refresh();
  clock += 5 * 60 * 1000;
  await bridge.refresh();
  assert.equal(lines.length, 1, "nothing new after five minutes");
  clock += 5 * 60 * 1000;
  await bridge.refresh();
  assert.equal(lines.length, 2, lines.join("\n"));
  assert.match(lines[1], /still unavailable after 600 s \(3 failed calls/);
  clock += 60 * 1000;
  await bridge.refresh();
  assert.equal(lines.length, 2, "and then quiet again for ten minutes");
});

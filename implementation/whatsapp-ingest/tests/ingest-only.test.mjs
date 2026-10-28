import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, mkdir, copyFile, readFile, rm } from "node:fs/promises";
import { tmpdir, homedir } from "node:os";
import { existsSync } from "node:fs";
import { join } from "node:path";
import vm from "node:vm";
import { createInboxFeeder, guardIngestOnlySocket, toInboxRecord } from "../lib/ingest-only.mjs";

const INSTALLED_INBOX = join(homedir(), ".openclaw/workspace-butler/lib/inbox.py");
const INSTALLED_DIST = join(homedir(), ".openclaw/extensions/whatsapp/dist");
const TEST_PYTHON = process.env.BUTLER_TEST_PYTHON || (existsSync("/opt/miniconda3/bin/python3") ? "/opt/miniconda3/bin/python3" : "python3");

const message = (extra = {}) => ({ accessControlPassed: true, from: "+15550000001",
  id: "fixture-id", body: "Входящее сообщение", timestamp: Date.now(), chatType: "direct", ...extra });

test("only admitted inbound content is buffered; self messages cannot loop", () => {
  assert.equal(toInboxRecord(message({ accessControlPassed: false })), null);
  assert.equal(toInboxRecord(message({ accessControlPassed: undefined })), null);
  assert.equal(toInboxRecord(message({ fromMe: true })), null);
  assert.equal(toInboxRecord(message({ body: " " })), null);
});

test("group source and speaker remain separate, including media placeholders", () => {
  const record = toInboxRecord(message({ chatType: "group", chatId: "fixture@g.us",
    groupSubject: "Project", senderName: "Sender", body: "<media:image>" }));
  assert.equal(record.source_id, "fixture@g.us");
  assert.equal(record.sender, "Sender");
  assert.equal(record.text, "<media:image>");
  assert.equal(record.from_self, false);
});

test("socket blocks text, media, reactions, online, typing, and read receipts", async () => {
  const calls = [];
  const sock = guardIngestOnlySocket({
    sendMessage: async (...args) => calls.push(["send", ...args]),
    sendPresenceUpdate: async (...args) => calls.push(["presence", ...args]),
    readMessages: async (...args) => calls.push(["read", ...args]),
  });
  for (const content of [{ text: "error" }, { image: Buffer.alloc(1) }, { react: { text: "👍" } }]) {
    await assert.rejects(sock.sendMessage("fixture", content), /outbound messages are disabled/);
  }
  for (const presence of ["available", "composing", "recording", "paused"]) {
    await sock.sendPresenceUpdate(presence, "fixture");
  }
  await sock.readMessages([{ id: "fixture-id" }]);
  assert.deepEqual(calls, []);
  await sock.sendPresenceUpdate("unavailable");
  assert.deepEqual(calls, [["presence", "unavailable", undefined]]);
});

test("real inbox persists, deduplicates bursts, preserves literal shell syntax and local timestamps",
  { skip: !existsSync(INSTALLED_INBOX) && "private inbox library is not installed" }, async () => {
  const workspace = await mkdtemp(join(tmpdir(), "wa-inbox-test-"));
  try {
    await mkdir(join(workspace, "lib"));
    await copyFile(INSTALLED_INBOX, join(workspace, "lib/inbox.py"));
    const feed = createInboxFeeder({ workspace, python: TEST_PYTHON });
    const text = 'Привет "Борис"\n$(touch SHOULD_NOT_EXIST) `echo unsafe` & <tag>';
    const input = message({ body: text });
    const receipts = await Promise.all(Array.from({ length: 5 }, () => feed(input)));
    assert.equal(receipts.filter((r) => r.buffered).length, 1);
    const rows = (await readFile(join(workspace, "state/inbox/buffer.jsonl"), "utf8")).trim().split("\n").map(JSON.parse);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].text, text);
    assert.equal(rows[0].key, "whatsapp:+15550000001:fixture-id");
    assert.ok(Math.abs(new Date(rows[0].ts).getTime() - input.timestamp) < 1000);
    assert.equal(rows[0].processed, false);
    await assert.rejects(readFile(join(workspace, "SHOULD_NOT_EXIST")), { code: "ENOENT" });
    assert.deepEqual(await feed(message({ id: "old", timestamp: Date.now() - 4 * 86400000 })), { buffered: false });
  } finally { await rm(workspace, { recursive: true, force: true }); }
});

test("worker failures and timeouts reject locally without exposing message content", async () => {
  const feed = createInboxFeeder({ workspace: tmpdir(), python: "/missing/python" });
  await assert.rejects(feed(message()), /could not start inbox worker/);
  const workspace = await mkdtemp(join(tmpdir(), "wa-timeout-test-"));
  try {
    const { writeFile } = await import("node:fs/promises");
    const writer = join(workspace, "slow.py");
    await writeFile(writer, "import time\ntime.sleep(10)\n");
    await assert.rejects(createInboxFeeder({ workspace, writer, python: TEST_PYTHON, timeoutMs: 100 })(message()), /timed out/);
  } finally { await rm(workspace, { recursive: true, force: true }); }
});

test("installed inbound handler returns before route, reactions, typing, commands, or model dispatch",
  { skip: !existsSync(INSTALLED_DIST) && "private WhatsApp extension is not installed" }, async () => {
  const dist = INSTALLED_DIST;
  const { readdir } = await import("node:fs/promises");
  const files = (await readdir(dist)).filter((f) => /^monitor-.*\.js$/.test(f));
  let handler;
  for (const file of files) {
    const text = await readFile(join(dist, file), "utf8");
    if (!text.includes("function createWebOnMessageHandler(params)")) continue;
    handler = text.split("function createWebOnMessageHandler(params)")[1].split("//#endregion")[0];
  }
  assert.ok(handler, "installed monitor exists");
  let writes = 0;
  const context = vm.createContext({ bufferWhatsAppInboxMessage: async () => { writes++; } });
  const onMessage = vm.runInContext("(function createWebOnMessageHandler(params)" + handler + ")({})", context);
  await onMessage(message({ body: "/status" }));
  assert.equal(writes, 1); // Any legacy route reference would throw in this context.
  const failed = vm.createContext({ bufferWhatsAppInboxMessage: async () => { throw new Error("storage failure"); } });
  const failingHandler = vm.runInContext("(function createWebOnMessageHandler(params)" + handler + ")({})", failed);
  await assert.rejects(failingHandler(message()), /storage failure/);
});

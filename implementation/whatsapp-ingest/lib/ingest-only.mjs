import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const WORKSPACE = "/Users/boris/.openclaw/workspace-butler";
const PYTHON = "/opt/miniconda3/bin/python3";
const WRITER = fileURLToPath(new URL("./write-inbox.py", import.meta.url));

// No message text in argv, shell evaluation, model calls, or outbound actions.
export function toInboxRecord(msg) {
  if (msg.accessControlPassed !== true || msg.fromMe === true) return null;
  const source = msg.chatType === "group" ? msg.chatId : msg.from;
  if (typeof source !== "string" || !source.trim()) {
    throw new Error("WhatsApp ingest-only: missing source identifier");
  }
  const text = typeof msg.body === "string" ? msg.body : "";
  if (!text.trim()) return null;
  return {
    platform: "whatsapp",
    source_id: source,
    text,
    source_title: msg.groupSubject ?? msg.senderName ?? msg.pushName ?? "",
    sender: msg.senderName ?? msg.pushName ?? msg.senderE164 ?? msg.senderJid ?? source,
    timestamp_ms: msg.timestamp ?? null,
    msg_id: msg.id ?? null,
    from_self: false,
  };
}

export function createInboxFeeder({ workspace = WORKSPACE, python = PYTHON,
                                    writer = WRITER, timeoutMs = 15000 } = {}) {
  let tail = Promise.resolve();
  const write = (record) => new Promise((resolve, reject) => {
    const child = spawn(python, [writer, workspace], {
      cwd: workspace, stdio: ["pipe", "pipe", "ignore"], shell: false,
    });
    let output = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { output = (output + chunk).slice(-256); });
    child.on("error", () => {
      clearTimeout(timer);
      reject(new Error("WhatsApp ingest-only: could not start inbox worker"));
    });
    child.stdin.on("error", () => {}); // A failed worker may close stdin early.
    child.on("close", (code) => {
      clearTimeout(timer);
      if (timedOut) return reject(new Error("WhatsApp ingest-only: inbox worker timed out"));
      if (code !== 0) return reject(new Error(`WhatsApp ingest-only: inbox worker failed (${code})`));
      try { resolve(JSON.parse(output.trim())); }
      catch { reject(new Error("WhatsApp ingest-only: invalid inbox worker receipt")); }
    });
    child.stdin.end(JSON.stringify(record));
  });
  return (msg) => {
    const record = toInboxRecord(msg);
    if (!record) return Promise.resolve({ buffered: false });
    // Await persistence before the existing durable receive journal commits.
    // Rejections propagate locally; the handler never falls through to an agent.
    const task = tail.then(() => write(record));
    tail = task.catch(() => {});
    return task;
  };
}

export const bufferWhatsAppInboxMessage = createInboxFeeder();

export function guardIngestOnlySocket(sock) {
  sock.sendMessage = async () => {
    throw new Error("WhatsApp ingest-only: outbound messages are disabled");
  };
  const sendPresence = sock.sendPresenceUpdate.bind(sock);
  sock.sendPresenceUpdate = async (presence, jid) => {
    if (presence === "unavailable") return sendPresence(presence, jid);
  };
  sock.readMessages = async () => {}; // Do not change Boris's read state.
  return sock;
}

// Bridge logic, independent of the gateway so it can be tested with fakes.
//
// Admission: every managed Telegram message is recorded by the core before the
// agent sees it; the receipt reaction is set only after the core confirmed the
// write. If the core cannot be reached the message passes through unchanged.
// Lifecycle: agent runs of admitted sessions are reported so the core can show
// running, finished, failed or interrupted turns. Sent replies are recorded as
// observed deliveries.

const SEEN_TTL_MS = 10 * 60 * 1000;
const WATCH_TTL_MS = 3 * 60 * 60 * 1000;
const CONFIG_TTL_MS = 60 * 1000;

function milliseconds(value, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return fallback;
  return number < 1e12 ? number * 1000 : number;
}

export function createBridge({ call, react, instance, version, now = () => Date.now(), log = () => {}, timeoutMs = 1200 }) {
  const seen = new Map(); // sessionKey -> { messageId, at }
  const watched = new Map(); // sessionKey -> last activity
  const runs = new Map(); // runId -> sessionKey
  const queues = new Map(); // sessionKey -> promise chain (keeps lifecycle calls in order)
  let config = null;
  let configAt = 0;
  let greeted = false;
  let pending = null;
  const stats = { unreachable: 0, admitted: 0, receipts: 0, receiptFailures: 0 };

  function prune() {
    const at = now();
    for (const [key, value] of seen) if (at - value.at > SEEN_TTL_MS) seen.delete(key);
    for (const [key, value] of watched) if (at - value > WATCH_TTL_MS) watched.delete(key);
  }

  function refresh() {
    if (pending) return pending;
    const method = greeted ? "bridge.config" : "bridge.hello";
    const params = greeted ? {} : { instance: instance.id, version, host: "openclaw", started_at: instance.startedAt };
    pending = call(method, params, timeoutMs)
      .then((result) => {
        greeted = true;
        config = method === "bridge.hello" ? result.config : result;
        configAt = now();
        return config;
      })
      .catch((error) => {
        stats.unreachable += 1;
        log("warn", `butler-core unavailable (${error.message})`);
        return config;
      })
      .finally(() => {
        pending = null;
      });
    return pending;
  }

  async function currentConfig() {
    if (config && now() - configAt < CONFIG_TTL_MS) return config;
    return refresh();
  }

  function inOrder(session, fn) {
    const previous = queues.get(session) ?? Promise.resolve();
    const next = previous.then(fn).catch((error) => log("warn", `butler-core lifecycle call failed (${error.message})`));
    queues.set(session, next);
    next.finally(() => {
      if (queues.get(session) === next) queues.delete(session);
    });
    return next;
  }

  function messageReceived(event, ctx) {
    if ((ctx?.channelId ?? "") !== "telegram") return;
    const session = ctx.sessionKey ?? event?.sessionKey;
    const messageId = event?.messageId ?? ctx.messageId;
    if (!session || !messageId) return;
    seen.set(session, { messageId: String(messageId), at: now() });
    if (seen.size > 500) prune();
  }

  async function beforeDispatch(event, ctx) {
    const channel = event?.channel ?? ctx?.channelId;
    if (channel !== "telegram") return undefined;
    const rules = await currentConfig();
    const account = ctx?.accountId;
    const rule = rules?.accounts?.[account];
    if (!rule) return undefined;
    const session = event.sessionKey ?? ctx.sessionKey;
    const sent = milliseconds(event.timestamp, now());
    const identity = session ? seen.get(session) : undefined;
    const messageId = identity?.messageId ?? null;
    const chat = String(ctx.conversationId ?? "").replace(/^telegram:/, "");
    const params = {
      account,
      sender: String(event.senderId ?? ctx.senderId ?? ""),
      chat,
      message_id: messageId ?? `t${sent}`,
      text: typeof event.content === "string" ? event.content : String(event.body ?? ""),
      date: sent / 1000,
      session_key: session,
      is_group: Boolean(event.isGroup),
    };
    let result;
    try {
      result = await call("bridge.admit", params, rules.timeout_ms ?? timeoutMs);
    } catch (error) {
      stats.unreachable += 1;
      log("warn", `butler-core admission failed, message passes without receipt (${error.message})`);
      return undefined;
    }
    if (result?.admitted) {
      stats.admitted += 1;
      if (session) watched.set(session, now());
      if (!result.duplicate && messageId) {
        const emoji = result.receipt ?? rule.receipt ?? "👌";
        react({ account, chatId: chat, messageId, emoji })
          .then((outcome) => {
            if (outcome.ok) {
              stats.receipts += 1;
              return call("bridge.metric", { name: "receipt.text", value_ms: now() - sent, labels: { account } }, timeoutMs);
            }
            stats.receiptFailures += 1;
            log("warn", `receipt reaction failed (${outcome.reason})`);
            return undefined;
          })
          .catch((error) => log("warn", `receipt bookkeeping failed (${error.message})`));
      }
    }
    if (result?.handled || result?.rejected) return { handled: true, text: result.text };
    return undefined;
  }

  function agentEvent(event) {
    if (event?.stream !== "lifecycle" || !event.runId) return;
    const phase = event.data?.phase;
    if (phase === "start") {
      const session = event.sessionKey;
      if (!session || !watched.has(session)) return;
      runs.set(event.runId, session);
      watched.set(session, now());
      inOrder(session, () => call("bridge.turn_started", { session_key: session, run_id: event.runId }, timeoutMs));
      return;
    }
    if (phase !== "end" && phase !== "error") return;
    const session = runs.get(event.runId);
    if (!session) return;
    runs.delete(event.runId);
    const params = { session_key: session, run_id: event.runId, outcome: phase };
    if (phase === "error") params.error = String(event.data?.error ?? "error").slice(0, 80);
    inOrder(session, () => call("bridge.turn_ended", params, timeoutMs));
  }

  function messageSent(event, ctx) {
    if ((ctx?.channelId ?? "") !== "telegram" || !event?.success || !event.messageId) return;
    if (!config?.accounts?.[ctx.accountId]) return;
    call("bridge.reply", {
      account: ctx.accountId,
      chat: String(event.to ?? ctx.conversationId ?? "").replace(/^telegram:/, ""),
      message_id: String(event.messageId),
    }, timeoutMs).catch((error) => log("warn", `reply record failed (${error.message})`));
  }

  return { messageReceived, beforeDispatch, agentEvent, messageSent, refresh, stats };
}

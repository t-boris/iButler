// butler-core-bridge: the gateway side of the Butler core (DEC-045, DEC-051, DEC-056).
//
// The plugin stays thin: which accounts are managed, their persona and their
// receipt come from the active core configuration. It never decides policy
// and never re-runs an agent turn.
import os from "node:os";
import path from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { resolveTelegramAccount } from "openclaw/plugin-sdk/telegram-account";
import { createBridge } from "./lib/bridge.js";
import { callCore } from "./lib/core.js";
import { setReaction } from "./lib/telegram.js";

const VERSION = "0.1.0";
const SHARED = Symbol.for("butler-core-bridge");

function processInstance() {
  // Stable for the life of the gateway process and new after every restart, also
  // when the plugin is registered again by a configuration hot reload.
  const startedAt = Math.round(Date.now() / 1000 - process.uptime());
  return { id: `${process.pid}@${startedAt}`, startedAt };
}

export default definePluginEntry({
  id: "butler-core-bridge",
  name: "Butler Core Bridge",
  description: "Records managed Telegram messages in butler-core before dispatch and reports turn lifecycle.",
  register(api) {
    globalThis[SHARED] ??= { instance: processInstance() };
    const shared = globalThis[SHARED];
    shared.unsubscribe?.();
    if (shared.timer) clearInterval(shared.timer);

    const options = api.pluginConfig ?? {};
    const socketPath = options.socket ?? path.join(os.homedir(), ".local/share/butler-core/core.sock");
    const timeoutMs = Number(options.timeoutMs ?? 1200);
    const log = (level, message) => api.logger?.[level]?.(`[butler-core-bridge] ${message}`);
    const react = ({ account, chatId, messageId, emoji }) => {
      let token = "";
      try {
        token = resolveTelegramAccount({ cfg: api.runtime.config.current(), accountId: account })?.token ?? "";
      } catch {
        return Promise.resolve({ ok: false, reason: "token unavailable" });
      }
      return setReaction({ token, chatId, messageId, emoji });
    };
    const bridge = createBridge({
      call: (method, params, timeout) => callCore(socketPath, method, params, timeout),
      react,
      instance: shared.instance,
      version: VERSION,
      log,
      timeoutMs,
    });

    api.on("message_received", (event, ctx) => {
      bridge.messageReceived(event, ctx);
    }, { priority: 100 });
    api.on("before_dispatch", (event, ctx) => bridge.beforeDispatch(event, ctx), { priority: 100, timeoutMs: timeoutMs + 800 });
    api.on("message_sent", (event, ctx) => {
      bridge.messageSent(event, ctx);
    });
    shared.unsubscribe = api.runtime.events.onAgentEvent((event) => bridge.agentEvent(event));
    bridge.refresh();
    shared.timer = setInterval(() => bridge.refresh(), 60_000);
    shared.timer.unref?.();
  },
});

// Drives the real bridge logic against a running core socket (used by the Python wiring test).
import { createBridge } from "../lib/bridge.js";
import { callCore } from "../lib/core.js";

const [socketPath, sender] = process.argv.slice(2);
const reactions = [];
const bridge = createBridge({
  call: (method, params, timeout) => callCore(socketPath, method, params, timeout),
  react: async (args) => {
    reactions.push(args);
    return { ok: true };
  },
  instance: { id: "4242@1790000000", startedAt: 1790000000 },
  version: "0.1.0",
});
const session = `agent:butler:telegram:direct:${sender}`;
const ctx = { channelId: "telegram", accountId: "butler", conversationId: sender, sessionKey: session, senderId: sender };
bridge.messageReceived({ messageId: "501", sessionKey: session }, { ...ctx, messageId: "501" });
const first = await bridge.beforeDispatch({ channel: "telegram", content: "Сделай сводку", sessionKey: session, senderId: sender, timestamp: Date.now() }, ctx);
bridge.agentEvent({ stream: "lifecycle", runId: "run-e2e", sessionKey: session, data: { phase: "start" } });
await new Promise((resolve) => setTimeout(resolve, 100));
bridge.agentEvent({ stream: "lifecycle", runId: "run-e2e", sessionKey: session, data: { phase: "end" } });
bridge.messageSent({ to: sender, success: true, messageId: "502" }, { channelId: "telegram", accountId: "butler" });
bridge.messageReceived({ messageId: "503", sessionKey: session }, { ...ctx, messageId: "503" });
const command = await bridge.beforeDispatch({ channel: "telegram", content: "/tasks", sessionKey: session, senderId: sender, timestamp: Date.now() }, ctx);
await new Promise((resolve) => setTimeout(resolve, 300));
console.log(JSON.stringify({ first: first ?? null, command, reactions, stats: bridge.stats }));

// Receipt reaction through the Telegram Bot API. The gateway has no in-process
// reaction API, so the bridge calls setMessageReaction with the bot's token.
const API = "https://api.telegram.org";

export async function setReaction({ token, chatId, messageId, emoji, timeoutMs = 4000, fetchImpl = fetch }) {
  if (!token) return { ok: false, reason: "no token" };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(`${API}/bot${token}/setMessageReaction`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        message_id: Number(messageId),
        reaction: [{ type: "emoji", emoji }],
      }),
      signal: controller.signal,
    });
    const body = await response.json().catch(() => ({}));
    return body.ok === true ? { ok: true } : { ok: false, reason: String(body.description ?? response.status).slice(0, 80) };
  } catch (error) {
    return { ok: false, reason: error.name === "AbortError" ? "timeout" : "network" };
  } finally {
    clearTimeout(timer);
  }
}

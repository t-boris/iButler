// JSON-line client for the butler-core Unix socket. One request per connection.
import net from "node:net";

export class CoreUnavailable extends Error {}

export class CoreError extends Error {
  constructor(code, message) {
    super(`${code}: ${message}`);
    this.code = code;
  }
}

let sequence = 0;

export function callCore(socketPath, method, params, timeoutMs) {
  return new Promise((resolve, reject) => {
    const id = `b${(sequence += 1)}`;
    const socket = net.createConnection({ path: socketPath });
    let buffer = "";
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      if (error) reject(error);
      else resolve(value);
    };
    const timer = setTimeout(() => finish(new CoreUnavailable(`core did not answer within ${timeoutMs} ms`)), timeoutMs);
    socket.setEncoding("utf8");
    socket.on("connect", () => socket.write(`${JSON.stringify({ id, method, params })}\n`));
    socket.on("data", (chunk) => {
      buffer += chunk;
      const end = buffer.indexOf("\n");
      if (end < 0) return;
      let response;
      try {
        response = JSON.parse(buffer.slice(0, end));
      } catch {
        finish(new CoreError("bad_response", "unparseable core response"));
        return;
      }
      if (response.ok) finish(null, response.result);
      else finish(new CoreError(response.error?.code ?? "error", response.error?.message ?? ""));
    });
    socket.on("error", (error) => finish(new CoreUnavailable(error.code ?? error.message)));
    socket.on("close", () => finish(new CoreUnavailable("connection closed")));
  });
}

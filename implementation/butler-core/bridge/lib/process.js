// Which process is this? The bridge belongs to the gateway.
//
// OpenClaw loads every enabled plugin into every `openclaw` command, so the bridge also woke up inside short-lived CLI
// processes (a message send, a cron listing, `gateway status`). There the core refuses bridge.hello by design, or the
// call times out while the CLI is busy starting, and the bridge logged a warning that looked like an outage (BUG-025:
// 29 in two days, 18 of them 11-15 s after a CLI start).
//
// The gateway is `openclaw gateway [--port N]` (the launch agent) or `openclaw gateway run`; `gateway status`,
// `gateway restart` and the other management words are CLI processes.
export function isGatewayProcess(argv = process.argv) {
  if (!Array.isArray(argv)) return false;
  const [command, subcommand] = argv.slice(2);
  if (command !== "gateway") return false;
  return subcommand === undefined || subcommand === "run" || subcommand.startsWith("-");
}

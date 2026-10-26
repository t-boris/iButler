import Foundation
import Darwin

// A stable app identity for the core daemon, so macOS privacy controls can allow
// its backup writes to the external Storage0 disk. It only starts and supervises
// the Python daemon; it reads no data and holds no credentials.
let state = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/butler-core")
let runtime = Bundle.main.bundleURL.deletingLastPathComponent()
var signalHandlers: [DispatchSourceSignal] = []

try? FileManager.default.createDirectory(at: state.appendingPathComponent("logs"), withIntermediateDirectories: true,
                                         attributes: [.posixPermissions: 0o700])
let process = Process()
process.executableURL = runtime.appendingPathComponent(".venv/bin/python")
process.arguments = ["-m", "butler_core.cli", "daemon", "--state", state.path]
var env = ProcessInfo.processInfo.environment
env["PATH"] = "/Users/boris/.nvm/versions/node/v22.22.3/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
env["PYTHONDONTWRITEBYTECODE"] = "1"
env["PYTHONUNBUFFERED"] = "1"
process.environment = env
process.currentDirectoryURL = runtime
let logPath = state.appendingPathComponent("logs/core.log")
if !FileManager.default.fileExists(atPath: logPath.path) {
    FileManager.default.createFile(atPath: logPath.path, contents: nil, attributes: [.posixPermissions: 0o600])
}
guard let log = FileHandle(forWritingAtPath: logPath.path) else { exit(70) }
log.seekToEndOfFile()
process.standardOutput = log
process.standardError = log
process.terminationHandler = { ended in exit(ended.terminationStatus) }
do {
    try process.run()
} catch {
    exit(71)
}
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { if process.isRunning { process.terminate() } else { exit(0) } }
    source.resume()
    signalHandlers.append(source)
}
dispatchMain()

import AppKit
import AVFoundation
import CoreGraphics
import Foundation
import Darwin

// A stable macOS app identity owns camera/microphone permissions for its local
// Python/FFmpeg children. No credentials, media, or personal context leave it.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let state = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/butler-ambient")
let runtime = Bundle.main.bundleURL.deletingLastPathComponent()
let arguments = CommandLine.arguments
let permissionsOnly = arguments.contains("--request-permissions")
let enrollment = arguments.contains("--enroll")
var child: Process?
var signalHandlers: [DispatchSourceSignal] = []

func record(_ value: [String: Any], name: String) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]) {
        let path = state.appendingPathComponent(name)
        try? data.write(to: path, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
    }
}

func runChild() throws {
    let process = Process()
    process.executableURL = runtime.appendingPathComponent(".venv/bin/python")
    process.arguments = ["-m", "butler_ambient.cli", "--state", state.path,
                         "--config", state.appendingPathComponent("config.json").path]
        + (enrollment ? ["enroll-live", "owner", "Boris", "--prior-agreement"] : ["run"])
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = "/opt/homebrew/bin:/Users/boris/.openclaw/tools/node/npm/bin:/Users/boris/.nvm/versions/node/v22.22.3/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process.environment = env
    process.currentDirectoryURL = runtime
    let logPath = state.appendingPathComponent("logs/" + (enrollment ? "enrollment.log" : "service.log"))
    FileManager.default.createFile(atPath: logPath.path, contents: nil, attributes: [.posixPermissions: 0o600])
    let log = try FileHandle(forWritingTo: logPath)
    try log.seekToEnd()
    process.standardOutput = log
    process.standardError = log
    process.terminationHandler = { ended in
        record(["operation": enrollment ? "enrollment" : "service", "exit": ended.terminationStatus,
                "time": ISO8601DateFormatter().string(from: Date())], name: enrollment ? "enrollment-result.json" : "service-exit.json")
        exit(ended.terminationStatus)
    }
    child = process
    try process.run()
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { if process.isRunning { process.terminate() } else { exit(0) } }
        source.resume()
        signalHandlers.append(source)
    }
}

Task { @MainActor in
    if permissionsOnly || enrollment {
        let camera = AVCaptureDevice.authorizationStatus(for: .video) == .authorized
            ? true : await AVCaptureDevice.requestAccess(for: .video)
        let microphone = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
            ? true : await AVCaptureDevice.requestAccess(for: .audio)
        let screen = CGPreflightScreenCaptureAccess()
        record(["camera": camera, "microphone": microphone, "screen": screen], name: "permissions.json")
        if permissionsOnly { exit(camera && microphone ? 0 : 1) }
        if !camera || !microphone { exit(1) }
    }
    do { try runChild() }
    catch {
        record(["error": "local-runtime-launch-failed"], name: "launcher-error.json")
        exit(1)
    }
}
app.run()

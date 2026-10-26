import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation
import Darwin

// Ordinary audio I/O plus application-local AEC. VoiceProcessingIO attenuates
// every other app even at minimum ducking, so it must not be enabled here.
// stdout is mono PCM16/16kHz only. Control/health JSON goes to stderr, never audio.
final class EchoFilter {
    typealias InitFn = @convention(c) (Int32, Int32) -> UnsafeMutableRawPointer?
    typealias DestroyFn = @convention(c) (UnsafeMutableRawPointer) -> Void
    typealias CtlFn = @convention(c) (UnsafeMutableRawPointer, Int32, UnsafeMutableRawPointer) -> Int32
    typealias PlayFn = @convention(c) (UnsafeMutableRawPointer, UnsafePointer<Int16>) -> Void
    typealias CaptureFn = @convention(c) (UnsafeMutableRawPointer, UnsafePointer<Int16>, UnsafeMutablePointer<Int16>) -> Void
    let library: UnsafeMutableRawPointer
    let state: UnsafeMutableRawPointer
    let destroy: DestroyFn
    let play: PlayFn
    let capture: CaptureFn
    let lock = NSLock()
    var inputPending: [Int16] = []
    var referencePending: [Int16] = []
    var lastReference = 0.0
    let frame = 320

    init() throws {
        let sibling = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
            .deletingLastPathComponent().appendingPathComponent("libspeexdsp.1.dylib").path
        let candidates = [sibling, "/opt/homebrew/lib/libspeexdsp.1.dylib", "/usr/local/lib/libspeexdsp.1.dylib"]
        var loaded: UnsafeMutableRawPointer?
        for path in candidates {
            if let handle = dlopen(path, RTLD_NOW | RTLD_LOCAL) { loaded = handle; break }
        }
        guard let handle = loaded else {
            throw NSError(domain: "local-aec-library", code: 1)
        }
        func symbol<T>(_ name: String, _ type: T.Type) throws -> T {
            guard let pointer = dlsym(handle, name) else { throw NSError(domain: "local-aec-symbol", code: 2) }
            return unsafeBitCast(pointer, to: type)
        }
        let create = try symbol("speex_echo_state_init", InitFn.self)
        destroy = try symbol("speex_echo_state_destroy", DestroyFn.self)
        play = try symbol("speex_echo_playback", PlayFn.self)
        capture = try symbol("speex_echo_capture", CaptureFn.self)
        let ctl = try symbol("speex_echo_ctl", CtlFn.self)
        guard let value = create(320, 4800) else { throw NSError(domain: "local-aec-state", code: 3) }
        library = handle
        state = value
        var rate: Int32 = 16000
        guard ctl(state, 24, &rate) == 0 else { throw NSError(domain: "local-aec-rate", code: 4) }
    }

    deinit { destroy(state); dlclose(library) }

    func reference(_ samples: [Int16]) {
        lock.lock(); defer { lock.unlock() }
        referencePending += samples
        if samples.contains(where: { abs(Int($0)) > 32 }) { lastReference = ProcessInfo.processInfo.systemUptime }
        while referencePending.count >= frame {
            referencePending.withUnsafeBufferPointer { play(state, $0.baseAddress!) }
            referencePending.removeFirst(frame)
        }
    }

    func microphone(_ samples: [Int16]) -> [Int16] {
        lock.lock(); defer { lock.unlock() }
        inputPending += samples
        var result: [Int16] = []
        while inputPending.count >= frame {
            var output = [Int16](repeating: 0, count: frame)
            inputPending.withUnsafeBufferPointer { input in
                output.withUnsafeMutableBufferPointer { capture(state, input.baseAddress!, $0.baseAddress!) }
            }
            // Do not filter the owner's ordinary microphone signal. Apply
            // AEC only while our own playback/reference or its tail is present.
            if ProcessInfo.processInfo.systemUptime - lastReference < 0.4 { result += output }
            else { result += inputPending.prefix(frame) }
            inputPending.removeFirst(frame)
        }
        return result
    }
}

let arguments = CommandLine.arguments
// CoreAudio activity metadata only: no process audio tap, browser contents or
// meeting recordings. Input use, rather than an open tab, is the quiet signal.
func meetingActivity() -> [String: Any] {
    if #available(macOS 14.2, *) {
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else {
            return ["available": false, "reason": "process-list-unavailable"]
        }
        var processes = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        let code = processes.withUnsafeMutableBytes {
            AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, $0.baseAddress!)
        }
        guard code == noErr else { return ["available": false, "reason": "process-list-unavailable"] }
        var inputApps = Set<String>()
        var outputApps = Set<String>()
        var ownSourcesIgnored = 0
        var systemCaptureSourcesIgnored = 0
        var complete = true
        for process in processes {
            func number(_ selector: AudioObjectPropertySelector) -> UInt32? {
                var property = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
                var value: UInt32 = 0, bytes = UInt32(MemoryLayout<UInt32>.size)
                return AudioObjectGetPropertyData(process, &property, 0, nil, &bytes, &value) == noErr ? value : nil
            }
            guard let pid = number(kAudioProcessPropertyPID) else { complete = false; continue }
            if pid == UInt32(getpid()) || pid == UInt32(getppid()) { continue }
            var processPath = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
            let pathLength = processPath.withUnsafeMutableBytes { proc_pidpath(Int32(pid), $0.baseAddress, UInt32($0.count)) }
            if pathLength > 0 {
                let path = processPath.withUnsafeBufferPointer { String(cString: $0.baseAddress!) }
                let own = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".openclaw/tools/ambient-awareness/native")
                if ["butler-audio","butler-system-audio"].contains(where: { path == own.appendingPathComponent($0).path }) {
                    ownSourcesIgnored += 1; continue
                }
            }
            var property = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyBundleID, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            var rawBundle: Unmanaged<CFString>?
            var bytes = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
            guard AudioObjectGetPropertyData(process, &property, 0, nil, &bytes, &rawBundle) == noErr,
                  let bundle = rawBundle?.takeUnretainedValue() else { complete = false; continue }
            let name = (bundle as String).lowercased()
            if name.hasPrefix("com.boris.butler") { continue }
            // ScreenCaptureKit's OS broker can report running input even for
            // our system-playback archive. It is capture infrastructure, not a
            // conferencing microphone client. Real browser/call input remains
            // independently visible, including while a screen is shared.
            if name == "com.apple.replayd" { systemCaptureSourcesIgnored += 1; continue }
            guard let input = number(kAudioProcessPropertyIsRunningInput),
                  let output = number(kAudioProcessPropertyIsRunningOutput) else { complete = false; continue }
            // Only fixed public application labels leave this helper.
            let label: String
            if name.contains("chrome") { label = "Chrome / возможный Google Meet" }
            else if name.contains("safari") || name.contains("webkit") { label = "Safari / WebKit" }
            else if name.contains("firefox") { label = "Firefox" }
            else if name.contains("microsoft.edg") { label = "Microsoft Edge" }
            else if name.contains("thebrowser") || name.contains("chromium") { label = "Браузер с микрофоном" }
            else if name.contains("zoom") { label = "Zoom" }
            else if name.contains("teams") { label = "Microsoft Teams" }
            else if name.contains("facetime") { label = "FaceTime" }
            else if name.contains("slack") { label = "Slack" }
            else { label = "Другое приложение с микрофоном" }
            if input != 0 { inputApps.insert(label) }
            if output != 0 { outputApps.insert(label) }
        }
        return ["available": complete, "input_apps": inputApps.sorted(), "output_apps": outputApps.sorted(), "own_sources_ignored":ownSourcesIgnored,
                "system_capture_sources_ignored":systemCaptureSourcesIgnored,
                "source": "CoreAudio active process input/output; no content capture"]
    }
    return ["available": false, "reason": "process-activity-unsupported"]
}
if arguments.contains("--meeting-status") {
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: meetingActivity(), options: [.sortedKeys]))
    exit(0)
}
if arguments.contains("--camera-modes") {
    let types: [AVCaptureDevice.DeviceType]
    if #available(macOS 14, *) { types = [.external, .builtInWideAngleCamera, .continuityCamera] }
    else { types = [.externalUnknown, .builtInWideAngleCamera] }
    let devices = AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .video, position: .unspecified).devices
    let result = devices.map { device -> [String: Any] in
        var sizes = Set<String>()
        for format in device.formats where format.videoSupportedFrameRateRanges.contains(where: { $0.maxFrameRate >= 29.9 && $0.minFrameRate <= 30.1 }) {
            let size = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            sizes.insert("\(size.width)x\(size.height)")
        }
        return ["name": device.localizedName, "sizes_at_30fps": sizes.sorted(), "format_count": device.formats.count]
    }
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: result))
    exit(0)
}
if arguments.contains("--test-echo") {
    let ordinary = try EchoFilter()
    let speech: [Int16] = (0..<320).map { Int16(($0 * 37) % 8001 - 4000) }
    let rawUnchanged = ordinary.microphone(Array(speech.prefix(100))).isEmpty &&
        ordinary.microphone(Array(speech.dropFirst(100))) == speech
    let filter = try EchoFilter()
    var seed: UInt32 = 7
    var references = [[Int16]]()
    var before = 0.0, after = 0.0
    for index in 0..<400 {
        let reference: [Int16] = (0..<320).map { _ in
            seed = seed &* 1664525 &+ 1013904223
            return Int16(Int(seed >> 16) % 6001 - 3000)
        }
        references.append(reference)
        filter.reference(reference)
        let echo = index >= 2 ? references[index - 2].map { Int16(Double($0) * 0.6) } : [Int16](repeating: 0, count: 320)
        let output = filter.microphone(echo)
        if index > 300 {
            before += echo.reduce(0) { $0 + Double($1) * Double($1) }
            after += output.reduce(0) { $0 + Double($1) * Double($1) }
        }
    }
    let reduction = 10 * log10(before / max(after, 1))
    let routing = mappedInputDevice([2], [(11, 2), (22, 1)]) == 22 &&
        mappedInputDevice([0, 1], [(11, 2), (22, 1)]) == 11 &&
        mappedInputDevice([0, 2], [(11, 2), (22, 1)]) == nil &&
        mappedInputDevice([-1], [(11, 2), (22, 1)]) == nil &&
        mappedInputDevice([3], [(11, 2), (22, 1)]) == nil &&
        mappedInputDevice([], [(11, 2), (22, 1)]) == nil &&
        mappedInputDevice(nil, [(22, 1), (33, 0)]) == 22 &&
        mappedInputDevice([-1], [(22, 1), (33, 0)]) == nil &&
        mappedInputDevice(nil, [(11, 2), (22, 1)]) == nil
    let passed = reduction > 6 && rawUnchanged && routing
    let data = try JSONSerialization.data(withJSONObject: ["synthetic": true, "echo_reduction_db": reduction,
                                                          "ordinary_input_unchanged": rawUnchanged,
                                                          "input_route_mapping": routing,
                                                          "other_audio_ducking": "none", "passed": passed])
    FileHandle.standardOutput.write(data)
    exit(passed ? 0 : 1)
}
let name = AVCaptureDevice.default(for: .audio)?.localizedName ?? ""
if arguments.contains("--describe") {
    var details: [String: Any] = ["default_microphone": name]
    if let device = selectedInput(name) { details.merge(inputHealth(device)) { _, new in new } }
    let data = try JSONSerialization.data(withJSONObject: details)
    FileHandle.standardOutput.write(data)
    exit(0)
}
guard arguments.count == 3, !name.isEmpty else { exit(2) }

func deviceString(_ device: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var title = AudioObjectPropertyAddress(mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    let pointer = UnsafeMutablePointer<CFString?>.allocate(capacity: 1)
    pointer.initialize(to: nil)
    defer { pointer.deinitialize(count: 1); pointer.deallocate() }
    var size = UInt32(MemoryLayout<CFString?>.size)
    guard AudioObjectGetPropertyData(device, &title, 0, nil, &size, pointer) == noErr,
          let value = pointer.pointee else { return nil }
    return value as String
}

func deviceName(_ device: AudioDeviceID) -> String? { deviceString(device, kAudioObjectPropertyName) }

func defaultDevice(_ selector: AudioObjectPropertySelector) -> AudioDeviceID? {
    var property = AudioObjectPropertyAddress(mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var device: AudioDeviceID = 0
    var bytes = UInt32(MemoryLayout<AudioDeviceID>.size)
    return AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &property, 0, nil, &bytes, &device) == noErr && device != 0 ? device : nil
}

func channelCount(_ device: AudioDeviceID) -> Int? {
    var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(device, &address, 0, nil, &size) == noErr, size >= 4 else { return nil }
    let data = UnsafeMutableRawPointer.allocate(byteCount: max(Int(size), MemoryLayout<AudioBufferList>.size),
        alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { data.deallocate() }
    guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, data) == noErr else { return nil }
    let buffers = UnsafeMutableAudioBufferListPointer(data.assumingMemoryBound(to: AudioBufferList.self))
    return buffers.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func physicalDevice(_ uid: String) -> AudioDeviceID? {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    let pointer = UnsafeMutablePointer<CFString?>.allocate(capacity: 1)
    pointer.initialize(to: uid as CFString)
    defer { pointer.deinitialize(count: 1); pointer.deallocate() }
    var result: AudioDeviceID = 0
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address,
        UInt32(MemoryLayout<CFString?>.size), pointer, &size, &result)
    return status == noErr && result != kAudioObjectUnknown ? result : nil
}

func aggregateInputs(_ device: AudioDeviceID, diagnostics: inout [String: Any]) -> [(AudioDeviceID, Int)]? {
    diagnostics["resolution"] = "aggregate-list-unavailable"
    var full = AudioObjectPropertyAddress(mSelector: kAudioAggregateDevicePropertyFullSubDeviceList,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    guard AudioObjectHasProperty(device, &full) else { return nil }
    let pointer = UnsafeMutablePointer<CFArray?>.allocate(capacity: 1)
    pointer.initialize(to: nil)
    defer { pointer.deinitialize(count: 1); pointer.deallocate() }
    var size = UInt32(MemoryLayout<CFArray?>.size)
    guard AudioObjectGetPropertyData(device, &full, 0, nil, &size, pointer) == noErr,
          let ids = pointer.pointee as? [String], !ids.isEmpty else { return nil }
    diagnostics["resolution"] = "aggregate-active-list-unavailable"
    var active = AudioObjectPropertyAddress(mSelector: kAudioAggregateDevicePropertyActiveSubDeviceList,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    size = 0
    guard AudioObjectGetPropertyDataSize(device, &active, 0, nil, &size) == noErr, size > 0 else { return nil }
    var objects = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    let status = objects.withUnsafeMutableBytes { AudioObjectGetPropertyData(device, &active, 0, nil, &size, $0.baseAddress!) }
    guard status == noErr else { return nil }
    diagnostics["resolution"] = "aggregate-device-identities-unavailable"
    let activeUIDs = objects.compactMap { deviceString($0, kAudioDevicePropertyDeviceUID) }
    guard activeUIDs.count == objects.count else { return nil }
    diagnostics["resolution"] = "aggregate-physical-layout-unavailable"
    var result: [(AudioDeviceID, Int)] = []
    for uid in ids where activeUIDs.contains(uid) {
        guard let physical = physicalDevice(uid), let count = channelCount(physical) else { return nil }
        result.append((physical, count))
    }
    diagnostics["input_sources"] = result.map { ["name": deviceName($0.0) ?? "unknown", "channels": $0.1] as [String: Any] }
    diagnostics["aggregate_channels"] = channelCount(device)
    diagnostics["resolution"] = "aggregate-channel-layout-mismatch"
    // Do not infer ranges when the aggregate exposes a different channel layout.
    guard let total = channelCount(device), result.reduce(0, { $0 + $1.1 }) == total else { return nil }
    diagnostics["resolution"] = "aggregate-channels-verified"
    return result
}

func mappedInputDevice(_ map: [Int32]?, _ devices: [(AudioDeviceID, Int)]) -> AudioDeviceID? {
    guard let map = map else {
        let inputs = devices.filter { $0.1 > 0 }
        return inputs.count == 1 ? inputs[0].0 : nil
    }
    guard !map.isEmpty else { return nil }
    var sources = Set<AudioDeviceID>()
    for channel in map {
        guard channel >= 0 else { return nil }
        var start = 0
        var source: AudioDeviceID?
        for (device, count) in devices {
            if Int(channel) >= start && Int(channel) < start + count { source = device; break }
            start += count
        }
        guard let source = source else { return nil }
        sources.insert(source)
    }
    return sources.count == 1 ? sources.first : nil
}

func selectedOutput(_ requested: String) -> AudioDeviceID? {
    // The speaker of the selected device (a Jabra speakerphone has both): Butler speaks where its microphone listens,
    // so the echo reference matches and a Bluetooth headset coming or going never touches the engine.
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return nil }
    var devices = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    let status = devices.withUnsafeMutableBytes {
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, $0.baseAddress!)
    }
    guard status == noErr else { return nil }
    for device in devices {
        var streams = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                                mScope: kAudioDevicePropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
        var streamSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &streams, 0, nil, &streamSize) == noErr, streamSize > 0 else { continue }
        if deviceName(device)?.localizedCaseInsensitiveContains(requested) == true { return device }
    }
    return nil
}

func pinPlayback(_ device: AudioDeviceID?) -> Bool {
    guard let selected = device else { return false }
    if #available(macOS 27, *) {
        do { try playbackEngine.outputNode.withAUAudioUnit { unit in try unit.setDeviceID(selected) }; return true } catch { return false }
    }
    return false     // older systems keep the system default output
}

func selectedInput(_ requested: String) -> AudioDeviceID? {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return nil }
    var devices = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    let status = devices.withUnsafeMutableBytes {
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, $0.baseAddress!)
    }
    guard status == noErr else { return nil }
    for device in devices {
        var streams = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                                mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
        var streamSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &streams, 0, nil, &streamSize) == noErr, streamSize > 0 else { continue }
        if deviceName(device)?.localizedCaseInsensitiveContains(requested) == true { return device }
    }
    return nil
}
final class MicrophoneHAL {
    let unit: AudioUnit
    let format: AVAudioFormat
    var consume: ((AVAudioPCMBuffer) -> Void)?

    init(device: AudioDeviceID) throws {
        var description = AudioComponentDescription(componentType: kAudioUnitType_Output,
            componentSubType: kAudioUnitSubType_HALOutput, componentManufacturer: kAudioUnitManufacturer_Apple,
            componentFlags: 0, componentFlagsMask: 0)
        guard let component = AudioComponentFindNext(nil, &description) else { throw NSError(domain: "input-component", code: 1) }
        var instance: AudioUnit?
        var status = AudioComponentInstanceNew(component, &instance)
        guard status == noErr, let instance = instance else { throw NSError(domain: "input-instance", code: Int(status)) }
        func set(_ property: AudioUnitPropertyID, _ scope: AudioUnitScope, _ bus: AudioUnitElement, _ value: inout UInt32) throws {
            let code = AudioUnitSetProperty(instance, property, scope, bus, &value, 4)
            if code != noErr { throw NSError(domain: "input-property", code: Int(code)) }
        }
        do {
            var yes: UInt32 = 1, no: UInt32 = 0, selected = device
            try set(kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &yes)
            try set(kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &no)
            try set(kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &selected)
            var hardware = AudioStreamBasicDescription()
            var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
            status = AudioUnitGetProperty(instance, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 1, &hardware, &size)
            guard status == noErr, hardware.mSampleRate > 0, hardware.mChannelsPerFrame > 0,
                  let value = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: hardware.mSampleRate,
                    channels: hardware.mChannelsPerFrame, interleaved: false) else { throw NSError(domain: "input-format", code: Int(status)) }
            var stream = value.streamDescription.pointee
            status = AudioUnitSetProperty(instance, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1, &stream, size)
            guard status == noErr else { throw NSError(domain: "input-client-format", code: Int(status)) }
            unit = instance
            format = value
        } catch { AudioComponentInstanceDispose(instance); throw error }
    }

    func start() throws {
        var callback = AURenderCallbackStruct(inputProc: { context, flags, timestamp, _, frames, _ in
            let input = Unmanaged<MicrophoneHAL>.fromOpaque(context).takeUnretainedValue()
            guard let buffer = AVAudioPCMBuffer(pcmFormat: input.format, frameCapacity: frames) else { return noErr }
            buffer.frameLength = frames
            let code = AudioUnitRender(input.unit, flags, timestamp, 1, frames, buffer.mutableAudioBufferList)
            if code == noErr { input.consume?(buffer) }
            return code
        }, inputProcRefCon: Unmanaged.passUnretained(self).toOpaque())
        var code = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_SetInputCallback, kAudioUnitScope_Global,
            1, &callback, UInt32(MemoryLayout<AURenderCallbackStruct>.size))
        guard code == noErr else { throw NSError(domain: "input-callback", code: Int(code)) }
        code = AudioUnitInitialize(unit)
        guard code == noErr else { throw NSError(domain: "input-initialize", code: Int(code)) }
        code = AudioOutputUnitStart(unit)
        guard code == noErr else { throw NSError(domain: "input-start", code: Int(code)) }
    }
    func stop() { AudioOutputUnitStop(unit) }
    deinit { stop(); AudioUnitUninitialize(unit); AudioComponentInstanceDispose(unit) }
}

let root = URL(fileURLWithPath: arguments[2]).resolvingSymlinksInPath().path
var microphone: MicrophoneHAL?
let playbackEngine = AVAudioEngine()
let player = AVAudioPlayerNode()
let controls = DispatchQueue(label: "butler.audio.controls")
var activeID: String?
var signalHandlers: [DispatchSourceSignal] = []
var stage = "local-echo-filter"
var formatInfo: [String: Any] = [:]
var healthTimer: DispatchSourceTimer?

func inputHealth(_ device: AudioDeviceID) -> [String: Any] {
    var result: [String: Any] = [:]
    for channel in [UInt32(0), UInt32(1)] {
        var muteAddress = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute,
            mScope: kAudioDevicePropertyScopeInput, mElement: channel)
        var muted: UInt32 = 0
        var muteSize = UInt32(MemoryLayout<UInt32>.size)
        if result["input_muted"] == nil && AudioObjectHasProperty(device, &muteAddress),
           AudioObjectGetPropertyData(device, &muteAddress, 0, nil, &muteSize, &muted) == noErr {
            result["input_muted"] = muted != 0
        }
        var gainAddress = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyVolumeScalar,
            mScope: kAudioDevicePropertyScopeInput, mElement: channel)
        var gain: Float32 = 0
        var gainSize = UInt32(MemoryLayout<Float32>.size)
        if result["input_volume"] == nil && AudioObjectHasProperty(device, &gainAddress),
           AudioObjectGetPropertyData(device, &gainAddress, 0, nil, &gainSize, &gain) == noErr, gain.isFinite {
            result["input_volume"] = Double(gain)
        }
    }
    return result
}

func currentInputDevice() -> AudioDeviceID? {
    guard let input = microphone else { return nil }
    var device: AudioDeviceID = 0
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let code = AudioUnitGetProperty(input.unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &device, &size)
    return code == noErr && device != 0 ? device : nil
}

func inputChannelMap() -> [Int32]? { nil }

func runningInputHealth(_ selected: AudioDeviceID) -> [String: Any] {
    let actual = currentInputDevice()
    var physical = actual
    var route: [String: Any] = [:]
    if let actual = actual, actual != selected {
        route["aggregate"] = deviceName(actual) ?? "aggregate"
        if let members = aggregateInputs(actual, diagnostics: &route) {
            let map = inputChannelMap()
            physical = mappedInputDevice(map, members)
            if let map = map { route["channel_map"] = map }
            route["resolution"] = physical == nil ? "aggregate-channel-source-unverified" : "aggregate-source-verified"
        }
    }
    var result = physical.map { inputHealth($0) } ?? [:]
    result["input_verified"] = physical == selected
    if let physical = physical, let title = deviceName(physical) { result["actual_microphone"] = title }
    else if let actual = actual { result["actual_microphone"] = deviceName(actual) ?? "unknown" }
    result["input_route"] = route
    var description = AudioComponentDescription()
    let component = microphone.flatMap { AudioComponentInstanceGetComponent($0.unit) }
    let nativeInput = component.map { AudioComponentGetDescription($0, &description) == noErr && description.componentSubType == kAudioUnitSubType_HALOutput } ?? false
    result["input_backend"] = nativeInput ? "AUHAL-input-only" : "unverified"
    result["voice_processing_enabled"] = !nativeInput || playbackEngine.outputNode.isVoiceProcessingEnabled
    result["other_audio_ducking"] = result["voice_processing_enabled"] as? Bool == true ? "unexpected-voice-processing" : "none"
    result["meeting"] = meetingActivity()
    if let systemInput = defaultDevice(kAudioHardwarePropertyDefaultInputDevice) {
        result["system_default_microphone"] = deviceName(systemInput)
        result["microphone_is_system_default"] = physical == systemInput
    }
    if let systemOutput = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice) {
        result["system_default_output"] = deviceName(systemOutput)
        for channel in [UInt32(0), UInt32(1)] {
            var property = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute,
                mScope: kAudioDevicePropertyScopeOutput, mElement: channel)
            var muted: UInt32 = 0, bytes = UInt32(MemoryLayout<UInt32>.size)
            if result["system_output_muted"] == nil, AudioObjectHasProperty(systemOutput, &property),
               AudioObjectGetPropertyData(systemOutput, &property, 0, nil, &bytes, &muted) == noErr {
                result["system_output_muted"] = muted != 0
            }
            property.mSelector = kAudioDevicePropertyVolumeScalar
            var volume: Float32 = 0
            bytes = UInt32(MemoryLayout<Float32>.size)
            if result["system_output_volume"] == nil, AudioObjectHasProperty(systemOutput, &property),
               AudioObjectGetPropertyData(systemOutput, &property, 0, nil, &bytes, &volume) == noErr, volume.isFinite {
                result["system_output_volume"] = Double(volume)
            }
        }
    }
    return result
}

func report(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value) {
        FileHandle.standardError.write(Data("BUTLER_AUDIO ".utf8) + data + Data("\n".utf8))
    }
}

do {
    let echo = try EchoFilter()
    stage = "microphone-selection"
    guard let device = selectedInput(arguments[1]) else {
        throw NSError(domain: "selected-microphone-unavailable", code: 2)
    }
    microphone = try MicrophoneHAL(device: device)
    stage = "playback-device"
    let speaker = selectedOutput(arguments[1])
    let playbackPinned = pinPlayback(speaker)
    stage = "formats"
    guard let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true) else { exit(3) }
    // Ordinary I/O preserves the hardware's normal output format and volume.
    let rate = playbackEngine.outputNode.inputFormat(forBus: 0).sampleRate
    guard rate > 0, let playback = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false) else { exit(3) }
    playbackEngine.attach(player)
    stage = "connect-output"
    if #available(macOS 27, *) { try playbackEngine.connectNode(player, to: playbackEngine.mainMixerNode, format: playback) }
    else { playbackEngine.connect(player, to: playbackEngine.mainMixerNode, format: playback) }
    if #available(macOS 27, *) { try playbackEngine.connectNode(playbackEngine.mainMixerNode, to: playbackEngine.outputNode, format: nil) }
    else { playbackEngine.connect(playbackEngine.mainMixerNode, to: playbackEngine.outputNode, format: nil) }
    // Input-only AUHAL owns the selected microphone clock. Playback has its
    // own AVAudioEngine, so output clock changes cannot stop microphone input.
    let input = microphone!.format
    let output = playbackEngine.mainMixerNode.outputFormat(forBus: 0)
    formatInfo = ["input_rate": input.sampleRate, "input_channels": input.channelCount, "output_rate": output.sampleRate,
                  "playback_device": playbackPinned ? (speaker.flatMap(deviceName) ?? "selected") : "system-default"]
    guard input.sampleRate > 0, input.channelCount > 0 else { exit(3) }
    var referenceFormat: AVAudioFormat?
    var referenceConverter: AVAudioConverter?
    let referenceTap: AVAudioNodeTapBlock = { buffer, _ in
        if referenceFormat != buffer.format {
            referenceFormat = buffer.format
            referenceConverter = AVAudioConverter(from: buffer.format, to: target)
        }
        guard let referenceConverter = referenceConverter, buffer.format.sampleRate > 0 else { return }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16000 / buffer.format.sampleRate + 64)
        guard let converted = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        referenceConverter.convert(to: converted, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true; status.pointee = .haveData; return buffer
        }
        if error == nil, converted.frameLength > 0, let samples = converted.int16ChannelData {
            echo.reference(Array(UnsafeBufferPointer(start: samples[0], count: Int(converted.frameLength))))
        }
    }
    if #available(macOS 27, *) {
        try playbackEngine.mainMixerNode.installAudioTap(onBus: 0, bufferSize: 1024, format: nil) { buffer, time in
            referenceTap(AVAudioPCMBuffer(copying: buffer), time)
        }
    } else { playbackEngine.mainMixerNode.installTap(onBus: 0, bufferSize: 1024, format: nil, block: referenceTap) }
    var converterFormat: AVAudioFormat?
    var converter: AVAudioConverter?
    let tap: AVAudioNodeTapBlock = { buffer, _ in
        if converterFormat != buffer.format {
            converterFormat = buffer.format
            converter = AVAudioConverter(from: buffer.format, to: target)
        }
        guard let converter = converter, buffer.format.sampleRate > 0 else { return }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16000 / buffer.format.sampleRate + 64)
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var supplied = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if supplied { status.pointee = .noDataNow; return nil }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        if error == nil, output.frameLength > 0, let samples = output.int16ChannelData {
            let clean = echo.microphone(Array(UnsafeBufferPointer(start: samples[0], count: Int(output.frameLength))))
            clean.withUnsafeBytes { FileHandle.standardOutput.write(Data($0)) }
        }
    }
    microphone!.consume = { buffer in tap(buffer, AVAudioTime(hostTime: mach_absolute_time())) }
    stage = "input-start"
    try microphone!.start()
    stage = "output-engine-start"
    playbackEngine.prepare()
    try playbackEngine.start()
    var ready = runningInputHealth(device)
    ready.merge(["state": "ready", "echo_cancellation": true, "echo_backend": "speexdsp",
        "playback_reference": true,
        "formats": formatInfo]) { _, new in new }
    report(ready)
    // A device change (a Bluetooth headset connecting, the default output moving) stops an AVAudioEngine. Before,
    // the engine stayed stopped: every playback failed and the service lost its voice until a restart.
    NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: playbackEngine, queue: nil) { _ in
        controls.async {
            _ = pinPlayback(speaker)
            playbackEngine.prepare()
            do {
                try playbackEngine.start()
                report(["state": "health", "engine": "restarted-after-configuration-change"])
            } catch {
                report(["state": "unavailable", "stage": "engine-restart", "code": (error as NSError).code, "formats": formatInfo])
                exit(1)
            }
        }
    }
    healthTimer = DispatchSource.makeTimerSource(queue: controls)
    healthTimer?.schedule(deadline: .now(), repeating: 1)
    healthTimer?.setEventHandler {
        var health = runningInputHealth(device)
        health["state"] = "health"
        report(health)
    }
    healthTimer?.resume()

    func cancel() {
        player.stop()
        if let id = activeID { report(["playback": "cancelled", "id": id]) }
        activeID = nil
    }

    var streamPending = 0
    var streamCompleted = 0
    var streamNext = 0
    var streamFinished = false

    func finishStream(_ id: String) {
        guard activeID == id else { return }
        streamFinished = true
        if streamPending == 0 { activeID = nil; report(["playback": "done", "id": id]) }
    }

    func play(_ path: String, id: String, sequence: Int? = nil) throws {
        if let sequence = sequence {
            guard activeID == id, !streamFinished, sequence == streamNext, streamPending < 2 else {
                throw NSError(domain: "invalid-stream", code: 1)
            }
        }
        let url = URL(fileURLWithPath: path).resolvingSymlinksInPath()
        guard url.path.hasPrefix(root + "/"), url.pathExtension == "wav",
              id.range(of: "^[A-Za-z0-9_-]{1,80}$", options: .regularExpression) != nil else { throw NSError(domain: "invalid", code: 1) }
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        guard let size = attributes[.size] as? NSNumber, size.intValue <= 20_000_000 else { throw NSError(domain: "invalid", code: 2) }
        let file = try AVAudioFile(forReading: url)
        guard file.length > 0, Double(file.length) <= file.processingFormat.sampleRate * 90,
              let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)),
              let convert = AVAudioConverter(from: file.processingFormat, to: playback) else { throw NSError(domain: "invalid", code: 3) }
        try file.read(into: buffer)
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * playback.sampleRate / file.processingFormat.sampleRate + 64)
        guard let output = AVAudioPCMBuffer(pcmFormat: playback, frameCapacity: capacity) else { throw NSError(domain: "invalid", code: 4) }
        var supplied = false
        var error: NSError?
        convert.convert(to: output, error: &error) { _, status in
            if supplied { status.pointee = .endOfStream; return nil }
            supplied = true
            status.pointee = .haveData
            return buffer
        }
        if let error = error { throw error }
        if sequence == nil { cancel(); activeID = id }
        else { streamPending += 1; streamNext += 1 }
        player.scheduleBuffer(output, completionCallbackType: .dataPlayedBack) { _ in
            controls.async {
                guard activeID == id else { return }
                if sequence == nil { activeID = nil; report(["playback": "done", "id": id]) }
                else {
                    streamPending -= 1
                    streamCompleted += 1
                    report(["playback": "progress", "id": id, "completed_chunks": streamCompleted])
                    if streamFinished && streamPending == 0 { activeID = nil; report(["playback": "done", "id": id]) }
                }
            }
        }
        if !player.isPlaying {
            if #available(macOS 27, *) { try player.playAudio() }
            else { player.play() }
        }
        if sequence == nil || sequence == 0 { report(["playback": "started", "id": id]) }
    }

    DispatchQueue.global(qos: .userInitiated).async {
        while let line = readLine() {
            guard line.utf8.count <= 8192, let data = line.data(using: .utf8),
                  let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: String] else { continue }
            controls.async {
                if request["command"] == "cancel" { cancel() }
                else if request["command"] == "begin", let id = request["id"],
                        id.range(of: "^[A-Za-z0-9_-]{1,80}$", options: .regularExpression) != nil {
                    cancel(); activeID = id
                    streamPending = 0; streamCompleted = 0; streamNext = 0; streamFinished = false
                }
                else if request["command"] == "finish", let id = request["id"] { finishStream(id) }
                else if request["command"] == "append", let path = request["path"], let id = request["id"],
                        let sequence = request["sequence"].flatMap(Int.init) {
                    guard activeID == id else { return }
                    do { try play(path, id: id, sequence: sequence) }
                    catch { cancel(); report(["playback": "failed", "id": id]) }
                }
                else if request["command"] == "play", let path = request["path"], let id = request["id"] {
                    do { try play(path, id: id) }
                    catch { report(["playback": "failed", "id": id]) }
                }
            }
        }
        microphone?.stop()
        playbackEngine.stop()
        exit(0)
    }
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler { microphone?.stop(); playbackEngine.stop(); exit(0) }
        source.resume()
        signalHandlers.append(source)
    }
    RunLoop.current.run()
} catch {
    report(["state": "unavailable", "stage": stage, "code": (error as NSError).code, "formats": formatInfo])
    exit(1)
}

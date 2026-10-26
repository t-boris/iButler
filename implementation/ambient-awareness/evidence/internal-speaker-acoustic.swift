import AVFoundation
import CoreAudio
import Foundation

// Select only this test engine's output. Never set the system default or volume.
func name(_ device: AudioDeviceID) -> String? {
    var address = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    let pointer = UnsafeMutablePointer<CFString?>.allocate(capacity: 1)
    pointer.initialize(to: nil)
    defer { pointer.deinitialize(count: 1); pointer.deallocate() }
    var size = UInt32(MemoryLayout<CFString?>.size)
    guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, pointer) == noErr else { return nil }
    return pointer.pointee as String?
}
var devicesAddress = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
    mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
var size: UInt32 = 0
guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &devicesAddress, 0, nil, &size) == noErr else { exit(2) }
var devices = [AudioDeviceID](repeating: 0, count: Int(size)/MemoryLayout<AudioDeviceID>.size)
guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &devicesAddress, 0, nil, &size, &devices) == noErr,
      var device = devices.first(where: { name($0) == "Mac Studio Speakers" }),
      CommandLine.arguments.count == 2 else { exit(3) }
let engine = AVAudioEngine()
let routed: Bool
if #available(macOS 27, *) {
    routed = engine.outputNode.withAudioUnit { unit in
        guard let unit = unit else { return false }
        return AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
            &device, UInt32(MemoryLayout<AudioDeviceID>.size)) == noErr
    }
} else {
    routed = engine.outputNode.audioUnit.map { AudioUnitSetProperty($0,
        kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &device,
        UInt32(MemoryLayout<AudioDeviceID>.size)) == noErr } ?? false
}
guard routed else { exit(4) }
let file = try AVAudioFile(forReading: URL(fileURLWithPath: CommandLine.arguments[1]))
let player = AVAudioPlayerNode()
engine.attach(player)
if #available(macOS 27, *) { try engine.connectNode(player, to: engine.mainMixerNode, format: file.processingFormat) }
else { engine.connect(player, to: engine.mainMixerNode, format: file.processingFormat) }
let done = DispatchSemaphore(value: 0)
player.scheduleFile(file, at: nil, completionCallbackType: .dataPlayedBack) { _ in done.signal() }
try engine.start()
if #available(macOS 27, *) { try player.playAudio() }
else { player.play() }
let completed = done.wait(timeout: .now()+10) == .success
player.stop()
engine.stop()
exit(completed ? 0 : 5)

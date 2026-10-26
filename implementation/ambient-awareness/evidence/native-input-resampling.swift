import AVFoundation
import Foundation
func run() throws {
let file = try AVAudioFile(forReading: URL(fileURLWithPath: CommandLine.arguments[1]))
let original = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length))!
try file.read(into: original)
let hardware = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 1, interleaved: false)!
let up = AVAudioConverter(from: file.processingFormat, to: hardware)!
let input = AVAudioPCMBuffer(pcmFormat: hardware, frameCapacity: AVAudioFrameCount(Double(original.frameLength) * 48000 / file.processingFormat.sampleRate + 64))!
var supplied = false
var error: NSError?
up.convert(to: input, error: &error) { _, status in
    if supplied { status.pointee = .endOfStream; return nil }
    supplied = true; status.pointee = .haveData; return original
}
if let error = error { throw error }
let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
let converter = AVAudioConverter(from: hardware, to: target)!
let outputFile = try AVAudioFile(forWriting: URL(fileURLWithPath: CommandLine.arguments[2]), settings: target.settings, commonFormat: .pcmFormatInt16, interleaved: true)
var outputFrames = 0
var offset = 0
while offset < Int(input.frameLength) {
    let length = min(1024, Int(input.frameLength) - offset)
    let buffer = AVAudioPCMBuffer(pcmFormat: hardware, frameCapacity: AVAudioFrameCount(length))!
    buffer.frameLength = AVAudioFrameCount(length)
    buffer.floatChannelData![0].update(from: input.floatChannelData![0].advanced(by: offset), count: length)
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16000 / hardware.sampleRate + 64)
    let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity)!
    var supplied = false
    var error: NSError?
    converter.convert(to: output, error: &error) { _, status in
        if supplied { status.pointee = .noDataNow; return nil }
        supplied = true; status.pointee = .haveData; return buffer
    }
    if let error = error { throw error }
    try outputFile.write(from: output)
    outputFrames += Int(output.frameLength)
    offset += length
}
let metadata: [String: Any] = ["synthetic": true, "input_rate": 48000, "output_rate": 16000, "hardware_frames": input.frameLength, "pcm16_frames": outputFrames, "original_frames": original.frameLength]
print(String(data: try JSONSerialization.data(withJSONObject: metadata), encoding: .utf8)!)
}
try run()

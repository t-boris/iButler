import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreGraphics
import Darwin

// PCM16/16kHz stdout, content-free health stderr. No screen output is attached.
func report(_ state: String, _ reason: String = "") {
    let value: [String:Any] = ["state":state,"source":"system-audio","reason":reason,"rate":16000,"channels":1]
    if let data = try? JSONSerialization.data(withJSONObject:value) {
        FileHandle.standardError.write(Data("BUTLER_SYSTEM_AUDIO ".utf8)+data+Data("\n".utf8))
    }
}

final class AudioOutput: NSObject, SCStreamOutput, SCStreamDelegate {
    let target = AVAudioFormat(commonFormat:.pcmFormatInt16,sampleRate:16000,channels:1,interleaved:true)!
    var source: AVAudioFormat?
    var converter: AVAudioConverter?
    func stream(_ stream:SCStream,didOutputSampleBuffer sample:CMSampleBuffer,of type:SCStreamOutputType) {
        guard type == .audio, CMSampleBufferDataIsReady(sample),
              let description = CMSampleBufferGetFormatDescription(sample),
              let basic = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              let format = AVAudioFormat(streamDescription:basic) else { return }
        let count = CMSampleBufferGetNumSamples(sample)
        guard count > 0, let pcm = AVAudioPCMBuffer(pcmFormat:format,frameCapacity:AVAudioFrameCount(count)) else { return }
        pcm.frameLength = AVAudioFrameCount(count)
        guard CMSampleBufferCopyPCMDataIntoAudioBufferList(sample,at:0,frameCount:Int32(count),into:pcm.mutableAudioBufferList) == noErr else { return }
        if source != format { source = format;converter = AVAudioConverter(from:format,to:target) }
        guard let converter = converter,
              let converted = AVAudioPCMBuffer(pcmFormat:target,frameCapacity:AVAudioFrameCount(Double(count)*16000/format.sampleRate+64)) else { return }
        var supplied = false
        var error: NSError?
        converter.convert(to:converted,error:&error) { _, status in
            if supplied { status.pointee = .noDataNow;return nil }
            supplied = true;status.pointee = .haveData;return pcm
        }
        if error == nil, converted.frameLength > 0, let values = converted.int16ChannelData {
            FileHandle.standardOutput.write(Data(bytes:values[0],count:Int(converted.frameLength)*2))
        }
    }
    func stream(_ stream:SCStream,didStopWithError error:Error) { report("unavailable","capture-stopped");exit(1) }
}

if CommandLine.arguments.contains("--test-format") {
    let output=AudioOutput()
    let format=output.target
    let passed = format.sampleRate == 16000 && format.channelCount == 1 && format.commonFormat == .pcmFormatInt16
    print("{\"passed\":\(passed),\"source\":\"system-audio\",\"synthetic_only\":true}")
    exit(passed ? 0 : 1)
}
guard CGPreflightScreenCaptureAccess() else { report("unavailable","macos-screen-audio-permission");exit(2) }
let output=AudioOutput()
var capture: SCStream?
let queue=DispatchQueue(label:"butler.system-audio.pcm")
Task {
    do {
        let content=try await SCShareableContent.excludingDesktopWindows(false,onScreenWindowsOnly:true)
        guard let display=content.displays.first else { report("unavailable","display-unavailable");exit(3) }
        let filter=SCContentFilter(display:display,excludingWindows:[])
        let configuration=SCStreamConfiguration()
        configuration.width=2;configuration.height=2
        configuration.minimumFrameInterval=CMTime(value:1,timescale:1)
        configuration.queueDepth=3
        configuration.capturesAudio=true;configuration.excludesCurrentProcessAudio=true
        configuration.sampleRate=48000;configuration.channelCount=2
        let stream=SCStream(filter:filter,configuration:configuration,delegate:output)
        try stream.addStreamOutput(output,type:.audio,sampleHandlerQueue:queue)
        try await stream.startCapture()
        capture=stream;report("ready")
    } catch { report("unavailable","capture-start-failed");exit(4) }
}
var handlers=[DispatchSourceSignal]()
for value in [SIGTERM,SIGINT] {
    signal(value,SIG_IGN)
    let handler=DispatchSource.makeSignalSource(signal:value,queue:.main)
    handler.setEventHandler { Task { try? await capture?.stopCapture();exit(0) } }
    handler.resume();handlers.append(handler)
}
RunLoop.main.run()

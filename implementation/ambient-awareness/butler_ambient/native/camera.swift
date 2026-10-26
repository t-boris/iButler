import Foundation
import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers

// Owner-local, video-only child of the existing signed permission parent.
// No files, face recognition, permission prompts or alternative device choice.
func report(_ state: String, _ stage: String) {
    let value = ["state": state, "stage": stage]
    if let data = try? JSONSerialization.data(withJSONObject: value) {
        FileHandle.standardError.write(Data("BUTLER_CAMERA ".utf8) + data + Data([10]))
    }
}

func jpeg(_ image: CGImage) -> Data? {
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.8] as CFDictionary)
    return CGImageDestinationFinalize(destination) ? data as Data : nil
}

if CommandLine.arguments.contains("--video-effects") {
    AVCaptureDevice.showSystemUserInterface(.videoEffects)
    RunLoop.main.run(until: Date().addingTimeInterval(10))
    exit(0)
}

if CommandLine.arguments.contains("--test-jpeg") {
    let context = CIContext()
    let image = CIImage(color: CIColor(red: 0.2, green: 0.4, blue: 0.6)).cropped(to: CGRect(x: 0, y: 0, width: 640, height: 480))
    guard let cg = context.createCGImage(image, from: image.extent), let data = jpeg(cg),
          let source = CGImageSourceCreateWithData(data as CFData, nil),
          let decoded = CGImageSourceCreateImageAtIndex(source, 0, nil),
          decoded.width == 640, decoded.height == 480 else { exit(1) }
    print("PASS synthetic JPEG video transport (640x480); no device access")
    exit(0)
}

guard CommandLine.arguments.count == 3 else { report("unavailable", "arguments"); exit(2) }
let selectedName = CommandLine.arguments[1]
let size = CommandLine.arguments[2]
let presets: [String: AVCaptureSession.Preset] = ["640x480": .vga640x480, "1280x720": .hd1280x720, "1920x1080": .hd1920x1080]
guard let preset = presets[size] else { report("unavailable", "size"); exit(2) }
guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized else { report("unavailable", "permission"); exit(3) }
let devices = AVCaptureDevice.DiscoverySession(deviceTypes: [.external, .builtInWideAngleCamera, .continuityCamera],
    mediaType: .video, position: .unspecified).devices.filter { $0.localizedName == selectedName }
guard devices.count == 1, let device = devices.first else { report("unavailable", "selected-device"); exit(4) }

final class VideoSink: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let context = CIContext(options: [.cacheIntermediates: false])
    var last = -Double.infinity
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        let now = ProcessInfo.processInfo.systemUptime
        guard now - last >= 0.18, let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        last = now
        autoreleasepool {
            let image = CIImage(cvPixelBuffer: buffer)
            guard let cg = context.createCGImage(image, from: image.extent), let data = jpeg(cg) else { return }
            do { try FileHandle.standardOutput.write(contentsOf: data) }
            catch { exit(0) }
        }
    }
}

let session = AVCaptureSession()
session.beginConfiguration()
guard session.canSetSessionPreset(preset) else { report("unavailable", "format"); exit(5) }
session.sessionPreset = preset
do {
    let input = try AVCaptureDeviceInput(device: device)
    guard session.canAddInput(input) else { report("unavailable", "input"); exit(6) }
    session.addInput(input)
} catch { report("unavailable", "open"); exit(6) }
let output = AVCaptureVideoDataOutput()
output.alwaysDiscardsLateVideoFrames = true
let nativePixelFormat = CMFormatDescriptionGetMediaSubType(device.activeFormat.formatDescription)
let pixelFormat = output.availableVideoPixelFormatTypes.contains(nativePixelFormat) ? nativePixelFormat
    : output.availableVideoPixelFormatTypes.contains(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) ? kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
    : kCVPixelFormatType_32BGRA
output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: pixelFormat]
let sink = VideoSink()
output.setSampleBufferDelegate(sink, queue: DispatchQueue(label: "butler.camera.frames"))
guard session.canAddOutput(output) else { report("unavailable", "output"); exit(7) }
session.addOutput(output)
session.commitConfiguration()
session.startRunning()
guard session.isRunning else { report("unavailable", "start"); exit(8) }
let ready: [String: Any] = ["state": "ready", "stage": "native-video", "pixel_format": pixelFormat,
    "portrait": AVCaptureDevice.isPortraitEffectEnabled, "studio_light": AVCaptureDevice.isStudioLightEnabled,
    "background_replacement": AVCaptureDevice.isBackgroundReplacementEnabled,
    "center_stage": AVCaptureDevice.isCenterStageEnabled]
if let data = try? JSONSerialization.data(withJSONObject: ready) {
    FileHandle.standardError.write(Data("BUTLER_CAMERA ".utf8) + data + Data([10]))
}
RunLoop.main.run()

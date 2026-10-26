import Foundation
import CoreGraphics
import ScreenCaptureKit
import ImageIO
import UniformTypeIdentifiers

// Permission setup requests access only; it never enumerates or captures a
// display, so it is safe even while participant checks have paused observation.
if CommandLine.arguments.contains("--request-permission") {
    let allowed = CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess()
    FileHandle.standardOutput.write(Data("{\"screen_permission\":\(allowed)}\n".utf8))
    exit(0)
}

// One image directly to stdout. No clipboard, file, preview, or network access.
do {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
    guard let display = content.displays.first else { throw NSError(domain: "unavailable", code: 1) }
    let filter = SCContentFilter(display: display, excludingWindows: [])
    let config = SCStreamConfiguration()
    config.width = display.width
    config.height = display.height
    config.showsCursor = false
    let image: CGImage = try await withCheckedThrowingContinuation { continuation in
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { image, error in
            if let image = image {
                continuation.resume(returning: image)
            } else {
                continuation.resume(throwing: error ?? NSError(domain: "unavailable", code: 4))
            }
        }
    }
    let bytes = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(bytes, UTType.png.identifier as CFString, 1, nil) else {
        throw NSError(domain: "unavailable", code: 2)
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else { throw NSError(domain: "unavailable", code: 3) }
    FileHandle.standardOutput.write(bytes as Data)
} catch {
    FileHandle.standardError.write(Data("Screen source unavailable.\n".utf8))
    exit(1)
}

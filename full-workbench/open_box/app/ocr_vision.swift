// 使用本机 Vision 识别公开名单图片；无网络、无模型 API。
import Foundation
import Vision
import ImageIO
import CoreGraphics

guard CommandLine.arguments.count == 2,
      let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
      image.width * image.height <= 40_000_000 else { exit(2) }
var rows: [[String: Any]] = []
let step = 1600
for top in stride(from: 0, to: image.height, by: step) {
    let height = min(step + 80, image.height - top)
    guard let tile = image.cropping(to: CGRect(x: 0, y: top, width: image.width, height: height)) else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: tile, options: [:]).perform([request])
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        rows.append(["text": candidate.string, "confidence": candidate.confidence,
                     "top": (Double(top) + (1 - box.maxY) * Double(height)) / Double(image.height),
                     "left": box.minX, "height": box.height * Double(height) / Double(image.height)])
    }
}
let data = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
FileHandle.standardOutput.write(data)

import AppKit
import Foundation

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: make_icon <output.iconset> [source_image]\n", stderr)
    exit(1)
}

let outDir = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

let scriptDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let srcPath = CommandLine.arguments.count > 2
    ? CommandLine.arguments[2]
    : scriptDir.appendingPathComponent("icon_source.jpg").path

guard let sourceImage = NSImage(contentsOfFile: srcPath),
      let srcCG = sourceImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("Failed to load icon source at \(srcPath)\n", stderr)
    exit(1)
}

// Crop tightly around the centered beveled squircle in icon_source.jpg (measured bounds: ~241..782 on 1024x1024)
let w = CGFloat(srcCG.width)
let h = CGFloat(srcCG.height)
let cropBox = CGRect(
    x: w * 0.233,
    y: h * 0.232,
    width: w * 0.534,
    height: h * 0.534
)
let croppedCG = srcCG.cropping(to: cropBox) ?? srcCG
let croppedImg = NSImage(cgImage: croppedCG, size: NSSize(width: cropBox.width, height: cropBox.height))

func renderIcon(size: Int) -> Data? {
    let fSize = CGFloat(size)
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: size,
        pixelsHigh: size,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    )!
    rep.size = NSSize(width: fSize, height: fSize)

    NSGraphicsContext.saveGraphicsState()
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!
    ctx.imageInterpolation = .high
    NSGraphicsContext.current = ctx

    // Standard Apple macOS HIG icon proportions (~82.4% squircle centered with subtle drop shadow)
    let tileSize = fSize * 0.83
    let origin = (fSize - tileSize) / 2.0
    let tileRect = NSRect(x: origin, y: origin, width: tileSize, height: tileSize)
    let cornerRadius = tileSize * 0.225
    let squirclePath = NSBezierPath(roundedRect: tileRect, xRadius: cornerRadius, yRadius: cornerRadius)

    // Subtle soft shadow underneath the transparent squircle
    let shadow = NSShadow()
    shadow.shadowColor = NSColor(white: 0.0, alpha: 0.45)
    shadow.shadowOffset = NSSize(width: 0, height: -fSize * 0.016)
    shadow.shadowBlurRadius = fSize * 0.032

    NSGraphicsContext.saveGraphicsState()
    shadow.set()
    NSColor.black.setFill()
    squirclePath.fill()
    NSGraphicsContext.restoreGraphicsState()

    // Clip to clean rounded squircle so outer corners remain 100% transparent
    NSGraphicsContext.saveGraphicsState()
    squirclePath.addClip()
    croppedImg.draw(
        in: tileRect,
        from: NSRect(origin: .zero, size: croppedImg.size),
        operation: .sourceOver,
        fraction: 1.0
    )
    NSGraphicsContext.restoreGraphicsState()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
}

let specs: [(Int, String)] = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]

for (sz, name) in specs {
    if let data = renderIcon(size: sz) {
        try data.write(to: outDir.appendingPathComponent(name))
    }
}

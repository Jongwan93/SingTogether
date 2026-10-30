import AppKit
import PDFKit

/// 이조한 악보 PDF를 원본과 같은 종류의 그림(PNG·JPEG)으로. 그림 악보를 넣었으면 그림으로 돌려주기 위해서다.
package enum Raster {
    package enum Format {
        case png, jpeg
        package var ext: String { self == .png ? "png" : "jpg" }
    }

    /// 원본 확장자에 맞는 출력 형식. jpg/jpeg → JPEG, 그 밖의 그림(png·webp·tif·bmp) → PNG, PDF → nil (PDF 그대로).
    package static func format(forExtension ext: String) -> Format? {
        switch ext.lowercased() {
        case "jpg", "jpeg": return .jpeg
        case "png", "webp", "tif", "tiff", "bmp": return .png
        default: return nil
        }
    }

    /// 그림 파일의 가로 픽셀 수 (디코딩 없이 머리만 읽는다)
    package static func pixelWidth(of url: URL) -> Int? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any] else { return nil }
        return props[kCGImagePropertyPixelWidth] as? Int
    }

    /// PDF의 쪽마다 하나씩, 너비 width픽셀의 그림 데이터. 흰 바탕. 메인 스레드가 아니어도 된다 (화면 밖 비트맵).
    package static func pages(of pdf: URL, width: Int, format: Format) throws -> [Data] {
        guard let doc = PDFDocument(url: pdf), doc.pageCount > 0 else { throw SheetOCR.Failure.failed("Could not open \(pdf.lastPathComponent)") }
        var out: [Data] = []
        for i in 0..<doc.pageCount {
            guard let page = doc.page(at: i) else { continue }
            let box = page.bounds(for: .mediaBox)
            let scale = CGFloat(width) / box.width
            let h = Int((box.height * scale).rounded())
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4,
                                             hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
                  let ctx = NSGraphicsContext(bitmapImageRep: rep) else { throw SheetOCR.Failure.failed("Could not draw page \(i + 1)") }
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = ctx
            let cg = ctx.cgContext
            cg.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
            cg.fill(CGRect(x: 0, y: 0, width: width, height: h))
            cg.interpolationQuality = .high
            cg.scaleBy(x: scale, y: scale)
            page.draw(with: .mediaBox, to: cg)
            NSGraphicsContext.restoreGraphicsState()
            let props: [NSBitmapImageRep.PropertyKey: Any] = format == .jpeg ? [.compressionFactor: 0.92] : [:]
            guard let data = rep.representation(using: format == .jpeg ? .jpeg : .png, properties: props) else {
                throw SheetOCR.Failure.failed("Could not encode page \(i + 1)")
            }
            out.append(data)
        }
        return out
    }
}

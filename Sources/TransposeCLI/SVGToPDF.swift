import AppKit
import PDFKit
import WebKit

/// Verovio가 그린 SVG(쪽마다 하나)를 한 PDF로. WebKit으로 그리므로 벡터가 유지되고 별도 설치가 필요 없다.
/// 메인 스레드에서 불러야 하며, 끝날 때까지 런 루프를 돌린다. (CoreSVG의 NSImage는 Verovio의 <symbol>/<use>를 못 그린다.)
enum SVGToPDF {
    static func convert(_ svgs: [URL], to out: URL) throws {
        let doc = PDFDocument()
        for svg in svgs {
            let data = try page(svg)
            guard let p = PDFDocument(data: data), p.pageCount > 0 else { throw Failure("WebKit produced no PDF page for \(svg.lastPathComponent)") }
            for k in 0..<p.pageCount { doc.insert(p.page(at: k)!, at: doc.pageCount) }
        }
        guard doc.write(to: out) else { throw Failure("could not write \(out.path)") }
    }

    struct Failure: Error, CustomStringConvertible { let description: String; init(_ d: String) { description = d } }

    private static func page(_ url: URL) throws -> Data {
        let svg = try String(contentsOf: url, encoding: .utf8)
        func attr(_ name: String) -> Double? {
            guard let re = try? NSRegularExpression(pattern: "<svg[^>]*\\s\(name)=\"([0-9.]+)"),
                  let m = re.firstMatch(in: svg, range: NSRange(svg.startIndex..., in: svg)),
                  let r = Range(m.range(at: 1), in: svg) else { return nil }
            return Double(svg[r])
        }
        let size = CGSize(width: attr("width") ?? 840, height: attr("height") ?? 1188)
        let loader = Loader(size: size)
        let html = "<!doctype html><html><head><meta charset=\"utf-8\"><style>html,body{margin:0;padding:0;background:#fff}</style></head><body>\(svg)</body></html>"
        loader.view.loadHTMLString(html, baseURL: url.deletingLastPathComponent())
        let deadline = Date(timeIntervalSinceNow: 60)
        while loader.result == nil && Date() < deadline {
            RunLoop.main.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05))
        }
        switch loader.result {
        case .success(let d)?: return d
        case .failure(let e)?: throw e
        case nil: throw Failure("timed out rendering \(url.lastPathComponent)")
        }
    }

    private final class Loader: NSObject, WKNavigationDelegate {
        let view: WKWebView
        var result: Result<Data, Error>?
        init(size: CGSize) {
            view = WKWebView(frame: CGRect(origin: .zero, size: size))
            super.init()
            view.navigationDelegate = self
        }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            // 글꼴 적용이 끝나도록 한 박자 쉬고 찍는다
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                let cfg = WKPDFConfiguration()
                cfg.rect = CGRect(origin: .zero, size: self.view.frame.size)
                self.view.createPDF(configuration: cfg) { self.result = $0 }
            }
        }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { result = .failure(error) }
        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { result = .failure(error) }
    }
}

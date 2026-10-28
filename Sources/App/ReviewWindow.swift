import AppKit
import PDFKit
import Transpose

/// 이미지·PDF 악보를 인식한 뒤 띄우는 확인 창.
/// 왼쪽: 원본 위에 확인할 곳 표시(review.png). 오른쪽: 이조된 악보 PDF.
/// 아래: 요약, 텍스트 고치기(기본 편집기로 .txt 열기), 고친 텍스트로 다시 만들기, 결과 파일 보기.
final class ReviewWindowController: NSWindowController {
    private let original: URL
    private let semitones: Int
    private var result: SheetOCR.Result
    private var delivered: URL

    private let summary = NSTextField(labelWithString: "")
    private let pdfView = PDFView()
    private let imageView = NSImageView()
    private let imageScroll = NSScrollView()
    private let spinner = NSProgressIndicator()

    init(original: URL, semitones: Int, result: SheetOCR.Result, delivered: URL) {
        self.original = original
        self.semitones = semitones
        self.result = result
        self.delivered = delivered
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 760),
                         styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "확인: \(original.lastPathComponent) → \(delivered.lastPathComponent)"
        w.minSize = NSSize(width: 800, height: 500)
        super.init(window: w)
        build()
        load()
    }
    required init?(coder: NSCoder) { fatalError() }

    private func build() {
        guard let content = window?.contentView else { return }
        let split = NSSplitView()
        split.isVertical = true
        split.dividerStyle = .thin

        imageView.imageScaling = .scaleProportionallyUpOrDown
        imageView.imageAlignment = .alignTop
        imageScroll.documentView = imageView
        imageScroll.hasVerticalScroller = true
        imageScroll.hasHorizontalScroller = true
        imageScroll.allowsMagnification = true
        imageScroll.minMagnification = 0.25
        imageScroll.maxMagnification = 4
        let left = labeled("원본 — 빨강: 확인할 음표, 주황: 코드, 보라: 가사", imageScroll)

        pdfView.autoScales = true
        pdfView.displayMode = .singlePageContinuous
        let right = labeled("이조 결과 — 의심스러운 곳은 빨간 음표", pdfView)

        split.addArrangedSubview(left)
        split.addArrangedSubview(right)

        summary.font = .systemFont(ofSize: 13, weight: .medium)
        summary.lineBreakMode = .byTruncatingTail
        let fix = NSButton(title: "텍스트 고치기…", target: self, action: #selector(openText))
        fix.toolTip = "압축 텍스트(.txt)를 기본 편집기로 엽니다. 마디 번호로 찾아 고치세요"
        let redo = NSButton(title: "고친 텍스트로 다시 만들기", target: self, action: #selector(rebuild))
        redo.keyEquivalent = "\r"
        let reveal = NSButton(title: "결과 파일 보기", target: self, action: #selector(revealResult))
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isHidden = true
        let bar = NSStackView(views: [summary, NSView(), spinner, fix, redo, reveal])
        bar.orientation = .horizontal
        bar.spacing = 10
        bar.edgeInsets = NSEdgeInsets(top: 8, left: 14, bottom: 10, right: 14)
        summary.setContentHuggingPriority(.defaultLow, for: .horizontal)
        summary.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let root = NSStackView(views: [split, bar])
        root.orientation = .vertical
        root.spacing = 0
        root.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
        ])
        split.setHoldingPriority(.defaultLow, forSubviewAt: 0)
        DispatchQueue.main.async { [weak self] in
            guard let self, let w = self.window else { return }
            split.setPosition(w.frame.width * 0.5, ofDividerAt: 0)
        }
    }

    private func labeled(_ title: String, _ view: NSView) -> NSView {
        let label = NSTextField(labelWithString: title)
        label.font = .systemFont(ofSize: 11)
        label.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [label, view])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.edgeInsets = NSEdgeInsets(top: 8, left: 10, bottom: 0, right: 10)
        view.translatesAutoresizingMaskIntoConstraints = false
        view.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -20).isActive = true
        return stack
    }

    private func load() {
        summary.stringValue = "\(result.summary)   ·   \(result.keys ?? "")   ·   \(result.engine)"
        if let png = result.reviewPNGs.first, let img = NSImage(contentsOf: png) {
            imageView.image = img
            imageView.frame = NSRect(origin: .zero, size: img.size)
            imageScroll.magnification = min(1, (imageScroll.bounds.width - 4) / max(1, img.size.width))
        } else if let img = NSImage(contentsOf: original) {
            imageView.image = img
            imageView.frame = NSRect(origin: .zero, size: img.size)
        }
        if let pdf = result.transposedPDF, let doc = PDFDocument(url: pdf) {
            pdfView.document = doc
        } else if let t = result.transposed, let text = try? String(contentsOf: t, encoding: .utf8) {
            // 코드 차트: PDF가 없으니 텍스트를 그대로 보여 준다
            let tv = NSTextView(frame: pdfView.bounds)
            tv.string = text
            tv.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
            tv.isEditable = false
            tv.autoresizingMask = [.width, .height]
            pdfView.addSubview(tv)
        }
    }

    @objc private func openText() {
        guard let t = result.text else { NSSound.beep(); return }
        NSWorkspace.shared.open(t)
    }

    @objc private func revealResult() {
        NSWorkspace.shared.activateFileViewerSelecting([delivered])
    }

    /// 사용자가 .txt를 고친 뒤: 인식 없이 텍스트 → 이조 → PDF를 다시 만들고, 원본 옆 결과 파일을 바꿔치운다.
    @objc private func rebuild() {
        guard let t = result.text else { NSSound.beep(); return }
        spinner.isHidden = false
        spinner.startAnimation(nil)
        summary.stringValue = "다시 만드는 중…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                var r = try SheetOCR.rerun(text: t, semitones: self.semitones)
                r.reviewPNGs = self.result.reviewPNGs     // 확인 그림은 처음 것을 그대로 둔다
                r.text = t
                try? FileManager.default.removeItem(at: self.delivered)
                let made = try SheetOCR.deliver(r, next: self.original, semitones: self.semitones)
                DispatchQueue.main.async {
                    self.result = r
                    self.delivered = made
                    self.load()
                    self.summary.stringValue = "고친 텍스트로 다시 만들었어요 → \(made.lastPathComponent)"
                    self.spinner.stopAnimation(nil); self.spinner.isHidden = true
                    NSSound(named: "Glass")?.play()
                }
            } catch {
                DispatchQueue.main.async {
                    self.summary.stringValue = "실패: \(error.localizedDescription)"
                    self.spinner.stopAnimation(nil); self.spinner.isHidden = true
                    NSSound(named: "Basso")?.play()
                }
            }
        }
    }
}

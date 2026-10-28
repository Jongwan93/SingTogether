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
    private let imageView = ReviewImageView()
    private var popover: NSPopover?
    private var rebuildWork: DispatchWorkItem?
    private let imageScroll = NSScrollView()
    private let spinner = NSProgressIndicator()

    init(original: URL, semitones: Int, result: SheetOCR.Result, delivered: URL) {
        self.original = original
        self.semitones = semitones
        self.result = result
        self.delivered = delivered
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 760),
                         styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "Review: \(original.lastPathComponent) → \(delivered.lastPathComponent)"
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
        let left = labeled("Original — red: notes to check, orange: chords, purple: lyrics · click a note to fix it (green: fixed)", imageScroll)
        imageView.onPick = { [weak self] spot, rect in self?.pick(spot, at: rect) }

        pdfView.autoScales = true
        pdfView.displayMode = .singlePageContinuous
        let right = labeled("Transposed — doubtful notes are red", pdfView)

        split.addArrangedSubview(left)
        split.addArrangedSubview(right)

        summary.font = .systemFont(ofSize: 13, weight: .medium)
        summary.lineBreakMode = .byTruncatingTail
        let fix = NSButton(title: "Edit Text…", target: self, action: #selector(openText))
        fix.toolTip = "Opens the score as text (.txt) in your default editor. Find lines by measure number."
        let redo = NSButton(title: "Rebuild from Text", target: self, action: #selector(rebuild))
        redo.keyEquivalent = "\r"
        let reveal = NSButton(title: "Show Result in Finder", target: self, action: #selector(revealResult))
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
            if imageView.spots.isEmpty, let t = result.text {
                imageView.load(reviewJSON: t.deletingPathExtension().appendingPathExtension("review.json"))
            }
        } else if let img = NSImage(contentsOf: original) {
            imageView.image = img
            imageView.frame = NSRect(origin: .zero, size: img.size)
        }
        pdfView.subviews.filter { $0 is NSTextView }.forEach { $0.removeFromSuperview() }
        if let pdf = result.transposedPDF, let doc = PDFDocument(url: pdf) {
            pdfView.document = doc
        } else if let t = result.transposed, SheetOCR.extensions.contains(t.pathExtension.lowercased()),
                  let img = NSImage(contentsOf: t), let page = PDFPage(image: img) {
            // 코드 차트: 원본 위에서 코드만 바꾼 그림
            let doc = PDFDocument()
            doc.insert(page, at: 0)
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

    /// 그림에서 음표를 클릭: 팝오버를 띄우고, 고칠 때마다 .txt에 쓰고 잠시 뒤 PDF를 다시 만든다
    private func pick(_ spot: ReviewImageView.Spot, at rect: NSRect) {
        guard let textURL = result.text else { return }
        popover?.close()
        let editor = NoteEditorController(spot: spot) { [weak self] edit in
            guard let self, var text = (try? String(contentsOf: textURL, encoding: .utf8)).map(CompactText.init) else { return nil }
            var newName: String? = nil
            var newLength: String? = nil
            switch edit {
            case .step(let d):
                newName = text.shiftStep(measure: spot.measure, index: spot.index, by: d)?.name
            case .accidental(let a):
                newName = text.update(measure: spot.measure, index: spot.index) { var t = $0; if t.step != nil { t.alter = a }; return t }?.name
            case .length(let l):
                newLength = text.update(measure: spot.measure, index: spot.index) { var t = $0; t.length = l; return t }?.length
            case .rest:
                newName = text.update(measure: spot.measure, index: spot.index) { var t = $0; t.step = nil; t.tie = false; return t }?.name
            case .delete:
                text.update(measure: spot.measure, index: spot.index) { _ in nil }
            }
            try? text.text.write(to: textURL, atomically: true, encoding: .utf8)
            if case .delete = edit {
                self.imageView.remove(measure: spot.measure, index: spot.index)
            } else {
                self.imageView.update(measure: spot.measure, index: spot.index, name: newName, duration: newLength)
            }
            self.scheduleRebuild()
            if case .delete = edit { return nil }
            return newName ?? self.imageView.spots.first { $0.measure == spot.measure && $0.index == spot.index }?.name ?? spot.name
        }
        let pop = NSPopover()
        pop.contentViewController = editor
        pop.behavior = .transient
        pop.show(relativeTo: rect, of: imageView, preferredEdge: .maxY)
        popover = pop
    }

    private func scheduleRebuild() {
        rebuildWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.rebuild() }
        rebuildWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9, execute: work)
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
        summary.stringValue = "Rebuilding…"
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
                    self.summary.stringValue = "Rebuilt from the edited text → \(made.lastPathComponent)"
                    self.spinner.stopAnimation(nil); self.spinner.isHidden = true
                    NSSound(named: "Glass")?.play()
                }
            } catch {
                DispatchQueue.main.async {
                    self.summary.stringValue = "Failed: \(error.localizedDescription)"
                    self.spinner.stopAnimation(nil); self.spinner.isHidden = true
                    NSSound(named: "Basso")?.play()
                }
            }
        }
    }
}

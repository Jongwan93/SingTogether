import AppKit
import PDFKit
import Transpose
import UniformTypeIdentifiers

/// 이미지·PDF 악보를 인식한 뒤 띄우는 확인 창.
/// 왼쪽: 원본 위에 확인할 곳 표시 (가장 의심스러운 것부터 Next 키로 돌 수 있고, ▶로 들어 볼 수 있다). 오른쪽: 이조된 악보 PDF.
/// 아래: 요약, 텍스트 고치기(기본 편집기로 .txt 열기), 고친 텍스트로 다시 만들기, 결과 파일 보기, 정답으로 저장(평가 세트).
final class ReviewWindowController: NSWindowController {
    private let original: URL
    private let semitones: Int
    private var result: SheetOCR.Result
    private var delivered: URL
    private var extraPages: [URL]         // 그림 결과가 여러 쪽일 때 둘째 쪽부터 (다시 만들 때 같이 지운다)
    private let split = NSSplitView()
    private var splitRatio: CGFloat = 0.5 // 왼쪽(원본) 너비 비율
    private var pendingTop = true         // 처음 맞출 때 악보 맨 위가 보이게 (뒤집히지 않은 뷰라 기본은 아래쪽이 보인다)

    private let summary = NSTextField(labelWithString: "")
    private let pdfView = MarkablePDFView()
    private var marks: [[String: Any]] = []    // 제자리 이조: 사용자가 결과에 찍은 고칠 곳 (원본 그림 픽셀 좌표)
    private let applyMarksButton = NSButton(title: "Fix Marked Places", target: nil, action: nil)
    private let clearMarksButton = NSButton(title: "Clear Marks", target: nil, action: nil)
    private var resultPixelWidth: CGFloat = 0
    private let imageView = ReviewImageView()
    private var popover: NSPopover?
    private var rebuildWork: DispatchWorkItem?
    private let imageScroll = NSScrollView()
    private let spinner = NSProgressIndicator()
    private var history: [(text: String, spots: [ReviewImageView.Spot], edited: Set<String>)] = []
    private let undoButton = NSButton(title: "Undo", target: nil, action: nil)
    private let fontPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private var chartFont: String?        // 코드 차트: 사용자가 고른 글꼴 (nil = 자동)
    private let leftLabel = NSTextField(labelWithString: "")
    private let nextButton = NSButton(title: "Next doubtful", target: nil, action: nil)
    private let playButton = NSButton(title: "▶ Play", target: nil, action: nil)
    private let tempoPopup = NSPopUpButton(frame: .zero, pullsDown: false)
    private let truthButton = NSButton(title: "Save as Truth…", target: nil, action: nil)
    private let fixButton = NSButton(title: "Edit Text…", target: nil, action: nil)
    private let rebuildButton = NSButton(title: "Rebuild from Text", target: nil, action: nil)
    private let rightLabel = NSTextField(labelWithString: "Transposed — doubtful notes are red")
    private var lastPicked: ReviewImageView.Spot?

    init(original: URL, semitones: Int, result: SheetOCR.Result, delivered: URL, extraPages: [URL] = []) {
        self.original = original
        self.semitones = semitones
        self.result = result
        self.delivered = delivered
        self.extraPages = extraPages
        // 화면을 가득 채워 연다: 왼쪽 반은 원본, 오른쪽 반은 이조한 악보 (마우스가 있는 화면)
        let screen = NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) } ?? NSScreen.main
        let frame = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1180, height: 760)
        let w = NSWindow(contentRect: frame, styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "Review: \(original.lastPathComponent) → \(delivered.lastPathComponent)"
        w.minSize = NSSize(width: 860, height: 500)
        super.init(window: w)
        build()
        load()
        NotificationCenter.default.addObserver(self, selector: #selector(windowResized(_:)), name: NSWindow.didResizeNotification, object: w)
        NotificationCenter.default.addObserver(self, selector: #selector(userMagnified(_:)), name: NSScrollView.didEndLiveMagnifyNotification, object: imageScroll)
    }
    required init?(coder: NSCoder) { fatalError() }

    deinit { MelodyPlayer.shared.stop() }

    private func build() {
        guard let content = window?.contentView else { return }
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
        leftLabel.font = .systemFont(ofSize: 11)
        leftLabel.textColor = .secondaryLabelColor
        leftLabel.lineBreakMode = .byTruncatingTail
        leftLabel.setContentHuggingPriority(.defaultLow, for: .horizontal)
        leftLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        nextButton.target = self; nextButton.action = #selector(nextDoubtful)
        nextButton.keyEquivalent = "n"
        nextButton.toolTip = "Jump to the next most doubtful note that is not fixed yet (N)"
        playButton.target = self; playButton.action = #selector(togglePlay)
        playButton.keyEquivalent = " "
        playButton.toolTip = "Play the melody as it was read, from the start (space). Click a note to hear just that note."
        for t in MelodyPlayer.tempos { tempoPopup.addItem(withTitle: "♩ = \(Int(t))") }
        tempoPopup.selectItem(at: MelodyPlayer.tempos.firstIndex(of: MelodyPlayer.tempo) ?? 2)
        tempoPopup.target = self; tempoPopup.action = #selector(tempoChanged)
        for b in [nextButton, playButton, tempoPopup] { b.controlSize = .small; b.font = .systemFont(ofSize: 11) }
        let leftBar = NSStackView(views: [leftLabel, nextButton, playButton, tempoPopup])
        leftBar.orientation = .horizontal
        leftBar.spacing = 8
        let left = labeled(leftBar, imageScroll)
        imageView.onPick = { [weak self] spot, rect in self?.pick(spot, at: rect) }

        pdfView.autoScales = true
        pdfView.displayMode = .singlePageContinuous
        let right = labeled(rightLabel, pdfView)

        split.addArrangedSubview(left)
        split.addArrangedSubview(right)

        summary.font = .systemFont(ofSize: 13, weight: .medium)
        summary.lineBreakMode = .byTruncatingTail
        let fix = fixButton
        fix.target = self; fix.action = #selector(openText)
        fix.toolTip = "Opens the score as text (.txt) in your default editor. Find lines by measure number."
        let redo = rebuildButton
        redo.target = self; redo.action = #selector(rebuild)
        redo.keyEquivalent = "\r"
        let reveal = NSButton(title: "Show Result in Finder", target: self, action: #selector(revealResult))
        undoButton.target = self
        undoButton.action = #selector(undo(_:))
        undoButton.toolTip = "Undo the last click-fix (⌘Z)"
        undoButton.isEnabled = false
        truthButton.target = self
        truthButton.action = #selector(saveTruth)
        truthButton.toolTip = "When every note, chord and syllable is right: save this score as ground truth for singtogether-eval (tools/ocr/truth/)"
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isHidden = true
        // 코드 차트: 코드 글꼴을 직접 고를 수 있다 (자동 판별이 세리프 글꼴을 잘 못 알아본다)
        fontPopup.addItem(withTitle: "Font: Automatic")
        fontPopup.addItems(withTitles: SheetOCR.chartFonts)
        fontPopup.target = self
        fontPopup.action = #selector(fontChanged)
        fontPopup.isHidden = !result.isChordSheet
        applyMarksButton.target = self; applyMarksButton.action = #selector(applyMarks)
        applyMarksButton.toolTip = "Transpose again with the places you marked on the result corrected"
        clearMarksButton.target = self; clearMarksButton.action = #selector(clearMarks)
        for b in [applyMarksButton, clearMarksButton] { b.isHidden = true }
        pdfView.onClick = { [weak self] point in self?.resultClicked(at: point) }
        pdfView.onDrag = { [weak self] rect in self?.resultDragged(over: rect) }
        let bar = NSStackView(views: [summary, NSView(), spinner, fontPopup, undoButton, fix, redo, truthButton, clearMarksButton, applyMarksButton, reveal])
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
        // 반반으로 나누고, 창 크기가 바뀌어도 그 비율을 지킨다 (delegate). 손으로 옮긴 비율은 기억한다.
        // 레이아웃이 잡힐 때마다 악보를 왼쪽 너비에 맞춘다 (창을 만들 때는 아직 크기가 0이라 그때 맞추면 최소 배율이 된다)
        split.delegate = self
        MelodyPlayer.shared.onEvent = { [weak self] e in self?.playing(e) }
    }

    private func labeled(_ header: NSView, _ view: NSView) -> NSView {
        if let label = header as? NSTextField {
            label.font = .systemFont(ofSize: 11)
            label.textColor = .secondaryLabelColor
        }
        let stack = NSStackView(views: [header, view])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.edgeInsets = NSEdgeInsets(top: 8, left: 10, bottom: 0, right: 10)
        for v in [header, view] {
            v.translatesAutoresizingMaskIntoConstraints = false
            v.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -20).isActive = true
        }
        return stack
    }

    private func load() {
        summary.stringValue = "\(result.summary)   ·   \(result.keys ?? "")   ·   \(result.engine)"
        if let f = result.chartFont, chartFont == nil, let i = SheetOCR.chartFonts.firstIndex(of: f) {
            fontPopup.selectItem(at: i + 1)
            fontPopup.itemArray[0].title = "Font: Automatic (\(f))"
        }
        // 표시 없는 쪽 그림이 있으면 앱이 직접 표시를 그린다 (의심 순서 강조, 고친 것은 표시가 사라진다). 없으면 그려진 review.png.
        let clean = result.pagePNGs.first.flatMap { NSImage(contentsOf: $0) }
        if let img = clean ?? result.reviewPNGs.first.flatMap({ NSImage(contentsOf: $0) }) {
            imageView.drawsFlags = clean != nil
            imageView.image = img
            imageView.frame = NSRect(origin: .zero, size: img.size)
            fitToWidth = true
            pendingTop = true
            if applyFit(toTop: true) { pendingTop = false }
            if imageView.spots.isEmpty, let t = result.text {
                imageView.load(reviewJSON: result.reviewJSON ?? t.deletingPathExtension().appendingPathExtension("review.json"))
            }
        } else if let img = NSImage(contentsOf: original) {
            imageView.image = img
            imageView.frame = NSRect(origin: .zero, size: img.size)
        }
        let isStaff = !result.isChordSheet && !result.isInPlace && result.text != nil
        nextButton.isHidden = !isStaff
        playButton.isHidden = !isStaff
        tempoPopup.isHidden = !isStaff
        for b in [undoButton, fixButton, rebuildButton, truthButton] { b.isHidden = result.isInPlace }   // 제자리 이조: 고칠 인식 자료가 없다
        leftLabel.stringValue = result.isInPlace ? "Original · ⌘+ ⌘− zoom"
            : result.isChordSheet
            ? "Original — orange: chords to check · click any chord or word to fix it (green: fixed)"
            : "Original — red: notes to check (thick: \(imageView.top) most doubtful), orange: chords, purple: lyrics · click to fix (green: fixed) · ⌘+ ⌘− zoom"
        rightLabel.stringValue = result.isInPlace
            ? "Transposed — click a wrong place, or drag over several, to mark it"
            : result.isChordSheet ? "Transposed — chords rewritten on the original" : "Transposed — doubtful notes are red"
        pdfView.subviews.filter { $0 is NSTextView }.forEach { $0.removeFromSuperview() }
        if let pdf = result.transposedPDF, let doc = PDFDocument(url: pdf) {
            pdfView.document = doc
        } else if let t = result.transposed, SheetOCR.extensions.contains(t.pathExtension.lowercased()),
                  let img = NSImage(contentsOf: t), let page = PDFPage(image: img) {
            // 코드 차트: 원본 위에서 코드만 바꾼 그림
            let doc = PDFDocument()
            doc.insert(page, at: 0)
            resultPixelWidth = img.representations.first.map { CGFloat($0.pixelsWide) } ?? img.size.width
            markChecks(on: page, image: img)
            drawMarks(on: page)
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

    /// 제자리 이조의 검수가 고친 곳을 결과 쪽 위에 옅은 초록 상자로 그린다 (의심만 가는 곳은 그리지 않는다: 사용자가 보고 표시한다).
    /// 상자 좌표는 원본 그림 픽셀(위가 0)이라 쪽 크기에 맞춰 바꾸고 위아래를 뒤집는다. 마우스를 올리면 설명이 보인다
    private func markChecks(on page: PDFPage, image: NSImage) {
        guard result.isInPlace, result.checks.contains(where: { $0.fixed && $0.kind != "comment" }) else { return }
        let px = image.representations.first.map { CGFloat($0.pixelsWide) } ?? image.size.width
        let bounds = page.bounds(for: .mediaBox)
        let k = bounds.width / max(1, px)
        for c in result.checks where c.box.count == 4 && c.fixed && c.kind != "comment" {
            let pad: CGFloat = 3
            let r = NSRect(x: CGFloat(c.box[0]) * k - pad, y: bounds.height - CGFloat(c.box[3]) * k - pad,
                           width: CGFloat(c.box[2] - c.box[0]) * k + 2 * pad, height: CGFloat(c.box[3] - c.box[1]) * k + 2 * pad)
            let a = PDFAnnotation(bounds: r, forType: .square, withProperties: nil)
            let border = PDFBorder()
            border.lineWidth = 1
            a.border = border
            a.color = NSColor.systemGreen.withAlphaComponent(0.6)
            let place = "Line \(c.staff)" + (c.measure > 0 ? ", bar \(c.measure)" : "")
            a.contents = "Fixed · \(place): \(c.found)" + (c.expected.isEmpty ? "" : " → \(c.expected)")
            page.addAnnotation(a)
        }
    }

    // MARK: 제자리 이조 — 결과에 고칠 곳 찍기

    /// 결과 쪽 좌표 → 원본 그림 픽셀 (위가 0)
    private func imagePoint(_ p: NSPoint, page: PDFPage) -> NSPoint {
        let b = page.bounds(for: .mediaBox)
        let k = b.width / max(1, resultPixelWidth)
        return NSPoint(x: p.x / k, y: (b.height - p.y) / k)
    }

    private func resultClicked(at pagePoint: NSPoint) {
        guard result.isInPlace, let page = pdfView.document?.page(at: 0), resultPixelWidth > 0 else { return }
        showMarkMenu(at: imagePoint(pagePoint, page: page), box: nil)
    }

    /// 네모로 끌어 고른 곳: 안의 음표·코드를 한꺼번에 고친다
    private func resultDragged(over pageRect: NSRect) {
        guard result.isInPlace, let page = pdfView.document?.page(at: 0), resultPixelWidth > 0 else { return }
        let a = imagePoint(NSPoint(x: pageRect.minX, y: pageRect.maxY), page: page)
        let b = imagePoint(NSPoint(x: pageRect.maxX, y: pageRect.minY), page: page)
        showMarkMenu(at: NSPoint(x: (a.x + b.x) / 2, y: (a.y + b.y) / 2), box: [Double(a.x), Double(a.y), Double(b.x), Double(b.y)])
    }

    private func showMarkMenu(at ip: NSPoint, box: [Double]?) {
        let menu = NSMenu()
        menu.allowsContextMenuPlugIns = false       // 맨 아래 'Services'를 붙이지 않는다
        let many = box != nil
        if box == nil, let n = markIndex(at: ip) {
            menu.addItem(item("Remove This Mark") { [weak self] in self?.removeMark(n) })
            menu.addItem(.separator())
        }
        func add(_ m: [String: Any]) {
            var m = m
            if let box { m["box"] = box }
            addMark(m, at: ip)
        }
        menu.addItem(item("This chord should be…") { [weak self] in self?.askChord { add(["kind": "chord", "text": $0]) } })
        menu.addItem(.separator())
        menu.addItem(item(many ? "Move these notes up a step" : "Move this note up a step") { add(["kind": "shift", "steps": 1]) })
        menu.addItem(item(many ? "Move these notes down a step" : "Move this note down a step") { add(["kind": "shift", "steps": -1]) })
        menu.addItem(item(many ? "These notes were not moved" : "This note was not moved") { add(["kind": "not_moved"]) })
        menu.addItem(item(many ? "These are not notes (rests or symbols)" : "This is not a note (rest or symbol)") { add(["kind": "not_a_note"]) })
        menu.addItem(.separator())
        menu.addItem(item("Comment…") { [weak self] in self?.askComment { add(["kind": "comment", "text": $0]) } })
        if let e = NSApp.currentEvent { NSMenu.popUpContextMenu(menu, with: e, for: pdfView) }
    }

    /// 그 자리에 이미 찍은 표시 (점은 둘레 10 쪽 단위, 네모는 그 안)
    private func markIndex(at ip: NSPoint) -> Int? {
        guard let page = pdfView.document?.page(at: 0) else { return nil }
        let k = page.bounds(for: .mediaBox).width / max(1, resultPixelWidth)
        return marks.lastIndex { m in
            if let b = m["box"] as? [Double], b.count == 4 {
                return Double(ip.x) >= min(b[0], b[2]) && Double(ip.x) <= max(b[0], b[2]) && Double(ip.y) >= min(b[1], b[3]) && Double(ip.y) <= max(b[1], b[3])
            }
            guard let x = m["x"] as? Double, let y = m["y"] as? Double else { return false }
            return hypot(x - Double(ip.x), y - Double(ip.y)) * Double(k) < 10
        }
    }

    private func removeMark(_ n: Int) {
        guard marks.indices.contains(n) else { return }
        marks.remove(at: n)
        if let page = pdfView.document?.page(at: 0) { drawMarks(on: page) }
        updateMarkButtons()
    }

    private final class Action: NSObject {
        let run: () -> Void
        init(_ run: @escaping () -> Void) { self.run = run }
        @objc func fire() { run() }
    }
    private var actions: [Action] = []
    private func item(_ title: String, _ run: @escaping () -> Void) -> NSMenuItem {
        let a = Action(run)
        actions.append(a)
        let i = NSMenuItem(title: title, action: #selector(Action.fire), keyEquivalent: "")
        i.target = a
        return i
    }

    private func askChord(_ done: (String) -> Void) {
        let alert = NSAlert()
        alert.messageText = "What should this chord be?"
        alert.informativeText = "Type the chord in the new key, for example F#m7 or A/C#."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
        alert.accessoryView = field
        alert.addButton(withTitle: "Mark")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let t = field.stringValue.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return }
        done(t)
    }

    /// 무엇이 틀렸는지 말로 적는다. 다시 돌릴 때 AI가 원본과 결과를 보고 고칠 일로 바꾼다
    private func askComment(_ done: (String) -> Void) {
        let alert = NSAlert()
        alert.messageText = "What is wrong here?"
        alert.informativeText = "Say it in your own words, for example \"this note should be one step higher\" or \"the chord should be E7\". "
            + "When you fix the marked places, the AI reader looks at this place on the original and the result and makes the change."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 48))
        field.cell?.wraps = true
        field.cell?.isScrollable = false
        alert.accessoryView = field
        alert.addButton(withTitle: "Mark")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let t = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        done(t)
    }

    private func addMark(_ m: [String: Any], at ip: NSPoint) {
        var m = m
        m["x"] = Double(ip.x)
        m["y"] = Double(ip.y)
        marks.append(m)
        if let page = pdfView.document?.page(at: 0) { drawMarks(on: page) }
        updateMarkButtons()
    }

    private func updateMarkButtons() {
        applyMarksButton.isHidden = marks.isEmpty
        clearMarksButton.isHidden = marks.isEmpty
        applyMarksButton.title = "Fix \(marks.count) Marked Place\(marks.count == 1 ? "" : "s")"
    }

    /// 찍은 곳은 파란 동그라미, 네모로 고른 곳은 파란 네모로 (설명은 마우스를 올리면)
    private func drawMarks(on page: PDFPage) {
        for a in page.annotations where a.userName == "mark" { page.removeAnnotation(a) }
        let b = page.bounds(for: .mediaBox)
        let k = b.width / max(1, resultPixelWidth)
        for m in marks {
            guard let x = m["x"] as? Double, let y = m["y"] as? Double else { continue }
            var r = NSRect(x: CGFloat(x) * k - 9, y: b.height - CGFloat(y) * k - 9, width: 18, height: 18)
            let boxed = (m["box"] as? [Double]).map { $0.count == 4 } ?? false
            if boxed, let q = m["box"] as? [Double] {
                r = NSRect(x: CGFloat(min(q[0], q[2])) * k, y: b.height - CGFloat(max(q[1], q[3])) * k,
                           width: CGFloat(abs(q[2] - q[0])) * k, height: CGFloat(abs(q[3] - q[1])) * k)
            }
            let a = PDFAnnotation(bounds: r, forType: boxed ? .square : .circle, withProperties: nil)
            let border = PDFBorder()
            border.lineWidth = 2.5
            a.border = border
            a.color = .systemBlue
            a.userName = "mark"
            switch m["kind"] as? String {
            case "chord": a.contents = "Chord → \(m["text"] as? String ?? "")"
            case "shift": a.contents = (m["steps"] as? Int ?? 0) > 0 ? "Move up a step" : "Move down a step"
            case "not_moved": a.contents = "Note was not moved"
            case "comment": a.contents = "Comment: \(m["text"] as? String ?? "")"
            default: a.contents = "Not a note"
            }
            page.addAnnotation(a)
        }
    }

    @objc private func clearMarks() {
        marks.removeAll()
        if let page = pdfView.document?.page(at: 0) { drawMarks(on: page) }
        updateMarkButtons()
    }

    @objc private func applyMarks() {
        guard !marks.isEmpty else { return }
        spinner.isHidden = false
        spinner.startAnimation(nil)
        summary.stringValue = "Transposing again with \(marks.count) correction\(marks.count == 1 ? "" : "s")…"
        applyMarksButton.isEnabled = false
        let marks = self.marks
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let r = try SheetOCR.rerunInPlace(self.original, semitones: self.semitones, marks: marks)
                for old in [self.delivered] + self.extraPages { try? FileManager.default.removeItem(at: old) }
                let made = try SheetOCR.deliverAll(r, next: self.original, semitones: self.semitones)
                DispatchQueue.main.async {
                    self.result = r
                    self.delivered = made[0]
                    self.extraPages = Array(made.dropFirst())
                    self.load()                      // 표시는 남겨 둔다: 다음에 또 고치면 처음부터 같이 적용된다
                    self.summary.stringValue = "Fixed \(marks.count) marked place\(marks.count == 1 ? "" : "s") → \(made[0].lastPathComponent)   ·   " + r.summary
                    self.spinner.stopAnimation(nil); self.spinner.isHidden = true
                    self.applyMarksButton.isEnabled = true
                    if AppSettings.playSound { NSSound(named: "Glass")?.play() }
                }
            } catch {
                DispatchQueue.main.async {
                    self.summary.stringValue = "Failed: \(error.localizedDescription)"
                    self.spinner.stopAnimation(nil); self.spinner.isHidden = true
                    self.applyMarksButton.isEnabled = true
                    NSSound(named: "Basso")?.play()
                }
            }
        }
    }

    private func readText() -> CompactText? {
        guard let t = result.text, let s = try? String(contentsOf: t, encoding: .utf8) else { return nil }
        return CompactText(s)
    }

    /// 그림에서 음표·코드·가사를 클릭: 팝오버를 띄우고, 고칠 때마다 .txt에 쓰고 잠시 뒤 PDF를 다시 만든다
    private func pick(_ spot: ReviewImageView.Spot, at rect: NSRect) {
        guard let textURL = result.text else { return }
        popover?.close()
        MelodyPlayer.shared.stop()
        lastPicked = spot
        imageView.current = spot.key
        let read = { [weak self] in self?.readText() }
        let editor: NSViewController
        switch spot.kind {
        case .note:
            editor = NoteEditorController(spot: spot, chord: read()?.chord(measure: spot.measure, index: spot.index),
                                          playMeasure: { [weak self] in self?.play(measure: spot.measure) }) { [weak self] edit in
                guard let self, var text = read() else { return nil }
                self.snapshot(text.text)
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
                case .chord(let c):
                    text.setChord(measure: spot.measure, index: spot.index, c)
                }
                try? text.text.write(to: textURL, atomically: true, encoding: .utf8)
                switch edit {
                case .delete:
                    self.imageView.remove(measure: spot.measure, index: spot.index)
                    self.imageView.current = nil
                case .chord(let c):
                    self.imageView.update(.chord, measure: spot.measure, index: spot.index, name: c)
                default:
                    self.imageView.update(.note, measure: spot.measure, index: spot.index, name: newName, duration: newLength)
                }
                self.scheduleRebuild()
                if case .delete = edit { return nil }
                return newName ?? self.imageView.spot(measure: spot.measure, index: spot.index)?.name ?? spot.name
            }
        case .chord, .lyric:
            editor = TextEditorController(spot: spot) { [weak self] value in
                guard let self, var text = read() else { return }
                self.snapshot(text.text)
                if spot.kind == .chord {
                    text.setChord(measure: spot.measure, index: spot.index, value)
                } else {
                    text.setLyric(measure: spot.measure, index: spot.index, value ?? "")
                }
                try? text.text.write(to: textURL, atomically: true, encoding: .utf8)
                self.imageView.update(spot.kind, measure: spot.measure, index: spot.index, name: value ?? (spot.kind == .chord ? "" : "_"))
                self.scheduleRebuild()
            }
        }
        let pop = NSPopover()
        pop.contentViewController = editor
        pop.behavior = .transient
        pop.show(relativeTo: rect, of: imageView, preferredEdge: .maxY)
        popover = pop
    }

    /// N: 아직 안 고친 의심 음표 중 다음 것으로 (의심스러운 순서, 끝에 가면 처음으로). 화면에 보이게 하고 편집 팝오버를 연다.
    @objc private func nextDoubtful() {
        let list = imageView.doubtful
        guard !list.isEmpty else {
            summary.stringValue = imageView.spots.isEmpty ? "No review data for this score" : "No doubtful notes left — everything flagged has been fixed"
            NSSound.beep()
            return
        }
        var k = 0
        if let cur = lastPicked, let i = list.firstIndex(where: { $0.key == cur.key }) { k = (i + 1) % list.count }
        else if let cur = lastPicked, let i = list.firstIndex(where: { $0.rank > cur.rank }) { k = i }
        let spot = list[k]
        let rect = imageView.viewRect(spot)
        // 웹 캡처처럼 작은 악보에서는 음표가 깨알만 하다: 오선 간격이 화면에서 10pt는 되게 당겨서 보여 준다
        if imageView.interline > 0, imageView.interline * imageScroll.magnification < 10 {
            fitToWidth = false
            imageScroll.setMagnification(min(imageScroll.maxMagnification, 14 / imageView.interline), centeredAt: NSPoint(x: rect.midX, y: rect.midY))
        }
        imageView.scrollToVisible(rect.insetBy(dx: -120, dy: -120))
        summary.stringValue = "Doubtful note \(k + 1) of \(list.count): measure \(spot.measure), \(spot.name) (confidence \(String(format: "%.2f", spot.conf)))"
        pick(spot, at: rect)
    }

    // MARK: 확대 — 창 너비에 맞추되(창을 키우면 따라 커진다), 손으로 확대하면 그 배율을 둔다. View 메뉴: ⌘= ⌘- ⌘0

    private var fitToWidth = true

    private func fitMagnification() -> CGFloat {
        guard let img = imageView.image, img.size.width > 0 else { return 1 }
        return max(imageScroll.minMagnification, min(imageScroll.maxMagnification, (imageScroll.bounds.width - 4) / img.size.width))
    }

    /// 너비에 맞춘다. 레이아웃 전(너비 0)이면 아무것도 안 하고 false.
    @discardableResult
    private func applyFit(toTop: Bool = false) -> Bool {
        guard fitToWidth, imageScroll.bounds.width > 10, imageView.image != nil else { return false }
        imageScroll.magnification = fitMagnification()
        if toTop { scrollToTop() }
        return true
    }

    private func scrollToTop() {
        let clip = imageScroll.contentView
        clip.scroll(to: NSPoint(x: 0, y: max(0, imageView.bounds.height - clip.bounds.height)))
        imageScroll.reflectScrolledClipView(clip)
    }

    @objc private func windowResized(_ note: Notification) { applyFit() }
    @objc private func userMagnified(_ note: Notification) { fitToWidth = false }

    @objc func zoomIn(_ sender: Any?) { zoom(to: imageScroll.magnification * 1.25) }
    @objc func zoomOut(_ sender: Any?) { zoom(to: imageScroll.magnification / 1.25) }
    @objc func zoomToFit(_ sender: Any?) { fitToWidth = true; applyFit() }

    private func zoom(to m: CGFloat) {
        fitToWidth = false
        let visible = imageScroll.contentView.bounds          // 문서 좌표의 보이는 영역 (배율이 반영돼 있다)
        imageScroll.setMagnification(max(imageScroll.minMagnification, min(imageScroll.maxMagnification, m)),
                                     centeredAt: NSPoint(x: visible.midX, y: visible.midY))
    }

    // MARK: 재생

    @objc private func togglePlay() {
        if MelodyPlayer.shared.isPlaying { MelodyPlayer.shared.stop(); return }
        popover?.close()
        play(from: nil)
    }

    private func play(measure: Int) {
        popover?.close()
        guard let events = readText()?.events().filter({ $0.measure == measure }), !events.isEmpty else { NSSound.beep(); return }
        MelodyPlayer.shared.play(events)
    }

    private func play(from measure: Int?) {
        guard let events = readText()?.events(), !events.isEmpty else { NSSound.beep(); return }
        let fromHere = measure.map { m in events.filter { $0.measure >= m } } ?? events
        MelodyPlayer.shared.play(fromHere)
    }

    @objc private func tempoChanged() {
        MelodyPlayer.tempo = MelodyPlayer.tempos[tempoPopup.indexOfSelectedItem]
    }

    /// 울리는 음표를 그림에 표시하고 보이게 스크롤한다. nil = 끝났다.
    private func playing(_ e: CompactText.Event?) {
        playButton.title = e == nil ? "▶ Play" : "■ Stop"
        guard let e else { imageView.playing = nil; return }
        imageView.playing = e.key
        if let s = imageView.spot(measure: e.measure, index: e.index) {
            imageView.scrollToVisible(imageView.viewRect(s).insetBy(dx: -200, dy: -150))
        }
    }

    // MARK: 정답 저장

    /// 다 고친 악보를 평가 세트의 정답(tools/ocr/truth/<이름>.txt)으로 저장한다. 저장소 안에서 돌릴 때는 그 폴더를 기본으로 열고,
    /// 같은 이름의 악보가 music_scores/에 없으면 원본 그림도 복사한다 (singtogether-eval이 이름으로 짝짓는다).
    @objc private func saveTruth() {
        guard let t = result.text, let text = try? String(contentsOf: t, encoding: .utf8), let window else { NSSound.beep(); return }
        popover?.close()
        let panel = NSSavePanel()
        panel.title = "Save as Ground Truth"
        panel.message = "Saves the corrected score as ground truth for singtogether-eval. Make sure every note, chord and syllable is right first."
        panel.nameFieldStringValue = original.deletingPathExtension().lastPathComponent + ".txt"
        panel.allowedContentTypes = [.plainText]
        panel.canCreateDirectories = true
        if let dir = SheetOCR.truthDirectory(chordSheet: result.isChordSheet) { panel.directoryURL = dir }
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .OK, let url = panel.url else { return }
            do {
                try text.write(to: url, atomically: true, encoding: .utf8)
                var note = "Saved as ground truth → \(url.lastPathComponent)"
                if let copied = try SheetOCR.copyToScores(self.original, truth: url) {
                    note += " · image copied to music_scores/\(copied.lastPathComponent)"
                }
                self.summary.stringValue = note
                if AppSettings.playSound { NSSound(named: "Glass")?.play() }
            } catch {
                self.summary.stringValue = "Could not save the truth file: \(error.localizedDescription)"
                NSSound(named: "Basso")?.play()
            }
        }
    }

    /// 클릭 수정 전의 텍스트와 표시 상태를 쌓아 둔다 (⌘Z / Undo 버튼).
    private func snapshot(_ text: String) {
        history.append((text, imageView.spots, imageView.edited))
        if history.count > 50 { history.removeFirst() }
        undoButton.isEnabled = true
    }

    @objc func undo(_ sender: Any?) {
        guard let textURL = result.text, let last = history.popLast() else { NSSound.beep(); return }
        popover?.close()
        try? last.text.write(to: textURL, atomically: true, encoding: .utf8)
        imageView.spots = last.spots
        imageView.edited = last.edited
        imageView.needsDisplay = true
        undoButton.isEnabled = !history.isEmpty
        scheduleRebuild()
    }

    private func scheduleRebuild() {
        rebuildWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.rebuild() }
        rebuildWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9, execute: work)
    }

    @objc private func fontChanged() {
        let i = fontPopup.indexOfSelectedItem
        chartFont = i == 0 ? nil : SheetOCR.chartFonts[i - 1]
        rebuild()
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
                var r = try SheetOCR.rerun(text: t, semitones: self.semitones,
                                           source: self.result.isChordSheet ? self.original : nil, font: self.chartFont)
                r.reviewPNGs = self.result.reviewPNGs     // 확인 그림과 자리 정보는 처음 것을 그대로 둔다
                r.pagePNGs = self.result.pagePNGs
                r.reviewJSON = self.result.reviewJSON
                r.text = t
                for old in [self.delivered] + self.extraPages { try? FileManager.default.removeItem(at: old) }
                let made = try SheetOCR.deliverAll(r, next: self.original, semitones: self.semitones)
                DispatchQueue.main.async {
                    self.result = r
                    self.delivered = made[0]
                    self.extraPages = Array(made.dropFirst())
                    self.load()
                    self.summary.stringValue = "Rebuilt from the edited text → \(made[0].lastPathComponent)"
                    self.spinner.stopAnimation(nil); self.spinner.isHidden = true
                    if AppSettings.playSound { NSSound(named: "Glass")?.play() }
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

/// 반반 분할: 창 크기가 바뀌어도 비율을 지키고, 레이아웃이 잡힐 때마다 악보를 왼쪽 너비에 맞춘다.
extension ReviewWindowController: NSSplitViewDelegate {
    func splitView(_ sv: NSSplitView, resizeSubviewsWithOldSize oldSize: NSSize) {
        guard sv.subviews.count == 2 else { return }
        let total = sv.bounds.width - sv.dividerThickness
        let left = (total * splitRatio).rounded()
        sv.subviews[0].frame = NSRect(x: 0, y: 0, width: left, height: sv.bounds.height)
        sv.subviews[1].frame = NSRect(x: left + sv.dividerThickness, y: 0, width: total - left, height: sv.bounds.height)
    }

    func splitViewDidResizeSubviews(_ notification: Notification) {
        let total = split.bounds.width - split.dividerThickness
        if total > 0, let left = split.subviews.first { splitRatio = left.frame.width / total }
        if applyFit(toTop: pendingTop) { pendingTop = false }
    }
}


/// 결과 쪽을 누르면 그 자리(쪽 좌표)를 알려 주는 PDFView. 링크·주석 기본 동작은 그대로
final class MarkablePDFView: PDFView {
    var onClick: ((NSPoint) -> Void)?
    var onDrag: ((NSRect) -> Void)?           // 쪽 좌표의 네모
    private var start: NSPoint?
    private let band = NSView()

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        guard onClick != nil, page(for: p, nearest: false) != nil else { return super.mouseDown(with: event) }
        start = p
    }

    override func mouseDragged(with event: NSEvent) {
        guard let s = start else { return super.mouseDragged(with: event) }
        let p = convert(event.locationInWindow, from: nil)
        if band.superview == nil {
            band.wantsLayer = true
            band.layer?.borderColor = NSColor.systemBlue.cgColor
            band.layer?.borderWidth = 1.5
            band.layer?.backgroundColor = NSColor.systemBlue.withAlphaComponent(0.1).cgColor
            addSubview(band)
        }
        band.frame = NSRect(x: min(s.x, p.x), y: min(s.y, p.y), width: abs(p.x - s.x), height: abs(p.y - s.y))
    }

    override func mouseUp(with event: NSEvent) {
        guard let s = start else { return super.mouseUp(with: event) }
        start = nil
        let p = convert(event.locationInWindow, from: nil)
        let dragged = band.superview != nil && hypot(p.x - s.x, p.y - s.y) > 6
        band.removeFromSuperview()
        guard let page = page(for: s, nearest: true) else { return }
        if dragged, let onDrag {
            let a = convert(s, to: page), b = convert(p, to: page)
            onDrag(NSRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(b.x - a.x), height: abs(b.y - a.y)))
        } else {
            onClick?(convert(s, to: page))
        }
    }
}

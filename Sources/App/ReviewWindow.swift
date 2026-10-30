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

    private let summary = NSTextField(labelWithString: "")
    private let pdfView = PDFView()
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
    private var lastPicked: ReviewImageView.Spot?

    init(original: URL, semitones: Int, result: SheetOCR.Result, delivered: URL, extraPages: [URL] = []) {
        self.original = original
        self.semitones = semitones
        self.result = result
        self.delivered = delivered
        self.extraPages = extraPages
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 760),
                         styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        w.title = "Review: \(original.lastPathComponent) → \(delivered.lastPathComponent)"
        w.minSize = NSSize(width: 860, height: 500)
        super.init(window: w)
        build()
        load()
    }
    required init?(coder: NSCoder) { fatalError() }

    deinit { MelodyPlayer.shared.stop() }

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
        let right = labeled(NSTextField(labelWithString: "Transposed — doubtful notes are red"), pdfView)

        split.addArrangedSubview(left)
        split.addArrangedSubview(right)

        summary.font = .systemFont(ofSize: 13, weight: .medium)
        summary.lineBreakMode = .byTruncatingTail
        let fix = NSButton(title: "Edit Text…", target: self, action: #selector(openText))
        fix.toolTip = "Opens the score as text (.txt) in your default editor. Find lines by measure number."
        let redo = NSButton(title: "Rebuild from Text", target: self, action: #selector(rebuild))
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
        let bar = NSStackView(views: [summary, NSView(), spinner, fontPopup, undoButton, fix, redo, truthButton, reveal])
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
            imageScroll.magnification = min(1, (imageScroll.bounds.width - 4) / max(1, img.size.width))
            if imageView.spots.isEmpty, let t = result.text {
                imageView.load(reviewJSON: result.reviewJSON ?? t.deletingPathExtension().appendingPathExtension("review.json"))
            }
        } else if let img = NSImage(contentsOf: original) {
            imageView.image = img
            imageView.frame = NSRect(origin: .zero, size: img.size)
        }
        let isStaff = !result.isChordSheet && result.text != nil
        nextButton.isHidden = !isStaff
        playButton.isHidden = !isStaff
        tempoPopup.isHidden = !isStaff
        leftLabel.stringValue = result.isChordSheet
            ? "Original — orange: chords to check · click any chord or word to fix it (green: fixed)"
            : "Original — red: notes to check (thick: \(imageView.top) most doubtful), orange: chords, purple: lyrics · click to fix (green: fixed)"
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
        imageView.scrollToVisible(rect.insetBy(dx: -120, dy: -120))
        summary.stringValue = "Doubtful note \(k + 1) of \(list.count): measure \(spot.measure), \(spot.name) (confidence \(String(format: "%.2f", spot.conf)))"
        pick(spot, at: rect)
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

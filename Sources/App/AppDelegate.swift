import AppKit
import Transpose

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let panel = RingPanel()
    private let ring = RingView(frame: NSRect(x: 0, y: 0, width: RingView.size, height: RingView.size))

    private var timer: Timer?
    private var mouseWasDown = false
    private var dragStartCount = 0
    private var ringShown = false
    private var hideWork: DispatchWorkItem?
    private var reviewWindows: [ReviewWindowController] = []
    private var busy = false
    private let readerItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "music.note", accessibilityDescription: "SingTogether")

        let menu = NSMenu()
        let info = NSMenuItem(title: "Hold Shift while dragging a score file", action: nil, keyEquivalent: "")
        info.isEnabled = false
        menu.addItem(info)
        let formats = NSMenuItem(title: "Supports .txt .cho .chordpro .musicxml .xml .mxl · images and PDF", action: nil, keyEquivalent: "")
        formats.isEnabled = false
        menu.addItem(formats)
        menu.addItem(.separator())
        readerItem.action = #selector(chooseReader)
        readerItem.target = self
        menu.addItem(readerItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit SingTogether", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        statusItem.menu = menu

        panel.contentView = ring
        ring.onDrop = { [weak self] semis, urls in self?.handleDrop(semis: semis, urls: urls) }

        let t = Timer(timeInterval: 1.0 / 60.0, target: self, selector: #selector(tick), userInfo: nil, repeats: true)
        RunLoop.main.add(t, forMode: .common)
        timer = t

        updateReaderItem()
        // 메뉴 막대에 아이콘이 뜬 뒤 글자를 읽을 AI(Claude / Gemini)를 고르게 한다
        DispatchQueue.main.async { [weak self] in self?.chooseReader() }

        // 개발용: SingTogether --sheet <이미지> <반음> 으로 띄우면 드래그 없이 인식 → 확인 창까지 간다
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--sheet"), i + 2 < args.count, let s = Int(args[i + 2]) {
            panel.show(centeredAt: NSEvent.mouseLocation)
            ringShown = true
            handleSheetDrop(semis: s, url: URL(fileURLWithPath: args[i + 1]))
        }
    }

    /// 드래그 상태 감시: 마우스가 눌린 상태에서 드래그 페이스트보드가 바뀌면 = 무언가 드래그 중
    @objc private func tick() {
        let down = (NSEvent.pressedMouseButtons & 1) != 0
        let pb = NSPasteboard(name: .drag)

        if down && !mouseWasDown { dragStartCount = pb.changeCount }
        if !down && mouseWasDown && ringShown && ring.message == nil {
            scheduleHide(after: 0.35)   // 링 밖에 놓았을 때. 링 안에 놓으면 handleDrop이 메시지로 대체
        }
        mouseWasDown = down

        guard down, !ringShown, pb.changeCount != dragStartCount else { return }

        let flags = CGEventSource.flagsState(.combinedSessionState)
        guard flags.contains(.maskShift) else { return }   // Shift 또는 ⌘⇧

        let urls = (pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
        let supported = urls.filter { Converter.kind(of: $0) != nil }
        guard let first = supported.first else { return }

        hideWork?.cancel()
        ring.message = nil
        ring.hovered = nil
        ring.keyInfo = Converter.keyInfo(for: first)
        panel.show(centeredAt: NSEvent.mouseLocation)
        ringShown = true
    }

    private func handleDrop(semis: Int, urls: [URL]) {
        if let sheet = urls.first(where: { Converter.kind(of: $0) == .sheet }) {
            handleSheetDrop(semis: semis, url: sheet)
            return
        }
        var made: [String] = []
        var errors: [String] = []
        for url in urls where Converter.kind(of: url) != nil {
            do { made.append(try Converter.convert(url, semitones: semis).lastPathComponent) }
            catch { errors.append("\(url.lastPathComponent): \(error.localizedDescription)") }
        }
        if errors.isEmpty && !made.isEmpty {
            ring.message = "Created\n\(made[0])" + (made.count > 1 ? "\nand \(made.count - 1) more" : "")
            NSSound(named: "Glass")?.play()
        } else if !errors.isEmpty {
            ring.message = "Transposition failed\n" + errors.joined(separator: "\n")
            NSSound(named: "Basso")?.play()
        } else {
            ring.message = "This file type is not supported"
        }
        scheduleHide(after: errors.isEmpty ? 1.6 : 3.5)
    }

    /// 이미지·PDF: 인식에 10초~1분이 걸리므로 링에 진행 중 표시를 두고 백그라운드로 돌린 뒤 확인 창을 띄운다.
    private func handleSheetDrop(semis: Int, url: URL) {
        guard !busy else { ring.message = "Still reading the previous score"; scheduleHide(after: 2); return }
        guard SheetOCR.locate() != nil else {
            ring.message = "Score reader not installed\n(run uv sync in tools/ocr)"
            NSSound(named: "Basso")?.play()
            scheduleHide(after: 4)
            return
        }
        busy = true
        hideWork?.cancel()
        ring.message = "Reading the score…\n\(url.lastPathComponent)\n(10 s to 1 min)"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let r = try SheetOCR.run(url, semitones: semis)
                let made = try SheetOCR.deliver(r, next: url, semitones: semis)
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.busy = false
                    self.ring.message = "Created\n\(made.lastPathComponent)\n\(r.summary)"
                    NSSound(named: "Glass")?.play()
                    self.scheduleHide(after: 2.5)
                    let wc = ReviewWindowController(original: url, semitones: semis, result: r, delivered: made)
                    self.reviewWindows.append(wc)
                    wc.window?.center()
                    wc.showWindow(nil)
                    NSApp.activate(ignoringOtherApps: true)
                    wc.window?.makeKeyAndOrderFront(nil)
                }
            } catch {
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.busy = false
                    self.ring.message = "Could not read the score\n\(error.localizedDescription)"
                    NSSound(named: "Basso")?.play()
                    self.scheduleHide(after: 5)
                }
            }
        }
    }

    private func scheduleHide(after delay: TimeInterval) {
        hideWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.panel.orderOut(nil)
            self.ringShown = false
            self.ring.message = nil
            self.ring.hovered = nil
        }
        hideWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }
}

/// 시작할 때와 메뉴에서: 이미지 악보의 코드·가사를 Claude와 Gemini 중 무엇으로 읽을지 버튼으로 고른다.
extension AppDelegate {
    @objc func chooseReader() {
        let choices = SheetOCR.llmChoices
        let installed = choices.map { SheetOCR.isInstalled($0.id) }
        let alert = NSAlert()
        alert.messageText = "Which AI should read your scores?"
        alert.informativeText = "Chords and lyrics in image scores are read by an AI that is signed in on this Mac. You can change this later from the ♪ menu."
            + (installed.contains(false) ? "\nOptions that are not installed are disabled." : "")
        for (c, ok) in zip(choices, installed) {
            let button = alert.addButton(withTitle: c.label + (ok ? "" : " (not installed)"))
            button.isEnabled = ok
            button.keyEquivalent = c.id == SheetOCR.llm ? "\r" : ""
        }
        NSApp.activate(ignoringOtherApps: true)
        let index = alert.runModal().rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
        if choices.indices.contains(index) { SheetOCR.llm = choices[index].id }
        updateReaderItem()
    }

    func updateReaderItem() {
        let name = SheetOCR.llmChoices.first { $0.id == SheetOCR.llm }?.label ?? "Apple Vision"
        readerItem.title = "Reading with \(name) — Change…"
    }
}

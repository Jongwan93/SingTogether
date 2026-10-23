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

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "music.note", accessibilityDescription: "SingTogether")

        let menu = NSMenu()
        let info = NSMenuItem(title: "악보 파일을 드래그하면서 Shift를 누르세요", action: nil, keyEquivalent: "")
        info.isEnabled = false
        menu.addItem(info)
        let formats = NSMenuItem(title: "지원: .txt .cho .chordpro .musicxml .xml .mxl", action: nil, keyEquivalent: "")
        formats.isEnabled = false
        menu.addItem(formats)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "SingTogether 종료", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        statusItem.menu = menu

        panel.contentView = ring
        ring.onDrop = { [weak self] semis, urls in self?.handleDrop(semis: semis, urls: urls) }

        let t = Timer(timeInterval: 1.0 / 60.0, target: self, selector: #selector(tick), userInfo: nil, repeats: true)
        RunLoop.main.add(t, forMode: .common)
        timer = t
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
        var made: [String] = []
        var errors: [String] = []
        for url in urls where Converter.kind(of: url) != nil {
            do { made.append(try Converter.convert(url, semitones: semis).lastPathComponent) }
            catch { errors.append("\(url.lastPathComponent): \(error.localizedDescription)") }
        }
        if errors.isEmpty && !made.isEmpty {
            ring.message = "만들었어요\n\(made[0])" + (made.count > 1 ? "\n외 \(made.count - 1)개" : "")
            NSSound(named: "Glass")?.play()
        } else if !errors.isEmpty {
            ring.message = "변환 실패\n" + errors.joined(separator: "\n")
            NSSound(named: "Basso")?.play()
        } else {
            ring.message = "지원하지 않는 파일이에요"
        }
        scheduleHide(after: errors.isEmpty ? 1.6 : 3.5)
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

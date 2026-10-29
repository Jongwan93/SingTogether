import AppKit
import ServiceManagement
import Transpose

/// 사용자 설정 (UserDefaults). 기본값은 전부 "켜짐".
enum AppSettings {
    private static let d = UserDefaults.standard
    static var askAtLaunch: Bool { get { d.object(forKey: "askAtLaunch") as? Bool ?? true } set { d.set(newValue, forKey: "askAtLaunch") } }
    static var showReview: Bool { get { d.object(forKey: "showReview") as? Bool ?? true } set { d.set(newValue, forKey: "showReview") } }
    static var playSound: Bool { get { d.object(forKey: "playSound") as? Bool ?? true } set { d.set(newValue, forKey: "playSound") } }

    /// 로그인할 때 열기. macOS 13부터는 시스템 API, 그 아래는 System Events로 로그인 항목을 넣는다.
    static var openAtLogin: Bool {
        get {
            if #available(macOS 13, *) { return SMAppService.mainApp.status == .enabled }
            return loginItemScript("exists login item \"SingTogether\"") == "true"
        }
        set {
            if #available(macOS 13, *) {
                do { newValue ? try SMAppService.mainApp.register() : try SMAppService.mainApp.unregister() } catch { NSSound.beep() }
                return
            }
            if newValue {
                _ = loginItemScript("make login item at end with properties {path:\"\(Bundle.main.bundlePath)\", hidden:false}")
            } else {
                _ = loginItemScript("delete login item \"SingTogether\"")
            }
        }
    }

    private static func loginItemScript(_ body: String) -> String? {
        NSAppleScript(source: "tell application \"System Events\" to \(body)")?.executeAndReturnError(nil).stringValue
    }

    static var cacheDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Caches/SingTogether")
    }

    static func cacheSize() -> Int64 {
        guard let e = FileManager.default.enumerator(at: cacheDirectory, includingPropertiesForKeys: [.fileSizeKey]) else { return 0 }
        var total: Int64 = 0
        for case let f as URL in e { total += Int64((try? f.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
        return total
    }

    static func clearCache() {
        try? FileManager.default.removeItem(at: cacheDirectory)
    }
}

/// 설정 창 (♪ 메뉴 → Settings…, ⌘,). 로그인할 때 열기, 확인 창·소리, 글자를 읽을 AI와 로그인 상태, 인식 캐시.
final class SettingsWindowController: NSWindowController {
    static let shared = SettingsWindowController()
    var onReaderChange: (() -> Void)?

    private let login = NSButton(checkboxWithTitle: "Open SingTogether at login", target: nil, action: nil)
    private let ask = NSButton(checkboxWithTitle: "Ask which AI to use when the app starts", target: nil, action: nil)
    private let review = NSButton(checkboxWithTitle: "Open the review window after reading an image score", target: nil, action: nil)
    private let sound = NSButton(checkboxWithTitle: "Play a sound when a file is created", target: nil, action: nil)
    private var readers: [NSButton] = []
    private let status = NSTextField(wrappingLabelWithString: "")
    private let checkButton = NSButton(title: "Check sign-in", target: nil, action: nil)
    private let cacheLabel = NSTextField(labelWithString: "")
    private let toolLabel = NSTextField(wrappingLabelWithString: "")

    private init() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 10), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "SingTogether Settings"
        w.isReleasedWhenClosed = false
        super.init(window: w)
        build()
    }
    required init?(coder: NSCoder) { fatalError() }

    private func build() {
        for (b, sel) in [(login, #selector(loginChanged)), (ask, #selector(askChanged)), (review, #selector(reviewChanged)), (sound, #selector(soundChanged))] {
            b.target = self; b.action = sel
        }
        let choices = SheetOCR.llmChoices.map { ($0.id, $0.label) } + [("none", "Apple Vision only (no AI, less accurate)")]
        readers = choices.map { id, label in
            let b = NSButton(radioButtonWithTitle: label, target: self, action: #selector(readerChanged(_:)))
            b.identifier = NSUserInterfaceItemIdentifier(id)
            return b
        }
        status.font = .systemFont(ofSize: 11)
        status.textColor = .secondaryLabelColor
        checkButton.target = self; checkButton.action = #selector(checkSignIn)
        toolLabel.font = .systemFont(ofSize: 11)
        toolLabel.textColor = .secondaryLabelColor
        let clear = NSButton(title: "Clear cache", target: self, action: #selector(clearCache))
        let cacheRow = NSStackView(views: [cacheLabel, clear]); cacheRow.spacing = 10

        let general = section("General", [login, ask, review, sound])
        let reader = section("Reading image scores", readers + [NSStackView(views: [status, checkButton]), toolLabel])
        let cache = section("Recognition cache", [NSTextField(wrappingLabelWithString: "Recognized scores are remembered by file content, so the same image is instant next time."), cacheRow])
        let root = NSStackView(views: [general, reader, cache])
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 18
        root.edgeInsets = NSEdgeInsets(top: 18, left: 20, bottom: 20, right: 20)
        root.translatesAutoresizingMaskIntoConstraints = false
        guard let content = window?.contentView else { return }
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            root.widthAnchor.constraint(equalToConstant: 520),
        ])
        for v in [status, toolLabel] { v.widthAnchor.constraint(lessThanOrEqualToConstant: 440).isActive = true }
    }

    private func section(_ title: String, _ views: [NSView]) -> NSView {
        let head = NSTextField(labelWithString: title)
        head.font = .systemFont(ofSize: 13, weight: .semibold)
        let body = NSStackView(views: views)
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 6
        body.edgeInsets = NSEdgeInsets(top: 0, left: 12, bottom: 0, right: 0)
        let s = NSStackView(views: [head, body])
        s.orientation = .vertical
        s.alignment = .leading
        s.spacing = 8
        return s
    }

    func show() {
        refresh()
        window?.center()
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    private func refresh() {
        login.state = AppSettings.openAtLogin ? .on : .off
        ask.state = AppSettings.askAtLaunch ? .on : .off
        review.state = AppSettings.showReview ? .on : .off
        sound.state = AppSettings.playSound ? .on : .off
        for b in readers {
            let id = b.identifier?.rawValue ?? ""
            let installed = id == "none" || SheetOCR.isInstalled(id)
            b.isEnabled = installed
            if !installed, !b.title.hasSuffix("(not installed)") { b.title += " (not installed)" }
            b.state = id == SheetOCR.llm ? .on : .off
        }
        showStatus()
        toolLabel.stringValue = SheetOCR.locate().map { "Score reader: \($0.path)" }
            ?? "Score reader not found. Run uv sync in tools/ocr, or set SINGTOGETHER_OCR."
        cacheLabel.stringValue = ByteCountFormatter.string(fromByteCount: AppSettings.cacheSize(), countStyle: .file)
    }

    private func showStatus() {
        let id = SheetOCR.llm
        if id == "none" || !SheetOCR.llmChoices.contains(where: { $0.id == id }) {
            status.stringValue = "Chords and lyrics are read with Apple Vision only."
            checkButton.isHidden = true
            return
        }
        checkButton.isHidden = false
        switch SheetOCR.llmReady {
        case true?: status.stringValue = "Signed in. Chords and lyrics are read with \(SheetOCR.llmChoices.first { $0.id == id }?.label ?? id)."
        case false?: status.stringValue = "Not signed in — Apple Vision is used until you sign in. Open Terminal and run: \(id == "claude" ? "claude" : "agy")"
        default: status.stringValue = "Sign-in not checked yet."
        }
    }

    @objc private func loginChanged() { AppSettings.openAtLogin = login.state == .on; login.state = AppSettings.openAtLogin ? .on : .off }
    @objc private func askChanged() { AppSettings.askAtLaunch = ask.state == .on }
    @objc private func reviewChanged() { AppSettings.showReview = review.state == .on }
    @objc private func soundChanged() { AppSettings.playSound = sound.state == .on }

    @objc private func readerChanged(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue, id != SheetOCR.llm else { return }
        SheetOCR.llm = id
        SheetOCR.llmReady = nil
        for b in readers { b.state = b === sender ? .on : .off }
        showStatus()
        onReaderChange?()
        if id != "none" { checkSignIn() }
    }

    @objc private func checkSignIn() {
        let id = SheetOCR.llm
        status.stringValue = "Checking…"
        checkButton.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let r = SheetOCR.check(llm: id)
            DispatchQueue.main.async {
                guard let self else { return }
                self.checkButton.isEnabled = true
                if SheetOCR.llm == id { self.status.stringValue = r.ok ? "Signed in. " + r.message : "Not signed in — Apple Vision is used until you sign in. " + r.message }
                self.onReaderChange?()
            }
        }
    }

    @objc private func clearCache() {
        AppSettings.clearCache()
        cacheLabel.stringValue = ByteCountFormatter.string(fromByteCount: 0, countStyle: .file)
    }
}

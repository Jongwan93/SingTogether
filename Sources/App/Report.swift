import AppKit
import Transpose

/// ♪ 메뉴 → Report a Problem…: 앱·macOS 정보와 마지막 인식 기록을 모아 보여 주고, GitHub 이슈 작성 페이지를 연다.
/// 악보는 넣지 않는다. 홈 폴더 경로는 ~로 가린다. 사용자가 보내기 전에 전부 본다.
enum ProblemReport {
    private static let newIssue = "https://github.com/\(UpdateCheck.repo)/issues/new"
    private static let urlLimit = 7000      // GitHub가 받는 URL 길이 안에서 (한글은 글자당 9바이트로 늘어난다)

    /// 시스템 정보 + 마지막 인식 기록
    static func compose() -> String {
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") ?? "?"
        var s = "SingTogether \(UpdateCheck.current) (build \(build)), macOS \(ProcessInfo.processInfo.operatingSystemVersionString), \(cpu())\n"
        s += "Reader: \(SheetOCR.llm)" + (SheetOCR.llmReady.map { $0 ? " (signed in)" : " (not signed in)" } ?? "") + "\n"
        s += "Recognizer: \(SheetOCR.locate()?.path ?? "not found")\n"
        for (title, url) in [("Last recognizer run", SheetOCR.logFile), ("Last failed run", SheetOCR.failureLogFile)] {
            if let text = try? String(contentsOf: url, encoding: .utf8) { s += "\n--- \(title)\n" + text }
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return s.replacingOccurrences(of: home, with: "~")
    }

    private static func cpu() -> String {
        var size = 0
        sysctlbyname("machdep.cpu.brand_string", nil, &size, nil, 0)
        guard size > 0 else { return "unknown CPU" }
        var buf = [CChar](repeating: 0, count: size)
        sysctlbyname("machdep.cpu.brand_string", &buf, &size, nil, 0)
        return String(cString: buf)
    }

    static func run() {
        let details = compose()
        let alert = NSAlert()
        alert.messageText = "Report a problem on GitHub"
        alert.informativeText = "This opens a new issue in your browser with the details below filled in. Your scores are not included. "
            + "The full text is also copied to the clipboard, so you can paste it into the issue if the page cuts it short."
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 520, height: 220))
        let text = NSTextView(frame: scroll.bounds)
        text.string = details
        text.isEditable = false
        text.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
        text.isVerticallyResizable = true
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        scroll.documentView = text
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        alert.accessoryView = scroll
        alert.addButton(withTitle: "Open GitHub")
        alert.addButton(withTitle: "Copy Only")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        let response = alert.runModal()
        guard response != .alertThirdButtonReturn else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(details, forType: .string)
        guard response == .alertFirstButtonReturn else { return }
        NSWorkspace.shared.open(issueURL(details))
    }

    /// 제목·본문을 채운 새 이슈 주소. 본문이 길면 기록의 앞부분을 잘라 URL 길이 안에 맞춘다.
    static func issueURL(_ details: String) -> URL {
        let unreserved = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
        func encode(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: unreserved) ?? "" }
        let title = encode("Problem with SingTogether \(UpdateCheck.current)")
        let all = details.components(separatedBy: "\n")
        let head = Array(all.prefix(3))                 // 버전·AI·인식기 경로는 늘 남긴다
        var rest = Array(all.dropFirst(3))
        var cut = false
        while true {
            let body = "### What happened\n\n(Which score, what you did, what you expected. A screenshot helps.)\n\n### Details\n\n```\n"
                + (head + (cut ? ["… (earlier lines are on the clipboard)"] : []) + rest).joined(separator: "\n") + "\n```\n"
            let url = "\(newIssue)?title=\(title)&body=\(encode(body))"
            if url.count <= urlLimit || rest.isEmpty { return URL(string: url)! }
            rest.removeFirst(max(1, rest.count / 8))    // 기록의 앞쪽부터 지운다 (오류는 끝에 있다)
            cut = true
        }
    }
}

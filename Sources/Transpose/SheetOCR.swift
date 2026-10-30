import Foundation

/// 이미지·PDF 악보: Python 패키지(tools/ocr, `singtogether-ocr`)를 불러 인식 → 이조 → PDF까지 만든다.
/// 앱은 결과의 확인 자료(review.png, .txt)를 창에 띄우고, 사용자가 .txt를 고치면 `rerun(text:)`로 다시 만든다.
package enum SheetOCR {
    package struct Result {
        package var transposed: URL?        // 이조된 악보 (오선 악보면 .musicxml, 코드 차트면 .txt)
        package var transposedPDF: URL?     // 이조된 악보를 그린 PDF (오선 악보만)
        package var text: URL?              // 사람이 고칠 수 있는 압축 텍스트
        package var reviewJSON: URL?        // 음표·코드·가사 자리와 신뢰도 (없으면 text 옆의 <이름>.review.json)
        package var reviewPNGs: [URL]       // 원본 위에 확인할 곳을 표시한 그림 (쪽마다)
        package var pagePNGs: [URL] = []    // 표시 없는 쪽 그림 (review.json 좌표와 같은 픽셀). 앱이 그 위에 직접 표시를 그린다
        package var keys: String?           // "G -> A"
        package var summary: String         // "To check: notes 31/118, chords 2/25, lyrics 2/80"
        package var isChordSheet: Bool
        package var engine: String
        package var chartFont: String?      // 코드 차트: 코드를 다시 쓸 때 쓴 글꼴
    }

    /// 코드 차트의 코드를 다시 쓸 때 고를 수 있는 글꼴 (tools/ocr chordoverlay.FONTS와 같은 이름)
    package static let chartFonts = ["Arial", "Arial Bold", "Helvetica", "Helvetica Bold", "Futura", "Futura Bold", "Avenir", "Avenir Heavy",
                                     "Gill Sans", "Gill Sans Bold", "Verdana", "Verdana Bold", "Trebuchet", "Trebuchet Bold",
                                     "Times", "Times Bold", "Georgia", "Georgia Bold"]

    package enum Failure: LocalizedError {
        case notInstalled, failed(String)
        package var errorDescription: String? {
            switch self {
            case .notInstalled: return "The score reader (singtogether-ocr) was not found. Run uv sync in tools/ocr or set SINGTOGETHER_OCR to its path."
            case .failed(let s): return s
            }
        }
    }

    package static let extensions: Set<String> = ["png", "jpg", "jpeg", "webp", "pdf", "tif", "tiff", "bmp"]

    /// singtogether-ocr 실행 파일. 환경 변수 → 설정 → 저장소 안(.venv) → PATH 순.
    /// 글자를 읽을 AI CLI: "claude" / "gemini". 앱이 시작할 때 고른다. 고른 것이 없으면 설치된 것을 쓴다.
    package static var llm: String {
        get { UserDefaults.standard.string(forKey: "llm") ?? "auto" }
        set { UserDefaults.standard.set(newValue, forKey: "llm") }
    }

    package static let llmChoices: [(id: String, label: String, binaries: [String])] = [
        ("claude", "Claude", ["claude"]),
        ("gemini", "Gemini", ["agy", "gemini"]),
    ]

    /// llm.py와 같은 폴더들을 뒤져 설치된 AI CLI를 찾는다 (Finder에서 연 앱은 PATH가 짧다).
    package static func isInstalled(_ id: String) -> Bool {
        guard let names = llmChoices.first(where: { $0.id == id })?.binaries else { return false }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var dirs = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        dirs += ["/opt/homebrew/bin", "/usr/local/bin", home + "/.local/bin", home + "/.npm-global/bin", home + "/.bun/bin",
                 home + "/.volta/bin", home + "/.claude/local"]
        return names.contains { n in dirs.contains { FileManager.default.isExecutableFile(atPath: $0 + "/" + n) } }
    }

    /// 마지막 로그인 확인 결과. false면 인식할 때 AI를 건너뛴다 (실패를 기다리느라 시간을 버리지 않게).
    package static var llmReady: Bool?

    /// 고른 AI CLI가 설치·로그인돼 있는지 (`singtogether-ocr --check-llm`). Claude는 0.1초, Gemini는 약 7초 걸린다.
    package static func check(llm name: String) -> (ok: Bool, message: String) {
        guard llmChoices.contains(where: { $0.id == name }) else { return (false, "No AI selected") }
        guard let tool = locate() else { return (false, Failure.notInstalled.localizedDescription) }
        let p = Process()
        p.executableURL = tool
        p.arguments = ["--check-llm", name, "--json"]
        p.environment = environment()
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        do { try p.run() } catch { return (false, error.localizedDescription) }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let text = String(decoding: data, as: UTF8.self)
        guard let line = text.components(separatedBy: "\n").last(where: { $0.hasPrefix("JSON ") }),
              let dict = try? JSONSerialization.jsonObject(with: Data(line.dropFirst(5).utf8)) as? [String: Any] else {
            return (false, "Could not check \(name): " + text.suffix(200))
        }
        let ok = dict["signed_in"] as? Bool == true
        llmReady = ok
        return (ok, dict["message"] as? String ?? "")
    }

    private static func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = (env["PATH"] ?? "") + ":/opt/homebrew/bin:/usr/local/bin:" + FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin").path
        env[Preferences.enharmonicVariable] = Preferences.enharmonic.rawValue    // 인식기가 부르는 CLI가 F♯/G♭ 선택을 따르게
        // 인식 도구가 이조·SVG→PDF에 쓰는 CLI. 앱 번들 안의 것을 쓰게 한다 (없으면 저장소의 빌드 결과를 찾는다)
        if env["SINGTOGETHER_TRANSPOSE"] == nil,
           let cli = Bundle.main.executableURL?.deletingLastPathComponent().appendingPathComponent("singtogether-transpose"),
           FileManager.default.isExecutableFile(atPath: cli.path) {
            env["SINGTOGETHER_TRANSPOSE"] = cli.path
        }
        return env
    }

    static func readerName(_ method: String) -> String {
        method.replacingOccurrences(of: "claude", with: "Claude").replacingOccurrences(of: "gemini", with: "Gemini").replacingOccurrences(of: "vision", with: "Apple Vision")
            .replacingOccurrences(of: "none", with: "Apple Vision")
    }

    package static func locate() -> URL? {
        let fm = FileManager.default
        var candidates: [String] = []
        if let e = ProcessInfo.processInfo.environment["SINGTOGETHER_OCR"] { candidates.append(e) }
        if let d = UserDefaults.standard.string(forKey: "ocrPath") { candidates.append(d) }
        // 배포용 앱(dist.sh): Python과 인식 도구가 앱 안에 들어 있다
        if let r = Bundle.main.resourceURL { candidates.append(r.appendingPathComponent("ocr/bin/singtogether-ocr").path) }
        let bundle = Bundle.main.bundleURL
        for up in 1...4 {   // build/SingTogether.app → 저장소 루트, .build/release/SingTogether → 저장소 루트
            var root = bundle
            for _ in 0..<up { root = root.deletingLastPathComponent() }
            candidates.append(root.appendingPathComponent("tools/ocr/.venv/bin/singtogether-ocr").path)
        }
        candidates += ["/opt/homebrew/bin/singtogether-ocr", "/usr/local/bin/singtogether-ocr",
                       fm.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/singtogether-ocr").path]
        return candidates.first { fm.isExecutableFile(atPath: $0) }.map { URL(fileURLWithPath: $0) }
    }

    /// 작업 폴더: 중간 파일(.musicxml, .txt, review.png)은 여기에, 최종 PDF만 원본 옆에 복사한다.
    package static func workDirectory(for input: URL) -> URL {
        let cache = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SingTogether/work", isDirectory: true)
        let dir = cache.appendingPathComponent(input.deletingPathExtension().lastPathComponent, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// 인식 + 이조. 오래 걸린다 (10초~1분). 백그라운드에서 부를 것.
    package static func run(_ input: URL, semitones: Int) throws -> Result {
        let work = workDirectory(for: input)
        let out = work.appendingPathComponent(input.deletingPathExtension().lastPathComponent + ".musicxml")
        return try invoke(input: input, semitones: semitones, out: out, work: work)
    }

    /// 사용자가 고친 압축 텍스트로 다시 만들기 (인식 없이 바로 이조·렌더링).
    /// 코드 차트면 source(원본 그림)를 주어 그 위에 코드를 다시 쓴다. font는 코드 글꼴 (nil = 자동).
    package static func rerun(text: URL, semitones: Int, source: URL? = nil, font: String? = nil) throws -> Result {
        try invoke(input: text, semitones: semitones, out: nil, work: text.deletingLastPathComponent(), source: source, font: font)
    }

    private static func invoke(input: URL, semitones: Int, out: URL?, work: URL, source: URL? = nil, font: String? = nil) throws -> Result {
        guard let tool = locate() else { throw Failure.notInstalled }
        // 로그인 확인에 실패한 AI는 다시 한 번 확인해 보고, 그래도 안 되면 Apple Vision만 쓴다
        var reader = llm
        if llmReady == false, !check(llm: llm).ok { reader = "none" }
        var args = [input.path, "--engine", "auto", "--llm", reader, "--transpose", String(semitones), "--render", "--json",
                    "--work", work.appendingPathComponent("work").path]
        if let out { args += ["--out", out.path] }
        if let source { args += ["--source", source.path] }
        if let font { args += ["--chart-font", font] }
        let p = Process()
        p.executableURL = tool
        p.arguments = args
        p.environment = environment()
        let outPipe = Pipe(), errPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = errPipe
        let started = Date()
        try p.run()
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let stdout = String(data: outData, encoding: .utf8) ?? ""
        let stderr = String(data: errData, encoding: .utf8) ?? ""
        log(args: args, status: p.terminationStatus, stdout: stdout, stderr: stderr, seconds: Date().timeIntervalSince(started))
        guard p.terminationStatus == 0 else {
            throw Failure.failed(String((stderr.isEmpty ? stdout : stderr).suffix(600)))
        }
        guard let line = stdout.split(separator: "\n").last(where: { $0.hasPrefix("JSON ") }),
              let data = line.dropFirst(5).data(using: .utf8),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Failure.failed("Could not read the result:\n" + String(stdout.suffix(400)))
        }
        func url(_ k: String) -> URL? { (dict[k] as? String).map { URL(fileURLWithPath: $0) } }
        var summary = "No review data"
        if let s = dict["summary"] as? [String: Any] {
            func pair(_ a: String, _ b: String) -> String { "\(s[a] ?? 0)/\(s[b] ?? 0)" }
            summary = "To check: notes \(pair("notes_flagged", "notes")), chords \(pair("chords_flagged", "chords")), lyrics \(pair("lyrics_flagged", "lyrics"))"
        } else if dict["chordsheet"] as? Bool == true {
            if let chart = dict["chart"] as? [String: Any] {
                let notes = (chart["notes"] as? [String]) ?? []
                summary = "Chord chart: rewrote \(chart["replaced"] ?? 0) chords on the original" + (notes.isEmpty ? "" : " · ⚠︎ " + notes.joined(separator: " · "))
                if let by = dict["read_by"] as? String { summary += " · read by " + readerName(by) }
            } else if let err = dict["overlay_error"] as? String {
                summary = "Chord chart: made a text version only, the image failed (\(err))"
            } else {
                summary = "Chord chart: made a text version of chords and lyrics"
            }
        }
        return Result(transposed: url("transposed"), transposedPDF: url("transposed_pdf"), text: url("txt"), reviewJSON: url("review_json"),
                      reviewPNGs: ((dict["review_png"] as? [String]) ?? []).map { URL(fileURLWithPath: $0) },
                      pagePNGs: ((dict["page_png"] as? [String]) ?? []).map { URL(fileURLWithPath: $0) },
                      keys: dict["keys"] as? String, summary: summary,
                      isChordSheet: dict["chordsheet"] as? Bool == true, engine: dict["engine"] as? String ?? "",
                      chartFont: (dict["chart"] as? [String: Any])?["font"] as? String)
    }

    // MARK: 기록 — 마지막 인식 실행의 출력. 문제 신고(♪ 메뉴 → Report a Problem…)에 붙인다

    package static var logDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/SingTogether", isDirectory: true)
    }
    package static var logFile: URL { logDirectory.appendingPathComponent("recognizer.log") }
    package static var failureLogFile: URL { logDirectory.appendingPathComponent("recognizer-failed.log") }

    /// 마지막 실행은 늘 recognizer.log에, 실패한 실행은 recognizer-failed.log에도 (그 뒤에 성공해도 남게)
    private static func log(args: [String], status: Int32, stdout: String, stderr: String, seconds: TimeInterval) {
        var s = "\(ISO8601DateFormatter().string(from: Date())) singtogether-ocr \(args.joined(separator: " "))\n"
        s += "exit \(status) after \(Int(seconds.rounded())) s\n"
        if !stdout.isEmpty { s += "--- stdout\n" + stdout + (stdout.hasSuffix("\n") ? "" : "\n") }
        if !stderr.isEmpty { s += "--- stderr\n" + stderr + (stderr.hasSuffix("\n") ? "" : "\n") }
        try? FileManager.default.createDirectory(at: logDirectory, withIntermediateDirectories: true)
        try? s.write(to: logFile, atomically: true, encoding: .utf8)
        if status != 0 { try? s.write(to: failureLogFile, atomically: true, encoding: .utf8) }
    }

    // MARK: 정답(truth) 저장 — 확인 창에서 다 고친 악보를 평가 세트(tools/ocr/truth)에 넣는다

    /// 저장소 루트. 인식 도구가 저장소 안(tools/ocr/.venv)에 있거나 앱이 저장소 안에서 빌드됐을 때만 찾는다 (배포 앱은 nil).
    package static func repoRoot() -> URL? {
        let fm = FileManager.default
        var roots: [URL] = []
        for p in [ProcessInfo.processInfo.environment["SINGTOGETHER_OCR"], UserDefaults.standard.string(forKey: "ocrPath")].compactMap({ $0 }) {
            if let r = p.range(of: "/tools/ocr/") { roots.append(URL(fileURLWithPath: String(p[..<r.lowerBound]))) }
        }
        var up = Bundle.main.bundleURL
        for _ in 1...4 {
            up = up.deletingLastPathComponent()
            roots.append(up)
        }
        return roots.first { fm.fileExists(atPath: $0.appendingPathComponent("tools/ocr/truth").path) }
    }

    /// 정답 폴더: 오선 악보는 truth/, 코드 차트는 truth/chordsheets/.
    package static func truthDirectory(chordSheet: Bool) -> URL? {
        repoRoot()?.appendingPathComponent(chordSheet ? "tools/ocr/truth/chordsheets" : "tools/ocr/truth")
    }

    /// 정답을 저장소의 truth 폴더에 넣었고 같은 이름의 악보가 music_scores/에 아직 없으면 원본을 거기 복사한다
    /// (평가는 이름이 같은 그림과 정답을 짝짓는다). 복사한 곳을 돌려준다. music_scores/는 git에 올라가지 않는다.
    @discardableResult
    package static func copyToScores(_ original: URL, truth: URL) throws -> URL? {
        guard let root = repoRoot(), truth.path.hasPrefix(root.appendingPathComponent("tools/ocr/truth").path) else { return nil }
        let scores = root.appendingPathComponent("music_scores")
        let fm = FileManager.default
        guard fm.fileExists(atPath: scores.path) else { return nil }
        let stem = truth.deletingPathExtension().lastPathComponent
        if let e = fm.enumerator(at: scores, includingPropertiesForKeys: nil) {
            for case let f as URL in e where f.deletingPathExtension().lastPathComponent == stem && extensions.contains(f.pathExtension.lowercased()) {
                return nil
            }
        }
        let dest = scores.appendingPathComponent(stem + "." + original.pathExtension)
        try fm.copyItem(at: original, to: dest)
        return dest
    }

    /// 최종 결과물을 원본 옆에 "이름 (+2, A).pdf"로 복사한다. 코드 차트는 코드를 다시 쓴 그림(없으면 .txt).
    /// 그림 악보는 설정(Preferences.imageOutput)에 따라 PDF 대신 원본과 같은 종류의 그림으로 그린다
    /// (쪽이 여럿이면 "이름 (+2, A) p1.png", "… p2.png"). 돌려주는 값: 만든 파일들, 첫 번째가 대표.
    package static func deliverAll(_ r: Result, next original: URL, semitones: Int) throws -> [URL] {
        let src: URL
        if let pdf = r.transposedPDF { src = pdf }
        else if let t = r.transposed { src = t }
        else { throw Failure.failed("No transposed file was produced") }
        let sign = semitones > 0 ? "+\(semitones)" : "\(semitones)"
        let newKey = r.keys?.components(separatedBy: " -> ").last.map { ", \($0)" } ?? ""
        let base = "\(original.deletingPathExtension().lastPathComponent) (\(sign)\(newKey))"
        let dir = original.deletingLastPathComponent()

        if Preferences.imageOutput, src.pathExtension.lowercased() == "pdf", let format = Raster.format(forExtension: original.pathExtension) {
            // 원본보다 작지 않게, A4 200dpi(1654px)보다는 크게 그린다
            let width = min(max(Raster.pixelWidth(of: original) ?? 0, 1654), 4000)
            let pages = try Raster.pages(of: src, width: width, format: format)
            let urls = outputURLs(dir, base: base, ext: format.ext, pages: pages.count)
            for (u, data) in zip(urls, pages) { try data.write(to: u, options: .withoutOverwriting) }
            return urls
        }
        let dest = outputURLs(dir, base: base, ext: src.pathExtension, pages: 1)[0]
        try FileManager.default.copyItem(at: src, to: dest)
        return [dest]
    }

    @discardableResult
    package static func deliver(_ r: Result, next original: URL, semitones: Int) throws -> URL {
        try deliverAll(r, next: original, semitones: semitones)[0]
    }

    /// 아직 없는 이름들: "base.ext", 쪽이 여럿이면 "base p1.ext"…; 있으면 "base 2.ext" / "base 2 p1.ext"…
    private static func outputURLs(_ dir: URL, base: String, ext: String, pages: Int) -> [URL] {
        let fm = FileManager.default
        var n = 1
        while true {
            let stem = base + (n == 1 ? "" : " \(n)")
            let urls = pages == 1 ? [dir.appendingPathComponent("\(stem).\(ext)")]
                                  : (1...pages).map { dir.appendingPathComponent("\(stem) p\($0).\(ext)") }
            if !urls.contains(where: { fm.fileExists(atPath: $0.path) }) { return urls }
            n += 1
        }
    }
}

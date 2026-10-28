import Foundation

/// 이미지·PDF 악보: Python 패키지(tools/ocr, `singtogether-ocr`)를 불러 인식 → 이조 → PDF까지 만든다.
/// 앱은 결과의 확인 자료(review.png, .txt)를 창에 띄우고, 사용자가 .txt를 고치면 `rerun(text:)`로 다시 만든다.
package enum SheetOCR {
    package struct Result {
        package var transposed: URL?        // 이조된 악보 (오선 악보면 .musicxml, 코드 차트면 .txt)
        package var transposedPDF: URL?     // 이조된 악보를 그린 PDF (오선 악보만)
        package var text: URL?              // 사람이 고칠 수 있는 압축 텍스트
        package var reviewPNGs: [URL]       // 원본 위에 확인할 곳을 표시한 그림 (쪽마다)
        package var keys: String?           // "G -> A"
        package var summary: String         // "To check: notes 31/118, chords 2/25, lyrics 2/80"
        package var isChordSheet: Bool
        package var engine: String
    }

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

    static func readerName(_ method: String) -> String {
        method.replacingOccurrences(of: "claude", with: "Claude").replacingOccurrences(of: "gemini", with: "Gemini").replacingOccurrences(of: "vision", with: "Apple Vision")
            .replacingOccurrences(of: "none", with: "Apple Vision")
    }

    package static func locate() -> URL? {
        let fm = FileManager.default
        var candidates: [String] = []
        if let e = ProcessInfo.processInfo.environment["SINGTOGETHER_OCR"] { candidates.append(e) }
        if let d = UserDefaults.standard.string(forKey: "ocrPath") { candidates.append(d) }
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
    package static func rerun(text: URL, semitones: Int) throws -> Result {
        try invoke(input: text, semitones: semitones, out: nil, work: text.deletingLastPathComponent())
    }

    private static func invoke(input: URL, semitones: Int, out: URL?, work: URL) throws -> Result {
        guard let tool = locate() else { throw Failure.notInstalled }
        var args = [input.path, "--engine", "auto", "--llm", llm, "--transpose", String(semitones), "--render", "--json",
                    "--work", work.appendingPathComponent("work").path]
        if let out { args += ["--out", out.path] }
        let p = Process()
        p.executableURL = tool
        p.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = (env["PATH"] ?? "") + ":/opt/homebrew/bin:/usr/local/bin:" + FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin").path
        p.environment = env
        let outPipe = Pipe(), errPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = errPipe
        try p.run()
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let stdout = String(data: outData, encoding: .utf8) ?? ""
        guard p.terminationStatus == 0 else {
            let err = String(data: errData, encoding: .utf8) ?? ""
            throw Failure.failed(String((err.isEmpty ? stdout : err).suffix(600)))
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
        return Result(transposed: url("transposed"), transposedPDF: url("transposed_pdf"), text: url("txt"),
                      reviewPNGs: ((dict["review_png"] as? [String]) ?? []).map { URL(fileURLWithPath: $0) },
                      keys: dict["keys"] as? String, summary: summary,
                      isChordSheet: dict["chordsheet"] as? Bool == true, engine: dict["engine"] as? String ?? "")
    }

    /// 최종 결과물을 원본 옆에 "이름 (+2, A).pdf"로 복사한다. 코드 차트는 .txt.
    package static func deliver(_ r: Result, next original: URL, semitones: Int) throws -> URL {
        let src: URL
        if let pdf = r.transposedPDF { src = pdf }
        else if let t = r.transposed { src = t }
        else { throw Failure.failed("No transposed file was produced") }
        let sign = semitones > 0 ? "+\(semitones)" : "\(semitones)"
        let newKey = r.keys?.components(separatedBy: " -> ").last.map { ", \($0)" } ?? ""
        let base = original.deletingPathExtension().lastPathComponent
        let dir = original.deletingLastPathComponent()
        var dest = dir.appendingPathComponent("\(base) (\(sign)\(newKey)).\(src.pathExtension)")
        var n = 2
        while FileManager.default.fileExists(atPath: dest.path) {
            dest = dir.appendingPathComponent("\(base) (\(sign)\(newKey)) \(n).\(src.pathExtension)")
            n += 1
        }
        try FileManager.default.copyItem(at: src, to: dest)
        return dest
    }
}

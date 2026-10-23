import Foundation

enum ConvertError: LocalizedError {
    case unreadable, toolFailed(String), noScore
    var errorDescription: String? {
        switch self {
        case .unreadable: return "파일을 읽을 수 없어요 (텍스트 인코딩 확인)"
        case .toolFailed(let t): return "\(t) 실행에 실패했어요"
        case .noScore: return "MXL 안에서 악보 파일을 찾지 못했어요"
        }
    }
}

/// 드래그한 파일의 원래 키 정보. 링에 표시할 새 키 이름을 계산한다.
package enum KeyInfo {
    case chords(MusicKey)
    case xml(MusicXML.KeySig)

    package var originalName: String {
        switch self {
        case .chords(let k): return Notes.keyName(k)
        case .xml(let k): return MusicXML.keyName(k)
        }
    }
    package func name(shiftedBy s: Int) -> String {
        switch self {
        case .chords(let k): return Notes.keyName(MusicKey(root: k.root + s, minor: k.minor))
        case .xml(let k): return MusicXML.keyName(.init(fifths: MusicXML.targetFifths(k.fifths, s), minor: k.minor))
        }
    }
}

package enum Converter {
    package enum Kind { case text, musicxml, mxl }

    package static func kind(of url: URL) -> Kind? {
        switch url.pathExtension.lowercased() {
        case "txt", "cho", "chopro", "chordpro", "crd", "pro": return .text
        case "musicxml", "xml": return .musicxml
        case "mxl": return .mxl
        default: return nil
        }
    }

    // MARK: 키 미리보기

    package static func keyInfo(for url: URL) -> KeyInfo? {
        switch kind(of: url) {
        case .text:
            guard let (s, _) = try? readText(url), let k = ChordText.detectKey(s) else { return nil }
            return .chords(k)
        case .musicxml:
            guard let doc = try? MusicXML.load(url), let k = MusicXML.firstKey(doc) else { return nil }
            return .xml(k)
        case .mxl:
            return try? withUnzippedMXL(url) { (_: URL, score: URL) -> KeyInfo? in
                guard let doc = try? MusicXML.load(score), let k = MusicXML.firstKey(doc) else { return nil }
                return KeyInfo.xml(k)
            }
        case nil:
            return nil
        }
    }

    // MARK: 변환

    /// 변환된 파일을 만들고 그 URL을 돌려준다. 원본은 건드리지 않는다.
    /// output을 주지 않으면 원본과 같은 폴더에 "이름 (+2, A).ext"로 만든다.
    @discardableResult
    package static func convert(_ url: URL, semitones s: Int, to output: URL? = nil) throws -> URL {
        let sign = s > 0 ? "+\(s)" : "\(s)"
        let label = keyInfo(for: url).map { "\(sign), \($0.name(shiftedBy: s))" } ?? sign
        let out = output ?? uniqueOutputURL(for: url, label: label)

        switch kind(of: url) {
        case .text:
            let (text, enc) = try readText(url)
            guard let data = ChordText.transpose(text, semitones: s).data(using: enc) else { throw ConvertError.unreadable }
            try data.write(to: out, options: .withoutOverwriting)
        case .musicxml:
            let doc = try MusicXML.load(url)
            MusicXML.transpose(doc, semitones: s)
            try doc.xmlData(options: [.nodePreserveAll]).write(to: out, options: .withoutOverwriting)
        case .mxl:
            try withUnzippedMXL(url) { (dir: URL, score: URL) -> Void in
                let doc = try MusicXML.load(score)
                MusicXML.transpose(doc, semitones: s)
                try doc.xmlData(options: [.nodePreserveAll]).write(to: score, options: .atomic)
                let tmpZip = dir.deletingLastPathComponent().appendingPathComponent("out.mxl")
                // mimetype은 무압축으로 맨 앞에 와야 함
                if FileManager.default.fileExists(atPath: dir.appendingPathComponent("mimetype").path) {
                    try run("/usr/bin/zip", ["-X", "-0", "-q", tmpZip.path, "mimetype"], cwd: dir)
                }
                try run("/usr/bin/zip", ["-X", "-r", "-q", tmpZip.path, ".", "-x", "mimetype"], cwd: dir)
                try FileManager.default.moveItem(at: tmpZip, to: out)
            }
        case nil:
            throw ConvertError.unreadable
        }
        return out
    }

    static func uniqueOutputURL(for url: URL, label: String) -> URL {
        let dir = url.deletingLastPathComponent()
        let base = url.deletingPathExtension().lastPathComponent
        let ext = url.pathExtension
        var candidate = dir.appendingPathComponent("\(base) (\(label)).\(ext)")
        var n = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = dir.appendingPathComponent("\(base) (\(label)) \(n).\(ext)")
            n += 1
        }
        return candidate
    }

    // MARK: 도우미

    static func readText(_ url: URL) throws -> (String, String.Encoding) {
        let data = try Data(contentsOf: url)
        if let s = String(data: data, encoding: .utf8) { return (s, .utf8) }
        let korean = String.Encoding(rawValue: CFStringConvertEncodingToNSStringEncoding(
            CFStringEncoding(CFStringEncodings.dosKorean.rawValue)))   // CP949 / EUC-KR
        if let s = String(data: data, encoding: korean) { return (s, korean) }
        if let s = String(data: data, encoding: .utf16) { return (s, .utf16) }
        throw ConvertError.unreadable
    }

    static func run(_ tool: String, _ args: [String], cwd: URL) throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        p.currentDirectoryURL = cwd
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try p.run()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { throw ConvertError.toolFailed(tool) }
    }

    /// .mxl(zip)을 임시 폴더에 풀고 (폴더, 본 악보 파일)을 넘겨준다.
    static func withUnzippedMXL<T>(_ url: URL, _ body: (URL, URL) throws -> T) throws -> T {
        let fm = FileManager.default
        let work = fm.temporaryDirectory.appendingPathComponent("SingTogether-\(UUID().uuidString)")
        let dir = work.appendingPathComponent("x")
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: work) }
        try run("/usr/bin/unzip", ["-q", url.path, "-d", dir.path], cwd: work)

        var score: URL?
        let container = dir.appendingPathComponent("META-INF/container.xml")
        if let doc = try? XMLDocument(contentsOf: container, options: []),
           let path = (try? doc.nodes(forXPath: "//rootfile/@full-path"))?.first?.stringValue {
            score = dir.appendingPathComponent(path)
        }
        if score == nil {
            score = fm.enumerator(at: dir, includingPropertiesForKeys: nil)?
                .compactMap { $0 as? URL }
                .first { ["xml", "musicxml"].contains($0.pathExtension.lowercased()) && !$0.path.contains("META-INF") }
        }
        guard let score else { throw ConvertError.noScore }
        return try body(dir, score)
    }
}

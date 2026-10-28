import Foundation

/// 압축 텍스트 악보(tools/ocr의 `<이름>.txt`)를 마디·음표 단위로 고친다. 확인 창의 클릭 수정이 쓴다.
///
///     key: G major
///     5: [Em] r/8 G4/8 G4/8 [D] B4/4~ | 그 의 능
///
/// 음표 토큰 `<음이름><#|b>?<옥타브>/<길이>[.][~]`, 쉼표 `r/<길이>[.]`. index는 마디 안에서 음표·쉼표만 센 순서(0부터).
package struct CompactText {
    package private(set) var lines: [String]

    package init(_ text: String) { lines = text.components(separatedBy: "\n") }
    package var text: String { lines.joined(separator: "\n") }

    package struct Token {
        package var step: String?
        package var alter: Int
        package var octave: Int
        package var length: String      // "8", "4.", …
        package var tie: Bool
        package var name: String {
            guard let step else { return "r" }
            return step + (alter == 1 ? "#" : alter == -1 ? "b" : "") + String(octave)
        }
    }

    private static let note = try! NSRegularExpression(pattern: "^([A-G])([#bn]?)(\\d)/(\\d+\\.?)(~?)$")
    private static let rest = try! NSRegularExpression(pattern: "^r/(\\d+\\.?)$")
    private static let steps = ["C", "D", "E", "F", "G", "A", "B"]

    /// 조표: "key: G major" → fifths
    package var fifths: Int {
        guard let line = lines.first(where: { $0.lowercased().hasPrefix("key:") }) else { return 0 }
        let body = line.dropFirst(4).trimmingCharacters(in: .whitespaces)
        let minor = body.lowercased().contains("minor")
        let name = body.split(separator: " ").first.map(String.init) ?? "C"
        let table = minor ? MusicXML.minorByFifths.map { String($0.dropLast()) } : MusicXML.majorByFifths
        return (table.firstIndex(of: name) ?? 6) - 6
    }

    /// 조표에 따른 임시표: G장조의 F → ♯
    package static func keyAlter(step: String, fifths: Int) -> Int {
        let sharps = ["F", "C", "G", "D", "A", "E", "B"], flats = ["B", "E", "A", "D", "G", "C", "F"]
        if fifths > 0, let i = sharps.firstIndex(of: step), i < fifths { return 1 }
        if fifths < 0, let i = flats.firstIndex(of: step), i < -fifths { return -1 }
        return 0
    }

    private static func fullMatch(_ re: NSRegularExpression, _ s: String) -> [String]? {
        let ns = s as NSString
        guard let m = re.firstMatch(in: s, range: NSRange(location: 0, length: ns.length)), m.range.length == ns.length else { return nil }
        return (1..<m.numberOfRanges).map { m.range(at: $0).location == NSNotFound ? "" : ns.substring(with: m.range(at: $0)) }
    }

    package static func parse(_ tok: String) -> Token? {
        if let g = fullMatch(note, tok) {
            return Token(step: g[0], alter: g[1] == "#" ? 1 : g[1] == "b" ? -1 : 0, octave: Int(g[2]) ?? 4, length: g[3], tie: g[4] == "~")
        }
        if let g = fullMatch(rest, tok) { return Token(step: nil, alter: 0, octave: 0, length: g[0], tie: false) }
        return nil
    }

    package static func render(_ t: Token) -> String {
        guard let step = t.step else { return "r/\(t.length)" }
        return "\(step)\(t.alter == 1 ? "#" : t.alter == -1 ? "b" : "")\(t.octave)/\(t.length)\(t.tie ? "~" : "")"
    }

    /// (줄 번호, 토큰 배열, 그 음표의 토큰 위치). 마디가 없거나 index가 넘치면 nil.
    private func locate(measure: Int, index: Int) -> (line: Int, head: String, toks: [String], tail: String, at: Int)? {
        for (li, line) in lines.enumerated() {
            guard let colon = line.firstIndex(of: ":"), Int(line[..<colon].trimmingCharacters(in: .whitespaces)) == measure else { continue }
            let rest = String(line[line.index(after: colon)...])
            let bar = rest.firstIndex(of: "|")
            let body = bar.map { String(rest[..<$0]) } ?? rest
            let tail = bar.map { String(rest[$0...]) } ?? ""
            let toks = body.split(separator: " ").map(String.init)
            var k = -1
            for (ti, tok) in toks.enumerated() where Self.parse(tok) != nil {
                k += 1
                if k == index { return (li, String(line[...colon]), toks, tail, ti) }
            }
            return nil
        }
        return nil
    }

    package func token(measure: Int, index: Int) -> Token? {
        locate(measure: measure, index: index).flatMap { Self.parse($0.toks[$0.at]) }
    }

    private mutating func write(_ loc: (line: Int, head: String, toks: [String], tail: String, at: Int), _ toks: [String]) {
        lines[loc.line] = loc.head + " " + toks.joined(separator: " ") + (loc.tail.isEmpty ? "" : " " + loc.tail)
    }

    /// 음표를 바꾼다. change가 nil을 돌려주면 삭제.
    @discardableResult
    package mutating func update(measure: Int, index: Int, _ change: (Token) -> Token?) -> Token? {
        guard let loc = locate(measure: measure, index: index), let old = Self.parse(loc.toks[loc.at]) else { return nil }
        var toks = loc.toks
        if let new = change(old) {
            toks[loc.at] = Self.render(new)
            write(loc, toks)
            return new
        }
        toks.remove(at: loc.at)
        write(loc, toks)
        return nil
    }

    /// 계이름 한 칸(음이름 글자) 위·아래. 임시표는 조표를 따른다 (G장조에서 E→F는 F♯).
    package mutating func shiftStep(measure: Int, index: Int, by delta: Int) -> Token? {
        let f = fifths
        return update(measure: measure, index: index) { t in
            guard let step = t.step, let i = Self.steps.firstIndex(of: step) else { return t }
            let j = i + delta
            let octave = t.octave + Int((Double(j) / 7).rounded(.down))
            let newStep = Self.steps[((j % 7) + 7) % 7]
            return Token(step: newStep, alter: Self.keyAlter(step: newStep, fifths: f), octave: octave, length: t.length, tie: t.tie)
        }
    }
}

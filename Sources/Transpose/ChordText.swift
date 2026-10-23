import Foundation

/// 조성 (근음 pitch class 0–11, 단조 여부)
package struct MusicKey {
    var root: Int
    var minor: Bool
}

enum Notes {
    static let sharp = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    static let flat  = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
    static let flatMajor: Set<Int> = [5, 10, 3, 8, 1, 6]   // F Bb Eb Ab Db Gb
    static let flatMinor: Set<Int> = [2, 7, 0, 5, 10, 3]   // Dm Gm Cm Fm Bbm Ebm

    static func mod12(_ x: Int) -> Int { ((x % 12) + 12) % 12 }

    static func usesFlats(_ k: MusicKey) -> Bool {
        k.minor ? flatMinor.contains(mod12(k.root)) : flatMajor.contains(mod12(k.root))
    }
    static func name(_ pc: Int, flats: Bool) -> String { (flats ? flat : sharp)[mod12(pc)] }
    static func keyName(_ k: MusicKey) -> String {
        name(k.root, flats: usesFlats(k)) + (k.minor ? "m" : "")
    }
}

/// 텍스트 코드 악보 (가사 위 코드 줄, [G]가사 인라인 형식)
enum ChordText {
    struct Chord { var root: Int; var quality: String; var bass: Int? }

    private static let chordRE = try! NSRegularExpression(
        pattern: "^([A-G])([#b♯♭]?)([^\\s/]*)(?:/([A-G])([#b♯♭]?))?$")
    private static let qualityRE = try! NSRegularExpression(
        pattern: "^(?:maj|min|dim|aug|sus|add|alt|no|omit|m|M|Δ|°|ø|\\+|-|#|b|♯|♭|\\d|\\(|\\)|,)*$")
    private static let neutralRE = try! NSRegularExpression(
        pattern: "^(\\||\\|\\||-+|\\(?x\\d+\\)?|N\\.?C\\.?|%|:\\|\\||\\|\\|:|/)$",
        options: [.caseInsensitive])
    private static let bracketRE = try! NSRegularExpression(pattern: "\\[([^\\]]+)\\]")
    private static let tokenRE = try! NSRegularExpression(pattern: "\\S+")

    private static let letterPC: [String: Int] = ["C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11]

    private static func fullMatch(_ re: NSRegularExpression, _ s: String) -> NSTextCheckingResult? {
        let r = NSRange(location: 0, length: (s as NSString).length)
        guard let m = re.firstMatch(in: s, range: r), m.range == r else { return nil }
        return m
    }

    private static func pc(_ letter: String, _ acc: String) -> Int {
        var p = letterPC[letter] ?? 0
        if acc == "#" || acc == "♯" { p += 1 }
        if acc == "b" || acc == "♭" { p -= 1 }
        return Notes.mod12(p)
    }

    static func parse(_ token: String) -> Chord? {
        let ns = token as NSString
        guard let m = fullMatch(chordRE, token) else { return nil }
        func g(_ i: Int) -> String {
            let r = m.range(at: i); return r.location == NSNotFound ? "" : ns.substring(with: r)
        }
        let q = g(3)
        guard fullMatch(qualityRE, q) != nil else { return nil }
        let bass = g(4).isEmpty ? nil : pc(g(4), g(5))
        return Chord(root: pc(g(1), g(2)), quality: q, bass: bass)
    }

    private static func render(_ c: Chord, _ s: Int, _ flats: Bool) -> String {
        var out = Notes.name(c.root + s, flats: flats) + c.quality
        if let b = c.bass { out += "/" + Notes.name(b + s, flats: flats) }
        return out
    }

    /// "(Am7)" 같은 괄호 포함 토큰 처리
    private static func split(_ tok: String) -> (lead: String, core: String, trail: String) {
        var core = Substring(tok)
        var lead = "", trail = ""
        while core.first == "(" { lead.append("("); core = core.dropFirst() }
        while core.last == ")" && !core.isEmpty { trail.append(")"); core = core.dropLast() }
        return (lead, String(core), trail)
    }

    private static func tokens(_ line: String) -> [NSTextCheckingResult] {
        tokenRE.matches(in: line, range: NSRange(location: 0, length: (line as NSString).length))
    }

    static func isChordLine(_ line: String) -> Bool {
        var chords = 0, other = 0
        for m in tokens(line) {
            let t = (line as NSString).substring(with: m.range)
            if fullMatch(neutralRE, t) != nil { continue }
            if parse(split(t).core) != nil { chords += 1 } else { other += 1 }
        }
        return chords > 0 && chords >= other * 3
    }

    static func detectKey(_ text: String) -> MusicKey? {
        func key(_ c: Chord) -> MusicKey {
            let q = c.quality
            let minor = (q.hasPrefix("m") && !q.hasPrefix("maj")) || q.hasPrefix("min")
            return MusicKey(root: c.root, minor: minor)
        }
        for line in text.components(separatedBy: "\n") {
            let ns = line as NSString
            for m in bracketRE.matches(in: line, range: NSRange(location: 0, length: ns.length)) {
                if let c = parse(ns.substring(with: m.range(at: 1)).trimmingCharacters(in: .whitespaces)) {
                    return key(c)
                }
            }
            if isChordLine(line) {
                for m in tokens(line) {
                    if let c = parse(split(ns.substring(with: m.range)).core) { return key(c) }
                }
            }
        }
        return nil
    }

    static func transposeLine(_ line: String, _ s: Int, _ flats: Bool) -> String {
        let ns = line as NSString
        if isChordLine(line) {
            var out = ""
            for m in tokens(line) {
                let tok = ns.substring(with: m.range)
                let p = split(tok)
                let t = parse(p.core).map { p.lead + render($0, s, flats) + p.trail } ?? tok
                let outLen = (out as NSString).length
                // 원래 열 위치를 유지하되, 코드가 길어지면 최소 한 칸 띄움
                let pos = outLen == 0 ? m.range.location : max(m.range.location, outLen + 1)
                out += String(repeating: " ", count: max(0, pos - outLen)) + t
            }
            return out
        }
        var out = "", last = 0
        for m in bracketRE.matches(in: line, range: NSRange(location: 0, length: ns.length)) {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let inner = ns.substring(with: m.range(at: 1)).trimmingCharacters(in: .whitespaces)
            if let c = parse(inner) { out += "[" + render(c, s, flats) + "]" }
            else { out += ns.substring(with: m.range) }
            last = m.range.location + m.range.length
        }
        out += ns.substring(from: last)
        return out
    }

    static func transpose(_ text: String, semitones s: Int) -> String {
        var body = text
        let bom = body.hasPrefix("\u{FEFF}")
        if bom { body.removeFirst() }
        let crlf = body.contains("\r\n")
        if crlf { body = body.replacingOccurrences(of: "\r\n", with: "\n") }

        let flats = detectKey(body).map { Notes.usesFlats(MusicKey(root: $0.root + s, minor: $0.minor)) } ?? false
        var out = body.components(separatedBy: "\n")
            .map { transposeLine($0, s, flats) }
            .joined(separator: crlf ? "\r\n" : "\n")
        if bom { out = "\u{FEFF}" + out }
        return out
    }
}

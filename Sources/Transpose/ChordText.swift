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
    /// 옮긴 뒤의 조가 F#/Gb(단조는 D#m/Ebm)처럼 둘 다 가능하면 원래 조가 쓰던 쪽(♯ 악보는 ♯)을 따른다
    static func usesFlats(_ target: MusicKey, from source: MusicKey) -> Bool {
        mod12(target.root) == (target.minor ? 3 : 6) ? usesFlats(source) : usesFlats(target)
    }
    static func name(_ pc: Int, flats: Bool) -> String { (flats ? flat : sharp)[mod12(pc)] }
    static func keyName(_ k: MusicKey, from source: MusicKey? = nil) -> String {
        name(k.root, flats: source.map { usesFlats(k, from: $0) } ?? usesFlats(k)) + (k.minor ? "m" : "")
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

    /// "Eb-F", "Bb9-Dm7-Eb-F": 붙임표로 이은 코드 진행. 조각이 전부 코드일 때만 인정한다 (C-7 같은 단일 코드는 parse가 먼저 받는다)
    static func parseRun(_ core: String) -> [Chord]? {
        if let c = parse(core) { return [c] }
        guard core.contains("-") else { return nil }
        let parts = core.split(separator: "-", omittingEmptySubsequences: false).map(String.init)
        guard parts.count >= 2 else { return nil }
        let chords = parts.compactMap(parse)
        return chords.count == parts.count ? chords : nil
    }

    private static func renderRun(_ run: [Chord], _ s: Int, _ flats: Bool) -> String {
        run.map { render($0, s, flats) }.joined(separator: "-")
    }

    private static func tokens(_ line: String) -> [NSTextCheckingResult] {
        tokenRE.matches(in: line, range: NSRange(location: 0, length: (line as NSString).length))
    }

    static func isChordLine(_ line: String) -> Bool {
        var chords = 0, other = 0
        for m in tokens(line) {
            let t = (line as NSString).substring(with: m.range)
            if fullMatch(neutralRE, t) != nil { continue }
            if let run = parseRun(split(t).core) { chords += run.count } else { other += 1 }
        }
        return chords > 0 && chords >= other * 3
    }

    static func isMinor(_ c: Chord) -> Bool {
        let q = c.quality
        return (q.hasPrefix("m") && !q.hasPrefix("maj")) || q.hasPrefix("min") || q.hasPrefix("-")
    }

    /// 악보에 나온 코드 전부 (줄 순서)
    static func allChords(_ text: String) -> [Chord] {
        var out: [Chord] = []
        for line in text.components(separatedBy: "\n") {
            let ns = line as NSString
            for m in bracketRE.matches(in: line, range: NSRange(location: 0, length: ns.length)) {
                if let c = parse(ns.substring(with: m.range(at: 1)).trimmingCharacters(in: .whitespaces)) { out.append(c) }
            }
            if isChordLine(line) {
                for m in tokens(line) {
                    if let run = parseRun(split(ns.substring(with: m.range)).core) { out += run }
                }
            }
        }
        return out
    }

    /// 조 판별. 코드 근음이 온음계에 들면 +1, 3화음 성질(I·IV·V 장, ii·iii·vi 단)까지 맞으면 +1, 틀리면 −1.
    /// 동점이면 첫 코드의 조. 첫 코드가 vi 단3화음이면 나란한 단조로 본다.
    static func detectKey(_ text: String) -> MusicKey? {
        let chords = allChords(text)
        guard let first = chords.first else { return nil }
        let majorDegrees = [0, 2, 4, 5, 7, 9, 11], minorDegrees: Set<Int> = [2, 4, 9]
        var best = (score: Int.min, root: first.root)
        for tonic in 0..<12 {
            var score = 0
            for c in chords {
                let d = Notes.mod12(c.root - tonic)
                guard majorDegrees.contains(d) else { score -= 1; continue }
                score += 1
                if d == 11 { continue }                          // vii°는 성질을 따지지 않는다
                score += isMinor(c) == minorDegrees.contains(d) ? 1 : -1
            }
            if score > best.score || (score == best.score && tonic == first.root) { best = (score, tonic) }
        }
        if isMinor(first) && Notes.mod12(first.root - best.root) == 9 { return MusicKey(root: first.root, minor: true) }
        if isMinor(first) && first.root == best.root { return MusicKey(root: first.root, minor: true) }
        return MusicKey(root: best.root, minor: false)
    }

    static func transposeLine(_ line: String, _ s: Int, _ flats: Bool) -> String {
        let ns = line as NSString
        if isChordLine(line) {
            return transposeChordLine(line, s, flats)
        }
        return transposeLyricLine(line, ns, s, flats)
    }

    private static func transposeChordLine(_ line: String, _ s: Int, _ flats: Bool) -> String {
        let ns = line as NSString
        var out = ""
        for m in tokens(line) {
            let tok = ns.substring(with: m.range)
            let p = split(tok)
            let t = parseRun(p.core).map { p.lead + renderRun($0, s, flats) + p.trail } ?? tok
            let outLen = (out as NSString).length
            // 원래 열 위치를 유지하되, 코드가 길어지면 최소 한 칸 띄움
            let pos = outLen == 0 ? m.range.location : max(m.range.location, outLen + 1)
            out += String(repeating: " ", count: max(0, pos - outLen)) + t
        }
        return out
    }

    private static func transposeLyricLine(_ line: String, _ ns: NSString, _ s: Int, _ flats: Bool) -> String {
        // 코드 줄이 아니어도 "(Repeat Chorus) Eb-F (2x)"처럼 붙임표로 이은 코드 진행(2개 이상)은 옮긴다
        var runs = "", runLast = 0
        for m in tokens(line) {
            let tok = ns.substring(with: m.range), p = split(tok)
            guard p.core.contains("-"), let run = parseRun(p.core), run.count >= 2 else { continue }
            runs += ns.substring(with: NSRange(location: runLast, length: m.range.location - runLast))
            runs += p.lead + renderRun(run, s, flats) + p.trail
            runLast = m.range.location + m.range.length
        }
        let line = runs + ns.substring(from: runLast)
        let ns = line as NSString
        var out = "", last = 0
        for m in bracketRE.matches(in: line, range: NSRange(location: 0, length: ns.length)) {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let inner = ns.substring(with: m.range(at: 1)).trimmingCharacters(in: CharacterSet.whitespaces)
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

        let flats = detectKey(body).map { Notes.usesFlats(MusicKey(root: $0.root + s, minor: $0.minor), from: $0) } ?? false
        var out = body.components(separatedBy: "\n")
            .map { transposeLine($0, s, flats) }
            .joined(separator: crlf ? "\r\n" : "\n")
        if bom { out = "\u{FEFF}" + out }
        return out
    }
}

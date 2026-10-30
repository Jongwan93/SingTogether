import Foundation

/// MusicXML 음표, 조표, 코드 기호 이조
package enum MusicXML {
    static let steps = ["C", "D", "E", "F", "G", "A", "B"]
    static let stepPC = [0, 2, 4, 5, 7, 9, 11]
    static let majorByFifths = ["Gb", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E", "B", "F#"]
    static let minorByFifths = ["Ebm", "Bbm", "Fm", "Cm", "Gm", "Dm", "Am", "Em", "Bm", "F#m", "C#m", "G#m", "D#m"]

    package struct KeySig { var fifths: Int; var minor: Bool }

    /// 반음 s만큼 옮긴 뒤의 조표(-6…6). F#/Gb처럼 둘 다 가능하면 설정(Preferences.enharmonic)을 따른다:
    /// 기본은 원래 조표의 쪽(♯ 조표면 ♯, C장조도 ♯), 아니면 늘 ♯ 또는 늘 ♭.
    package static func targetFifths(_ old: Int, _ s: Int) -> Int {
        let d0 = Notes.mod12(7 * s)
        let candidates = (-3...3).map { old + d0 + 12 * $0 }.filter { (-6...6).contains($0) }
        guard let lo = candidates.min(), let hi = candidates.max() else { return old }
        switch Preferences.enharmonic {
        case .sharps: return hi
        case .flats: return lo
        case .original: return old >= 0 ? hi : lo
        }
    }

    static func keyName(_ k: KeySig) -> String {
        let i = max(-6, min(6, k.fifths)) + 6
        return k.minor ? minorByFifths[i] : majorByFifths[i]
    }

    static func load(_ url: URL) throws -> XMLDocument {
        var data = try Data(contentsOf: url)
        if let s = String(data: data, encoding: .utf8), s.contains("<elision>") || s.contains("<text>") {
            data = Data(protectWhitespaceOnlyText(s).utf8)
        }
        return try XMLDocument(data: data, options: [.nodePreserveAll])
    }

    /// Foundation은 공백만 든 텍스트 노드(`<elision> </elision>`, 가사 이음 기호)를 버린다.
    /// 문자 참조(`&#32;`)로 바꿔 두면 그대로 보존되어 다시 써진다.
    private static let whitespaceOnlyText = try! NSRegularExpression(
        pattern: "<(elision|text)(\\s[^>]*)?>([ \\t\\u00A0\\u2009]+)</\\1>")

    static func protectWhitespaceOnlyText(_ s: String) -> String {
        let ns = s as NSString
        var out = s
        for m in whitespaceOnlyText.matches(in: s, range: NSRange(location: 0, length: ns.length)).reversed() {
            let tag = ns.substring(with: m.range(at: 1))
            let attrs = m.range(at: 2).location == NSNotFound ? "" : ns.substring(with: m.range(at: 2))
            let refs = ns.substring(with: m.range(at: 3)).unicodeScalars.map { "&#\($0.value);" }.joined()
            out = (out as NSString).replacingCharacters(in: m.range, with: "<\(tag)\(attrs)>\(refs)</\(tag)>")
        }
        return out
    }

    /// 저장용 직렬화. Foundation의 xmlData는 DOCTYPE을 두 번째 직렬화에서 닫는 `>` 없이 써서(`…dtd"<score-partwise`)
    /// 그 결과를 다시 읽을 수 없다. 선언과 DOCTYPE은 직접 쓰고 루트 요소만 Foundation에 맡긴다.
    static func serialize(_ doc: XMLDocument) -> Data {
        var s = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        if let dtd = doc.dtd, let name = dtd.name {
            s += "<!DOCTYPE \(name)"
            if let pub = dtd.publicID {
                s += " PUBLIC \"\(pub)\" \"\(dtd.systemID ?? "")\""
            } else if let sys = dtd.systemID {
                s += " SYSTEM \"\(sys)\""
            }
            s += ">\n"
        }
        s += doc.rootElement()?.xmlString(options: [.nodePreserveAll]) ?? ""
        s += "\n"
        return Data(s.utf8)
    }

    static func firstKey(_ doc: XMLDocument) -> KeySig? {
        guard let keyEl = (try? doc.nodes(forXPath: "//attributes/key"))?.first as? XMLElement,
              let f = keyEl.elements(forName: "fifths").first?.stringValue,
              let fifths = Int(f.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
        let mode = keyEl.elements(forName: "mode").first?.stringValue?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return KeySig(fifths: fifths, minor: mode == "minor")
    }

    private static func elements(_ doc: XMLDocument, _ xpath: String) -> [XMLElement] {
        ((try? doc.nodes(forXPath: xpath)) ?? []).compactMap { $0 as? XMLElement }
    }

    private static func text(_ el: XMLElement, _ name: String) -> String? {
        el.elements(forName: name).first?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 겹임시표, 또는 조표가 품지 않는 E♯·B♯·F♭·C♭ (E♯은 F♯장조, C♭은 G♭장조에만 있다)
    static func awkwardSpelling(_ letter: Int, _ alter: Int, fifths: Int) -> Bool {
        abs(alter) == 2 || (alter == 1 && ((letter == 2 && fifths < 6) || letter == 6)) || (alter == -1 && ((letter == 0 && fifths > -6) || letter == 3))
    }

    /// pitch class를 가장 단순하게 적는 (글자 index, 임시표): 자연음이면 그대로, 아니면 ♯ 또는 ♭ 하나
    static func simpleSpelling(_ pc: Int, sharps: Bool) -> (Int, Int) {
        if let i = stepPC.firstIndex(of: pc) { return (i, 0) }
        return sharps ? (stepPC.firstIndex(of: Notes.mod12(pc - 1))!, 1) : (stepPC.firstIndex(of: Notes.mod12(pc + 1))!, -1)
    }

    private static func format(_ a: Double) -> String {
        a == a.rounded() ? String(Int(a)) : String(a)
    }

    /// step/alter 요소 쌍을 새 값으로 바꿈. alter가 0이면 요소 삭제.
    private static func setStepAlter(_ parent: XMLElement, stepName: String, alterName: String,
                                     step: Int, alter: Double) {
        parent.elements(forName: stepName).first?.stringValue = steps[step]
        let existing = parent.elements(forName: alterName).first
        if alter == 0 {
            existing?.detach()
        } else if let existing {
            existing.stringValue = format(alter)
        } else if let stepEl = parent.elements(forName: stepName).first {
            parent.insertChild(XMLElement(name: alterName, stringValue: format(alter)), at: stepEl.index + 1)
        }
    }

    static func transpose(_ doc: XMLDocument, semitones s: Int) {
        let oldFifths = firstKey(doc)?.fifths ?? 0
        let d = targetFifths(oldFifths, s) - oldFifths     // 5도권에서 움직인 거리
        let dia = ((d * 4) % 7 + 7) % 7                     // 계이름(음이름 글자) 이동량
        let newFifths = oldFifths + d

        // 조표
        for f in elements(doc, "//attributes/key/fifths") {
            if let v = Int(f.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "") {
                f.stringValue = String(v + d)
            }
        }

        // 음표
        for pitch in elements(doc, "//note/pitch") {
            guard let st = text(pitch, "step"), let i = steps.firstIndex(of: st),
                  let oct = Int(text(pitch, "octave") ?? "") else { continue }
            // <alter>가 없는데 <accidental>만 있는 (규격에 어긋나지만 흔한) 파일은 임시표를 실제 음으로 본다 (music21과 같은 해석)
            let note = pitch.parent as? XMLElement
            let accidentalAlter: [String: Double] = ["sharp": 1, "flat": -1, "natural": 0, "double-sharp": 2, "flat-flat": -2,
                                                     "sharp-sharp": 2, "natural-sharp": 1, "natural-flat": -1]
            let fromAccidental = note.flatMap { text($0, "accidental") }.flatMap { accidentalAlter[$0] } ?? 0
            let alterD = Double(text(pitch, "alter") ?? "") ?? fromAccidental
            let alterI = Int(alterD.rounded())
            let frac = alterD - Double(alterI)                // 미분음 보존

            let midi = 12 * (oct + 1) + stepPC[i] + alterI + s
            var ni = (i + dia) % 7
            var no = oct + (i + dia) / 7
            var a = midi - (12 * (no + 1) + stepPC[ni])
            while a > 6 { no += 1; a -= 12 }
            while a < -6 { no -= 1; a += 12 }
            // 음표도 겹임시표나 E♯·B♯·F♭·C♭(조표가 품는 F♯·G♭장조 밖)은 읽기 쉬운 이름으로 (잘못 읽힌 D♭이 +1에서 E♭♭이 되던 것)
            if frac == 0, awkwardSpelling(ni, a, fifths: newFifths) {
                (ni, a) = simpleSpelling(Notes.mod12(midi), sharps: newFifths >= 0)
                no = (midi - a - stepPC[ni]) / 12 - 1
            }

            setStepAlter(pitch, stepName: "step", alterName: "alter", step: ni, alter: Double(a) + frac)
            pitch.elements(forName: "octave").first?.stringValue = String(no)

            // 원래 임시표가 붙어 있던 음은 새 음에 맞는 임시표로 (조성 관계가 유지되므로 위치는 그대로)
            if let note = pitch.parent as? XMLElement, let acc = note.elements(forName: "accidental").first,
               frac == 0 {
                switch a {
                case -2: acc.stringValue = "flat-flat"
                case -1: acc.stringValue = "flat"
                case 0:  acc.stringValue = "natural"
                case 1:  acc.stringValue = "sharp"
                case 2:  acc.stringValue = "double-sharp"
                default: break
                }
            }
        }

        // 코드 기호 (근음, 베이스). 글자는 조와 같이 움직이되, 결과가 E♯·B♯·F♭·C♭·겹임시표면 읽을 수 있는 이름으로
        // (조표를 품는 F♯·G♭장조만 예외). 인식된 조가 틀렸을 때 E장조 악보의 E가 E♯이 되던 것을 막는다.
        func shiftPC(_ el: XMLElement, _ stepName: String, _ alterName: String) {
            guard let st = text(el, stepName), let i = steps.firstIndex(of: st) else { return }
            let alter = Int((Double(text(el, alterName) ?? "0") ?? 0).rounded())
            let target = Notes.mod12(stepPC[i] + alter + s)
            var ni = (i + dia) % 7
            var a = Notes.mod12(target - stepPC[ni])
            if a > 6 { a -= 12 }
            if awkwardSpelling(ni, a, fifths: newFifths) { (ni, a) = simpleSpelling(target, sharps: newFifths >= 0) }
            setStepAlter(el, stepName: stepName, alterName: alterName, step: ni, alter: Double(a))
        }
        for r in elements(doc, "//harmony/root") { shiftPC(r, "root-step", "root-alter") }
        for b in elements(doc, "//harmony/bass") { shiftPC(b, "bass-step", "bass-alter") }
    }
}

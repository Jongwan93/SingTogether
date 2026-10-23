import Foundation

/// MusicXML 음표, 조표, 코드 기호 이조
package enum MusicXML {
    static let steps = ["C", "D", "E", "F", "G", "A", "B"]
    static let stepPC = [0, 2, 4, 5, 7, 9, 11]
    static let majorByFifths = ["Gb", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E", "B", "F#"]
    static let minorByFifths = ["Ebm", "Bbm", "Fm", "Cm", "Gm", "Dm", "Am", "Em", "Bm", "F#m", "C#m", "G#m", "D#m"]

    package struct KeySig { var fifths: Int; var minor: Bool }

    /// 반음 s만큼 옮긴 뒤의 조표(-6…6). F#/Gb처럼 둘 다 가능하면 플랫 쪽.
    static func targetFifths(_ old: Int, _ s: Int) -> Int {
        let d0 = Notes.mod12(7 * s)
        let candidates = (-3...3).map { old + d0 + 12 * $0 }.filter { (-6...6).contains($0) }
        return candidates.min() ?? old
    }

    static func keyName(_ k: KeySig) -> String {
        let i = max(-6, min(6, k.fifths)) + 6
        return k.minor ? minorByFifths[i] : majorByFifths[i]
    }

    static func load(_ url: URL) throws -> XMLDocument {
        try XMLDocument(contentsOf: url, options: [.nodePreserveAll])
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
            let alterD = Double(text(pitch, "alter") ?? "0") ?? 0
            let alterI = Int(alterD.rounded())
            let frac = alterD - Double(alterI)                // 미분음 보존

            let midi = 12 * (oct + 1) + stepPC[i] + alterI + s
            let ni = (i + dia) % 7
            var no = oct + (i + dia) / 7
            var a = midi - (12 * (no + 1) + stepPC[ni])
            while a > 6 { no += 1; a -= 12 }
            while a < -6 { no -= 1; a += 12 }

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

        // 코드 기호 (근음, 베이스)
        func shiftPC(_ el: XMLElement, _ stepName: String, _ alterName: String) {
            guard let st = text(el, stepName), let i = steps.firstIndex(of: st) else { return }
            let alter = Int((Double(text(el, alterName) ?? "0") ?? 0).rounded())
            let target = Notes.mod12(stepPC[i] + alter + s)
            let ni = (i + dia) % 7
            var a = Notes.mod12(target - stepPC[ni])
            if a > 6 { a -= 12 }
            setStepAlter(el, stepName: stepName, alterName: alterName, step: ni, alter: Double(a))
        }
        for r in elements(doc, "//harmony/root") { shiftPC(r, "root-step", "root-alter") }
        for b in elements(doc, "//harmony/bass") { shiftPC(b, "bass-step", "bass-alter") }
    }
}

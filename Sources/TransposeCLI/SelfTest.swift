import Foundation
import Transpose

/// `singtogether-transpose --selftest`: XCTest 없이(Command Line Tools만으로) 도는 간단한 자체 검사.
enum SelfTest {
    static func run() -> Int32 {
        var failures = 0
        func check(_ ok: Bool, _ what: String) {
            if !ok { failures += 1; print("  failed: \(what)") }
        }
        let sample = """
        title: 테스트
        key: G major
        time: 4/4
        1: [G] r/8 G4/8 G4/8 [D/F#] F#4/8 E4/8~ | 주 님 의 임
        2: [C2] E4/8 D4/2. | _ 서
        """
        var t = CompactText(sample)
        check(t.fifths == 1, "G major → fifths 1")
        check(t.token(measure: 1, index: 0)?.name == "r", "first token is a rest")
        check(t.token(measure: 1, index: 1)?.name == "G4", "second token is G4")
        check(t.shiftStep(measure: 1, index: 1, by: -1)?.name == "F#4", "one step down in G major → F#4")
        check(t.shiftStep(measure: 1, index: 2, by: 3)?.name == "C5", "three steps up → C5 (crosses the octave)")
        check(t.lines[3] == "1: [G] r/8 F#4/8 C5/8 [D/F#] F#4/8 E4/8~ | 주 님 의 임", "line rewritten correctly: \(t.lines[3])")
        t.update(measure: 2, index: 1) { var x = $0; x.length = "4."; return x }
        check(t.lines[4].contains("D4/4."), "change length")
        t.update(measure: 2, index: 0) { var x = $0; x.step = nil; return x }
        check(t.lines[4].contains("[C2] r/8 D4/4."), "make rest: \(t.lines[4])")
        t.update(measure: 2, index: 0) { _ in nil }
        check(t.lines[4] == "2: [C2] D4/4. | _ 서", "delete: \(t.lines[4])")
        check(t.token(measure: 9, index: 0) == nil, "missing measure")
        check(CompactText.keyAlter(step: "F", fifths: 1) == 1, "F in G major")
        check(CompactText.keyAlter(step: "E", fifths: -2) == -1, "E in Bb major")
        check(CompactText.keyAlter(step: "A", fifths: -2) == 0, "A in Bb major")
        check(CompactText(sample.replacingOccurrences(of: "G major", with: "Eb major")).fifths == -3, "Eb major")
        check(CompactText(sample.replacingOccurrences(of: "G major", with: "E minor")).fifths == 1, "E minor")
        check(ChordText.transpose("Intro: Bb9-Dm7-Eb-F", semitones: 2).contains("C9-Em7-F-G"), "hyphenated chord run transposed")
        check(ChordText.detectKey("C G D Em\n") .map { $0.root } == 7, "C G D Em → G")
        print(failures == 0 ? "self-test passed" : "self-test failed: \(failures)")
        return failures == 0 ? 0 : 1
    }
}

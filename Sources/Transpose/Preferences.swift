import Foundation

/// 이명동음 표기: 옮긴 조가 F♯/G♭(단조는 D♯m/E♭m)처럼 두 가지로 적을 수 있을 때 어느 쪽을 쓸지.
package enum Enharmonic: String, CaseIterable {
    case original   // 원본이 ♯ 조였으면 ♯, ♭ 조였으면 ♭ (C장조·A단조는 ♯ 쪽으로 친다)
    case sharps
    case flats
}

/// 앱(설정 창)과 CLI(플래그·환경 변수)가 채우는 이조 선택지.
/// 인식기(Python)는 이조를 CLI에 맡기므로 앱은 환경 변수로 넘기고 CLI는 그걸 읽는다.
package enum Preferences {
    package static var enharmonic: Enharmonic = .original
    /// 그림(PNG·JPEG…) 악보를 이조한 결과를 PDF 대신 같은 종류의 그림으로 (false면 PDF)
    package static var imageOutput = true

    package static let enharmonicVariable = "SINGTOGETHER_ENHARMONIC"

    /// 환경 변수에서 읽는다 (CLI, 그리고 앱이 띄운 인식기가 부르는 CLI).
    package static func readEnvironment() {
        if let v = ProcessInfo.processInfo.environment[enharmonicVariable], let e = Enharmonic(rawValue: v) { enharmonic = e }
    }
}

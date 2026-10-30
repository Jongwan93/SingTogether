import Foundation
import Transpose

// singtogether-transpose <악보 파일> <반음> [--out <출력 파일>] [--sharps|--flats] [--pdf]
// 출력 파일을 주지 않으면 앱과 같은 규칙으로 원본 옆에 "이름 (-2, F).ext"를 만든다.
// --sharps/--flats: 새 조가 F♯/G♭처럼 둘 다 가능할 때 (기본은 원본 쪽; 환경 변수 SINGTOGETHER_ENHARMONIC도 같다).
// --pdf: 그림 악보의 결과를 그림 대신 PDF로.
// 표준 출력: 첫 줄 "원래 키 -> 새 키", 둘째 줄 만든 파일 경로.

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

let usage = "usage: singtogether-transpose <score file> <semitones> [--out <output file>] [--sharps|--flats] [--pdf]"
let switches = ["--sharps", "--flats", "--pdf"]
Preferences.readEnvironment()
if CommandLine.arguments.contains("--sharps") { Preferences.enharmonic = .sharps }
if CommandLine.arguments.contains("--flats") { Preferences.enharmonic = .flats }
if CommandLine.arguments.contains("--pdf") { Preferences.imageOutput = false }
let args = Array(CommandLine.arguments.dropFirst()).filter { !switches.contains($0) }
if args.first == "--selftest" { exit(SelfTest.run()) }
if args.first == "--svg-to-pdf" {
    // singtogether-transpose --svg-to-pdf <out.pdf> <page1.svg> [page2.svg …]   (tools/ocr의 render.py가 부른다)
    guard args.count >= 3 else { fail("usage: singtogether-transpose --svg-to-pdf <out.pdf> <page.svg>…", code: 2) }
    do {
        try SVGToPDF.convert(args[2...].map { URL(fileURLWithPath: $0) }, to: URL(fileURLWithPath: args[1]))
        print(args[1])
        exit(0)
    } catch {
        fail("svg to pdf failed: \(error)", code: 1)
    }
}
guard args.count >= 2, let semitones = Int(args[1]) else {
    fail(usage, code: 2)
}
let input = URL(fileURLWithPath: args[0])
guard Converter.kind(of: input) != nil else {
    fail("unsupported file type: \(input.lastPathComponent)", code: 1)
}

var output: URL?
if let i = args.firstIndex(of: "--out"), i + 1 < args.count {
    let url = URL(fileURLWithPath: args[i + 1])
    try? FileManager.default.removeItem(at: url)
    output = url
}

do {
    let info = Converter.keyInfo(for: input)
    let made = try Converter.convert(input, semitones: semitones, to: output)
    if Converter.kind(of: input) == .sheet {
        // 이미지·PDF는 인식 뒤에야 키를 안다. 결과 파일 이름의 "(−2, F)"에서 새 키를 보여 준다
        let name = made.deletingPathExtension().lastPathComponent
        let newKey = name.split(separator: ",").last.map { $0.trimmingCharacters(in: CharacterSet(charactersIn: " )")) } ?? "?"
        print("(recognized) -> \(newKey)")
    } else {
        print("\(info?.originalName ?? "?") -> \(info?.name(shiftedBy: semitones) ?? "?")")
    }
    print(made.path)
} catch {
    fail("transposition failed: \(error.localizedDescription)", code: 1)
}

import Foundation
import Transpose

// singtogether-transpose <악보 파일> <반음> [--out <출력 파일>]
// 출력 파일을 주지 않으면 앱과 같은 규칙으로 원본 옆에 "이름 (-2, F).ext"를 만든다.
// 표준 출력: 첫 줄 "원래 키 -> 새 키", 둘째 줄 만든 파일 경로.

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

let args = Array(CommandLine.arguments.dropFirst())
if args.first == "--selftest" { exit(SelfTest.run()) }
guard args.count >= 2, let semitones = Int(args[1]) else {
    fail("usage: singtogether-transpose <score file> <semitones> [--out <output file>]", code: 2)
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

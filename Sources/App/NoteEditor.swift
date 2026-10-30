import AppKit
import Transpose

/// 확인 창 왼쪽 그림: review.json의 음표·코드·가사 자리를 알고 있어 클릭하면 어느 것인지 찾는다.
/// 표시 없는 쪽 그림(page.png) 위에 확인할 곳을 직접 그린다: 음표 빨강(가장 의심스러운 top개는 굵게, 나머지는 옅게),
/// 코드 주황, 가사 보라, 고친 것 초록, 지금 보고 있는 것 파랑 테두리, 울리는 중인 음표 파랑 칠.
final class ReviewImageView: NSImageView {
    enum Kind { case note, chord, lyric }
    struct Spot {
        let kind: Kind
        let measure: Int
        let index: Int
        var name: String          // 음표 이름 / 코드 / 음절
        var duration: String
        let conf: Double
        let rank: Int             // 1 = 가장 의심스러움 (음표만, 나머지는 0)
        let reasons: [String]
        let x: CGFloat            // 이미지 픽셀, 상자 중심
        let y: CGFloat
        var key: String { "\(kind):\(measure):\(index)" }
    }
    var spots: [Spot] = []
    var edited: Set<String> = []
    var onPick: ((Spot, NSRect) -> Void)?
    var drawsFlags = false            // 표시 없는 그림을 쓸 때 true (review.png에는 이미 그려져 있다)
    var threshold = 0.6
    var top = 12
    var current: String? { didSet { needsDisplay = true } }      // 보고 있는 자리의 key
    var playing: String? { didSet { needsDisplay = true } }      // 울리는 중인 음표의 key
    private(set) var interline: CGFloat = 24

    /// review.json → 자리 목록 (좌표는 page.png·review.png와 같은 이미지 픽셀, 원점 왼쪽 위).
    /// 코드 상자는 음표 위 2.2il, 가사 상자는 음표 아래 2.5il (review.overlay와 같은 자리).
    func load(reviewJSON url: URL) {
        guard let data = try? Data(contentsOf: url),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { spots = []; return }
        interline = CGFloat(dict["interline"] as? Double ?? 24)
        threshold = dict["threshold"] as? Double ?? 0.6
        top = dict["top"] as? Int ?? 12
        let il = interline
        func items(_ key: String, _ kind: Kind, _ field: String, dy: CGFloat) -> [Spot] {
            ((dict[key] as? [[String: Any]]) ?? []).compactMap { n in
                guard let x = n["x"] as? Double, let y = n["y"] as? Double else { return nil }
                return Spot(kind: kind, measure: n["measure"] as? Int ?? 0, index: n["index"] as? Int ?? 0,
                            name: n[field] as? String ?? "?", duration: n["duration"] as? String ?? "",
                            conf: n["conf"] as? Double ?? 1, rank: n["rank"] as? Int ?? 0,
                            reasons: n["reasons"] as? [String] ?? [], x: x + (kind == .chord ? 0.5 * il : 0), y: y + dy)
            }
        }
        spots = items("notes", .note, "note", dy: 0) + items("chords", .chord, "chord", dy: -2.2 * il)
              + items("lyrics", .lyric, "syllable", dy: 2.5 * il)
        needsDisplay = true
    }

    /// 아직 안 고친 의심 음표를 의심스러운 순서로 (Next 키가 돈다)
    var doubtful: [Spot] {
        spots.filter { $0.kind == .note && $0.conf < threshold && !edited.contains($0.key) }.sorted { $0.rank < $1.rank }
    }

    func spot(measure: Int, index: Int, kind: Kind = .note) -> Spot? {
        spots.first { $0.kind == kind && $0.measure == measure && $0.index == index }
    }

    /// 상자 반지름 (이미지 픽셀). 음표는 넉넉하게, 코드·가사는 그려진 상자만큼.
    private func radius(_ kind: Kind) -> CGFloat {
        switch kind {
        case .note: return max(30, 0.9 * interline)
        case .chord: return 0.8 * interline
        case .lyric: return 0.7 * interline
        }
    }

    /// 이미지 픽셀(원점 왼쪽 위) → 뷰 좌표 (원점 왼쪽 아래)
    func viewPoint(_ s: Spot) -> NSPoint {
        guard let img = image else { return .zero }
        let sx = bounds.width / img.size.width, sy = bounds.height / img.size.height
        return NSPoint(x: s.x * sx, y: bounds.height - s.y * sy)
    }

    /// 클릭 상자 (뷰 좌표). 팝오버 위치와 화면에 보이게 스크롤할 때 쓴다.
    func viewRect(_ s: Spot) -> NSRect {
        guard let img = image else { return .zero }
        let q = viewPoint(s)
        let r = radius(s.kind) * bounds.width / img.size.width
        return NSRect(x: q.x - r, y: q.y - r, width: 2 * r, height: 2 * r)
    }

    override var acceptsFirstResponder: Bool { true }

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        guard let img = image else { return }
        let scale = bounds.width / img.size.width
        var best: (Spot, CGFloat)?
        for s in spots {
            let q = viewPoint(s)
            let d = hypot(q.x - p.x, q.y - p.y) / max(scale, 0.001)
            if d <= radius(s.kind) && (best == nil || d < best!.1) { best = (s, d) }
        }
        guard let (spot, _) = best else { super.mouseDown(with: event); return }
        onPick?(spot, viewRect(spot))
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let img = image else { return }
        let scale = bounds.width / img.size.width
        let il = interline * scale
        func box(_ s: Spot, _ r: CGFloat, color: NSColor, width: CGFloat, fill: Bool = false) {
            let q = viewPoint(s)
            let rect = NSRect(x: q.x - r, y: q.y - r, width: 2 * r, height: 2 * r)
            let path = NSBezierPath(roundedRect: rect, xRadius: 3, yRadius: 3)
            if fill { color.withAlphaComponent(0.25).setFill(); path.fill() }
            color.setStroke()
            path.lineWidth = width
            path.stroke()
        }
        if drawsFlags {
            let font = NSFont.systemFont(ofSize: 13 * scale, weight: .medium)
            for s in spots where s.conf < threshold && !edited.contains(s.key) {
                switch s.kind {
                case .note:
                    let strong = s.rank <= top
                    let color = NSColor.systemRed.withAlphaComponent(strong ? 1 : 0.45)
                    box(s, 0.7 * il, color: color, width: strong ? 3 : 1.2)
                    let q = viewPoint(s)
                    ("\(s.measure): \(s.name)" as NSString).draw(at: NSPoint(x: q.x - 0.7 * il, y: q.y - 0.8 * il - font.pointSize * 1.2),
                                                                   withAttributes: [.font: font, .foregroundColor: color])
                case .chord:
                    box(s, 0.6 * il, color: .systemOrange, width: 2)
                case .lyric:
                    box(s, 0.6 * il, color: .systemPurple, width: 2)
                }
            }
        }
        for s in spots where edited.contains(s.key) {
            box(s, (s.kind == .note ? 16 : 0.6 * interline) * scale, color: .systemGreen, width: 2.5)
        }
        if let playing, let s = spots.first(where: { $0.key == playing }) {
            box(s, 0.6 * il, color: .systemBlue, width: 2, fill: true)
        }
        if let current, let s = spots.first(where: { $0.key == current }) {
            box(s, radius(s.kind) * scale, color: .systemBlue, width: 2.5)
        }
    }

    func update(_ kind: Kind, measure: Int, index: Int, name: String?, duration: String? = nil) {
        if let i = spots.firstIndex(where: { $0.kind == kind && $0.measure == measure && $0.index == index }) {
            if let name { spots[i].name = name }
            if let duration { spots[i].duration = duration }
        }
        edited.insert("\(kind):\(measure):\(index)")
        needsDisplay = true
    }

    /// 음표를 지우면 그 음표의 코드·가사 자리도 없어지고, 같은 마디의 뒤쪽 번호가 하나씩 당겨진다.
    func remove(measure: Int, index: Int) {
        spots.removeAll { $0.measure == measure && $0.index == index }
        for i in spots.indices where spots[i].measure == measure && spots[i].index > index {
            let s = spots[i]
            spots[i] = Spot(kind: s.kind, measure: s.measure, index: s.index - 1, name: s.name, duration: s.duration, conf: s.conf,
                            rank: s.rank, reasons: s.reasons, x: s.x, y: s.y)
        }
        needsDisplay = true
    }
}

/// 음표 하나를 고치는 팝오버: 한 칸 위·아래, 임시표, 길이, 쉼표, 삭제, 그리고 이 음표 위의 코드. 누르는 즉시 텍스트에 반영된다.
/// 열릴 때와 음높이를 바꿀 때마다 그 음을 울려 준다. ▶ Measure는 이 마디를 들려준다.
final class NoteEditorController: NSViewController, NSTextFieldDelegate {
    private let spot: ReviewImageView.Spot
    private let apply: (NoteEdit) -> String?     // 적용하고 새 이름을 돌려준다 (삭제면 nil)
    private let playMeasure: () -> Void
    private let heading = NSTextField(labelWithString: "")
    private let chordField = NSTextField(string: "")
    enum NoteEdit { case step(Int), accidental(Int), length(String), rest, delete, chord(String) }

    private static let lengths: [(String, String)] = [("whole", "1"), ("half", "2"), ("dotted half", "2."), ("quarter", "4"), ("dotted quarter", "4."),
                                                      ("eighth", "8"), ("dotted eighth", "8."), ("16th", "16")]

    init(spot: ReviewImageView.Spot, chord: String?, playMeasure: @escaping () -> Void, apply: @escaping (NoteEdit) -> String?) {
        self.spot = spot
        self.apply = apply
        self.playMeasure = playMeasure
        super.init(nibName: nil, bundle: nil)
        chordField.stringValue = chord ?? ""
    }
    required init?(coder: NSCoder) { fatalError() }

    override func loadView() {
        heading.font = .systemFont(ofSize: 13, weight: .semibold)
        setTitle(spot.name)
        let why = spot.reasons.isEmpty ? "" : " · " + spot.reasons.joined(separator: "; ")
        let conf = NSTextField(wrappingLabelWithString: String(format: "confidence %.2f", spot.conf) + why)
        conf.font = .systemFont(ofSize: 11); conf.textColor = .secondaryLabelColor
        conf.widthAnchor.constraint(lessThanOrEqualToConstant: 360).isActive = true

        let hear = NSButton(title: "▶ Note", target: self, action: #selector(playNote))
        hear.toolTip = "Hear this note as it was read"
        let measure = NSButton(title: "▶ Measure", target: self, action: #selector(playThisMeasure))
        measure.toolTip = "Hear this measure as it was read"
        let up = NSButton(title: "▲ Step up", target: self, action: #selector(stepUp))
        let down = NSButton(title: "▼ Step down", target: self, action: #selector(stepDown))
        let acc = NSSegmentedControl(labels: ["♭", "♮", "♯"], trackingMode: .selectOne, target: self, action: #selector(accidental(_:)))
        acc.selectedSegment = spot.name.contains("#") ? 2 : (spot.name.dropFirst().contains("b") ? 0 : 1)
        let length = NSPopUpButton(frame: .zero, pullsDown: false)
        length.addItems(withTitles: Self.lengths.map { $0.0 })
        if let i = Self.lengths.firstIndex(where: { Self.lengthToken(spot.duration) == $0.1 }) { length.selectItem(at: i) }
        length.target = self; length.action = #selector(lengthChanged(_:))
        let rest = NSButton(title: "Make rest", target: self, action: #selector(toRest))
        let del = NSButton(title: "Delete", target: self, action: #selector(delete))
        del.contentTintColor = .systemRed

        chordField.placeholderString = "none"
        chordField.widthAnchor.constraint(equalToConstant: 90).isActive = true
        chordField.delegate = self
        chordField.toolTip = "Chord symbol over this note, e.g. G, D/F#, Em7. Empty removes it. Press Return to apply."
        let setChord = NSButton(title: "Set chord", target: self, action: #selector(chordApply))

        let row0 = NSStackView(views: [hear, measure]); row0.spacing = 8
        let row1 = NSStackView(views: [up, down, acc]); row1.spacing = 8
        let row2 = NSStackView(views: [NSTextField(labelWithString: "Length"), length, rest, del]); row2.spacing = 8
        let row3 = NSStackView(views: [NSTextField(labelWithString: "Chord"), chordField, setChord]); row3.spacing = 8
        let stack = NSStackView(views: [heading, conf, row0, row1, row2, row3])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 14, bottom: 12, right: 14)
        view = stack
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        playNote()
    }

    private func setTitle(_ name: String) {
        heading.stringValue = "Measure \(spot.measure) · note \(spot.index + 1): \(name)"
    }
    private func did(_ e: NoteEdit) {
        if let name = apply(e) {
            setTitle(name)
            if case .length = e {} else { hear(name) }
        } else {
            view.window?.close()
        }
    }
    private func hear(_ name: String) {
        if let midi = CompactText.midi(ofName: name) { MelodyPlayer.shared.play(midi: midi) }
    }
    @objc private func playNote() { hear(heading.stringValue.components(separatedBy: ": ").last ?? spot.name) }
    @objc private func playThisMeasure() { playMeasure() }
    @objc private func stepUp() { did(.step(1)) }
    @objc private func stepDown() { did(.step(-1)) }
    @objc private func accidental(_ s: NSSegmentedControl) { did(.accidental(s.selectedSegment - 1)) }
    @objc private func lengthChanged(_ p: NSPopUpButton) { did(.length(Self.lengths[p.indexOfSelectedItem].1)) }
    @objc private func toRest() { did(.rest) }
    @objc private func delete() { did(.delete) }
    @objc private func chordApply() { _ = apply(.chord(chordField.stringValue)) }
    func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        if selector == #selector(NSResponder.insertNewline(_:)) { chordApply(); return true }
        return false
    }

    /// review.json의 길이("1/2", "3/4", "1")를 텍스트 토큰("8", "4.")으로
    static func lengthToken(_ d: String) -> String {
        let parts = d.split(separator: "/").compactMap { Int($0) }
        let num = parts.first ?? 1, den = parts.count > 1 ? parts[1] : 1
        let q = Double(num) / Double(den)                 // 4분음표 = 1
        for (base, tok) in [(4.0, "1"), (2.0, "2"), (1.0, "4"), (0.5, "8"), (0.25, "16")] {
            if abs(q - base) < 1e-6 { return tok }
            if abs(q - base * 1.5) < 1e-6 { return tok + "." }
        }
        return "4"
    }
}

/// 코드 기호나 가사 음절 하나를 고치는 팝오버: 글자 칸 + 적용 / 지우기. Return으로 적용.
final class TextEditorController: NSViewController, NSTextFieldDelegate {
    private let spot: ReviewImageView.Spot
    private let apply: (String?) -> Void          // nil = 지우기
    private let field = NSTextField(string: "")

    init(spot: ReviewImageView.Spot, apply: @escaping (String?) -> Void) {
        self.spot = spot
        self.apply = apply
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError() }

    override func loadView() {
        let isChord = spot.kind == .chord
        let heading = NSTextField(labelWithString: "Measure \(spot.measure) · \(isChord ? "chord" : "lyric") on note \(spot.index + 1)")
        heading.font = .systemFont(ofSize: 13, weight: .semibold)
        let why = spot.reasons.isEmpty ? "" : " · " + spot.reasons.joined(separator: "; ")
        let conf = NSTextField(wrappingLabelWithString: String(format: "confidence %.2f · read as “%@”", spot.conf, spot.name) + why)
        conf.font = .systemFont(ofSize: 11); conf.textColor = .secondaryLabelColor
        conf.widthAnchor.constraint(lessThanOrEqualToConstant: 320).isActive = true
        field.stringValue = spot.name
        field.widthAnchor.constraint(equalToConstant: 140).isActive = true
        field.delegate = self
        field.placeholderString = isChord ? "e.g. D/F#" : "syllable"
        let ok = NSButton(title: "Apply", target: self, action: #selector(applyText))
        ok.keyEquivalent = "\r"
        let clear = NSButton(title: isChord ? "Remove chord" : "No syllable", target: self, action: #selector(clearText))
        clear.contentTintColor = .systemRed
        let row = NSStackView(views: [field, ok, clear]); row.spacing = 8
        let stack = NSStackView(views: [heading, conf, row])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 14, bottom: 12, right: 14)
        view = stack
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        view.window?.makeFirstResponder(field)
        field.selectText(nil)
    }

    @objc private func applyText() { apply(field.stringValue); view.window?.close() }
    @objc private func clearText() { apply(nil); view.window?.close() }
    func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        if selector == #selector(NSResponder.insertNewline(_:)) { applyText(); return true }
        return false
    }
}

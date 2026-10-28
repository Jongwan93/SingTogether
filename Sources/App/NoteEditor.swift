import AppKit
import Transpose

/// 확인 창 왼쪽 그림: review.json의 음표 자리를 알고 있어 클릭하면 어느 음표인지 찾는다. 고친 음표는 초록 테두리.
final class ReviewImageView: NSImageView {
    struct Spot { let measure: Int; let index: Int; var name: String; var duration: String; let conf: Double; let x: CGFloat; let y: CGFloat }
    var spots: [Spot] = []
    var edited: Set<String> = []           // "measure:index"
    var onPick: ((Spot, NSRect) -> Void)?
    var hitRadius: CGFloat = 30            // 이미지 픽셀

    /// review.json → 자리 목록 (좌표는 review.png와 같은 이미지 픽셀, 원점 왼쪽 위)
    func load(reviewJSON url: URL) {
        guard let data = try? Data(contentsOf: url),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let notes = dict["notes"] as? [[String: Any]] else { spots = []; return }
        spots = notes.compactMap { n in
            guard let x = n["x"] as? Double, let y = n["y"] as? Double else { return nil }
            return Spot(measure: n["measure"] as? Int ?? 0, index: n["index"] as? Int ?? 0, name: n["note"] as? String ?? "?",
                        duration: n["duration"] as? String ?? "", conf: n["conf"] as? Double ?? 1, x: x, y: y)
        }
        needsDisplay = true
    }

    /// 이미지 픽셀(원점 왼쪽 위) → 뷰 좌표 (원점 왼쪽 아래)
    private func viewPoint(_ s: Spot) -> NSPoint {
        guard let img = image else { return .zero }
        let sx = bounds.width / img.size.width, sy = bounds.height / img.size.height
        return NSPoint(x: s.x * sx, y: bounds.height - s.y * sy)
    }

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        guard let img = image else { return }
        let scale = bounds.width / img.size.width
        var best: (Spot, CGFloat)?
        for s in spots {
            let q = viewPoint(s)
            let d = hypot(q.x - p.x, q.y - p.y)
            if d <= hitRadius * scale && (best == nil || d < best!.1) { best = (s, d) }
        }
        guard let (spot, _) = best else { super.mouseDown(with: event); return }
        let q = viewPoint(spot)
        let r = hitRadius * scale
        onPick?(spot, NSRect(x: q.x - r, y: q.y - r, width: 2 * r, height: 2 * r))
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let img = image else { return }
        let scale = bounds.width / img.size.width
        let r = 16 * scale
        NSColor.systemGreen.setStroke()
        for s in spots where edited.contains("\(s.measure):\(s.index)") {
            let q = viewPoint(s)
            let path = NSBezierPath(roundedRect: NSRect(x: q.x - r, y: q.y - r, width: 2 * r, height: 2 * r), xRadius: 3, yRadius: 3)
            path.lineWidth = 2.5
            path.stroke()
        }
    }

    func update(measure: Int, index: Int, name: String?, duration: String?) {
        if let i = spots.firstIndex(where: { $0.measure == measure && $0.index == index }) {
            if let name { spots[i].name = name }
            if let duration { spots[i].duration = duration }
        }
        edited.insert("\(measure):\(index)")
        needsDisplay = true
    }

    func remove(measure: Int, index: Int) {
        spots.removeAll { $0.measure == measure && $0.index == index }
        for i in spots.indices where spots[i].measure == measure && spots[i].index > index {
            let s = spots[i]
            spots[i] = Spot(measure: s.measure, index: s.index - 1, name: s.name, duration: s.duration, conf: s.conf, x: s.x, y: s.y)
        }
        needsDisplay = true
    }
}

/// 음표 하나를 고치는 팝오버: 한 칸 위·아래, 임시표, 길이, 쉼표, 삭제. 누르는 즉시 텍스트에 반영된다.
final class NoteEditorController: NSViewController {
    private let spot: ReviewImageView.Spot
    private let apply: (NoteEdit) -> String?     // 적용하고 새 이름을 돌려준다 (삭제면 nil)
    private let heading = NSTextField(labelWithString: "")
    enum NoteEdit { case step(Int), accidental(Int), length(String), rest, delete }

    private static let lengths: [(String, String)] = [("온음표", "1"), ("2분", "2"), ("점2분", "2."), ("4분", "4"), ("점4분", "4."),
                                                      ("8분", "8"), ("점8분", "8."), ("16분", "16")]

    init(spot: ReviewImageView.Spot, apply: @escaping (NoteEdit) -> String?) {
        self.spot = spot
        self.apply = apply
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError() }

    override func loadView() {
        heading.font = .systemFont(ofSize: 13, weight: .semibold)
        setTitle(spot.name)
        let conf = NSTextField(labelWithString: String(format: "신뢰도 %.2f", spot.conf))
        conf.font = .systemFont(ofSize: 11); conf.textColor = .secondaryLabelColor

        let up = NSButton(title: "▲ 한 칸 위", target: self, action: #selector(stepUp))
        let down = NSButton(title: "▼ 한 칸 아래", target: self, action: #selector(stepDown))
        let acc = NSSegmentedControl(labels: ["♭", "♮", "♯"], trackingMode: .selectOne, target: self, action: #selector(accidental(_:)))
        acc.selectedSegment = spot.name.contains("#") ? 2 : (spot.name.dropFirst().contains("b") ? 0 : 1)
        let length = NSPopUpButton(frame: .zero, pullsDown: false)
        length.addItems(withTitles: Self.lengths.map { $0.0 })
        if let i = Self.lengths.firstIndex(where: { Self.lengthToken(spot.duration) == $0.1 }) { length.selectItem(at: i) }
        length.target = self; length.action = #selector(lengthChanged(_:))
        let rest = NSButton(title: "쉼표로", target: self, action: #selector(toRest))
        let del = NSButton(title: "삭제", target: self, action: #selector(delete))
        del.contentTintColor = .systemRed

        let row1 = NSStackView(views: [up, down, acc]); row1.spacing = 8
        let row2 = NSStackView(views: [NSTextField(labelWithString: "길이"), length, rest, del]); row2.spacing = 8
        let stack = NSStackView(views: [heading, conf, row1, row2])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 14, bottom: 12, right: 14)
        view = stack
    }

    private func setTitle(_ name: String) {
        heading.stringValue = "마디 \(spot.measure) · \(spot.index + 1)번째 음: \(name)"
    }
    private func did(_ e: NoteEdit) {
        if let name = apply(e) { setTitle(name) } else { view.window?.close() }
    }
    @objc private func stepUp() { did(.step(1)) }
    @objc private func stepDown() { did(.step(-1)) }
    @objc private func accidental(_ s: NSSegmentedControl) { did(.accidental(s.selectedSegment - 1)) }
    @objc private func lengthChanged(_ p: NSPopUpButton) { did(.length(Self.lengths[p.indexOfSelectedItem].1)) }
    @objc private func toRest() { did(.rest) }
    @objc private func delete() { did(.delete) }

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

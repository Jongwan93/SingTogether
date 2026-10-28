import AppKit
import Transpose

/// 커서 주위에 뜨는 −5…+5 키 링. 파일을 칸 위에 놓으면 onDrop(반음, 파일들) 호출.
final class RingView: NSView {
    static let size: CGFloat = 320
    private let inner: CGFloat = 48
    private let outer: CGFloat = 150
    private let half: CGFloat = 18      // 칸 하나 = 36°

    var keyInfo: KeyInfo? { didSet { needsDisplay = true } }
    var hovered: Int? { didSet { if hovered != oldValue { needsDisplay = true } } }
    var message: String? { didSet { needsDisplay = true } }
    var onDrop: ((Int, [URL]) -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes([.fileURL])
    }
    required init?(coder: NSCoder) { fatalError() }

    /// +1…+5는 위쪽에서 시계 방향(오른쪽), −1…−5는 위쪽에서 반시계 방향(왼쪽)
    private func centerAngle(_ k: Int) -> CGFloat {
        k > 0 ? 72 - 36 * CGFloat(k - 1) : 108 + 36 * CGFloat(-k - 1)
    }
    private static let semis = [1, 2, 3, 4, 5, -1, -2, -3, -4, -5]
    private var mid: NSPoint { NSPoint(x: bounds.midX, y: bounds.midY) }

    private func segment(at p: NSPoint) -> Int? {
        let dx = p.x - mid.x, dy = p.y - mid.y
        let r = hypot(dx, dy)
        guard r >= inner, r <= outer + 8 else { return nil }
        var a = atan2(dy, dx) * 180 / .pi
        if a < 0 { a += 360 }
        return Self.semis.first { k in
            var diff = abs(a - (centerAngle(k).truncatingRemainder(dividingBy: 360) + 360).truncatingRemainder(dividingBy: 360))
            if diff > 180 { diff = 360 - diff }
            return diff <= half
        }
    }

    // MARK: 그리기

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill(); bounds.fill()

        if let message {
            drawMessage(message); return
        }

        for k in Self.semis {
            let c = centerAngle(k)
            let path = NSBezierPath()
            path.appendArc(withCenter: mid, radius: outer, startAngle: c - half + 1, endAngle: c + half - 1)
            path.appendArc(withCenter: mid, radius: inner + 4, startAngle: c + half - 1, endAngle: c - half + 1, clockwise: true)
            path.close()

            let isHot = hovered == k
            let base = k > 0 ? NSColor(calibratedRed: 0.16, green: 0.20, blue: 0.27, alpha: 0.92)
                             : NSColor(calibratedRed: 0.20, green: 0.17, blue: 0.25, alpha: 0.92)
            (isHot ? NSColor.controlAccentColor : base).setFill()
            path.fill()
            NSColor(white: 1, alpha: isHot ? 0.6 : 0.12).setStroke()
            path.lineWidth = 1; path.stroke()

            let rad = c * .pi / 180
            let rText = (inner + outer) / 2 + 6
            let center = NSPoint(x: mid.x + cos(rad) * rText, y: mid.y + sin(rad) * rText)
            let numStr = k > 0 ? "+\(k)" : "\(k)"
            if let info = keyInfo {
                drawText(info.name(shiftedBy: k), at: NSPoint(x: center.x, y: center.y + 4),
                         font: .monospacedSystemFont(ofSize: isHot ? 19 : 16, weight: .semibold), alpha: 1)
                drawText(numStr, at: NSPoint(x: center.x, y: center.y - 13),
                         font: .monospacedDigitSystemFont(ofSize: 11, weight: .medium), alpha: 0.7)
            } else {
                drawText(numStr, at: center,
                         font: .monospacedDigitSystemFont(ofSize: isHot ? 20 : 17, weight: .semibold), alpha: 1)
            }
        }

        // 가운데: 원래 키 / 취소 영역
        let hub = NSBezierPath(ovalIn: NSRect(x: mid.x - inner, y: mid.y - inner, width: inner * 2, height: inner * 2))
        NSColor(white: 0.08, alpha: 0.9).setFill(); hub.fill()
        drawText(keyInfo?.originalName ?? "♪", at: NSPoint(x: mid.x, y: mid.y + 6),
                 font: .monospacedSystemFont(ofSize: 17, weight: .bold), alpha: 1)
        drawText("Drop here to cancel", at: NSPoint(x: mid.x, y: mid.y - 14),
                 font: .systemFont(ofSize: 9), alpha: 0.6)
    }

    private func drawText(_ s: String, at p: NSPoint, font: NSFont, alpha: CGFloat) {
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor(white: 1, alpha: alpha)]
        let size = (s as NSString).size(withAttributes: attrs)
        (s as NSString).draw(at: NSPoint(x: p.x - size.width / 2, y: p.y - size.height / 2), withAttributes: attrs)
    }

    private func drawMessage(_ s: String) {
        let style = NSMutableParagraphStyle(); style.alignment = .center
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 13, weight: .medium),
            .foregroundColor: NSColor.white, .paragraphStyle: style]
        let maxW: CGFloat = 280
        let textRect = (s as NSString).boundingRect(with: NSSize(width: maxW - 28, height: 200),
                                                    options: [.usesLineFragmentOrigin], attributes: attrs)
        let box = NSRect(x: mid.x - maxW / 2, y: mid.y - textRect.height / 2 - 14,
                         width: maxW, height: textRect.height + 28)
        NSColor(white: 0.1, alpha: 0.92).setFill()
        NSBezierPath(roundedRect: box, xRadius: 12, yRadius: 12).fill()
        (s as NSString).draw(with: box.insetBy(dx: 14, dy: 14), options: [.usesLineFragmentOrigin], attributes: attrs)
    }

    // MARK: 드래그 받기

    private func update(_ info: NSDraggingInfo) -> NSDragOperation {
        guard message == nil else { return [] }
        hovered = segment(at: convert(info.draggingLocation, from: nil))
        guard hovered != nil else { return [] }
        // 원본은 절대 건드리지 않음. Finder가 허용하는 작업 중 복사를 우선 선택.
        let mask = info.draggingSourceOperationMask
        for op: NSDragOperation in [.copy, .generic, .link, .move] where mask.contains(op) { return op }
        return []
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { update(sender) }
    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation { update(sender) }
    override func draggingExited(_ sender: NSDraggingInfo?) { hovered = nil }
    override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool { hovered != nil }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard let k = hovered else { return false }
        let urls = (sender.draggingPasteboard.readObjects(
            forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
        guard !urls.isEmpty else { return false }
        DispatchQueue.main.async { [weak self] in self?.onDrop?(k, urls) }
        return true
    }
}

final class RingPanel: NSPanel {
    init() {
        super.init(contentRect: NSRect(x: 0, y: 0, width: RingView.size, height: RingView.size),
                   styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
        level = .popUpMenu
        hidesOnDeactivate = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
    }
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    func show(centeredAt p: NSPoint) {
        let s = RingView.size
        var origin = NSPoint(x: p.x - s / 2, y: p.y - s / 2)
        if let screen = NSScreen.screens.first(where: { NSMouseInRect(p, $0.frame, false) }) {
            let f = screen.frame
            origin.x = min(max(origin.x, f.minX), f.maxX - s)
            origin.y = min(max(origin.y, f.minY), f.maxY - s)
        }
        setFrameOrigin(origin)
        orderFrontRegardless()
    }
}

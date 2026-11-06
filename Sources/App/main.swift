import AppKit

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)

/// 메뉴 막대 앱(accessory)은 창이 떠 있어도 Dock과 ⌘Tab에 안 나온다. 제목 있는 창(결과 확인·설정)이 하나라도 보이는 동안은
/// 일반 앱으로 두어 ⌘Tab으로 돌아올 수 있게 하고, 다 닫히면 메뉴 막대 앱으로 돌아간다. 링(제목 없는 패널)은 세지 않는다.
private func updateActivationPolicy(closing: NSWindow? = nil) {
    let open = app.windows.contains { $0 !== closing && $0.isVisible && $0.styleMask.contains(.titled) && !($0 is NSPanel) }
    let want: NSApplication.ActivationPolicy = open ? .regular : .accessory
    guard app.activationPolicy() != want else { return }
    app.setActivationPolicy(want)
    if want == .regular { app.activate(ignoringOtherApps: true) }
}
let center = NotificationCenter.default
for name in [NSWindow.didBecomeKeyNotification, NSWindow.didChangeOcclusionStateNotification, NSWindow.didDeminiaturizeNotification] {
    center.addObserver(forName: name, object: nil, queue: .main) { _ in updateActivationPolicy() }
}
center.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { note in
    updateActivationPolicy(closing: note.object as? NSWindow)
}
app.run()

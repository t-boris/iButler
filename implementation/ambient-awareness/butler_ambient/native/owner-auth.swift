import AppKit
import LocalAuthentication

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value) {
        FileHandle.standardOutput.write(data + Data([10]))
    }
}

final class SetupWindow: NSObject, NSWindowDelegate {
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 570, height: 330), styleMask: [.titled, .closable], backing: .buffered, defer: false)
    let progress = NSTextField(wrappingLabelWithString: "После начала говорите 15 секунд обычным голосом. В комнате должны говорить только вы.")
    let start = NSButton(title: "Начать 15 секунд", target: nil, action: nil)
    var timer: Timer?
    var seconds = 15
    init(challenge: String, guestName: String? = nil) {
        super.init()
        window.title = guestName.map { "Butler — знакомство: " + $0 } ?? "Butler — подтверждение голоса владельца"
        if guestName != nil { progress.stringValue = "После начала перед камерой должен быть один гость. Говорит только он, 15 секунд. Владелец подтверждает регистрацию своей учётной записью Mac." }
        window.delegate = self
        window.isReleasedWhenClosed = false
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 18
        let title = NSTextField(labelWithString: "Прочтите каждую цифру отдельно")
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        let code = NSTextField(labelWithString: challenge.map(String.init).joined(separator: "  "))
        code.font = .monospacedDigitSystemFont(ofSize: 34, weight: .medium)
        let detail = NSTextField(wrappingLabelWithString: guestName == nil ? "Затем расскажите пару предложений обычным голосом. Только локально; аудиозапись и расшифровка удаляются после проверки. Лицо и прежние эталоны сохраняются." : "Гость читает цифры и говорит обычным голосом. Сохранятся только его локальные эталоны лица и голоса с указанным именем. Материалы регистрации удаляются; обычный архив приостановлен на эти 15 секунд.")
        detail.textColor = .secondaryLabelColor
        start.target = self; start.action = #selector(begin)
        let cancel = NSButton(title: "Отмена", target: self, action: #selector(cancel))
        let actions = NSStackView(views: [start,cancel]); actions.spacing = 12
        for view in [title,code,progress,detail,actions] { stack.addArrangedSubview(view) }
        window.contentView?.addSubview(stack)
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo:window.contentView!.leadingAnchor,constant:26),
            stack.trailingAnchor.constraint(equalTo:window.contentView!.trailingAnchor,constant:-26),
            stack.topAnchor.constraint(equalTo:window.contentView!.topAnchor,constant:24)])
    }
    func show() { window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true) }
    @objc func begin() {
        start.isEnabled = false
        emit(["authorized":true,"start":true])
        progress.stringValue = "Запись: 15 секунд. Прочтите цифры, затем говорите обычным голосом."
        timer = Timer.scheduledTimer(withTimeInterval:1,repeats:true) { [weak self] timer in
            guard let self = self else { timer.invalidate(); return }
            self.seconds -= 1
            self.progress.stringValue = self.seconds > 0 ? "Осталось \(self.seconds) секунд. Говорите обычным голосом." : "Проверяю контрольную фразу и обновляю локальные эталоны…"
            if self.seconds <= 0 { timer.invalidate() }
        }
    }
    @objc func cancel() { emit(["cancelled":true]); exit(1) }
    func windowShouldClose(_ sender: NSWindow) -> Bool { cancel(); return false }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
func runApplication() {
    // The same event loop is used by registration and the native click test.
    app.run()
}
if CommandLine.arguments.contains("--test-layout") {
    let view = SetupWindow(challenge:"123456")
    view.window.contentView?.layoutSubtreeIfNeeded()
    guard view.window.frame.width == 570, view.start.isEnabled else { exit(1) }
    print("PASS owner setup layout; no authentication or device access")
    exit(0)
}
if CommandLine.arguments.contains("--test-events") {
    // Isolated synthetic window: no LocalAuthentication or microphone access.
    let view = SetupWindow(challenge:"123456")
    view.show()
    view.window.contentView?.layoutSubtreeIfNeeded()
    let point = view.start.convert(NSPoint(x:view.start.bounds.midX,y:view.start.bounds.midY),to:nil)
    guard view.window.contentView?.hitTest(view.window.contentView!.convert(point,from:nil)) != nil,
          view.start.isEnabled, view.start.target === view,
          view.start.action == #selector(SetupWindow.begin) else { exit(1) }
    DispatchQueue.main.async {
        for kind: NSEvent.EventType in [.leftMouseDown,.leftMouseUp] {
            guard let event = NSEvent.mouseEvent(with:kind,location:point,modifierFlags:[],timestamp:ProcessInfo.processInfo.systemUptime,
                windowNumber:view.window.windowNumber,context:nil,eventNumber:0,clickCount:1,pressure:1) else { exit(1) }
            app.postEvent(event,atStart:false)
        }
    }
    let check = Timer.scheduledTimer(withTimeInterval:0.05,repeats:true) { _ in
        if !view.start.isEnabled {
            print("PASS native Start mouse event dispatched; no authentication or device access")
            exit(0)
        }
    }
    _ = check
    DispatchQueue.main.asyncAfter(deadline:.now()+3) { print("FAIL native Start event was not dispatched"); exit(1) }
    runApplication()
    exit(1)
}
guard (2...3).contains(CommandLine.arguments.count),
      CommandLine.arguments[1].range(of:"^[1-9][0-9]{5}$",options:.regularExpression) != nil else { exit(2) }
app.activate(ignoringOtherApps:true)
let challenge = CommandLine.arguments[1]
let guestName = CommandLine.arguments.count == 3 ? CommandLine.arguments[2] : nil
if let name = guestName, name.isEmpty || name.count > 80 || name.unicodeScalars.contains(where: { $0.value < 32 }) { exit(2) }
let context = LAContext()
var authError: NSError?
guard context.canEvaluatePolicy(.deviceOwnerAuthentication,error:&authError) else { emit(["authorized":false]); exit(3) }
var setup: SetupWindow?
context.evaluatePolicy(.deviceOwnerAuthentication,localizedReason:guestName == nil ? "Обновить голос владельца Butler. Эталоны останутся на этом Mac." : "Представить гостя Butler и сохранить его локальные эталоны лица и голоса.") { success,_ in
    DispatchQueue.main.async {
        guard success else { emit(["authorized":false]); exit(4) }
        setup = SetupWindow(challenge:challenge,guestName:guestName)
        setup?.show()
    }
}
DispatchQueue.global().async {
    while let line = readLine() {
        if line == "done" { exit(0) }
        if line == "cancel" { emit(["cancelled":true]); exit(1) }
    }
    exit(1)
}
// AppKit must dispatch native mouse/key events, not just timers and callbacks.
// Running the bare RunLoop displays the window but leaves its buttons inert.
runApplication()

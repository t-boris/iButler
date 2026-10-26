import AppKit
import Foundation
import Darwin
import WebKit
import AVFoundation
import AVKit

func previewImage(_ preview: [String: Any]) -> NSImage? {
    if let encoded = preview["jpeg_base64"] as? String, let data = Data(base64Encoded: encoded) {
        return NSImage(data: data)
    }
    if let path = preview["path"] as? String { return NSImage(contentsOfFile: path) }
    return nil
}

// Console look: one dark phosphor palette and one monospaced face for the whole window.
enum Theme {
    static let bg = NSColor(srgbRed: 0.016, green: 0.031, blue: 0.020, alpha: 1)
    static let panel = NSColor(srgbRed: 0.035, green: 0.067, blue: 0.043, alpha: 1)
    static let raised = NSColor(srgbRed: 0.06, green: 0.11, blue: 0.07, alpha: 1)
    static let line = NSColor(srgbRed: 0.11, green: 0.26, blue: 0.14, alpha: 1)
    static let text = NSColor(srgbRed: 0.62, green: 0.95, blue: 0.62, alpha: 1)
    static let bright = NSColor(srgbRed: 0.88, green: 1.0, blue: 0.88, alpha: 1)
    static let dim = NSColor(srgbRed: 0.33, green: 0.56, blue: 0.36, alpha: 1)
    static let accent = NSColor(srgbRed: 0.22, green: 1.0, blue: 0.42, alpha: 1)
    static let owner = NSColor(srgbRed: 0.55, green: 0.85, blue: 1.0, alpha: 1)
    static let notice = NSColor(srgbRed: 1.0, green: 0.72, blue: 0.33, alpha: 1)
    static let warn = notice
    static let error = NSColor(srgbRed: 1.0, green: 0.38, blue: 0.38, alpha: 1)
    static func font(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont { .monospacedSystemFont(ofSize: size, weight: weight) }
    // Colors and faces that AppKit assigned by default; explicit choices are kept.
    static func apply(_ view: NSView) {
        if let field = view as? NSTextField {
            if let font = field.font, !font.fontDescriptor.symbolicTraits.contains(.monoSpace) { field.font = Theme.font(font.pointSize) }
            if field.textColor == .labelColor || field.textColor == .controlTextColor { field.textColor = text }
            if field.isEditable { field.drawsBackground = true; field.backgroundColor = raised; field.textColor = bright }
        }
        if let table = view as? NSTableView { table.backgroundColor = panel; table.usesAlternatingRowBackgroundColors = false; table.gridColor = line }
        if let scroll = view as? NSScrollView, scroll.drawsBackground { scroll.backgroundColor = panel }
        if let box = view as? NSBox, box.boxType == .separator { box.borderColor = line }
        for child in view.subviews { apply(child) }
    }
}

struct FeedEntry: Equatable {
    let id: String, at: Double, who: String, kind: String, label: String, text: String, image: String?
    init(_ value: [String: Any]) {
        id = value["id"] as? String ?? UUID().uuidString
        at = value["at"] as? Double ?? 0
        who = value["who"] as? String ?? "Система"
        kind = value["kind"] as? String ?? "event"
        label = value["label"] as? String ?? ""
        text = value["text"] as? String ?? ""
        image = value["image"] as? String
    }
    var tint: NSColor {
        switch kind {
        case "heard": return Theme.owner
        case "reply", "image", "display": return Theme.accent
        case "notification": return Theme.notice
        case "error": return Theme.error
        default: return Theme.dim
        }
    }
}

// One entry of the dialogue timeline: a stamp line (when · who · what), optional text and optional picture.
final class FeedEntryView: NSView {
    let stamp = NSTextField(labelWithString: "")
    let body = NSTextField(wrappingLabelWithString: "")
    let picture = IntrinsicFreeImageView()
    private(set) var entry: FeedEntry
    var widthConstrained = false
    private var pictureHeight: NSLayoutConstraint!
    init(_ entry: FeedEntry) {
        self.entry = entry
        super.init(frame: .zero)
        wantsLayer = true
        stamp.font = Theme.font(11, .medium)
        stamp.lineBreakMode = .byTruncatingTail
        stamp.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        body.font = Theme.font(13)
        body.isSelectable = true
        body.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        picture.imageScaling = .scaleProportionallyDown
        picture.imageAlignment = .alignTopLeft
        picture.wantsLayer = true
        picture.layer?.cornerRadius = 6
        picture.layer?.masksToBounds = true
        for child in [stamp, body, picture] { addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        pictureHeight = picture.heightAnchor.constraint(equalToConstant: 0)
        NSLayoutConstraint.activate([
            stamp.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10), stamp.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
            stamp.topAnchor.constraint(equalTo: topAnchor, constant: 6),
            body.leadingAnchor.constraint(equalTo: stamp.leadingAnchor), body.trailingAnchor.constraint(equalTo: stamp.trailingAnchor),
            body.topAnchor.constraint(equalTo: stamp.bottomAnchor, constant: 3),
            picture.leadingAnchor.constraint(equalTo: stamp.leadingAnchor), picture.widthAnchor.constraint(lessThanOrEqualToConstant: 360),
            picture.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor, constant: -20),
            picture.topAnchor.constraint(equalTo: body.bottomAnchor, constant: 4), pictureHeight,
            picture.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -6)])
        update(entry)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(_ entry: FeedEntry) {
        let changed = self.entry != entry || stamp.stringValue.isEmpty
        self.entry = entry
        guard changed else { return }
        let date = Date(timeIntervalSince1970: entry.at)
        let formatter = DateFormatter(); formatter.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm:ss" : "dd.MM HH:mm:ss"
        let compact = entry.kind == "event"
        stamp.stringValue = "\(formatter.string(from: date))  ▌\(entry.who)  ·  \(entry.label)" + (compact && !entry.text.isEmpty ? "  ·  \(entry.text)" : "")
        stamp.textColor = entry.tint
        body.stringValue = compact ? "" : entry.text
        body.textColor = entry.kind == "heard" ? Theme.bright : entry.kind == "error" ? Theme.error : Theme.text
        body.isHidden = body.stringValue.isEmpty
        let image = entry.image.flatMap { NSImage(contentsOfFile: $0) }
        picture.image = image
        pictureHeight.constant = image == nil ? 0 : 180
        layer?.backgroundColor = (entry.kind == "notification" ? Theme.notice.withAlphaComponent(0.06) : entry.kind == "heard" ? Theme.owner.withAlphaComponent(0.05) : NSColor.clear).cgColor
    }
}

// The dialogue timeline. Newest at the bottom; follows new entries only while the reader is already there.
final class FeedView: NSView {
    let scroll = NSScrollView()
    let stack = VerticalStackView()
    let placeholder = NSTextField(wrappingLabelWithString: "Пока тихо. Реплики, события, снимки и уведомления появятся здесь.")
    let eventsToggle = NSButton(checkboxWithTitle: "служебные события", target: nil, action: nil)
    private var views: [String: FeedEntryView] = [:]
    private var entries: [FeedEntry] = []
    private var filled = false
    override init(frame: NSRect) {
        super.init(frame: frame)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 2
        stack.edgeInsets = NSEdgeInsets(top: 6, left: 4, bottom: 10, right: 4)
        scroll.hasVerticalScroller = true
        scroll.scrollerStyle = .legacy
        scroll.autohidesScrollers = false
        scroll.drawsBackground = true
        scroll.backgroundColor = Theme.bg
        scroll.documentView = stack
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([stack.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            stack.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor), stack.topAnchor.constraint(equalTo: scroll.contentView.topAnchor)])
        placeholder.font = Theme.font(12)
        placeholder.textColor = Theme.dim
        placeholder.alignment = .center
        eventsToggle.state = .on
        eventsToggle.font = Theme.font(11)
        eventsToggle.target = self; eventsToggle.action = #selector(refilter)
        for child in [scroll, placeholder] { addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([scroll.leadingAnchor.constraint(equalTo: leadingAnchor), scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            scroll.topAnchor.constraint(equalTo: topAnchor), scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
            placeholder.centerXAnchor.constraint(equalTo: centerXAnchor), placeholder.centerYAnchor.constraint(equalTo: centerYAnchor),
            placeholder.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor, constant: -40)])
        wantsLayer = true
        layer?.cornerRadius = 6
        layer?.borderWidth = 1
        layer?.borderColor = Theme.line.cgColor
        layer?.masksToBounds = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    var documentHeight: CGFloat { stack.fittingSize.height }
    var atBottom: Bool {
        let bounds = scroll.contentView.bounds
        return bounds.maxY >= documentHeight - 12
    }
    func scrollToBottom() {
        scroll.contentView.layoutSubtreeIfNeeded()
        let offset = max(0, documentHeight - scroll.contentView.bounds.height)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: offset))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    func scrollToTop() { scroll.contentView.scroll(to: .zero); scroll.reflectScrolledClipView(scroll.contentView) }
    @objc func refilter() { show(entries, force: true) }
    var visibleEntries: [FeedEntry] { entries.filter { eventsToggle.state == .on || $0.kind != "event" } }
    func show(_ entries: [FeedEntry], force: Bool = false) {
        let unchanged = entries == self.entries
        self.entries = entries
        if unchanged && !force { return }
        let follow = !filled || atBottom
        let anchor = follow ? nil : readingAnchor()
        let shown = visibleEntries
        var kept: [String: FeedEntryView] = [:]
        var ordered: [FeedEntryView] = []
        for entry in shown {
            let view = views[entry.id] ?? FeedEntryView(entry)
            view.update(entry)
            kept[entry.id] = view
            ordered.append(view)
        }
        // Rows that left the timeline go; every other row stays where it is, so nothing is rebuilt.
        for view in stack.arrangedSubviews where kept[(view as? FeedEntryView)?.entry.id ?? ""] == nil {
            stack.removeArrangedSubview(view); view.removeFromSuperview()
        }
        views = kept
        for (index, view) in ordered.enumerated() {
            if index < stack.arrangedSubviews.count, stack.arrangedSubviews[index] === view { continue }
            if view.superview === stack { stack.removeArrangedSubview(view); view.removeFromSuperview() }
            stack.insertArrangedSubview(view, at: min(index, stack.arrangedSubviews.count))
            view.translatesAutoresizingMaskIntoConstraints = false
            if !view.widthConstrained {
                view.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -8).isActive = true
                view.widthConstrained = true
            }
        }
        placeholder.isHidden = !shown.isEmpty
        stack.layoutSubtreeIfNeeded()
        if follow { scrollToBottom() } else if let anchor { keep(anchor) }
        filled = filled || !shown.isEmpty
    }
    // The row the reader is looking at and where it sits in the viewport.
    private func readingAnchor() -> (id: String, offset: CGFloat)? {
        let top = scroll.contentView.bounds.origin.y
        for case let view as FeedEntryView in stack.arrangedSubviews where view.frame.maxY > top {
            return (view.entry.id, view.frame.minY - top)
        }
        return nil
    }
    private func keep(_ anchor: (id: String, offset: CGFloat)) {
        guard let view = views[anchor.id], view.superview === stack else { return }
        let limit = max(0, documentHeight - scroll.contentView.bounds.height)
        let y = max(0, min(view.frame.minY - anchor.offset, limit))
        scroll.contentView.scroll(to: NSPoint(x: 0, y: y))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    func note(_ text: String) { show([]); placeholder.stringValue = text; placeholder.isHidden = false }
}

final class UILock {
    let descriptor: Int32
    init(path: URL) throws {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let fd = Darwin.open(path.path, O_CREAT | O_RDWR, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            let code = errno; Darwin.close(fd); throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
        }
        descriptor = fd
        fchmod(fd, 0o600)
    }
    deinit { Darwin.close(descriptor) }
}

// Owner-local AppKit UI. Live media and diagnostic text never leave the local CLI.
// Image availability must never contribute an intrinsic size to the window.
final class VerticalStackView: NSStackView {
    override var isFlipped: Bool { true }
}
final class NavigationRowView: NSTableRowView {
    override func drawSelection(in dirtyRect: NSRect) {
        Theme.accent.withAlphaComponent(0.16).setFill()
        NSBezierPath(roundedRect: bounds.insetBy(dx: 4, dy: 2), xRadius: 7, yRadius: 7).fill()
    }
}
final class NavigationCellView: NSTableCellView {
    override var backgroundStyle: NSView.BackgroundStyle {
        didSet { textField?.textColor = Theme.text; imageView?.contentTintColor = Theme.text }
    }
}
final class OpaqueHeaderView: NSTableHeaderView {
    override var isOpaque: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        Theme.raised.setFill(); dirtyRect.fill()
        super.draw(dirtyRect)
    }
}
final class BackgroundView: NSView {
    override func draw(_ dirtyRect: NSRect) { Theme.bg.setFill(); dirtyRect.fill() }
}
final class SidebarView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        Theme.panel.setFill(); dirtyRect.fill()
        Theme.line.setFill(); NSRect(x: bounds.maxX - 1, y: 0, width: 1, height: bounds.height).fill()
    }
}
final class IntrinsicFreeImageView: NSImageView {
    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: NSView.noIntrinsicMetric) }
}
final class PreviewView: NSView {
    private let picture = IntrinsicFreeImageView()
    private let placeholder = NSStackView()
    let caption = NSTextField(wrappingLabelWithString: "Нет разрешённого кадра\nОжидаю камеру или проверку участников")
    var image: NSImage? {
        get { picture.image }
        set { picture.image = newValue; placeholder.isHidden = newValue != nil }
    }
    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: NSView.noIntrinsicMetric) }
    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.cornerRadius = 10
        layer?.masksToBounds = true
        picture.imageScaling = .scaleProportionallyUpOrDown
        picture.setContentHuggingPriority(.init(1), for: .horizontal)
        picture.setContentHuggingPriority(.init(1), for: .vertical)
        picture.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        picture.setContentCompressionResistancePriority(.init(1), for: .vertical)
        let icon = NSImageView(image: NSImage(systemSymbolName: "video.slash", accessibilityDescription: nil) ?? NSImage())
        icon.contentTintColor = Theme.dim
        icon.setFrameSize(NSSize(width: 28, height: 24))
        caption.alignment = .center
        caption.textColor = Theme.dim
        caption.font = Theme.font(12)
        placeholder.orientation = .vertical
        placeholder.spacing = 8
        placeholder.addArrangedSubview(icon)
        placeholder.addArrangedSubview(caption)
        for child in [picture, placeholder] { child.translatesAutoresizingMaskIntoConstraints = false; addSubview(child) }
        NSLayoutConstraint.activate([
            picture.leadingAnchor.constraint(equalTo: leadingAnchor), picture.trailingAnchor.constraint(equalTo: trailingAnchor),
            picture.topAnchor.constraint(equalTo: topAnchor), picture.bottomAnchor.constraint(equalTo: bottomAnchor),
            placeholder.centerXAnchor.constraint(equalTo: centerXAnchor), placeholder.centerYAnchor.constraint(equalTo: centerYAnchor),
            placeholder.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor, constant: -24)
        ])
        setAccessibilityLabel("Локальный просмотр изображения")
        updateLayer()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var wantsUpdateLayer: Bool { true }
    override func viewDidChangeEffectiveAppearance() { super.viewDidChangeEffectiveAppearance(); needsDisplay = true }
    override func updateLayer() { layer?.backgroundColor = Theme.panel.cgColor }
}

// Top-left anchored document view for zoomable pictures.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}
// A link shown under a text material; opens only through Panel.openExternal.
final class LinkButton: NSButton {
    var url: URL?
}
// An app-defined action of a displayed material (e.g. open the reply draft in Mail.app).
final class DisplayActionButton: NSButton {
    var cardID = ""
    var actionID = ""
}

enum Section: String, CaseIterable {
    case dialogue, display, observations, voice, images, models, jobs, components, diagnostics
    var title: String {
        switch self {
        case .dialogue: return "Диалог"
        case .display: return "Дисплей"
        case .observations: return "Наблюдения"
        case .voice: return "Голос и язык"
        case .images: return "Изображения"
        case .models: return "Модели по задачам"
        case .jobs: return "Расписание"
        case .components: return "Компоненты и CLI"
        case .diagnostics: return "Диагностика"
        }
    }
    var symbol: String {
        switch self {
        case .dialogue: return "bubble.left.and.bubble.right"
        case .display: return "rectangle.on.rectangle"
        case .observations: return "eye"
        case .voice: return "waveform"
        case .images: return "photo.on.rectangle"
        case .models: return "cpu"
        case .jobs: return "calendar"
        case .components: return "slider.horizontal.3"
        case .diagnostics: return "stethoscope"
        }
    }
    var detail: String {
        switch self {
        case .dialogue: return "Видео и звук слева. Распознанные слова, решения и ответы справа."
        case .display: return "Материалы разговора: списки, ссылки, изображения, схемы, видео и письма с предложенными ответами."
        case .observations: return "Что Butler видит и замечает · исходники остаются на этом Mac."
        case .voice: return "Синтезированный голос, язык распознавания и разрешение на разговор."
        case .images: return "Ваши материалы, поиск и создание новых изображений по запросу."
        case .models: return "Главный агент, локальные диалог и зрение, отдельные темы и типы задач."
        case .jobs: return "Ежедневная работа Butler: расписание, модели и управление запусками."
        case .components: return "Действующие модели, локальные файлы и CLI · расширенные настройки."
        case .diagnostics: return "Фактические устройства, этапы обработки и ошибки · только локально."
        }
    }
}

final class Panel: NSObject, NSWindowDelegate, NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate, NSTextViewDelegate, WKNavigationDelegate {
    let cli: String
    let testing: Bool
    let defaults: UserDefaults
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
    let navigation = NSTableView()
    let content = NSView()
    var pages: [Section: NSView] = [:]
    var section = Section.dialogue
    let pageTitle = NSTextField(labelWithString: "")
    let pageDetail = NSTextField(labelWithString: "")
    let heading = NSTextField(labelWithString: "Подключаюсь к Butler…")
    let sources = NSTextField(wrappingLabelWithString: "")
    let history = NSTextField(wrappingLabelWithString: "")
    let message = NSTextField(wrappingLabelWithString: "")
    let image = PreviewView()
    let artifactImage = PreviewView()
    let artifactCaption = NSTextField(wrappingLabelWithString: "Выберите результат для просмотра.")
    let pause = NSButton(title: "Пауза", target: nil, action: nil)
    let voices = NSPopUpButton()
    let voiceState = NSTextField(wrappingLabelWithString: "Голос ещё не выбран. Прослушайте варианты и выберите подходящий.")
    let voiceToggle = NSButton(title: "Выключить голос", target: nil, action: nil)
    let faceWake = NSButton(checkboxWithTitle: "Постоянный разговор при подтверждённом владельце", target: nil, action: nil)
    let feed = FeedView()
    let latencySummary = NSTextField(wrappingLabelWithString: "Модели прогреваются · измерение ответа появится после реплики.")
    let commandReview = NSTextField(wrappingLabelWithString: "Изменения Butler применяются после вашего голосового подтверждения.")
    let meetingStatus = NSTextField(wrappingLabelWithString: "Проверяю режим встречи…")
    let meetingChoice = NSPopUpButton()
    let workStatus = NSTextField(wrappingLabelWithString: "Фоновые поручения появятся здесь. Проект, модель и задача требуют вашего подтверждения.")
    let archiveToggle = NSButton(checkboxWithTitle: "Постоянный анализ разговоров, видео и звука приложений", target: nil, action: nil)
    let storageInfo = NSTextField(wrappingLabelWithString: "Определяю объём временного буфера…")
    let ownerSetupStatus = NSTextField(wrappingLabelWithString: "Эталоны голоса хранятся на этом Mac. Восстановление владельца — через локальное подтверждение macOS.")
    let guestName = NSTextField()
    let debugStatus = NSTextField(wrappingLabelWithString: "Подключаюсь…")
    let audioRoute = NSTextField(wrappingLabelWithString: "Микрофон Butler: определяю…\nМикрофон Mac: определяю…\nВывод звука: определяю…")
    let debugImage = PreviewView()
    let participantSummary = NSTextField(wrappingLabelWithString: "Проверяю владельца…")
    let microphoneMeter = NSLevelIndicator()
    let microphoneWorking = NSTextField(wrappingLabelWithString: "● Микрофон: ожидаю данные…")
    let cameraWorking = NSTextField(labelWithString: "● Камера: ожидаю данные…")
    let dialogueLamp = NSTextField(labelWithString: "● Разговор: проверяю…")
    let recordingLamp = NSTextField(labelWithString: "● Запись: проверяю…")
    let voiceLamp = NSTextField(labelWithString: "● Голос: проверяю…")
    let cameraChoice = NSPopUpButton()
    let cameraSizeChoice = NSPopUpButton()
    let microphoneChoice = NSPopUpButton()
    let languageChoice = NSPopUpButton()
    let runtimeInfo = NSTextField(wrappingLabelWithString: "Читаю действующие настройки…")
    let settingMessage = NSTextField(wrappingLabelWithString: "")
    let componentTable = NSTableView()
    let routeTable = NSTableView()
    let jobTable = NSTableView()
    let routeChoice = NSPopUpButton()
    let routeApply = NSButton(title: "Применить к задаче", target: nil, action: nil)
    let routeTitle = NSTextField(labelWithString: "Выберите задачу")
    let routeCLI = NSTextField(labelWithString: "")
    let routeMessage = NSTextField(wrappingLabelWithString: "")
    let catalogStatus = NSTextField(wrappingLabelWithString: "Получаю каталог OpenAI и Anthropic…")
    var catalogRefreshing = false
    var catalogPollBusy = false
    var lastCatalogPoll = Date.distantPast
    let componentModel = NSComboBox()
    let componentCLI = NSTextField()
    let componentNote = NSTextField(wrappingLabelWithString: "Выберите компонент для настройки.")
    let componentMessage = NSTextField(wrappingLabelWithString: "")
    let jobMessage = NSTextField(wrappingLabelWithString: "Пауза отключает будущие запуски. Остановить прерывает текущий запуск.")
    let jobCron = NSTextField()
    let jobZone = NSTextField()
    let jobModel = NSComboBox()
    let jobDailyTime = NSTextField()
    var components: [[String: Any]] = []
    var localModels: [String] = []
    var routes: [[String: Any]] {
        let primary = ["agent", "conversation_model", "vision_model"]
        return primary.compactMap { key in components.first { $0["id"] as? String == key } }
            + components.filter { ($0["id"] as? String ?? "").hasPrefix("route:") }
    }
    var jobs: [[String: Any]] = []
    // Editor identities stay independent of row indices during asynchronous refresh.
    var componentID: String?
    var routeID: String?
    var jobID: String?
    var reconciling = false
    var managementBusy = false
    var lastManagementRefresh = Date.distantPast
    var cameraOptions: [[String: String]] = []
    var microphoneOptions: [[String: String]] = []
    var settingsLoaded = false
    let materials = NSPopUpButton()
    let query = NSTextField()
    let sendTelegram = NSButton(checkboxWithTitle: "Прислать в мой личный Telegram", target: nil, action: nil)
    let visualState = NSTextField(wrappingLabelWithString: "Материалы пока не выбраны.")
    let gallery = NSPopUpButton()
    var roots: [String] = []
    var artifacts: [[String: Any]] = []
    var lastVisualRequest = ""
    var state: [String: Any] = [:]
    var busy = false
    var timer: Timer?
    var profileIDs: [String] = []
    var cameraPreview = true
    var testCommands: [[String]] = []
    var testState: [String: Any]?
    var restoringFrame = true
    let tagline = NSTextField(labelWithString: "Личный помощник")
    var version: String?
    // Conversational display: what Butler has shown in this conversation and the material being read.
    let displayTable = NSTableView()
    let displayDetail = NSView()
    var displayCards: [[String: Any]] = []
    var displayCurrent: String?
    var displayHidden: String?
    var lastRaiseSerial = 0
    var displaySignature: [String] = []
    var displaySelecting = false
    var displayRenderedID: String?
    var displayRenderedKey: String?
    var displayRenderedStatus: String?
    var displayStatusLabel: NSTextField?
    var diagramWeb: WKWebView?
    var diagramReady = false
    var pendingDiagram: String?
    var videoView: AVPlayerView?

    static func preferenceStore() -> UserDefaults {
        // A bundled application's own preferences are the standard domain.
        // macOS can reject adding that same domain as a suite (returning nil).
        .standard
    }
    init(cli: String, testing: Bool = false, defaults: UserDefaults = Panel.preferenceStore()) {
        self.cli = cli
        self.testing = testing
        self.defaults = defaults
        super.init()
        window.title = "Butler"
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 1120, height: 730)
        window.tabbingMode = .disallowed
        window.toolbar = NSToolbar(identifier: "ButlerToolbar")
        window.toolbarStyle = .unified
        let root = BackgroundView()
        window.contentView = root
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = Theme.bg
        let sidebar = SidebarView()
        let brand = NSTextField(labelWithString: "Butler")
        brand.font = Theme.font(23, .semibold)
        tagline.textColor = Theme.dim
        tagline.font = Theme.font(11)
        tagline.lineBreakMode = .byTruncatingTail
        tagline.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        let column = NSTableColumn(identifier: .init("section"))
        column.width = 196
        navigation.addTableColumn(column)
        navigation.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        navigation.headerView = nil
        navigation.rowHeight = 38
        navigation.intercellSpacing = NSSize(width: 0, height: 4)
        navigation.style = .sourceList
        navigation.backgroundColor = .clear
        navigation.dataSource = self
        navigation.delegate = self
        navigation.allowsEmptySelection = false
        navigation.setAccessibilityLabel("Разделы Butler")
        let navScroll = NSScrollView()
        navScroll.documentView = navigation
        navScroll.drawsBackground = false
        navScroll.hasVerticalScroller = true
        let privacy = NSTextField(wrappingLabelWithString: "На этом Mac\nИсходные видео и звук локальны")
        privacy.font = Theme.font(11)
        privacy.textColor = Theme.dim
        for view in [brand, tagline, navScroll, privacy] { sidebar.addSubview(view); view.translatesAutoresizingMaskIntoConstraints = false }
        let workspace = NSView()
        let header = NSStackView()
        header.orientation = .vertical
        header.alignment = .leading
        header.spacing = 6
        pageTitle.font = Theme.font(25, .semibold)
        pageDetail.font = Theme.font(12)
        pageDetail.textColor = Theme.dim
        pageDetail.lineBreakMode = .byTruncatingTail
        for field in [pageTitle, pageDetail, settingMessage] {
            field.setContentCompressionResistancePriority(.init(1), for: .horizontal)
            field.lineBreakMode = .byTruncatingTail
        }
        header.addArrangedSubview(pageTitle)
        header.addArrangedSubview(pageDetail)
        for view in [sidebar, workspace] { root.addSubview(view); view.translatesAutoresizingMaskIntoConstraints = false }
        for view in [header, content, settingMessage] { workspace.addSubview(view); view.translatesAutoresizingMaskIntoConstraints = false }
        settingMessage.font = Theme.font(11)
        settingMessage.textColor = Theme.dim
        NSLayoutConstraint.activate([
            sidebar.leadingAnchor.constraint(equalTo: root.leadingAnchor), sidebar.topAnchor.constraint(equalTo: root.topAnchor),
            sidebar.bottomAnchor.constraint(equalTo: root.bottomAnchor), sidebar.widthAnchor.constraint(equalToConstant: 220),
            brand.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 20), brand.topAnchor.constraint(equalTo: sidebar.topAnchor, constant: 20),
            tagline.leadingAnchor.constraint(equalTo: brand.leadingAnchor), tagline.topAnchor.constraint(equalTo: brand.bottomAnchor, constant: 4),
            tagline.trailingAnchor.constraint(lessThanOrEqualTo: sidebar.trailingAnchor, constant: -16),
            navScroll.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 10), navScroll.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -10),
            navScroll.topAnchor.constraint(equalTo: tagline.bottomAnchor, constant: 22), navScroll.bottomAnchor.constraint(equalTo: privacy.topAnchor, constant: -16),
            privacy.leadingAnchor.constraint(equalTo: brand.leadingAnchor), privacy.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -16),
            privacy.bottomAnchor.constraint(equalTo: sidebar.bottomAnchor, constant: -20),
            workspace.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor), workspace.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            workspace.topAnchor.constraint(equalTo: root.topAnchor), workspace.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            header.leadingAnchor.constraint(equalTo: workspace.leadingAnchor, constant: 24), header.trailingAnchor.constraint(equalTo: workspace.trailingAnchor, constant: -24),
            header.topAnchor.constraint(equalTo: workspace.topAnchor, constant: 20), header.heightAnchor.constraint(equalToConstant: 62),
            content.leadingAnchor.constraint(equalTo: workspace.leadingAnchor, constant: 20), content.trailingAnchor.constraint(equalTo: workspace.trailingAnchor, constant: -20),
            content.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 12), content.bottomAnchor.constraint(equalTo: workspace.bottomAnchor, constant: -48),
            settingMessage.leadingAnchor.constraint(equalTo: content.leadingAnchor), settingMessage.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            settingMessage.bottomAnchor.constraint(equalTo: workspace.bottomAnchor, constant: -10), settingMessage.heightAnchor.constraint(equalToConstant: 30)
        ])
        pages[.dialogue] = dialogueView()
        pages[.display] = displayView()
        pages[.observations] = observationsView()
        pages[.voice] = voiceView()
        pages[.images] = imagesView()
        pages[.models] = modelsView()
        pages[.jobs] = managementView(isAI: false)
        pages[.components] = managementView(isAI: true)
        let diagnostics = vertical()
        debugStatus.font = Theme.font(12, .regular)
        debugStatus.isSelectable = true
        fill(diagnostics, debugStatus)
        pages[.diagnostics] = scrolling(diagnostics)
        for page in pages.values {
            content.addSubview(page)
            pin(page, to: content)
            page.isHidden = true
        }
        if let root = window.contentView { Theme.apply(root) }
        let stored = defaults.string(forKey: "section").flatMap(Section.init(rawValue:)) ?? .dialogue
        select(stored)
        if let frame = defaults.string(forKey: "windowFrame"), NSRectFromString(frame).width >= window.minSize.width,
           let clamped = Panel.clampedToScreen(NSRectFromString(frame)) {
            window.setFrame(clamped, display: false)
        } else { window.center() }
        restoringFrame = false
        let menu = NSMenu()
        for (title, action) in [("Открыть Butler", #selector(show)), ("Модели по задачам…", #selector(showModels)), ("Настройки и диагностика…", #selector(showDebug)), ("Пауза / продолжить", #selector(toggle))] {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
            entry.target = self
            menu.addItem(entry)
        }
        item.menu = menu
        item.button?.title = "◉ Butler"
        installAppMenu()
        if !testing {
            DistributedNotificationCenter.default().addObserver(self, selector: #selector(showFromNotification(_:)), name: .init("com.boris.butler-ui.show"), object: NSUserName())
            timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refresh() }
            refresh()
            loadSettings()
            loadManagement()
            if defaults.object(forKey: "windowVisible") == nil { show() }
            else if defaults.bool(forKey: "windowVisible") { window.orderFront(nil) }
        }
    }

    func pin(_ view: NSView, to parent: NSView, inset: CGFloat = 0) {
        view.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([view.leadingAnchor.constraint(equalTo: parent.leadingAnchor, constant: inset),
            view.trailingAnchor.constraint(equalTo: parent.trailingAnchor, constant: -inset), view.topAnchor.constraint(equalTo: parent.topAnchor, constant: inset),
            view.bottomAnchor.constraint(equalTo: parent.bottomAnchor, constant: -inset)])
    }
    func vertical(_ spacing: CGFloat = 12) -> NSStackView {
        let stack = VerticalStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = spacing
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        stack.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        return stack
    }
    func fill(_ stack: NSStackView, _ view: NSView) {
        stack.addArrangedSubview(view)
        view.translatesAutoresizingMaskIntoConstraints = false
        view.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -32).isActive = true
        view.setContentCompressionResistancePriority(.init(1), for: .horizontal)
    }
    func label(_ text: String) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.textColor = Theme.dim
        field.font = Theme.font(12)
        return field
    }
    func separator() -> NSBox {
        let line = NSBox(); line.boxType = .separator; line.title = ""
        return line
    }
    func title(_ text: String) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = Theme.font(14, .semibold)
        return field
    }
    func row(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.spacing = 8
        return stack
    }
    func scrolling(_ stack: NSStackView) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.scrollerStyle = .legacy
        scroll.autohidesScrollers = false
        scroll.drawsBackground = false
        scroll.documentView = stack
        stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([stack.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            stack.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor), stack.topAnchor.constraint(equalTo: scroll.contentView.topAnchor)])
        return scroll
    }
    func card(_ view: NSView) -> NSView {
        let box = NSBox()
        box.boxType = .custom
        box.borderColor = Theme.line
        box.fillColor = Theme.panel
        box.cornerRadius = 10
        box.contentViewMargins = .zero
        box.contentView = view
        pin(view, to: box)
        return box
    }
    func fixedLabel(_ label: NSTextField, height: CGFloat) {
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = height > 25 ? 2 : 1
        label.heightAnchor.constraint(equalToConstant: height).isActive = true
    }
    func dialogueView() -> NSView {
        let device = vertical(10)
        fill(device, debugImage)
        debugImage.heightAnchor.constraint(equalTo: debugImage.widthAnchor, multiplier: 9.0 / 16).isActive = true
        fill(device, title("Камера"))
        fill(device, cameraChoice)
        cameraSizeChoice.addItems(withTitles: ["640 × 480", "1280 × 720", "1920 × 1080"])
        fill(device, cameraSizeChoice)
        fill(device, cameraWorking)
        fill(device, title("Микрофон"))
        fill(device, microphoneChoice)
        fill(device, microphoneWorking)
        fixedLabel(microphoneWorking, height: 36)
        audioRoute.font = Theme.font(11)
        audioRoute.textColor = Theme.dim
        fill(device, audioRoute)
        fixedLabel(audioRoute, height: 48)
        microphoneMeter.levelIndicatorStyle = .continuousCapacity
        microphoneMeter.minValue = -70
        microphoneMeter.maxValue = 0
        microphoneMeter.warningValue = -12
        microphoneMeter.criticalValue = -3
        fill(device, microphoneMeter)
        device.addArrangedSubview(NSButton(title: "Применить устройства", target: self, action: #selector(applyDevices)))
        device.addArrangedSubview(NSButton(title: "Обновить устройства", target: self, action: #selector(loadSettings)))
        device.addArrangedSubview(NSButton(title: "Эффекты камеры macOS…", target: self, action: #selector(videoEffects)))
        for lamp in [cameraWorking, dialogueLamp, recordingLamp, voiceLamp] { lamp.font = Theme.font(12, .medium); fixedLabel(lamp, height: 18) }
        fill(device, separator())
        for lamp in [dialogueLamp, recordingLamp, voiceLamp] { fill(device, lamp) }
        participantSummary.textColor = Theme.dim
        participantSummary.font = Theme.font(11)
        fill(device, participantSummary)
        fixedLabel(participantSummary, height: 34)
        let left = card(scrolling(device))
        left.widthAnchor.constraint(equalToConstant: 330).isActive = true
        let conversation = vertical()
        heading.font = Theme.font(15, .semibold)
        fixedLabel(heading, height: 22)
        pause.target = self; pause.action = #selector(toggle)
        conversation.addArrangedSubview(row([heading, pause]))
        fill(conversation, label("Лента: кто и когда сказал, ответы, снимки и уведомления в Telegram. Новое внизу. История очищается при потере разрешения."))
        fill(conversation, latencySummary)
        fixedLabel(latencySummary, height: 42)
        latencySummary.font = Theme.font(11, .regular)
        latencySummary.textColor = Theme.dim
        fill(conversation, commandReview)
        fixedLabel(commandReview, height: 64)
        commandReview.font = Theme.font(12)
        commandReview.isSelectable = true
        fill(conversation, meetingStatus)
        fixedLabel(meetingStatus, height: 44)
        fill(conversation, workStatus)
        fixedLabel(workStatus, height: 44)
        workStatus.isSelectable = true
        fill(conversation, row([feed.eventsToggle]))
        fill(conversation, feed)
        feed.heightAnchor.constraint(greaterThanOrEqualToConstant: 260).isActive = true
        let right = card(conversation)
        let view = NSView()
        for child in [left, right] { view.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([left.leadingAnchor.constraint(equalTo: view.leadingAnchor), left.topAnchor.constraint(equalTo: view.topAnchor), left.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            right.leadingAnchor.constraint(equalTo: left.trailingAnchor, constant: 16), right.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            right.topAnchor.constraint(equalTo: view.topAnchor), right.bottomAnchor.constraint(equalTo: view.bottomAnchor)])
        return view
    }
    func observationsView() -> NSView {
        let stack = vertical()
        fill(stack, sources)
        stack.addArrangedSubview(row([NSButton(title: "Камера", target: self, action: #selector(camera)), NSButton(title: "Экран", target: self, action: #selector(screen))]))
        fill(stack, image)
        image.heightAnchor.constraint(equalToConstant: 310).isActive = true
        fill(stack, message)
        fill(stack, title("Последние наблюдения"))
        fill(stack, label("Выводы модели могут содержать ошибки."))
        fill(stack, history)
        for field in [sources, history, message] { field.isSelectable = true }
        return card(scrolling(stack))
    }
    func voiceView() -> NSView {
        let stack = vertical(16)
        fill(stack, title("Голос Butler"))
        fill(stack, voiceState)
        voices.addItem(withTitle: "Образцы готовятся…")
        stack.addArrangedSubview(row([voices, NSButton(title: "Прослушать", target: self, action: #selector(preview)), NSButton(title: "Выбрать голос", target: self, action: #selector(selectVoice))]))
        voiceToggle.target = self; voiceToggle.action = #selector(toggleVoice)
        stack.addArrangedSubview(voiceToggle)
        archiveToggle.target = self; archiveToggle.action = #selector(toggleArchive)
        stack.addArrangedSubview(archiveToggle)
        fill(stack, label("Во время встречи анализ продолжается, Butler молчит. Видео и звук хранятся во временном буфере до анализа; затем остаются результаты и нужные изображения. Полная Пауза останавливает все источники."))
        fill(stack, storageInfo)
        fixedLabel(storageInfo, height: 196)
        storageInfo.maximumNumberOfLines = 0
        storageInfo.lineBreakMode = .byWordWrapping
        storageInfo.isSelectable = true
        storageInfo.font = Theme.font(12)
        fill(stack, title("Встречи и другие собеседники"))
        meetingChoice.addItems(withTitles: ["Автоматически", "Я на встрече", "Сейчас говорю с Butler"])
        meetingChoice.target = self; meetingChoice.action = #selector(changeMeetingMode)
        stack.addArrangedSubview(meetingChoice)
        fill(stack, label("Во время встречи Butler отвечает только на явное обращение по имени. Автоматический режим смотрит, использует ли другое приложение микрофон; содержание звонка не читается."))
        fill(stack, separator())
        fill(stack, title("Язык распознавания"))
        languageChoice.addItems(withTitles: ["Русский", "Автоматически", "English", "Deutsch", "Українська"])
        stack.addArrangedSubview(row([languageChoice, NSButton(title: "Применить язык", target: self, action: #selector(applyLanguage))]))
        faceWake.target = self; faceWake.action = #selector(toggleFaceWake)
        stack.addArrangedSubview(faceWake)
        fill(stack, label("В обычном разговоре имя не обязательно. Владелец подтверждается лицом или зарегистрированным голосом; разрешение на запись проверяется отдельно."))
        stack.addArrangedSubview(NSButton(title: "Проверить на синтетическом образце", target: self, action: #selector(checkVoice)))
        stack.addArrangedSubview(NSButton(title: "Подтвердить или обновить мой голос…", target: self, action: #selector(ownerSetup)))
        guestName.placeholderString = "Имя гостя"
        guestName.widthAnchor.constraint(equalToConstant: 220).isActive = true
        stack.addArrangedSubview(row([guestName,NSButton(title:"Представить гостя…",target:self,action:#selector(guestSetup))]))
        fill(stack,ownerSetupStatus)
        fixedLabel(ownerSetupStatus,height:52)
        fill(stack, label("Проверка образца проверяет распознавание и динамик. Живую речь из микрофона смотрите в разделе «Диалог»."))
        fill(stack, separator())
        fill(stack, title("Действующие локальные модели"))
        runtimeInfo.isSelectable = true
        fill(stack, runtimeInfo)
        stack.addArrangedSubview(NSButton(title: "Настроить модели и CLI…", target: self, action: #selector(showComponents)))
        return card(scrolling(stack))
    }
    func imagesView() -> NSView {
        let stack = vertical()
        fill(stack, artifactImage)
        artifactImage.heightAnchor.constraint(equalToConstant: 270).isActive = true
        artifactImage.caption.stringValue = "Ваши изображения\nВыберите результат, найдите материал или попросите рисунок"
        fill(stack, artifactCaption)
        materials.addItem(withTitle: "Выберите файлы или папки…")
        materials.widthAnchor.constraint(lessThanOrEqualToConstant: 400).isActive = true
        stack.addArrangedSubview(row([materials, NSButton(title: "Добавить…", target: self, action: #selector(addMaterials)), NSButton(title: "Убрать", target: self, action: #selector(removeMaterial))]))
        query.placeholderString = "Что найти в моих материалах или нарисовать"
        fill(stack, query)
        stack.addArrangedSubview(row([NSButton(title: "Найти", target: self, action: #selector(findImage)), NSButton(title: "Нарисовать", target: self, action: #selector(generateImage)), NSButton(title: "Отменить", target: self, action: #selector(cancelImage))]))
        stack.addArrangedSubview(sendTelegram)
        fill(stack, visualState)
        gallery.addItem(withTitle: "Результатов пока нет")
        stack.addArrangedSubview(row([gallery, NSButton(title: "Показать", target: self, action: #selector(showArtifact)), NSButton(title: "Удалить", target: self, action: #selector(deleteArtifact))]))
        return card(scrolling(stack))
    }
    func tableScroll(_ table: NSTableView, columns: [(String, String, CGFloat)]) -> NSScrollView {
        for (key, title, width) in columns {
            let column = NSTableColumn(identifier: .init(key)); column.title = title; column.width = width
            column.minWidth = key == "status" ? 100 : 150
            table.addTableColumn(column)
        }
        table.headerView = OpaqueHeaderView(frame: NSRect(x: 0, y: 0, width: 800, height: 28))
        table.dataSource = self; table.delegate = self
        table.usesAlternatingRowBackgroundColors = true
        table.style = .inset
        table.rowHeight = 34
        table.allowsEmptySelection = false
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true
        return scroll
    }
    func modelsView() -> NSView {
        let stack = vertical()
        catalogStatus.font = Theme.font(12)
        catalogStatus.textColor = Theme.dim
        fill(stack, catalogStatus); fixedLabel(catalogStatus, height: 62); catalogStatus.maximumNumberOfLines = 3
        fill(stack, tableScroll(routeTable, columns: [("title", "Задача / тема", 230), ("model", "Текущая модель", 350), ("status", "Состояние", 120)]))
        routeTitle.font = Theme.font(15, .semibold)
        fill(stack, routeTitle)
        routeChoice.widthAnchor.constraint(greaterThanOrEqualToConstant: 290).isActive = true
        routeApply.target = self; routeApply.action = #selector(applyRoute)
        routeChoice.target = self; routeChoice.action = #selector(routeChoiceChanged)
        stack.addArrangedSubview(row([routeChoice, routeApply]))
        routeCLI.lineBreakMode = .byTruncatingMiddle
        routeCLI.textColor = Theme.dim
        routeCLI.isSelectable = true
        fill(stack, routeCLI)
        fill(stack, label("Меняется только выбранная строка. Для задач — со следующего запуска; локальный диалог и зрение перезапустят только сервис Butler. Модель отдельного задания задаётся в «Расписание»."))
        stack.addArrangedSubview(row([NSButton(title: "Обновить каталоги", target: self, action: #selector(refreshCatalog)), NSButton(title: "Компоненты и CLI…", target: self, action: #selector(showComponents))]))
        fill(stack, routeMessage)
        fixedLabel(routeMessage, height: 36)
        return card(stack)
    }
    func managementView(isAI: Bool) -> NSView {
        let stack = vertical()
        let table = isAI ? componentTable : jobTable
        fill(stack, tableScroll(table, columns: isAI ? [("status", "Состояние", 110), ("title", "Компонент", 250), ("model", "Модель / путь", 350)] : [("status", "Состояние", 110), ("name", "Задание", 260), ("schedule", "Расписание", 240)]))
        stack.addArrangedSubview(NSButton(title: "Обновить состояние", target: self, action: #selector(loadManagement)))
        if isAI {
            componentModel.isEditable = true
            fill(stack, title("Модель / локальный файл или папка")); fill(stack, componentModel)
            fill(stack, title("CLI / интерпретатор")); fill(stack, componentCLI)
            fill(stack, componentNote); fixedLabel(componentNote, height: 48)
            componentNote.isSelectable = true
            stack.addArrangedSubview(NSButton(title: "Сохранить компонент", target: self, action: #selector(applyComponent)))
            fill(stack, componentMessage); fixedLabel(componentMessage, height: 36)
        } else {
            let grid = NSGridView(views: [[title("Cron"), jobCron, title("Часовой пояс"), jobZone], [title("Модель задания"), jobModel, title("Сводка · HH:MM"), jobDailyTime]])
            grid.rowSpacing = 10; grid.columnSpacing = 12
            for (index, width) in [CGFloat(120), 230, 115, 220].enumerated() { grid.column(at: index).width = width }
            for field: NSControl in [jobCron, jobZone, jobModel, jobDailyTime] { field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal) }
            jobCron.placeholderString = "0 9 * * *"; jobModel.placeholderString = "Модель агента Butler"
            stack.addArrangedSubview(grid)
            let actions = row([])
            for (index, title) in ["Запустить", "Пауза", "Продолжить", "Остановить", "Сохранить"].enumerated() {
                let button = NSButton(title: title, target: self, action: #selector(controlJob(_:))); button.tag = index
                actions.addArrangedSubview(button)
            }
            stack.addArrangedSubview(actions)
            fill(stack, jobMessage); fixedLabel(jobMessage, height: 66); jobMessage.maximumNumberOfLines = 4
            jobMessage.isSelectable = true
        }
        return card(stack)
    }

    // MARK: Conversational display

    func displayView() -> NSView {
        let scroll = tableScroll(displayTable, columns: [("title", "История показов", 250)])
        scroll.hasHorizontalScroller = false
        displayTable.setAccessibilityLabel("История показов")
        let note = label("История показов в этом разговоре. После завершения разговора список очищается.")
        note.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        let left = NSView()
        for child in [scroll, note] { left.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: left.leadingAnchor, constant: 12), scroll.trailingAnchor.constraint(equalTo: left.trailingAnchor, constant: -12),
            scroll.topAnchor.constraint(equalTo: left.topAnchor, constant: 12),
            note.leadingAnchor.constraint(equalTo: scroll.leadingAnchor), note.trailingAnchor.constraint(equalTo: scroll.trailingAnchor),
            note.topAnchor.constraint(equalTo: scroll.bottomAnchor, constant: 8), note.bottomAnchor.constraint(equalTo: left.bottomAnchor, constant: -12)])
        let leftCard = card(left)
        leftCard.widthAnchor.constraint(equalToConstant: 300).isActive = true
        displayDetail.setAccessibilityLabel("Показанный материал")
        let rightCard = card(displayDetail)
        let view = NSView()
        for child in [leftCard, rightCard] { view.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([leftCard.leadingAnchor.constraint(equalTo: view.leadingAnchor), leftCard.topAnchor.constraint(equalTo: view.topAnchor), leftCard.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            rightCard.leadingAnchor.constraint(equalTo: leftCard.trailingAnchor, constant: 16), rightCard.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            rightCard.topAnchor.constraint(equalTo: view.topAnchor), rightCard.bottomAnchor.constraint(equalTo: view.bottomAnchor)])
        renderDisplayCard(nil)
        return view
    }
    func displayRowTitle(_ card: [String: Any]) -> String {
        let kinds = ["text": "текст", "image": "изображение", "diagram": "схема", "video": "видео", "email": "письмо"]
        let formatter = DateFormatter(); formatter.dateFormat = "HH:mm"
        let stamp = (card["at"] as? Double).map { formatter.string(from: Date(timeIntervalSince1970: $0)) } ?? "--:--"
        let kind = card["kind"] as? String ?? ""
        return "\(stamp) · \(kinds[kind] ?? (kind.isEmpty ? "материал" : kind)) · \(card["title"] as? String ?? "")"
    }
    // Poll result → history table and detail area. Rebuilds rows only when ids, titles or statuses changed.
    func updateDisplay(_ display: [String: Any]?) {
        let cards = (display?["cards"] as? [[String: Any]] ?? []).sorted { ($0["at"] as? Double ?? 0) < ($1["at"] as? Double ?? 0) }
        displayHidden = display?["hidden"] as? String
        displayCurrent = display?["current"] as? String
        let signature = cards.map { "\($0["id"] as? String ?? "")|\($0["title"] as? String ?? "")|\($0["status"] as? String ?? "")" }
        let selected = displayCards.indices.contains(displayTable.selectedRow) ? displayCards[displayTable.selectedRow]["id"] as? String : nil
        displayCards = cards
        if signature != displaySignature {
            displaySignature = signature
            displaySelecting = true
            displayTable.reloadData()
            if let index = cards.firstIndex(where: { $0["id"] as? String == selected }) { displayTable.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false) }
            displaySelecting = false
        }
        let serial = (display?["raise_serial"] as? NSNumber)?.intValue ?? 0
        if serial > lastRaiseSerial {
            lastRaiseSerial = serial
            if !testing { show() }
            select(.display)
            selectDisplayCard(displayCurrent)
            return
        }
        if cards.isEmpty || displayHidden == "private" {
            if displayRenderedKey != "empty:\(displayHidden ?? "")" { renderDisplayCard(nil) }
        } else if let id = displayRenderedID, let card = cards.first(where: { $0["id"] as? String == id }) {
            let status = card["status"] as? String
            if status != displayRenderedStatus { displayRenderedStatus = status; showDisplayStatus(status) }
        } else {
            selectDisplayCard(displayCurrent)
        }
    }
    func showDisplayStatus(_ status: String?) {
        displayStatusLabel?.stringValue = status ?? ""
        displayStatusLabel?.isHidden = (status ?? "").isEmpty
    }
    // Programmatic selection: silent (no ui-display-select); falls back to the newest card.
    func selectDisplayCard(_ id: String?) {
        guard let index = displayCards.firstIndex(where: { $0["id"] as? String == id }) ?? (displayCards.isEmpty ? nil : displayCards.count - 1) else { renderDisplayCard(nil); return }
        displaySelecting = true
        displayTable.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        displayTable.scrollRowToVisible(index)
        displaySelecting = false
        renderDisplayCard(displayCards[index])
    }
    func renderDisplayCard(_ card: [String: Any]?) {
        let content: NSStackView
        if let card {
            if let unavailable = card["unavailable"] as? String {
                content = explanation(unavailable == "paused" ? "Изображения и видео скрыты, пока запись наблюдений на паузе." : unavailable == "missing" ? "Файл материала больше не доступен." : "Этот тип материала не поддерживается в этой версии Butler.")
            } else {
                switch card["kind"] as? String ?? "" {
                case "text": content = textDisplayView(card)
                case "image": content = imageDisplayView(card)
                case "diagram": content = diagramDisplayView(card)
                case "video": content = videoDisplayView(card)
                case "email": content = emailDisplayView(card)
                default: content = explanation("Этот тип материала не поддерживается в этой версии Butler.")
                }
            }
        } else {
            content = explanation(displayHidden == "private" ? "Материалы разговора скрыты: владелец не подтверждён." : "Пока ничего не показано. Butler выведет сюда материал, когда он понадобится в разговоре.")
        }
        showDisplayContent(content)
        // The status line comes from the service only (result of an action, delivery state); never invented here.
        let status = label("")
        status.isSelectable = true
        status.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        fill(content, status)
        displayStatusLabel = status
        displayRenderedID = card?["id"] as? String
        displayRenderedStatus = card?["status"] as? String
        displayRenderedKey = card.map { "card:\($0["id"] as? String ?? "")" } ?? "empty:\(displayHidden ?? "")"
        showDisplayStatus(displayRenderedStatus)
    }
    // The detail container holds exactly one child; swapping it stops any playing video.
    func showDisplayContent(_ view: NSView) {
        videoView?.player?.pause(); videoView = nil
        displayStatusLabel = nil
        for child in displayDetail.subviews { child.removeFromSuperview() }
        displayDetail.addSubview(view)
        pin(view, to: displayDetail)
    }
    func explanation(_ text: String) -> NSStackView {
        let stack = vertical()
        let field = label(text)
        field.font = Theme.font(13)
        field.textColor = Theme.text
        field.isSelectable = true
        fill(stack, field)
        return stack
    }
    func detailStack() -> NSStackView {
        let stack = vertical(10)
        stack.distribution = .fill
        return stack
    }
    // Width-bound child that takes the remaining height of a detail stack.
    func stretch(_ stack: NSStackView, _ view: NSView) {
        fill(stack, view)
        view.setContentHuggingPriority(.init(1), for: .vertical)
        view.setContentCompressionResistancePriority(.init(1), for: .vertical)
    }
    func truncating(_ field: NSTextField) -> NSTextField {
        field.lineBreakMode = .byTruncatingTail
        field.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        field.toolTip = field.stringValue
        return field
    }
    func configure(_ textView: NSTextView, background: NSColor? = nil) {
        textView.isEditable = false
        textView.isSelectable = true
        textView.isAutomaticLinkDetectionEnabled = false
        textView.isAutomaticDataDetectionEnabled = false
        textView.font = Theme.font(13)
        textView.textColor = Theme.text
        textView.insertionPointColor = Theme.bright
        textView.drawsBackground = background != nil
        if let background { textView.backgroundColor = background }
        textView.textContainerInset = NSSize(width: 6, height: 8)
        textView.linkTextAttributes = [.foregroundColor: Theme.accent, .underlineStyle: NSUnderlineStyle.single.rawValue, .cursor: NSCursor.pointingHand]
        textView.selectedTextAttributes = [.backgroundColor: Theme.accent.withAlphaComponent(0.25)]
        textView.delegate = self
    }
    func readOnlyText(_ text: String, minHeight: CGFloat) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        scroll.drawsBackground = true
        scroll.backgroundColor = Theme.raised
        scroll.hasVerticalScroller = true
        if let textView = scroll.documentView as? NSTextView {
            configure(textView, background: Theme.raised)
            textView.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: [.font: Theme.font(13), .foregroundColor: Theme.text]))
        }
        let height = scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: minHeight)
        height.priority = .init(900)
        height.isActive = true
        return scroll
    }
    func textDisplayView(_ card: [String: Any]) -> NSStackView {
        let stack = detailStack()
        fill(stack, truncating(title(card["title"] as? String ?? "Текст")))
        let scroll = NSTextView.scrollableTextView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        if let textView = scroll.documentView as? NSTextView {
            configure(textView)
            textView.textStorage?.setAttributedString(renderMarkdown(card["markdown"] as? String ?? ""))
        }
        stretch(stack, scroll)
        let links = (card["links"] as? [[String: Any]] ?? []).compactMap { link -> (String, URL)? in
            guard let text = link["url"] as? String, let url = URL(string: text) else { return nil }
            return (link["label"] as? String ?? text, url)
        }
        if !links.isEmpty { fill(stack, separator()) }
        for (name, url) in links {
            let button = LinkButton(title: "", target: self, action: #selector(openDisplayLink(_:)))
            button.url = url
            button.isBordered = false
            button.alignment = .left
            button.lineBreakMode = .byTruncatingTail
            button.setContentCompressionResistancePriority(.init(1), for: .horizontal)
            button.attributedTitle = NSAttributedString(string: name, attributes: [.font: Theme.font(12), .foregroundColor: Theme.accent, .underlineStyle: NSUnderlineStyle.single.rawValue])
            button.toolTip = url.absoluteString
            fill(stack, button)
        }
        return stack
    }
    // Block-level markdown into themed rich text. Foundation parses the structure; headings, lists,
    // quotes and code get the console palette. Links keep their URL attribute for clickedOnLink.
    func renderMarkdown(_ source: String) -> NSAttributedString {
        let body = Theme.font(13)
        var options = AttributedString.MarkdownParsingOptions()
        options.interpretedSyntax = .full
        options.failurePolicy = .returnPartiallyParsedIfPossible
        guard let parsed = try? AttributedString(markdown: source, options: options) else {
            return NSAttributedString(string: source, attributes: [.font: body, .foregroundColor: Theme.text])
        }
        func isLeaf(_ kind: PresentationIntent.Kind) -> Bool {
            switch kind {
            case .paragraph, .header, .codeBlock, .thematicBreak, .tableCell: return true
            default: return false
            }
        }
        let result = NSMutableAttributedString()
        var lastBlock: Int?
        var lastRow: Int?
        for run in parsed.runs {
            var text = String(parsed[run.range].characters)
            var font = body
            var color = Theme.text
            var prefix = ""
            var block: Int?
            var row: Int?
            var isCell = false
            let paragraph = NSMutableParagraphStyle()
            paragraph.paragraphSpacing = 6
            if let intent = run.presentationIntent {
                block = (intent.components.first { isLeaf($0.kind) } ?? intent.components.first)?.identity
                var ordered = false
                var depth = 0
                for component in intent.components.reversed() {   // outermost first
                    switch component.kind {
                    case .header(let level):
                        font = Theme.font(level <= 1 ? 17 : level == 2 ? 15 : 14, .semibold); color = Theme.bright
                        paragraph.paragraphSpacingBefore = 8
                    case .orderedList: ordered = true
                    case .unorderedList: ordered = false
                    case .listItem(let ordinal):
                        depth += 1
                        prefix = ordered ? "\(ordinal). " : "• "
                        paragraph.paragraphSpacing = 2
                        paragraph.firstLineHeadIndent = CGFloat(depth - 1) * 16
                        paragraph.headIndent = paragraph.firstLineHeadIndent + 18
                    case .codeBlock: color = Theme.accent; paragraph.headIndent = 12; paragraph.firstLineHeadIndent = 12
                    case .blockQuote: color = Theme.dim; paragraph.headIndent = 12; paragraph.firstLineHeadIndent = 12
                    case .thematicBreak: text = "────────"; color = Theme.line
                    case .tableRow, .tableHeaderRow: row = component.identity
                    case .tableCell: isCell = true
                    default: break
                    }
                }
            }
            if let block, block != lastBlock {
                if result.length > 0 {
                    result.append(NSAttributedString(string: isCell && row == lastRow ? "  │  " : "\n", attributes: [.font: body, .foregroundColor: Theme.dim, .paragraphStyle: paragraph]))
                }
                lastBlock = block; lastRow = row
                if !prefix.isEmpty { result.append(NSAttributedString(string: prefix, attributes: [.font: body, .foregroundColor: Theme.accent, .paragraphStyle: paragraph])) }
            }
            var attributes: [NSAttributedString.Key: Any] = [.paragraphStyle: paragraph]
            if let inline = run.inlinePresentationIntent {
                if inline.contains(.softBreak) { text = " " }
                if inline.contains(.lineBreak) { text = "\n" }
                if inline.contains(.stronglyEmphasized) { font = Theme.font(font.pointSize, .bold); color = Theme.bright }
                if inline.contains(.emphasized) { font = NSFontManager.shared.convert(font, toHaveTrait: .italicFontMask) }
                if inline.contains(.code) { color = Theme.accent }
                if inline.contains(.strikethrough) { attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
            }
            attributes[.font] = font
            attributes[.foregroundColor] = color
            if let link = run.link { attributes[.link] = link }
            result.append(NSAttributedString(string: text, attributes: attributes))
        }
        return result
    }
    func openExternal(_ url: URL) {
        guard ["http", "https", "mailto"].contains(url.scheme?.lowercased() ?? "") else { return }
        if !testing { NSWorkspace.shared.open(url) }
    }
    @objc func openDisplayLink(_ sender: NSButton) {
        if let url = (sender as? LinkButton)?.url { openExternal(url) }
    }
    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        if let url = link as? URL { openExternal(url) }
        else if let text = link as? String, let url = URL(string: text) { openExternal(url) }
        return true
    }
    func imageDisplayView(_ card: [String: Any]) -> NSStackView {
        guard let path = card["image"] as? String, let image = NSImage(contentsOfFile: path), image.size.width > 0, image.size.height > 0 else {
            return explanation("Файл материала больше не доступен.")
        }
        let stack = detailStack()
        fill(stack, truncating(title(card["title"] as? String ?? "Изображение")))
        let scroll = NSScrollView()
        scroll.allowsMagnification = true
        scroll.minMagnification = 1
        scroll.maxMagnification = 6
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.drawsBackground = true
        scroll.backgroundColor = Theme.bg
        // Fitted to the area available now; zoom starts from the fitted size.
        let available = NSSize(width: max(displayDetail.bounds.width - 32, 200), height: max(displayDetail.bounds.height - 110, 160))
        let scale = min(1, min(available.width / image.size.width, available.height / image.size.height))
        let document = FlippedView(frame: NSRect(x: 0, y: 0, width: floor(image.size.width * scale), height: floor(image.size.height * scale)))
        let picture = IntrinsicFreeImageView(frame: document.bounds)
        picture.image = image
        picture.imageScaling = .scaleProportionallyUpOrDown
        picture.autoresizingMask = [.width, .height]
        document.addSubview(picture)
        scroll.documentView = document
        let zoom = NSClickGestureRecognizer(target: self, action: #selector(toggleImageZoom(_:)))
        zoom.numberOfClicksRequired = 2
        document.addGestureRecognizer(zoom)
        stretch(stack, scroll)
        fill(stack, label("Двойной щелчок — увеличить или вернуть масштаб."))
        return stack
    }
    @objc func toggleImageZoom(_ sender: NSClickGestureRecognizer) {
        guard let view = sender.view, let scroll = view.enclosingScrollView else { return }
        let point = sender.location(in: view)
        scroll.animator().setMagnification(scroll.magnification > 1.01 ? 1 : 2.5, centeredAt: point)
    }
    func diagramDisplayView(_ card: [String: Any]) -> NSStackView {
        guard let page = Bundle.main.url(forResource: "diagram", withExtension: "html") else {
            return explanation("Файл схемы не найден в Butler.app — переустановите приложение.")
        }
        let stack = detailStack()
        fill(stack, truncating(title(card["title"] as? String ?? "Схема")))
        let web: WKWebView
        if let existing = diagramWeb { web = existing } else {
            web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
            web.navigationDelegate = self
            web.underPageBackgroundColor = Theme.panel
            diagramWeb = web
            // The page and mermaid.min.js are files inside Butler.app; nothing is ever fetched.
            web.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
        }
        pendingDiagram = card["mermaid"] as? String ?? ""
        if diagramReady { flushDiagram() }
        stretch(stack, web)
        return stack
    }
    func flushDiagram() {
        guard let web = diagramWeb, let source = pendingDiagram,
              let data = try? JSONSerialization.data(withJSONObject: [source]), let json = String(data: data, encoding: .utf8) else { return }
        pendingDiagram = nil
        web.evaluateJavaScript("renderDiagram(\(json)[0])") { _, _ in }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        diagramReady = true
        flushDiagram()
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(navigationAction.request.url?.isFileURL == true ? .allow : .cancel)
    }
    func videoDisplayView(_ card: [String: Any]) -> NSStackView {
        let path = card["video"] as? String ?? ""
        let unavailable = "Видео недоступно или формат не поддерживается: \((path as NSString).lastPathComponent)"
        guard !path.isEmpty, FileManager.default.fileExists(atPath: path) else { return explanation(unavailable) }
        let stack = detailStack()
        fill(stack, truncating(title(card["title"] as? String ?? "Видео")))
        let player = AVPlayerView()
        player.controlsStyle = .floating
        let status = label("Проверяю видео…")
        stretch(stack, player)
        fill(stack, status)
        videoView = player
        let asset = AVURLAsset(url: URL(fileURLWithPath: path))
        Task { @MainActor [weak self, weak player] in
            let playable = (try? await asset.load(.isPlayable)) ?? false
            guard let self, let player, self.videoView === player else { return }
            if playable { player.player = AVPlayer(url: asset.url); status.isHidden = true }
            else { status.stringValue = unavailable; status.textColor = Theme.error }
        }
        return stack
    }
    func emailDisplayView(_ card: [String: Any]) -> NSStackView {
        let email = card["email"] as? [String: Any] ?? [:]
        let stack = detailStack()
        fill(stack, title("Письмо"))
        for (key, name) in [("sender", "От"), ("subject", "Тема"), ("date", "Дата"), ("account", "Аккаунт")] {
            let field = truncating(NSTextField(labelWithString: "\(name): \(email[key] as? String ?? "—")"))
            field.font = Theme.font(12)
            field.textColor = Theme.text
            field.isSelectable = true
            fill(stack, field)
        }
        let summary = label("Кратко: \(email["summary"] as? String ?? "—")")
        summary.isSelectable = true
        summary.maximumNumberOfLines = 4
        summary.lineBreakMode = .byTruncatingTail
        fill(stack, summary)
        stretch(stack, readOnlyText(email["body"] as? String ?? "", minHeight: 160))
        fill(stack, separator())
        fill(stack, title("Предложенный ответ"))
        stretch(stack, readOnlyText(email["draft"] as? String ?? "", minHeight: 120))
        let actions = (card["actions"] as? [[String: Any]] ?? []).filter { $0["id"] is String }
        if !actions.isEmpty {
            let buttons = row([])
            for action in actions {
                let id = action["id"] as? String ?? ""
                let button = DisplayActionButton(title: action["label"] as? String ?? id, target: self, action: #selector(runDisplayAction(_:)))
                button.cardID = card["id"] as? String ?? ""
                button.actionID = id
                buttons.addArrangedSubview(button)
            }
            stack.addArrangedSubview(buttons)
        }
        return stack
    }
    @objc func runDisplayAction(_ sender: NSButton) {
        guard let button = sender as? DisplayActionButton else { return }
        button.isEnabled = false
        showDisplayStatus("Выполняю…")
        jsonCommand("ui-display-action", ["id": button.cardID, "action": button.actionID]) { value in
            button.isEnabled = true
            let status = value?["status"] as? String ?? "Действие не выполнено: нет ответа от Butler."
            guard self.displayRenderedID == button.cardID else { return }
            self.displayRenderedStatus = status
            self.showDisplayStatus(status)
        }
    }

    func installAppMenu() {
        if testing { return }
        let menu = NSMenu()
        let appItem = NSMenuItem(); let appMenu = NSMenu(title: "Butler"); appItem.submenu = appMenu; menu.addItem(appItem)
        let about = NSMenuItem(title: "О Butler", action: #selector(about), keyEquivalent: ""); about.target = self; appMenu.addItem(about)
        appMenu.addItem(.separator())
        let settings = NSMenuItem(title: "Настройки…", action: #selector(showDebug), keyEquivalent: ","); settings.target = self; appMenu.addItem(settings)
        appMenu.addItem(NSMenuItem(title: "Скрыть Butler", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"))
        appMenu.addItem(NSMenuItem(title: "Закрыть Butler", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        let editItem = NSMenuItem(); let edit = NSMenu(title: "Правка"); editItem.submenu = edit; menu.addItem(editItem)
        for (title, action, key) in [("Отменить", Selector(("undo:")), "z"), ("Вырезать", #selector(NSText.cut(_:)), "x"), ("Копировать", #selector(NSText.copy(_:)), "c"), ("Вставить", #selector(NSText.paste(_:)), "v"), ("Выбрать всё", #selector(NSText.selectAll(_:)), "a")] { edit.addItem(NSMenuItem(title: title, action: action, keyEquivalent: key)) }
        let windowItem = NSMenuItem(); let windows = NSMenu(title: "Окно"); windowItem.submenu = windows; menu.addItem(windowItem)
        windows.addItem(NSMenuItem(title: "Свернуть", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
        windows.addItem(NSMenuItem(title: "Закрыть окно", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        NSApp.windowsMenu = windows; NSApp.mainMenu = menu
    }
    @objc func about() { NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "Butler", .credits: NSAttributedString(string: "Личный помощник · обработка наблюдений на этом Mac")]) }
    func select(_ value: Section) {
        section = value
        for (key, page) in pages { page.isHidden = key != value }
        pageTitle.stringValue = value.title; pageDetail.stringValue = value.detail
        window.subtitle = version.map { "\(value.title) · v\($0)" } ?? value.title
        if value != .display { videoView?.player?.pause() }
        let index = Section.allCases.firstIndex(of: value)!
        if navigation.selectedRow != index { navigation.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false) }
        defaults.set(value.rawValue, forKey: "section")
    }
    /// The frame fitted into the visible area of the screen it intersects; nil when it touches no screen.
    static func clampedToScreen(_ frame: NSRect) -> NSRect? {
        guard let screen = NSScreen.screens.first(where: { $0.visibleFrame.intersects(frame) }) else { return nil }
        let area = screen.visibleFrame
        var result = frame
        result.size.width = min(frame.width, area.width)
        result.size.height = min(frame.height, area.height)
        result.origin.x = min(max(frame.minX, area.minX), area.maxX - result.width)
        result.origin.y = min(max(frame.minY, area.minY), area.maxY - result.height)
        return result
    }
    func saveFrame() {
        guard !restoringFrame, let clamped = Panel.clampedToScreen(window.frame) else { return }
        if clamped != window.frame { window.setFrame(clamped, display: true) }   // never wider than the screen it is on
        defaults.set(NSStringFromRect(clamped), forKey: "windowFrame")
    }
    func windowDidResize(_ notification: Notification) { saveFrame() }
    func windowDidMove(_ notification: Notification) { saveFrame() }
    @objc func showFromNotification(_ notification: Notification) { show() }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { show(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    @objc func showModels() { select(.models); show() }
    @objc func showComponents() { select(.components); show() }

    func numberOfRows(in tableView: NSTableView) -> Int {
        if tableView === navigation { return Section.allCases.count }
        if tableView === displayTable { return displayCards.count }
        return rows(for: tableView).count
    }
    func rows(for table: NSTableView) -> [[String: Any]] { table === componentTable ? components : table === routeTable ? routes : jobs }
    func selectedID(_ table: NSTableView) -> String? {
        let values = rows(for: table); return values.indices.contains(table.selectedRow) ? values[table.selectedRow]["id"] as? String : nil
    }
    func taskTitle(_ value: [String: Any]) -> String {
        let kind = (value["id"] as? String ?? "").replacingOccurrences(of: "route:", with: "")
        return ["architecture": "Архитектура", "code": "Программирование", "review": "Проверка кода", "research": "Исследования", "documentation": "Документация", "tests": "Тестирование", "curate": "Подбор материалов", "classify": "Классификация", "news": "Новости", "video": "Видео"][kind] ?? value["title"] as? String ?? kind
    }
    func tableView(_ tableView: NSTableView, viewFor column: NSTableColumn?, row: Int) -> NSView? {
        if tableView === navigation {
            let section = Section.allCases[row]
            let cell = NavigationCellView()
            let icon = NSImageView(image: NSImage(systemSymbolName: section.symbol, accessibilityDescription: nil) ?? NSImage())
            let text = NSTextField(labelWithString: section.title)
            text.font = Theme.font(12, .medium)
            text.lineBreakMode = .byTruncatingTail
            cell.addSubview(icon); cell.addSubview(text); icon.translatesAutoresizingMaskIntoConstraints = false; text.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([icon.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8), icon.widthAnchor.constraint(equalToConstant: 18), icon.heightAnchor.constraint(equalToConstant: 18), icon.centerYAnchor.constraint(equalTo: cell.centerYAnchor), text.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 8), text.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4), text.centerYAnchor.constraint(equalTo: cell.centerYAnchor)])
            text.textColor = Theme.text; icon.contentTintColor = Theme.text
            cell.textField = text; cell.imageView = icon
            return cell
        }
        if tableView === displayTable {
            guard displayCards.indices.contains(row) else { return nil }
            let label = NSTextField(labelWithString: displayRowTitle(displayCards[row]))
            label.lineBreakMode = .byTruncatingTail
            label.setContentCompressionResistancePriority(.init(1), for: .horizontal)
            label.font = Theme.font(12)
            label.textColor = Theme.text
            label.toolTip = label.stringValue
            return label
        }
        let values = rows(for: tableView)
        guard values.indices.contains(row), let key = column?.identifier.rawValue else { return nil }
        let value = values[row]
        let label = NSTextField(labelWithString: "")
        label.lineBreakMode = .byTruncatingMiddle
        label.font = Theme.font(12)
        label.textColor = Theme.text
        if key == "status" {
            let isAI = tableView !== jobTable
            let ready = isAI ? value["ready"] as? Bool == true : value["enabled"] as? Bool == true && value["status"] as? String != "error"
            let running = value["running"] as? Bool == true
            label.stringValue = "● " + (running ? "выполняется" : isAI ? (ready ? "доступен" : "недоступен") : ["ready": "готов", "paused": "пауза", "scheduled": "ожидает", "error": "ошибка" ][value["status"] as? String ?? ""] ?? value["status"] as? String ?? "—")
            label.textColor = ready ? Theme.accent : Theme.error
        } else { label.stringValue = key == "title" && tableView === routeTable ? taskTitle(value) : value[key] as? String ?? "—" }
        label.toolTip = label.stringValue
        return label
    }
    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        tableView === navigation ? NavigationRowView() : nil
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !reconciling, let table = notification.object as? NSTableView else { return }
        if table === navigation { if Section.allCases.indices.contains(table.selectedRow) { select(Section.allCases[table.selectedRow]) }; return }
        if table === displayTable {
            // Only a click by the reader is reported; programmatic selection stays silent.
            guard !displaySelecting, displayCards.indices.contains(table.selectedRow) else { return }
            let card = displayCards[table.selectedRow]
            renderDisplayCard(card)
            if let id = card["id"] as? String { jsonCommand("ui-display-select", ["id": id]) { _ in } }
            return
        }
        populateSelection(table)
    }
    func populateSelection(_ table: NSTableView) {
        let values = rows(for: table)
        guard values.indices.contains(table.selectedRow) else { return }
        let value = values[table.selectedRow]
        if table === routeTable {
            routeID = value["id"] as? String
            routeMessage.stringValue = ""
            routeTitle.stringValue = "Модель для задачи «\(taskTitle(value))»"
            setRouteOptions(value, preserving: value["model"] as? String)
            routeCLI.stringValue = "CLI: \(value["executable"] ?? "—")"
            routeCLI.toolTip = routeCLI.stringValue
        } else if table === componentTable {
            componentID = value["id"] as? String
            componentMessage.stringValue = ""
            componentModel.removeAllItems(); componentModel.addItems(withObjectValues: value["choices"] as? [String] ?? (["conversation_model", "vision_model"].contains(value["id"] as? String ?? "") ? localModels : []))
            componentModel.stringValue = value["model"] as? String ?? ""
            componentModel.isEnabled = value["model_key"] is String
            componentCLI.stringValue = value["executable"] as? String ?? ""
            componentCLI.isEnabled = value["cli_key"] is String
            componentNote.stringValue = "\(value["title"] ?? "")\n\(value["note"] ?? "")"
            componentNote.toolTip = componentNote.stringValue
        } else {
            jobID = value["id"] as? String
            let cron = value["kind"] as? String == "cron"
            jobCron.stringValue = value["cron"] as? String ?? ""
            jobZone.stringValue = value["timezone"] as? String ?? "America/Chicago"
            jobModel.removeAllItems()
            jobModel.addItems(withObjectValues: components.first { $0["id"] as? String == "agent" }?["choices"] as? [String] ?? [])
            jobModel.stringValue = cron && value["model"] as? String != "модель агента Butler" ? value["model"] as? String ?? "" : ""
            for field: NSControl in [jobCron, jobZone, jobModel] { field.isEnabled = cron }
            jobDailyTime.stringValue = value["daily_time"] as? String ?? ""
            jobDailyTime.isEnabled = value["id"] as? String == "builtin:daily"
            jobMessage.stringValue = "Следующий: \(time(value["next_run"])) · Последний: \(time(value["last_run"]))\nМодель: \(value["model"] ?? "—")\n\(value["error"] ?? "")"
        }
    }
    func reconcile(_ table: NSTableView, id: String?) {
        let origin = table.enclosingScrollView?.contentView.bounds.origin ?? .zero
        reconciling = true
        table.reloadData()
        let values = rows(for: table)
        let index = values.firstIndex { $0["id"] as? String == id } ?? (values.isEmpty ? nil : 0)
        if let index = index { table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false) }
        else { table.deselectAll(nil) }
        reconciling = false
        if let scroll = table.enclosingScrollView { scroll.contentView.scroll(to: origin); scroll.reflectScrolledClipView(scroll.contentView) }
        // Keep unsaved edits on the same stable ID. New/missing rows get a fresh editor.
        if id == nil || selectedID(table) != id { populateSelection(table) }
    }
    func receiveComponents(_ rows: [[String: Any]]) {
        let component = selectedID(componentTable), route = selectedID(routeTable)
        components = rows
        let jobChoices = components.first { $0["id"] as? String == "agent" }?["choices"] as? [String] ?? []
        if jobModel.currentEditor() == nil && jobModel.objectValues as? [String] != jobChoices {
            let draft = jobModel.stringValue
            jobModel.removeAllItems(); jobModel.addItems(withObjectValues: jobChoices); jobModel.stringValue = draft
        }
        reconcile(componentTable, id: component); reconcile(routeTable, id: route)
        if let value = routes.first(where: { $0["id"] as? String == routeID }) {
            setRouteOptions(value, preserving: routeChoice.titleOfSelectedItem)
            routeCLI.stringValue = "CLI: \(value["executable"] ?? "—")"
            routeCLI.toolTip = routeCLI.stringValue
        }
    }
    func setRouteOptions(_ value: [String: Any], preserving selected: String?) {
        let choices = value["choices"] as? [String] ?? (["conversation_model", "vision_model"].contains(value["id"] as? String ?? "") ? localModels : [])
        routeChoice.removeAllItems(); routeChoice.addItems(withTitles: choices)
        if let selected = selected, !choices.contains(selected) {
            routeChoice.addItem(withTitle: selected)
            routeChoice.item(withTitle: selected)?.isEnabled = false
        }
        if let selected = selected { routeChoice.selectItem(withTitle: selected) }
        routeChoice.isEnabled = !choices.isEmpty
        routeApply.isEnabled = routeChoice.selectedItem?.isEnabled == true && !choices.isEmpty
        if !routeApply.isEnabled { routeMessage.stringValue = "Текущей модели нет в доступном каталоге. Выберите другую или обновите список; настройка сохранена." }
    }
    @objc func routeChoiceChanged() {
        routeApply.isEnabled = routeChoice.selectedItem?.isEnabled == true
        routeMessage.stringValue = ""
    }
    @objc func refreshCatalog() {
        command(["ui-catalog-refresh"]) { value in self.renderCatalog(value); self.loadManagement() }
    }
    func loadCatalog() {
        guard !catalogPollBusy else { return }
        catalogPollBusy = true; lastCatalogPoll = Date()
        command(["ui-catalog"]) { value in
            self.catalogPollBusy = false
            let wasRefreshing = self.catalogRefreshing
            self.renderCatalog(value)
            if wasRefreshing && !self.catalogRefreshing { self.loadManagement() }
        }
    }
    func renderCatalog(_ value: [String: Any]?) {
        catalogRefreshing = value?["refreshing"] as? Bool == true
        let providers = value?["providers"] as? [[String: Any]] ?? []
        catalogStatus.stringValue = providers.map { row in
            let name = row["backend"] as? String == "codex" ? "OpenAI · Codex" : "Anthropic · Claude"
            let count = (row["models"] as? [[String: Any]] ?? []).count
            let error = row["error"] as? String != nil
            let stale = row["stale"] as? Bool == true
            return "\(name): \(count) моделей · \(self.time(row["fetched_at"]))\(error ? " · ошибка обновления, кэш" : stale ? " · устаревший кэш" : "")"
        }.joined(separator: "\n")
        if providers.isEmpty { catalogStatus.stringValue = "Каталог пока недоступен. Нажмите «Обновить каталоги»." }
        if catalogRefreshing { catalogStatus.stringValue += " · обновляю…" }
        let runtime = value?["runtime"] as? [String: Any] ?? [:]
        let compatible = (runtime["models"] as? [String] ?? []).count
        catalogStatus.stringValue += compatible > 0 ? "\nСовместимость моделей агента и расписания проверена через OpenClaw" : "\nСовместимость с OpenClaw не подтверждена: для агента доступны прежние настройки"
    }
    @objc func loadManagement() {
        if managementBusy { return }
        managementBusy = true; lastManagementRefresh = Date()
        command(["ui-components"]) { value in
            if let rows = value?["components"] as? [[String: Any]] { self.receiveComponents(rows) }
            else { self.componentMessage.stringValue = "Не удалось получить компоненты Butler."; self.routeMessage.stringValue = "Не удалось получить маршруты. Обновите состояние." }
            self.command(["ui-schedules"]) { value in
                self.managementBusy = false
                if let rows = value?["jobs"] as? [[String: Any]] {
                    let selected = self.selectedID(self.jobTable); self.jobs = rows; self.reconcile(self.jobTable, id: selected)
                    if let error = value?["error"] as? String { self.jobMessage.stringValue = error }
                } else { self.jobMessage.stringValue = "Не удалось получить задания Butler." }
            }
        }
    }
    func jsonCommand(_ name: String, _ values: [String: String], completion: @escaping ([String: Any]?) -> Void) {
        guard let data = try? JSONSerialization.data(withJSONObject: values), let json = String(data: data, encoding: .utf8) else { return }
        command([name, json], completion: completion)
    }
    @objc func applyRoute() {
        guard routeApply.isEnabled, let id = routeID, id == selectedID(routeTable), routes.contains(where: { $0["id"] as? String == id }), let model = routeChoice.titleOfSelectedItem else { return }
        let name = taskTitle(routes.first { $0["id"] as? String == id }!)
        routeMessage.stringValue = "Проверяю и сохраняю выбранную задачу…"
        jsonCommand("ui-components-update", ["id": id, "model": model]) { value in
            self.routeMessage.stringValue = value?["saved"] as? Bool == true ? "Модель для «\(name)» сохранена. \(value?["restart_required"] as? Bool == true ? "Перезапускаю только локальный сервис Butler." : "Новая модель используется со следующего запуска.")" : value?["message"] as? String ?? "Модель не применена. Проверьте доступность CLI."
            self.loadManagement()
        }
    }
    @objc func applyComponent() {
        guard let id = componentID, id == selectedID(componentTable), let value = components.first(where: { $0["id"] as? String == id }) else { return }
        var values = ["id": id]
        if value["model_key"] is String { values["model"] = componentModel.stringValue }
        if value["cli_key"] is String { values["executable"] = componentCLI.stringValue }
        componentMessage.stringValue = "Проверяю совместимость и сохраняю…"
        jsonCommand("ui-components-update", values) { value in
            self.componentMessage.stringValue = value?["saved"] as? Bool == true ? "Сохранено. \(value?["restart_required"] as? Bool == true ? "Перезапускаю только сервис наблюдения Butler." : "Действует со следующего запуска задачи.")" : value?["message"] as? String ?? "Не применено: проверьте совместимость, CLI и локальную модель."
            self.loadManagement()
        }
    }
    @objc func controlJob(_ sender: NSButton) {
        guard let id = jobID, id == selectedID(jobTable), let value = jobs.first(where: { $0["id"] as? String == id }) else { return }
        let actions = ["run", "pause", "resume", "stop", "update"]
        guard actions.indices.contains(sender.tag) else { return }
        var values = ["id": id, "action": actions[sender.tag]]
        if sender.tag == 4 {
            if value["kind"] as? String == "cron" {
                if !jobCron.stringValue.isEmpty { values["cron"] = jobCron.stringValue }
                values["timezone"] = jobZone.stringValue
                if !jobModel.stringValue.isEmpty { values["model"] = jobModel.stringValue }
            } else if id == "builtin:daily" { values["daily_time"] = jobDailyTime.stringValue }
        }
        jobMessage.stringValue = "Применяю к выбранному заданию Butler…"
        jsonCommand("ui-schedule-control", values) { value in
            self.jobMessage.stringValue = value?["message"] as? String ?? "Не удалось применить действие."
            self.loadManagement()
        }
    }

    func command(_ arguments: [String], completion: @escaping ([String: Any]?) -> Void) {
        if testing {
            testCommands.append(arguments)
            switch arguments.first {
            case "ui-components": completion(["components": components])
            case "ui-schedules": completion(["jobs": jobs])
            case "ui-components-update", "ui-schedule-control": completion(["saved": true, "ok": true])
            case "ui-display-select": completion(["ok": true])
            case "ui-display-action": completion(["status": "синтетически: действие принято"])
            case "ui-state": completion(testState)
            default: completion(nil)
            }
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: self.cli)
            process.arguments = arguments
            let output = Pipe()
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + (arguments.first == "ui-state" ? 3 : 15), execute: timeout)
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                timeout.cancel()
                let value = process.terminationStatus == 0 ? (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] : nil
                DispatchQueue.main.async { completion(value) }
            } catch {
                DispatchQueue.main.async { completion(nil) }
            }
        }
    }

    func time(_ value: Any?) -> String {
        guard let number = value as? Double, number > 0, number.isFinite else { return "ещё нет" }
        let date = Date(timeIntervalSince1970: number)
        return DateFormatter.localizedString(from: date, dateStyle: Calendar.current.isDateInToday(date) ? .none : .short, timeStyle: .medium)
    }

    func refresh() {
        if window.isVisible && section == .models && Date().timeIntervalSince(lastCatalogPoll) > (catalogRefreshing ? 2 : 60) { loadCatalog() }
        if window.isVisible && Date().timeIntervalSince(lastManagementRefresh) > 15 { loadManagement() }
        if busy { return }
        busy = true
        command(window.isVisible && [.dialogue, .observations].contains(section) ? ["ui-state", "--preview"] : ["ui-state"]) { result in
            self.busy = false
            guard let value = result else {
                self.heading.stringValue = "Butler недоступен"
                self.voiceState.stringValue = "Нет связи с локальным сервисом. Живой текст очищен."
                self.participantSummary.stringValue = "Проверка недоступна"
                self.message.stringValue = "Нет связи с сервисом Butler."
                self.artifactCaption.stringValue = "Предпросмотр недоступен до восстановления связи."
                self.item.button?.contentTintColor = Theme.error
                self.image.image = nil
                self.artifactImage.image = nil
                self.feed.note("Butler недоступен: нет соединения с локальным сервисом.")
                self.debugStatus.stringValue = "Нет связи с сервисом Butler. При смене устройств ожидайте перезапуска."
                self.debugImage.image = nil
                self.debugImage.caption.stringValue = "Нет связи с Butler\nОжидаю локальный сервис"
                for lamp in [self.microphoneWorking, self.cameraWorking, self.dialogueLamp, self.recordingLamp, self.voiceLamp] {
                    lamp.stringValue = "● Нет связи с Butler · состояние недоступно"
                    lamp.textColor = Theme.error
                }
                self.microphoneMeter.doubleValue = -70
                return
            }
            self.state = value
            if let version = value["version"] as? String, !version.isEmpty, version != self.version {
                self.version = version
                self.tagline.stringValue = "Личный помощник · v\(version)"
                self.window.subtitle = "\(self.section.title) · v\(version)"
            }
            self.updateDisplay(value["display"] as? [String: Any])
            let allowed = value["recording_allowed"] as? Bool == true
            let enabled = value["enabled"] as? Bool == true
            let reasons = ["face-unavailable": "Не вижу распознаваемого лица. Запись на паузе.",
                           "face-unrecognized-or-ambiguous": "Не уверен, кто перед камерой. Запись на паузе.",
                           "voice-unrecognized-or-ambiguous": "Не уверен, чей голос слышен. Запись на паузе.",
                           "unverified-presence": "Не все участники подтверждены. Запись на паузе."]
            self.heading.stringValue = allowed ? "Наблюдение работает" : enabled ? "Проверяю участников — запись на паузе" : "Наблюдение выключено вами"
            self.item.button?.contentTintColor = allowed ? Theme.accent : Theme.warn
            self.item.button?.toolTip = self.heading.stringValue
            self.pause.title = enabled ? "Пауза" : "Продолжить"
            let states = value["sources"] as? [String: String] ?? [:]
            let captured = value["last_capture"] as? [String: Any] ?? [:]
            let analyzed = value["last_analysis"] as? [String: Any] ?? [:]
            let labels = ["active": "работает", "paused": "пауза", "unavailable": "недоступен"]
            self.sources.stringValue = [("camera", "Камера"), ("microphone", "Микрофон"), ("screen", "Экран"), ("system-audio", "Звук приложений")].map {
                "\($0.1): \(labels[states[$0.0] ?? ""] ?? "ожидание"). Данные: \(self.time(captured[$0.0])); анализ данных за \(self.time(analyzed[$0.0]))."
            }.joined(separator: "\n")
            if !allowed {
                self.image.image = nil
                self.artifactImage.image = nil
                self.message.stringValue = enabled ? reasons[value["participant_check"] as? String ?? ""] ?? "Нужны три свежие успешные проверки участников. Свежие изображения сейчас недоступны." : "Камера и микрофон остановлены. Нажмите «Продолжить», чтобы включить проверки."
            } else if self.cameraPreview {
                if let preview = value["preview"] as? [String: Any], let image = previewImage(preview) {
                    self.image.image = image
                    self.message.stringValue = "Камера · \(self.time(preview["captured_at"])) · разрешённый кадр, исходник остаётся на этом Mac"
                } else {
                    self.image.image = nil
                    self.message.stringValue = "Камера работает. Ожидаю первый разрешённый кадр…"
                }
            }
            let recent = value["recent"] as? [[String: Any]] ?? []
            self.history.stringValue = recent.prefix(3).map {
                "\(self.time($0["at"])) · \($0["source"] as? String ?? ""): \(($0["details"] as? [String] ?? []).joined(separator: "; "))"
            }.joined(separator: "\n")
            let voice = value["voice"] as? [String: Any] ?? [:]
            let diagnostics = voice["diagnostics"] as? [String: Any] ?? [:]
            let commandState = voice["commands"] as? [String: Any] ?? [:]
            let ownerSetup = voice["owner_setup"] as? [String: Any] ?? [:]
            let setupStage = ownerSetup["stage"] as? String ?? "idle"
            let setupLabels = ["idle":"Эталоны голоса хранятся на этом Mac.","authenticating":"Подтвердите владельца Mac, затем нажмите «Начать» в окне записи.","recording":"Записываю контрольную фразу и образцы голоса: \(ownerSetup["seconds"] ?? 0) / 15 с.","validating":"Проверяю контрольную фразу и локальные эталоны.","saved":"Голос обновлён. Прежние эталоны и лицо сохранены.","cancelled":"Регистрация отменена. Эталоны не изменились.","failed":"Не удалось обновить голос: \(ownerSetup["error"] ?? "локальная проверка не пройдена"). Эталоны сохранены."]
            self.ownerSetupStatus.stringValue = setupLabels[setupStage] ?? setupStage
            if ownerSetup["target"] as? String == "guest" {
                if setupStage == "saved" { self.ownerSetupStatus.stringValue = "Гость зарегистрирован локально. Эталоны владельца сохранены; материалы регистрации удалены." }
                if setupStage == "failed" { self.ownerSetupStatus.stringValue = "Гость не зарегистрирован: \(ownerSetup["error"] ?? "локальная проверка не пройдена"). Эталоны владельца сохранены." }
            }
            if let pending = commandState["pending"] as? [String: Any], let review = pending["review"] as? String {
                self.commandReview.stringValue = "Ожидаю подтверждения владельца\n\(review)\nСкажите «подтверждаю» или «отмена»."
                self.commandReview.textColor = Theme.warn
            } else {
                self.commandReview.stringValue = "Голосовое управление Butler\nИзменения применяются после вашего подтверждения; отмена и потеря разрешения сбрасывают команду."
                self.commandReview.textColor = Theme.dim
            }
            let readiness = diagnostics["readiness"] as? [String: String] ?? [:]
            let currentTimings = diagnostics["timings"] as? [String: Any] ?? [:]
            let timings = currentTimings["first_playback"] != nil ? currentTimings : diagnostics["last_response_timings"] as? [String: Any] ?? currentTimings
            func seconds(_ key: String) -> String {
                guard let value = timings[key] as? Double else { return "—" }
                return String(format: "%.2f с", value)
            }
            let warm = readiness["voice"] == "ready" && readiness["model"] == "ready" && readiness["asr"] == "ready"
            self.latencySummary.stringValue = "\(warm ? "Модели готовы" : "Прогрев / доступность: ASR \(readiness["asr"] ?? "—"), диалог \(readiness["model"] ?? "—"), голос \(readiness["voice"] ?? "—")") · контекст: \(diagnostics["context_turns"] ?? 0) реплик\nПоследний ответ: ASR \(seconds("asr_done")) · текст \(seconds("first_reply_text")) · звук \(seconds("first_playback"))\(timings["source"] as? String == "synthetic-check" ? " · синтетический образец" : "")"
            if allowed, let preview = value["preview"] as? [String: Any] {
                self.debugImage.image = previewImage(preview)
            } else { self.debugImage.image = nil }
            self.debugImage.caption.stringValue = allowed ? "Ожидаю первый кадр\nКамера подключается" : "Запись на паузе\nОжидаю подтверждение участников"
            let participant = voice["participant"] as? [String: Any] ?? [:]
            let audio = voice["audio"] as? [String: Any] ?? [:]
            let debug = voice["debug"] as? [String: Any] ?? [:]
            let microphone = debug["microphone"] as? [String: Any] ?? [:]
            let input = voice["input"] as? [String: Any] ?? [:]
            let working = input["working"] as? Bool == true
            let inputLabel = input["selection_verified"] as? Bool == false ? "Открытый микрофон не соответствует выбранному"
                : input["muted"] as? Bool == true ? "Микрофон выключен на устройстве / в системе"
                : (input["gain"] as? Double) == 0 ? "Уровень входа установлен в ноль" : working ? "Работает · поступает звук" : "Нет свежего звука"
            self.microphoneWorking.stringValue = "● \(input["name"] ?? "Микрофон")\n\(inputLabel)"
            self.microphoneWorking.textColor = working ? Theme.accent : Theme.error
            self.audioRoute.stringValue = "Микрофон Butler: \(input["name"] ?? "не определён")\nМикрофон Mac: \(audio["system_default_microphone"] ?? "не определён")\nВывод звука: \(audio["system_default_output"] ?? "не определён")"
            let cameraWorking = (voice["camera_input"] as? [String: Any])?["working"] as? Bool == true
            self.cameraWorking.stringValue = "● Камера: \(cameraWorking ? "получаю видео" : "нет свежего видео")"
            self.cameraWorking.textColor = cameraWorking ? Theme.accent : Theme.error
            let dialogueWorking = voice["dialogue_allowed"] as? Bool == true
            let listening = voice["listening"] as? [String: Any] ?? [:]
            let hearingEnabled = listening["enabled"] as? Bool == true
            // DEC-062: general replies need no owner identity; private stays gated.
            let generalWorking = !dialogueWorking && voice["dialogue_mode"] as? String == "general"
            self.dialogueLamp.stringValue = "● Личный диалог: \(dialogueWorking ? "разрешён" : "проверяю владельца")\(generalWorking ? " · общий разговор доступен" : "")\(hearingEnabled && working ? " · микрофон слушает" : "")"
            self.dialogueLamp.textColor = dialogueWorking ? Theme.accent : generalWorking ? Theme.warn : Theme.error
            let storageError = (value["archive"] as? [String: Any])?["storage_error"] as? String
            self.recordingLamp.stringValue = "● Запись: \(storageError == "low-disk-space" ? "не хватает места на диске" : storageError == "analysis-buffer-full" ? "буфер заполнен, ожидаю анализа" : storageError != nil ? "ошибка хранилища" : allowed ? "временный буфер" : "пауза")"
            self.recordingLamp.textColor = allowed && storageError == nil ? Theme.accent : Theme.error
            let storage = value["storage"] as? [String: Any] ?? [:]
            let classes = storage["classes"] as? [[String: Any]] ?? []
            func bytes(_ value: Any?) -> String {
                ByteCountFormatter.string(fromByteCount: (value as? NSNumber)?.int64Value ?? 0, countStyle: .binary)
            }
            let legacy = classes.first { $0["storage_class"] as? String == "legacy" }
            let preserved = storage["preserved"] as? [String: Any] ?? [:]
            let eventFrames = storage["event_frames"] as? [String: Any] ?? [:]
            self.storageInfo.stringValue = "После анализа исходники удаляются. Постоянный видеоархив выключен.\nБуфер: \(bytes(storage["spool_bytes"])) из \(bytes(storage["spool_limit_bytes"])); ожидают анализа: \(storage["pending"] ?? 0).\nСтарые исходники: \(bytes(legacy?["bytes"])); очищаются после анализа.\nСохранено отдельно: \(bytes(preserved["bytes"])), файлов: \(preserved["files"] ?? 0).\nКлючевые кадры событий: \(eventFrames["files"] ?? 0), сегодня \(eventFrames["today"] ?? 0) из \(eventFrames["daily_limit"] ?? 20)\(eventFrames["error"] is String ? " · ошибка сохранения" : "").\nИсходники: \(storage["raw_path"] ?? "—")\nРезультаты: \(storage["database"] ?? "—")\nСохранённое: \(storage["preserved_path"] ?? "—")\nОчистка: \(storage["cleanup_error"] as? String ?? "работает")"
            let voiceReady = voice["ready"] as? Bool == true && voice["state"] as? String != "unavailable" && readiness["voice"] == "ready"
            let outputMuted = audio["system_output_muted"] as? Bool == true || audio["system_output_volume"] as? Double == 0
            self.voiceLamp.stringValue = "● Голос: \(outputMuted ? "вывод звука выключен" : voiceReady ? "готов" : "недоступен или выключен")"
            self.voiceLamp.textColor = voiceReady && !outputMuted ? Theme.accent : Theme.error
            let phase = voice["phase"] as? String ?? "idle"
            let phaseLabels = ["idle": "ожидание", "listening": "слышу речь", "transcription": "распознаю слова", "address": "проверяю обращение", "reply": "готовлю ответ", "control": "управление Butler", "synthesis": "синтезирую голос", "playback": "воспроизвожу ответ"]
            let statusLines = ["ЖИВАЯ ОТЛАДКА · \(self.time(Date().timeIntervalSince1970))",
                         "Только на этом Mac. Пауза очищает временный текст; проверка владельца не выключает разрешённое прослушивание.", "",
                         "Постоянное прослушивание: \(hearingEnabled ? "включено" : "выключено"); этап: \(listening["phase"] ?? "—")",
                         "Счётчики прослушивания: \(listening["counts"] ?? [:])",
                         "Владелец перед камерой: \(participant["owner_camera_confirmed"] as? Bool == true ? "подтверждён" : "не подтверждён")",
                         "Владелец по голосу: \(participant["owner_voice_confirmed"] as? Bool == true ? "подтверждён" : "не подтверждён"); проверка: \(participant["voice_check"] ?? "нет ошибки")",
                         "Разговор подтверждён: \(participant["confirmed_by"] ?? "ожидаю владельца")",
                         "Свежие проверки: \(participant["consecutive_checks"] ?? 0)/3; возраст кадра/звука: \((participant["capture_age_seconds"] as? NSNumber)?.stringValue ?? "—") с; после проверки: \((participant["check_age_seconds"] as? NSNumber)?.stringValue ?? "—") с",
                         "Разговор: \(voice["dialogue_allowed"] as? Bool == true ? "разрешён" : "пауза"); запись: \(allowed ? "разрешена" : "пауза")",
                         "Режим ответа: \(["private": "личный (владелец подтверждён)", "general": "общий (без личных данных и команд)", "off": "выключен"][voice["dialogue_mode"] as? String ?? "off"] ?? "—")",
                         "Причина паузы: \(value["participant_check"] ?? "нет")",
                         "Камера: \(voice["camera_input"] ?? [:])",
                         "Микрофон: \(audio["ready"] as? Bool == true ? "работает" : "недоступен"); подавление эха: \(audio["echo_backend"] ?? "—")",
                         "Уровень: \(microphone["rms_dbfs"] ?? "—") dBFS; речь: \(microphone["speech"] as? Bool == true ? "да" : "нет")",
                         "Приглушение других приложений: \(audio["other_audio_ducking"] ?? "—")",
                         "Voice Processing (фактически): \(audio["voice_processing_enabled"] ?? "не подтверждено")",
                         "Микрофон выбран: \(input["selected_name"] ?? "—"); открыт: \(input["name"] ?? "—"); совпадение: \(input["selection_verified"] ?? "не подтверждено")",
                         "Маршрут входа: \(audio["input_route"] ?? "—")",
                         "Mute входа: \(audio["input_muted"] ?? "устройство не сообщает"); уровень входа: \(audio["input_volume"] ?? "устройство не сообщает")",
                         "Аудиоформаты: \(audio["formats"] ?? "—")",
                         "Состояние: \(voice["state"] ?? "—"); этап: \(phaseLabels[phase] ?? phase)",
                         "Воспроизведение: \((voice["playback"] as? [String: Any])?["state"] ?? "—")",
                         "Прогрев моделей: \(readiness)",
                         "Распознаватель (фактически): \(diagnostics["recognizer"] ?? "—")",
                         "Время от готовой реплики: \(timings)",
                         "Диалог · контекст и время модели (мс): \(diagnostics["inference"] ?? [:])",
                         "Фоновый анализ · текущие факты и сопоставление имён (мс): \(value["analysis_inference"] ?? [:]); кадров за запрос: \(value["camera_analysis_batch_size"] ?? "—")",
                         "Слух · ASR и ожидание проверки (мс): \((voice["listening"] as? [String: Any])?["timings"] ?? [:])",
                         "Проверка участников: \((value["check_latency_seconds"] as? NSNumber)?.stringValue ?? "—") с · пропущено: \(value["participant_check_skipped"] ?? 0) · ошибка: \(value["participant_check_error"] as? String ?? "нет")",
                         "Временный контекст: \(diagnostics["context_turns"] ?? 0) ответов, \(diagnostics["ambient_context_items"] ?? 0) разрешённых реплик",
                         "Ошибка звука: \(audio["failure"] ?? "нет"); код: \(audio["code"] ?? "—")",
                         "Последняя отмена: \(voice["last_cancel_reason"] ?? "нет")",
                         "Голосовые команды: \(voice["commands"] ?? [:])",
                         "Память и знания Butler: \(value["knowledge"] ?? [:])",
                         "Пассивный архив и звук приложений: \(value["archive"] ?? [:])",
                         "Отмены по этапам и причинам: \(diagnostics["cancellations"] ?? [:])",
                         "Последние решения (без текста): \(diagnostics["turns"] ?? [])",
                         "Ошибка фонового анализа: \(value["analysis_error"] ?? "нет")"]
            let analysisDiagnostic = value["analysis_diagnostic"] as? [String: Any] ?? [:]
            let disclosure = voice["disclosure"] as? [String: Any] ?? [:]
            self.debugStatus.stringValue = (statusLines + ["Этап фонового анализа: \(analysisDiagnostic)", "Собеседники и представление: \(disclosure)"]).joined(separator: "\n")
            self.participantSummary.stringValue = "Владелец: \(dialogueWorking ? "подтверждён · " + (participant["confirmed_by"] as? String == "voice" ? "голос" : "камера") : "проверяю")\nСвежие проверки: \(participant["consecutive_checks"] ?? 0)/3"
            self.microphoneMeter.doubleValue = input["rms_dbfs"] as? Double ?? -70
            self.feed.show((value["feed"] as? [[String: Any]] ?? []).map(FeedEntry.init))
            let profiles = voice["profiles"] as? [[String: String]] ?? []
            let ids = profiles.compactMap { $0["id"] }
            if ids != self.profileIDs {
                self.profileIDs = ids
                self.voices.removeAllItems()
                if profiles.isEmpty { self.voices.addItem(withTitle: "Образцы готовятся…") }
                for profile in profiles { self.voices.addItem(withTitle: profile["name"] ?? "Голос") }
            }
            if let selected = voice["selected"] as? String {
                let states = ["choose-voice": "выберите голос", "disabled": "выключен", "unavailable": "недоступен: проверьте локальную модель и звук", "paused": hearingEnabled ? "микрофон слушает; личный ответ ждёт подтверждения владельца" : "наблюдение на паузе", "ready": "готов к разговору; имя не обязательно", "listening": "слушаю вашу реплику", "thinking": "обдумываю ответ", "synthesizing": "готовлю голос", "speaking": "говорю", "not-configured": "подключается"]
                let key = voice["state"] as? String ?? "not-configured"
                let name = profiles.first { $0["id"] == selected }?["name"] ?? selected
                self.voiceState.stringValue = "\(name): \(states[key] ?? key)."
                if let heard = voice["last_heard"] as? String, !heard.isEmpty { self.voiceState.stringValue += "\nРаспознано: \(heard)" }
                if let reply = voice["last_reply"] as? String, !reply.isEmpty { self.voiceState.stringValue += "\nButler: \(reply)" }
                if key == "unavailable", let phase = voice["phase"] as? String {
                    let phases = ["transcription": "распознавание слов", "address": "распознавание обращения", "reply": "подготовка ответа", "synthesis": "синтез голоса", "playback": "воспроизведение"]
                    if let label = phases[phase] { self.voiceState.stringValue += "\nЭтап сбоя: \(label)." }
                }
            }
            self.voiceToggle.title = voice["enabled"] as? Bool == true ? "Выключить голос" : "Включить голос"
            self.faceWake.state = voice["face_wake_dialogue"] as? Bool == true ? .on : .off
            let archive = value["archive"] as? [String: Any] ?? [:]
            self.archiveToggle.state = archive["enabled"] as? Bool == true ? .on : .off
            let work = value["work"] as? [String: Any] ?? [:]
            let workRows = work["jobs"] as? [[String: Any]] ?? []
            let workLabels = ["running":"выполняется", "done":"готово", "failed":"ошибка", "cancelled":"отменено", "interrupted":"прервано"]
            self.workStatus.stringValue = workRows.isEmpty ? "Фоновых поручений пока нет. Можно поручить задачу Codex или Claude в конкретном проекте." : workRows.prefix(2).map {
                "\($0["project"] ?? "Проект") · \(workLabels[$0["status"] as? String ?? ""] ?? "проверяю") · \($0["backend"] ?? "CLI") / \($0["model"] ?? "—")"
            }.joined(separator: "\n")
            let meeting = voice["meeting"] as? [String: Any] ?? [:]
            let meetingQuiet = meeting["quiet"] as? Bool == true
            let meetingApps = meeting["apps"] as? [String] ?? []
            self.meetingStatus.stringValue = meetingQuiet
                ? "Встреча / тихий режим · отвечаю только на явное обращение к Butler.\n" + (meetingApps.isEmpty ? "Автоматическая проверка или выбранный вами режим." : meetingApps.joined(separator: ", "))
                : "Обычный разговор · имя не обязательно. Учитываю, к кому вы обращаетесь."
            self.meetingStatus.textColor = meetingQuiet ? Theme.warn : Theme.dim
            self.meetingChoice.selectItem(at: ["auto", "quiet", "available"].firstIndex(of: meeting["mode"] as? String ?? "auto") ?? 0)
            if voice["dialogue_allowed"] as? Bool == true && !allowed {
                self.voiceState.stringValue += "\nРазговор доступен; запись наблюдений на паузе."
            }
            if let visual = value["visual"] as? [String: Any] {
                let roots = visual["roots"] as? [String] ?? []
                if roots != self.roots {
                    self.roots = roots
                    self.materials.removeAllItems()
                    if roots.isEmpty { self.materials.addItem(withTitle: "Выберите файлы или папки…") }
                    for root in roots { self.materials.addItem(withTitle: root) }
                }
                let status = visual["status"] as? String ?? "idle"
                let labels = ["idle": "Выберите свои материалы для поиска или задайте рисунок.", "queued": "Запрос принят…", "working": "Обрабатываю локально…", "ready": "Готово. Источник и тип результата указаны под изображением.", "failed": "Не удалось: проверьте выбранные материалы, локальную модель и свободное место (резерв 11 ГБ).", "cancelled": "Отменено или остановлено разрешением участников.", "interrupted": "Прервано перезапуском.", "sending": "Отправка была начата; повтор запрещён.", "delivery-uncertain": "Результат отправки неизвестен; автоматического повтора не будет."]
                self.visualState.stringValue = labels[status] ?? status
                if let progress = visual["progress"] as? String, progress.hasPrefix("searching:") {
                    let parts = progress.replacingOccurrences(of: "searching:", with: "").components(separatedBy: ";available:")
                    if parts.count == 2 { self.visualState.stringValue += "\nПроверено \(parts[0]); всего доступно \(parts[1]). Поиск проверяет до 24 изображений за запрос." }
                }
                self.artifacts = visual["artifacts"] as? [[String: Any]] ?? []
                let request = visual["request_id"] as? String ?? ""
                let previous = self.gallery.itemTitles
                let titles = self.artifacts.enumerated().map { index, artifact in
                    let kind = artifact["kind"] as? String ?? ""
                    let name = ["captured": "Снимок", "imported": "Мой материал · кандидат", "generated": "Создано моделью"][kind] ?? kind
                    return "\(index + 1). \(name) · \(self.time(artifact["at"]))"
                }
                if previous != titles && !titles.isEmpty {
                    self.gallery.removeAllItems()
                    self.gallery.addItems(withTitles: titles)
                } else if titles.isEmpty {
                    self.gallery.removeAllItems()
                    self.gallery.addItem(withTitle: "Результатов пока нет")
                }
                if allowed && status == "ready" && request != self.lastVisualRequest && !self.artifacts.isEmpty {
                    self.lastVisualRequest = request
                    self.displayArtifact()
                }
            }
        }
    }

    @objc func show() {
        defaults.set(true, forKey: "windowVisible")
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func windowWillClose(_ notification: Notification) { defaults.set(false, forKey: "windowVisible") }
    @objc func showDebug() {
        select(.components)
        show()
        if !settingsLoaded { loadSettings() }
        loadManagement()
    }
    @objc func loadSettings() {
        command(["ui-settings"]) { value in
            guard let value = value, let devices = value["devices"] as? [String: Any] else {
                self.settingMessage.stringValue = "Не удалось прочитать устройства или настройки."
                return
            }
            self.cameraOptions = devices["camera"] as? [[String: String]] ?? []
            self.microphoneOptions = devices["microphone"] as? [[String: String]] ?? []
            for (menu, options, key) in [(self.cameraChoice, self.cameraOptions, "camera"), (self.microphoneChoice, self.microphoneOptions, "microphone")] {
                menu.removeAllItems()
                menu.addItems(withTitles: options.map { $0["name"] ?? "Устройство" })
                if let selected = options.firstIndex(where: { $0["id"] == value[key] as? String }) { menu.selectItem(at: selected) }
            }
            self.localModels = value["models"] as? [String] ?? []
            if self.routeChoice.numberOfItems == 0 { self.populateSelection(self.routeTable) }
            if let size = value["camera_size"] as? String, let index = ["640x480","1280x720","1920x1080"].firstIndex(of:size) { self.cameraSizeChoice.selectItem(at:index) }
            if let language = value["speech_language"] as? String, let index = ["ru", "auto", "en", "de", "uk"].firstIndex(of: language) { self.languageChoice.selectItem(at: index) }
            self.runtimeInfo.stringValue = "Диалог: \(value["conversation_model"] ?? "—")\nЗрение: \(value["vision_model"] ?? "—")\nРаспознавание: Whisper.cpp · \(value["asr_model"] ?? "—")\nЯзык: \(value["speech_language"] ?? "—")\nГолос: \(value["tts_model"] ?? "—")\nЭхо: \(value["echo_backend"] ?? "—")\nОбработка локальная; внешних и платных резервных сервисов нет."
            self.settingsLoaded = true
        }
    }
    func saveSettings(_ values: [String: String]) {
        guard let data = try? JSONSerialization.data(withJSONObject: values), let json = String(data: data, encoding: .utf8) else { return }
        settingMessage.stringValue = "Применяю. Butler перезапустится и заново проверит участников…"
        command(["ui-settings-update", json]) { value in
            if value?["saved"] as? Bool != true { self.settingMessage.stringValue = "Настройки не применены: устройство или локальная модель недоступны." }
        }
    }
    @objc func applyDevices() {
        guard cameraOptions.indices.contains(cameraChoice.indexOfSelectedItem), microphoneOptions.indices.contains(microphoneChoice.indexOfSelectedItem),
              let camera = cameraOptions[cameraChoice.indexOfSelectedItem]["id"], let microphone = microphoneOptions[microphoneChoice.indexOfSelectedItem]["id"] else { return }
        let sizes = ["640x480","1280x720","1920x1080"]
        let size = sizes.indices.contains(cameraSizeChoice.indexOfSelectedItem) ? sizes[cameraSizeChoice.indexOfSelectedItem] : "640x480"
        saveSettings(["camera": camera, "microphone": microphone, "camera_size": size])
    }
    @objc func videoEffects() { command(["ui-video-effects"]) { _ in } }
    @objc func ownerSetup() { command(["ui-owner-setup"]) { _ in } }

    @objc func checkVoice() {
        command(["ui-voice-check"]) { value in
            self.settingMessage.stringValue = value?["queued"] as? Bool == true
                ? "Синтетическая проверка запущена. Это проверка распознавания и динамика, не микрофона."
                : "Проверка не началась: нужны готовый звук и разрешение на разговор."
        }
    }
    @objc func applyLanguage() {
        let languages = ["ru", "auto", "en", "de", "uk"]
        guard languages.indices.contains(languageChoice.indexOfSelectedItem) else { return }
        saveSettings(["speech_language": languages[languageChoice.indexOfSelectedItem]])
    }
    @objc func toggle() {
        command([state["enabled"] as? Bool == true ? "pause" : "resume"]) { _ in self.refresh() }
    }
    func view(_ source: String) {
        cameraPreview = source == "camera"
        message.stringValue = "Получаю свежий разрешённый снимок…"
        command(["ui-observe", source]) { result in
            guard let result = result, result["status"] as? String == "ok", let path = result["path"] as? String,
                  self.state["recording_allowed"] as? Bool == true else {
                self.image.image = nil
                self.message.stringValue = "Свежий снимок недоступен: источник, анализ или разрешение на паузе."
                return
            }
            self.image.image = NSImage(contentsOfFile: path)
            self.message.stringValue = "\(source == "camera" ? "Камера" : "Экран") · \(self.time(result["captured_at"])) · исходник остаётся на этом Mac"
        }
    }
    @objc func camera() { view("camera") }
    @objc func screen() { view("screen") }
    @objc func preview() {
        guard profileIDs.indices.contains(voices.indexOfSelectedItem) else { return }
        command(["voice-preview", profileIDs[voices.indexOfSelectedItem]]) { _ in }
    }
    @objc func selectVoice() {
        guard profileIDs.indices.contains(voices.indexOfSelectedItem) else { return }
        command(["voice-select", profileIDs[voices.indexOfSelectedItem]]) { _ in self.refresh() }
    }
    @objc func toggleVoice() {
        let voice = state["voice"] as? [String: Any] ?? [:]
        command(["preferences", voice["enabled"] as? Bool == true ? "{\"voice_enabled\":false}" : "{\"voice_enabled\":true}"]) { _ in self.refresh() }
    }
    @objc func guestSetup() {
        let name = guestName.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
        guard !name.isEmpty && name.count <= 80 else { ownerSetupStatus.stringValue = "Введите короткое имя гостя."; return }
        command(["ui-guest-setup",name]) { value in
            if value?["queued"] as? Bool == true { self.guestName.stringValue = "" }
            self.refresh()
        }
    }
    @objc func changeMeetingMode() {
        let modes = ["auto", "quiet", "available"]
        guard modes.indices.contains(meetingChoice.indexOfSelectedItem) else { return }
        let payload = ["meeting_mode": modes[meetingChoice.indexOfSelectedItem]]
        guard let data = try? JSONSerialization.data(withJSONObject: payload), let json = String(data: data, encoding: .utf8) else { return }
        command(["preferences", json]) { _ in self.refresh() }
    }
    @objc func toggleArchive() {
        command(["preferences", archiveToggle.state == .on ? "{\"continuous_archive\":true}" : "{\"continuous_archive\":false}"]) { _ in self.refresh() }
    }
    @objc func toggleFaceWake() {
        command(["preferences", faceWake.state == .on ? "{\"face_wake_dialogue\":true}" : "{\"face_wake_dialogue\":false}"]) { _ in self.refresh() }
    }
    @objc func addMaterials() {
        let picker = NSOpenPanel()
        picker.title = "Выберите только те фотографии, рисунки или папки, где Butler может искать"
        picker.canChooseFiles = true
        picker.canChooseDirectories = true
        picker.allowsMultipleSelection = true
        picker.beginSheetModal(for: window) { response in
            if response == .OK {
                for url in picker.urls { self.command(["ui-material-add", url.path]) { _ in self.refresh() } }
            }
        }
    }
    @objc func removeMaterial() {
        guard roots.indices.contains(materials.indexOfSelectedItem) else { return }
        command(["ui-material-remove", roots[materials.indexOfSelectedItem]]) { _ in self.refresh() }
    }
    func requestImage(_ intent: String) {
        let text = query.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { visualState.stringValue = "Введите, что именно найти или нарисовать."; return }
        var args = ["ui-visual-request", intent, text]
        if sendTelegram.state == .on { args.append("--telegram") }
        command(args) { result in
            if result == nil { self.visualState.stringValue = "Запрос недоступен: проверьте разрешения и текущую задачу." }
            self.refresh()
        }
    }
    @objc func findImage() { requestImage("find-image") }
    @objc func generateImage() { requestImage("generate-image") }
    @objc func cancelImage() { command(["ui-visual-cancel"]) { _ in self.refresh() } }
    @objc func showArtifact() { displayArtifact(); select(.images) }
    func displayArtifact() {
        guard state["recording_allowed"] as? Bool == true, artifacts.indices.contains(gallery.indexOfSelectedItem),
              let path = artifacts[gallery.indexOfSelectedItem]["path"] as? String else { artifactImage.image = nil; return }
        artifactImage.image = NSImage(contentsOfFile: path)
        let artifact = artifacts[gallery.indexOfSelectedItem]
        let source = artifact["source"] as? String ?? "Локальная генерация"
        artifactCaption.stringValue = "\(gallery.titleOfSelectedItem ?? "Изображение")\n\(source)"
    }
    @objc func deleteArtifact() {
        guard artifacts.indices.contains(gallery.indexOfSelectedItem), let key = artifacts[gallery.indexOfSelectedItem]["id"] as? String else { return }
        artifactImage.image = nil
        command(["ui-visual-delete", key]) { _ in self.refresh() }
    }
}

// Runs without the service, devices, files, models or network. Bitmap exports are
// drawn from these synthetic views only; never from a screen or a live window.
func runLayoutTests(output: String) -> Int32 {
    let domain = "com.boris.butler-ui.test.\(UUID().uuidString)"
    let prefs = UserDefaults(suiteName: domain)!
    defer { prefs.removePersistentDomain(forName: domain) }
    let productionPreferences = Panel.preferenceStore()
    let panel = Panel(cli: "/usr/bin/false", testing: true, defaults: prefs)
    let directory = URL(fileURLWithPath: output)
    try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    var checks: [[String: Any]] = []
    var snapshots: [[String: Any]] = []
    func check(_ name: String, _ passed: Bool) { checks.append(["name": name, "passed": passed]) }
    func layout() {
        panel.window.contentView?.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        panel.window.contentView?.layoutSubtreeIfNeeded()
    }
    func geometry() -> [String] {
        let root = panel.window.contentView!
        return [panel.debugImage, panel.cameraChoice, panel.microphoneChoice, panel.microphoneMeter, panel.dialogueLamp, panel.navigation, panel.content].map {
            NSStringFromRect($0.convert($0.bounds, to: root))
        } + [NSStringFromRect(panel.window.frame)]
    }
    func syntheticImage(_ size: NSSize) -> NSImage {
        let image = NSImage(size: size)
        image.lockFocus()
        NSColor(calibratedRed: 0.12, green: 0.23, blue: 0.36, alpha: 1).setFill()
        NSBezierPath(rect: NSRect(origin: .zero, size: size)).fill()
        NSColor(calibratedRed: 0.28, green: 0.66, blue: 0.68, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: size.width * 0.2, y: size.height * 0.2, width: size.width * 0.6, height: size.height * 0.6), xRadius: 18, yRadius: 18).fill()
        let text = "SYNTHETIC PREVIEW"
        (text as NSString).draw(at: NSPoint(x: size.width * 0.23, y: size.height * 0.47), withAttributes: [.font: Theme.font(size.width / 32, .semibold), .foregroundColor: NSColor.white])
        image.unlockFocus()
        return image
    }
    let previewData = syntheticImage(NSSize(width: 64, height: 36)).tiffRepresentation!
    check("volatile-camera-preview-decodes-without-file", previewImage(["jpeg_base64": previewData.base64EncodedString()]) != nil)
    check("invalid-volatile-camera-preview-is-rejected", previewImage(["jpeg_base64": "invalid"]) == nil)
    func export(_ name: String) {
        layout()
        guard let view = panel.window.contentView, let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { check("export-\(name)", false); return }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        if let data = bitmap.representation(using: .png, properties: [:]) { try? data.write(to: directory.appendingPathComponent(name + ".png")) }
        else { check("export-\(name)", false) }
    }
    check("bundled-production-preferences-resolve", productionPreferences === UserDefaults.standard && Bundle.main.bundleIdentifier == "com.boris.butler-ui")
    panel.window.title = "Butler — синтетическая проверка"
    panel.window.setContentSize(NSSize(width: 1280, height: 820))
    panel.cameraChoice.addItem(withTitle: "Тестовая камера")
    panel.microphoneChoice.addItem(withTitle: "Тестовый микрофон")
    panel.microphoneMeter.doubleValue = -38
    panel.microphoneWorking.stringValue = "● Тестовый микрофон\nРаботает · поступает звук"
    panel.microphoneWorking.textColor = Theme.accent
    panel.cameraWorking.stringValue = "● Камера: получаю видео"; panel.cameraWorking.textColor = Theme.accent
    panel.dialogueLamp.stringValue = "● Разговор: разрешён"; panel.dialogueLamp.textColor = Theme.accent
    panel.recordingLamp.stringValue = "● Запись: разрешена"; panel.recordingLamp.textColor = Theme.accent
    panel.voiceLamp.stringValue = "● Голос: готов"; panel.voiceLamp.textColor = Theme.accent
    panel.heading.stringValue = "Butler готов к разговору"
    panel.participantSummary.stringValue = "Тестовые данные\nНикаких устройств и записи"
    let feedStart = 1_790_000_000.0
    func synthetic(_ index: Int, _ kind: String, _ who: String, _ label: String, _ text: String) -> FeedEntry {
        FeedEntry(["id": "synthetic-\(index)", "at": feedStart + Double(index) * 7, "who": who, "kind": kind, "label": label, "text": text])
    }
    var timeline = [synthetic(0, "notification", "Butler → Telegram", "status · доставлено", "СИНТЕТИЧЕСКИЙ ПРИМЕР · реальная речь не используется"),
                    synthetic(1, "heard", "Борис", "РАСПОЗНАНО", "Батлер, покажи мои задачи."),
                    synthetic(2, "event", "Система", "ПРОВЕРКА", "участники подтверждены"),
                    synthetic(3, "reply", "Butler", "ОТВЕТ", "Задачи и управление запусками доступны в разделе «Расписание».")]
    panel.feed.show(timeline)
    panel.select(.dialogue)
    layout()
    check("feed-separates-kinds", panel.feed.stack.arrangedSubviews.count == 4
          && (panel.feed.stack.arrangedSubviews[0] as? FeedEntryView)?.stamp.textColor == Theme.notice
          && (panel.feed.stack.arrangedSubviews[1] as? FeedEntryView)?.stamp.textColor == Theme.owner
          && (panel.feed.stack.arrangedSubviews[3] as? FeedEntryView)?.body.stringValue.hasPrefix("Задачи") == true)
    for index in 4..<60 { timeline.append(synthetic(index, index % 2 == 0 ? "heard" : "reply", index % 2 == 0 ? "Борис" : "Butler", "РЕПЛИКА", "Синтетическая реплика номер \(index) для проверки прокрутки ленты.")) }
    panel.feed.show(timeline); layout()
    check("feed-follows-new-entries-at-bottom", panel.feed.atBottom && panel.feed.documentHeight > panel.feed.scroll.contentView.bounds.height)
    panel.feed.scrollToTop(); layout()
    timeline.append(synthetic(60, "reply", "Butler", "ОТВЕТ", "Новая запись не должна дёргать читателя наверху."))
    panel.feed.show(timeline); layout()
    check("feed-keeps-position-while-reading-older", panel.feed.scroll.contentView.bounds.origin.y < 1 && panel.feed.stack.arrangedSubviews.count == 61)
    // Reading in the middle: a new entry must neither rebuild the rows nor move what is being read.
    let middle = panel.feed.documentHeight / 2
    panel.feed.scroll.contentView.scroll(to: NSPoint(x: 0, y: middle)); panel.feed.scroll.reflectScrolledClipView(panel.feed.scroll.contentView); layout()
    let anchorView = panel.feed.stack.arrangedSubviews.first { $0.frame.maxY > panel.feed.scroll.contentView.bounds.origin.y }!
    let anchorOffset = anchorView.frame.minY - panel.feed.scroll.contentView.bounds.origin.y
    let rowBefore = panel.feed.stack.arrangedSubviews[20]
    timeline.append(synthetic(61, "heard", "Борис", "РАСПОЗНАНО", "Ещё одна запись, пока читатель в середине ленты."))
    panel.feed.show(timeline); layout()
    check("feed-appends-without-rebuilding-rows", panel.feed.stack.arrangedSubviews[20] === rowBefore && panel.feed.stack.arrangedSubviews.count == 62)
    check("feed-keeps-anchor-while-reading-middle", abs((anchorView.frame.minY - panel.feed.scroll.contentView.bounds.origin.y) - anchorOffset) < 1)
    // A streamed reply grows in place: same id, longer text, same row.
    let growing = panel.feed.stack.arrangedSubviews.last!
    timeline[timeline.count - 1] = FeedEntry(["id": "synthetic-61", "at": feedStart + 61 * 7, "who": "Борис", "kind": "heard", "label": "РАСПОЗНАНО",
                                              "text": "Ещё одна запись, пока читатель в середине ленты. И её продолжение."])
    panel.feed.show(timeline); layout()
    check("feed-updates-entry-in-place", panel.feed.stack.arrangedSubviews.last === growing && (growing as? FeedEntryView)?.body.stringValue.hasSuffix("продолжение.") == true)
    panel.feed.eventsToggle.state = .off; panel.feed.refilter(); layout()
    check("feed-hides-service-events-on-request", panel.feed.stack.arrangedSubviews.count == 61)
    panel.feed.eventsToggle.state = .on; panel.feed.refilter(); layout()
    panel.feed.show(Array(timeline.prefix(4)))
    panel.feed.scrollToBottom(); layout()
    let initial = geometry()
    check("preview-has-reserved-area", panel.debugImage.bounds.width > 250 && panel.debugImage.bounds.height > 140)
    check("single-main-window", panel.pages.count == Section.allCases.count && panel.window.title.hasPrefix("Butler"))
    for size in [NSSize(width: 1280, height: 720), NSSize(width: 300, height: 900), NSSize(width: 4000, height: 1000)] {
        panel.debugImage.image = syntheticImage(size); layout()
        snapshots.append(["image": "\(size)", "geometry": geometry()])
        check("preview-stable-\(Int(size.width))x\(Int(size.height))", geometry() == initial)
    }
    export("butler-dialogue")
    panel.debugImage.image = nil; layout()
    check("preview-stable-on-loss", geometry() == initial)
    export("butler-no-camera")
    for size in [NSSize(width: 1120, height: 730), NSSize(width: 1600, height: 920)] {
        panel.window.setContentSize(size); layout()
        let before = geometry()
        panel.debugImage.image = syntheticImage(NSSize(width: 720, height: 1280)); layout()
        check("preview-stable-resized-\(Int(size.width))", geometry() == before)
        panel.debugImage.image = nil; layout()
        check("preview-loss-resized-\(Int(size.width))", geometry() == before)
    }
    panel.window.setContentSize(NSSize(width: 1280, height: 820))
    let choices = ["codex:gpt-6.1-sol", "claude:claude-opus-4-8", "claude:claude-sonnet-5"]
    let kinds = ["architecture", "code", "review", "research", "documentation", "tests", "curate", "classify", "news", "video"]
    let routes: [[String: Any]] = kinds.map { ["id": "route:" + $0, "title": $0, "model": choices[0], "executable": "/synthetic/codex", "choices": choices, "ready": true, "model_key": "dispatch_route", "cli_key": "dispatch_executable"] }
    panel.receiveComponents([["id": "asr", "title": "Распознавание речи · Whisper", "model": "/synthetic/whisper.bin", "executable": "/synthetic/whisper-cli", "model_key": "whisper_model", "cli_key": "whisper_cli", "ready": true]] + routes)
    panel.select(.models); layout()
    panel.routeTable.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
    panel.routeChoice.selectItem(withTitle: choices[1])
    let id = panel.routeID
    panel.receiveComponents(Array(panel.components.reversed()))
    check("route-selection-survives-reorder", panel.selectedID(panel.routeTable) == id && panel.routeID == "route:code")
    check("unsaved-route-choice-survives-refresh", panel.routeChoice.titleOfSelectedItem == choices[1])
    var refreshed = panel.components
    let edited = refreshed.firstIndex { $0["id"] as? String == "route:code" }!
    refreshed[edited]["choices"] = choices + ["codex:future-ui-model"]
    panel.receiveComponents(refreshed)
    check("new-catalog-choice-appears-without-losing-draft", panel.routeChoice.itemTitles.contains("codex:future-ui-model") && panel.routeChoice.titleOfSelectedItem == choices[1])
    panel.applyRoute()
    let routeUpdate = panel.testCommands.last { $0.first == "ui-components-update" }
    let routePayload = routeUpdate.flatMap { $0.count > 1 ? $0[1].data(using: .utf8) : nil }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    check("route-save-targets-stable-id-and-choice", routePayload == ["id": "route:code", "model": choices[1]])
    refreshed[edited]["choices"] = [choices[0]]
    panel.receiveComponents(refreshed)
    check("removed-draft-is-visible-and-cannot-be-applied", panel.routeChoice.titleOfSelectedItem == choices[1] && !panel.routeApply.isEnabled)
    refreshed[edited]["choices"] = choices
    panel.receiveComponents(refreshed)

    panel.componentTable.selectRowIndexes(IndexSet(integer: panel.components.firstIndex { $0["id"] as? String == "asr" }!), byExtendingSelection: false)
    panel.componentModel.stringValue = "/synthetic/unsaved.bin"
    panel.componentCLI.stringValue = "/synthetic/unsaved-cli"
    panel.receiveComponents(Array(panel.components.reversed()))
    check("unsaved-component-fields-survive-refresh", panel.componentModel.stringValue == "/synthetic/unsaved.bin" && panel.componentCLI.stringValue == "/synthetic/unsaved-cli" && panel.componentID == "asr")
    panel.applyComponent()
    let componentUpdate = panel.testCommands.last { $0.first == "ui-components-update" }
    let componentPayload = componentUpdate.flatMap { $0[1].data(using: .utf8) }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    check("component-save-targets-stable-id", componentPayload?["id"] == "asr" && componentPayload?["model"] == "/synthetic/unsaved.bin")
    panel.jobs = (0..<40).map { ["id": "synthetic-job-\($0)", "name": "Задание \($0)", "kind": "cron", "cron": "0 9 * * *", "schedule": "Ежедневно в 09:00", "model": "synthetic-model", "enabled": true, "status": "ready"] }
    panel.reconcile(panel.jobTable, id: nil)
    panel.select(.jobs); layout()
    panel.jobTable.selectRowIndexes(IndexSet(integer: 12), byExtendingSelection: false)
    panel.jobCron.stringValue = "0 10 * * *"
    panel.jobModel.stringValue = "synthetic-changed"
    panel.jobTable.scrollRowToVisible(20)
    let origin = panel.jobTable.enclosingScrollView!.contentView.bounds.origin
    let jobID = panel.selectedID(panel.jobTable)
    panel.jobs.reverse(); panel.reconcile(panel.jobTable, id: jobID); layout()
    check("job-selection-and-edits-survive-refresh", panel.jobID == "synthetic-job-12" && panel.selectedID(panel.jobTable) == jobID && panel.jobCron.stringValue == "0 10 * * *" && panel.jobModel.stringValue == "synthetic-changed")
    check("table-scroll-survives-refresh", panel.jobTable.enclosingScrollView!.contentView.bounds.origin == origin)
    export("butler-schedules")
    panel.select(.voice); layout()
    let storageWindowSize = panel.window.frame.size
    panel.storageInfo.stringValue = String(repeating: "Длинный путь и состояние буфера /synthetic/", count: 100)
    layout()
    check("storage-details-have-stable-geometry", abs(panel.storageInfo.bounds.height - 196) < 1 && panel.window.frame.size == storageWindowSize)
    check("storage-paths-allow-multiple-lines", panel.storageInfo.maximumNumberOfLines == 0 && panel.storageInfo.lineBreakMode == .byWordWrapping)
    panel.storageInfo.stringValue = "СИНТЕТИЧЕСКИЙ ПРИМЕР\nПосле анализа исходники удаляются.\nБуфер: 12 МиБ из 512 МиБ; ожидают анализа: 3.\nСтарые исходники: 120 МиБ; очищаются после анализа.\nСохранено отдельно: 4 МиБ, файлов: 2.\nИсходники: /synthetic/butler/raw\nРезультаты: /synthetic/butler/ambient.sqlite3\nСохранённое: /synthetic/butler/preserved\nОчистка: работает"
    for section in Section.allCases {
        panel.select(section); layout()
        check("navigation-\(section.rawValue)", panel.pages[section]?.isHidden == false && panel.pages.filter { !$0.value.isHidden }.count == 1 && prefs.string(forKey: "section") == section.rawValue && panel.content.bounds.width > 800)
    }
    panel.localModels = ["synthetic-local-a", "synthetic-local-b"]
    panel.receiveComponents(panel.components + [
        ["id": "agent", "title": "Главный агент Butler", "model": "openai/gpt-6.1-sol", "choices": ["openai/gpt-6.1-sol", "anthropic/claude-sonnet-5"], "ready": true, "model_key": "agent_model", "executable": "/synthetic/openclaw"],
        ["id": "conversation_model", "title": "Локальный диалог", "model": "synthetic-local-a", "ready": true, "model_key": "conversation_model", "executable": "Ollama · локально"],
        ["id": "vision_model", "title": "Локальное зрение", "model": "synthetic-local-a", "ready": true, "model_key": "vision_model", "executable": "Ollama · локально"]])
    for id in ["agent", "conversation_model", "vision_model"] {
        panel.routeTable.selectRowIndexes(IndexSet(integer: panel.routes.firstIndex { $0["id"] as? String == id }!), byExtendingSelection: false)
        let expected = id == "agent" ? "anthropic/claude-sonnet-5" : "synthetic-local-b"
        panel.routeChoice.selectItem(withTitle: expected)
        panel.applyRoute()
        let update = panel.testCommands.last { $0.first == "ui-components-update" }
        let payload = update.flatMap { $0[1].data(using: .utf8) }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
        check("separate-model-target-" + id, payload == ["id": id, "model": expected])
    }
    panel.select(.models); layout(); export("butler-task-models")
    panel.window.appearance = NSAppearance(named: .aqua); layout(); export("butler-task-models-light")
    panel.window.appearance = NSAppearance(named: .darkAqua)

    for section in [Section.voice, .images, .observations, .components, .diagnostics] { panel.select(section); layout(); export("butler-" + section.rawValue) }

    // Conversational display: one synthetic card of each kind; no service, devices or network.
    let imagePath = directory.appendingPathComponent("synthetic-display.png")
    if let png = NSBitmapImageRep(data: syntheticImage(NSSize(width: 960, height: 540)).tiffRepresentation!)?.representation(using: .png, properties: [:]) { try? png.write(to: imagePath) }
    let displayCards: [[String: Any]] = [
        ["id": "card-text", "at": feedStart, "kind": "text", "title": "Задачи на сегодня", "origin": "synthetic",
         "markdown": "# Задачи на сегодня\n\nСписок из синтетических данных:\n\n- Проверить расписание\n- Ответить на **письмо** клиента\n- Посмотреть [сайт проекта](https://familybuilder.io)\n\n> Ссылки открываются во внешнем браузере.",
         "links": [["label": "familybuilder.io", "url": "https://familybuilder.io"]]],
        ["id": "card-image", "at": feedStart + 10, "kind": "image", "title": "Синтетическое изображение", "origin": "synthetic", "image": imagePath.path],
        ["id": "card-diagram", "at": feedStart + 20, "kind": "diagram", "title": "Схема обработки", "origin": "synthetic", "mermaid": "graph TD\n  A[Речь] --> B[Butler]\n  B --> C[Дисплей]"],
        ["id": "card-video", "at": feedStart + 30, "kind": "video", "title": "Ролик", "origin": "synthetic", "video": "/synthetic/missing.mp4", "unavailable": "missing"],
        ["id": "card-email", "at": feedStart + 40, "kind": "email", "title": "Письмо от клиента", "origin": "synthetic", "status": "Черновик подготовлен",
         "email": ["key": "synthetic-1", "sender": "client@example.com", "subject": "Вопрос по проекту", "date": "2026-10-25 09:00", "account": "work",
                   "summary": "Клиент спрашивает о сроках следующего этапа.", "body": "Здравствуйте!\nКогда будет готов следующий этап?", "draft": "Здравствуйте! Следующий этап будет готов к пятнице."],
         "actions": [["id": "open-reply-in-mail", "label": "Открыть ответ в Mail.app"]]]]
    panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic",
                       "display": ["cards": displayCards, "current": "card-text", "raise_serial": 1]]
    panel.refresh(); layout()
    check("display-raise-opens-section", panel.section == .display && panel.displayRenderedID == "card-text" && panel.displayTable.selectedRow == 0)
    check("display-version-shown", panel.tagline.stringValue == "Личный помощник · v0.0.0-synthetic" && panel.window.subtitle.hasSuffix("v0.0.0-synthetic"))
    func textViews(_ view: NSView) -> [NSTextView] { ((view as? NSTextView).map { [$0] } ?? []) + view.subviews.flatMap(textViews) }
    func actionButtons(_ view: NSView) -> [DisplayActionButton] { ((view as? DisplayActionButton).map { [$0] } ?? []) + view.subviews.flatMap(actionButtons) }
    export("butler-display")
    check("display-page-renders", panel.displayDetail.subviews.count == 1 && panel.displayTable.numberOfRows == 5
          && textViews(panel.displayDetail).first?.string.contains("Задачи на сегодня") == true && panel.content.bounds.width > 800)
    check("display-markdown-keeps-link", textViews(panel.displayDetail).first.map { view in
        var found = false
        view.textStorage?.enumerateAttribute(.link, in: NSRange(location: 0, length: view.textStorage?.length ?? 0)) { value, _, _ in if (value as? URL)?.host == "familybuilder.io" { found = true } }
        return found } == true)
    check("display-no-command-for-programmatic-selection", !panel.testCommands.contains { $0.first == "ui-display-select" })
    panel.displayTable.selectRowIndexes(IndexSet(integer: 4), byExtendingSelection: false); layout()
    let selectCommand = panel.testCommands.last { $0.first == "ui-display-select" }
    let selectPayload = selectCommand.flatMap { $0.count > 1 ? $0[1].data(using: .utf8) : nil }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    check("display-user-selection-is-reported", selectPayload == ["id": "card-email"] && panel.displayRenderedID == "card-email" && panel.displayStatusLabel?.stringValue == "Черновик подготовлен")
    if let button = actionButtons(panel.displayDetail).first { panel.runDisplayAction(button) }
    let actionCommand = panel.testCommands.last { $0.first == "ui-display-action" }
    let actionPayload = actionCommand.flatMap { $0.count > 1 ? $0[1].data(using: .utf8) : nil }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    check("display-action-is-sent-and-status-shown", actionPayload == ["id": "card-email", "action": "open-reply-in-mail"] && panel.displayStatusLabel?.stringValue == "синтетически: действие принято")
    layout(); export("butler-display-email")
    let displayWindowSize = panel.window.frame.size
    for rowIndex in [1, 2, 3] { panel.displayTable.selectRowIndexes(IndexSet(integer: rowIndex), byExtendingSelection: false); layout() }
    check("display-every-kind-renders", panel.displayDetail.subviews.count == 1 && panel.displayRenderedID == "card-video" && panel.window.frame.size == displayWindowSize)
    export("butler-display-missing-video")
    let rendered = panel.displayDetail.subviews.first
    panel.refresh(); layout()
    check("display-poll-keeps-rendered-card", panel.displayDetail.subviews.first === rendered && panel.section == .display && panel.displayTable.selectedRow == 3)
    panel.testState = ["enabled": true, "display": ["cards": [], "raise_serial": 1, "hidden": "private"]]
    panel.refresh(); layout()
    check("display-private-hides-cards", panel.displayTable.numberOfRows == 0 && panel.displayRenderedID == nil && panel.displayDetail.subviews.count == 1)
    panel.select(.models)
    panel.saveFrame()
    let restored = Panel(cli: "/usr/bin/false", testing: true, defaults: prefs)
    check("window-and-section-restored", restored.section == .models && restored.window.frame == panel.window.frame)
    restored.window.close()
    let chosen = panel.section
    let frameBeforeDisconnect = panel.window.frame
    panel.voiceState.stringValue = "synthetic transient text"
    panel.testState = nil; panel.refresh(); layout()
    check("disconnect-does-not-navigate-or-resize", panel.section == chosen && panel.window.frame == frameBeforeDisconnect)
    check("disconnect-clears-feed", panel.feed.stack.arrangedSubviews.isEmpty && !panel.feed.placeholder.isHidden)
    check("disconnect-clears-transient-voice-text", !panel.voiceState.stringValue.contains("synthetic transient text"))
    check("disconnect-clears-media", panel.debugImage.image == nil && panel.image.image == nil && panel.artifactImage.image == nil)
    check("test-mode-never-starts-cli", panel.testing && restored.testing && panel.timer == nil && restored.timer == nil)
    panel.window.close()
    NSStatusBar.system.removeStatusItem(panel.item); NSStatusBar.system.removeStatusItem(restored.item)
    let lockPath = directory.appendingPathComponent(".ui-test.lock")
    do {
        let first = try UILock(path: lockPath)
        var duplicateRejected = false
        do { _ = try UILock(path: lockPath) } catch let error as POSIXError { duplicateRejected = error.code == .EWOULDBLOCK } catch {}
        withExtendedLifetime(first) { check("duplicate-ui-instance-rejected", duplicateRejected) }
    } catch { check("duplicate-ui-instance-rejected", false) }
    do { let reopened = try UILock(path: lockPath); withExtendedLifetime(reopened) { check("ui-lock-released-for-reopen", true) } }
    catch { check("ui-lock-released-for-reopen", false) }
    try? FileManager.default.removeItem(at: lockPath)
    let passed = checks.allSatisfy { $0["passed"] as? Bool == true }
    let report: [String: Any] = ["synthetic_only": true, "service_or_devices_used": false, "passed": passed, "checks": checks, "geometry": initial, "snapshots": snapshots]
    if let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) { try? data.write(to: directory.appendingPathComponent("native-panel-layout.json")); FileHandle.standardOutput.write(data); print("") }
    return passed ? 0 : 1
}

ProcessInfo.processInfo.processName = "Butler"
let app = NSApplication.shared
// The service opens this executable directly. Set the bundled portrait explicitly
// as well as registering CFBundleIconFile, so Dock never keeps the generic icon.
if let portraitURL = Bundle.main.url(forResource: "Butler", withExtension: "icns"),
   let portrait = NSImage(contentsOf: portraitURL) {
    app.applicationIconImage = portrait
    app.dockTile.display()
}
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--test-layout" {
    app.setActivationPolicy(.accessory)
    exit(runLayoutTests(output: CommandLine.arguments[2]))
}
guard CommandLine.arguments.count <= 2 else { exit(2) }
let home = FileManager.default.homeDirectoryForCurrentUser
var uiLock: UILock?
do { uiLock = try UILock(path: home.appendingPathComponent(".local/share/butler-ambient/ui.lock")) }
catch let error as POSIXError where error.code == .EWOULDBLOCK {
    DistributedNotificationCenter.default().postNotificationName(.init("com.boris.butler-ui.show"), object: NSUserName(), userInfo: nil, deliverImmediately: true)
    exit(0)
} catch { exit(1) }
app.setActivationPolicy(.regular)
let ownerCLI = CommandLine.arguments.count == 2 ? CommandLine.arguments[1] : home.appendingPathComponent(".openclaw/tools/ambient-awareness/bin/butler-ambient").path
let panel = Panel(cli: ownerCLI)
app.delegate = panel
app.run()

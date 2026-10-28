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

struct FeedbackMemory: Equatable {
    let id: String, text: String
    init(_ value: [String: Any]) { id = value["id"] as? String ?? ""; text = String((value["snippet"] as? String ?? "Фрагмент памяти").prefix(80)) }
}
struct FeedEntry: Equatable {
    let id: String, at: Double, who: String, kind: String, label: String, text: String, image: String?
    let markdown: Bool
    let episode: String?, revision: Int, annotation: String?
    let memories: [FeedbackMemory]
    init(_ value: [String: Any]) {
        id = value["id"] as? String ?? UUID().uuidString
        at = value["at"] as? Double ?? 0
        who = value["who"] as? String ?? "Система"
        kind = value["kind"] as? String ?? "event"
        label = value["label"] as? String ?? ""
        text = value["text"] as? String ?? ""
        image = value["image"] as? String
        markdown = value["markdown"] as? Bool ?? false
        episode = value["episode"] as? String
        revision = value["revision"] as? Int ?? 0
        annotation = value["annotation"] as? String
        memories = (value["memories"] as? [[String: Any]] ?? []).map(FeedbackMemory.init)
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
    var onCorrection: ((FeedEntry, String) -> Void)?
    var markdownRenderer: ((String) -> NSAttributedString)?
    let correctButton = NSButton(title: "Исправить", target: nil, action: nil)
    let stamp = NSTextField(labelWithString: "")
    let body = NSTextField(wrappingLabelWithString: "")
    let picture = IntrinsicFreeImageView()
    private(set) var entry: FeedEntry
    var widthConstrained = false
    private var pictureHeight: NSLayoutConstraint!
    private var stampTrailing: NSLayoutConstraint!
    init(_ entry: FeedEntry, markdownRenderer: ((String) -> NSAttributedString)? = nil) {
        self.entry = entry
        self.markdownRenderer = markdownRenderer      // set before the first update(), which already renders with it
        super.init(frame: .zero)
        wantsLayer = true
        correctButton.target = self
        correctButton.action = #selector(showCorrections)
        correctButton.bezelStyle = .inline
        stamp.font = Theme.font(11, .medium)
        stamp.lineBreakMode = .byTruncatingTail
        stamp.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        body.font = Theme.font(13)
        body.isSelectable = true
        body.allowsEditingTextAttributes = true      // lets a link inside rendered markdown be clicked
        body.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        picture.imageScaling = .scaleProportionallyDown
        picture.imageAlignment = .alignTopLeft
        picture.wantsLayer = true
        picture.layer?.cornerRadius = 6
        picture.layer?.masksToBounds = true
        for child in [stamp, body, picture, correctButton] { addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        pictureHeight = picture.heightAnchor.constraint(equalToConstant: 0)
        stampTrailing = stamp.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -90)
        NSLayoutConstraint.activate([
            correctButton.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
            correctButton.topAnchor.constraint(equalTo: topAnchor, constant: 3), correctButton.widthAnchor.constraint(equalToConstant: 74),
            stamp.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10), stampTrailing,
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
    @objc func showCorrections() {
        let menu = NSMenu()
        for (title, kind) in [("Я не тебе говорил", "not_addressed"), ("Это было тебе", "missed_address"),
                              ("Речь распознана неверно…", "recognition"), ("Я ещё не закончил", "unfinished"),
                              ("Неверно понят запрос", "reasoning"), ("Неверная память", "stale_memory"),
                              ("Неверный результат действия", "execution"), ("Было обращено к Butler", "label_addressed"),
                              ("Говорили другому", "label_other"), ("Отозвать исправление", "retract")] {
            let item = NSMenuItem(title: title, action: #selector(correctSelected(_:)), keyEquivalent: "")
            item.target = self; item.representedObject = kind
            if kind == "stale_memory" && !entry.memories.isEmpty {
                let choices = NSMenu()
                for memory in entry.memories {
                    let choice = NSMenuItem(title: memory.text, action: #selector(correctSelected(_:)), keyEquivalent: "")
                    choice.target = self; choice.representedObject = "stale_memory:" + memory.id; choices.addItem(choice)
                }
                item.submenu = choices
            }
            item.isEnabled = kind != "retract" || entry.annotation != nil
            menu.addItem(item)
        }
        menu.popUp(positioning: nil, at: NSPoint(x: correctButton.frame.minX, y: correctButton.frame.minY), in: self)
    }
    @objc func correctSelected(_ sender: NSMenuItem) {
        if let kind = sender.representedObject as? String { onCorrection?(entry, kind) }
    }
    func update(_ entry: FeedEntry) {
        let changed = self.entry != entry || stamp.stringValue.isEmpty
        self.entry = entry
        correctButton.isHidden = entry.episode == nil || entry.revision < 1
        stampTrailing.constant = correctButton.isHidden ? -10 : -90      // the label gets the room the hidden button leaves
        guard changed else { return }
        let date = Date(timeIntervalSince1970: entry.at)
        let formatter = DateFormatter(); formatter.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm:ss" : "dd.MM HH:mm:ss"
        let compact = entry.kind == "event"
        stamp.stringValue = "\(formatter.string(from: date))  ▌\(entry.who)  ·  \(entry.label)" + (compact && !entry.text.isEmpty ? "  ·  \(entry.text)" : "")
        stamp.textColor = entry.tint
        if entry.markdown, !compact, let render = markdownRenderer {
            body.attributedStringValue = render(entry.text)
        } else {
            body.stringValue = compact ? "" : entry.text
            body.textColor = entry.kind == "heard" ? Theme.bright : entry.kind == "error" ? Theme.error : Theme.text
        }
        body.isHidden = entry.text.isEmpty || compact
        let image = entry.image.flatMap { NSImage(contentsOfFile: $0) }
        picture.image = image
        pictureHeight.constant = image == nil ? 0 : 180
        layer?.backgroundColor = (entry.kind == "notification" ? Theme.notice.withAlphaComponent(0.06) : entry.kind == "heard" ? Theme.owner.withAlphaComponent(0.05) : NSColor.clear).cgColor
    }
}

// The dialogue timeline. Newest at the bottom; follows new entries only while the reader is already there.
final class FeedView: NSView {
    var onCorrection: ((FeedEntry, String) -> Void)?
    var markdownRenderer: ((String) -> NSAttributedString)?
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
            let view = views[entry.id] ?? FeedEntryView(entry, markdownRenderer: markdownRenderer)
            view.onCorrection = onCorrection
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

// The message field of the Display page. Return sends; Shift+Return and Option+Return start a new line.
// A Return that confirms an input-method composition (marked text) is never taken as "send".
final class ComposerTextView: NSTextView {
    var onSend: (() -> Void)?
    var placeholder = "" { didSet { needsDisplay = true } }
    override func keyDown(with event: NSEvent) {
        let isReturn = event.keyCode == 36 || event.keyCode == 76
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if isReturn && !hasMarkedText() {
            if modifiers.isDisjoint(with: [.shift, .option, .control, .command]) { onSend?(); return }
            if modifiers.contains(.shift) || modifiers.contains(.option) { insertNewlineIgnoringFieldEditor(nil); return }
        }
        super.keyDown(with: event)
    }
    override func didChangeText() { super.didChangeText(); needsDisplay = true }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty else { return }
        let origin = NSPoint(x: textContainerInset.width + (textContainer?.lineFragmentPadding ?? 5), y: textContainerInset.height)
        placeholder.draw(at: origin, withAttributes: [.font: Theme.font(13), .foregroundColor: Theme.dim])
    }
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
/// A selectable label that the 1 s refresh may «set» again without touching it: rewriting the text of a field the reader is
/// selecting from replaces what they are selecting. `writes` counts the real changes, so that rule can be tested.
final class StableTextField: NSTextField {
    private(set) var writes = 0
    override var stringValue: String {
        get { super.stringValue }
        set {
            if super.stringValue == newValue { return }
            writes += 1
            super.stringValue = newValue
        }
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
// An action a displayed material declares: a reply to the chat, a link, a file shown in Finder, or the letter's reply draft in Mail.app.
// The panel only ever names it by card and action id; the service holds the address, the path and the reply.
final class DisplayActionButton: NSButton {
    var cardID = ""
    var actionID = ""
    var kind = ""
    var baseTitle = ""
}

/// What one CLI run reports besides its JSON: whether our own timer killed it, and the first bytes of its stderr.
/// Written from the timer and the pipe handler queues, read after the process exits.
final class CommandRun: @unchecked Sendable {
    private let lock = NSLock()
    private var killed = false
    private var stderr = Data()
    var timedOut: Bool { lock.lock(); defer { lock.unlock() }; return killed }
    var errorText: String { lock.lock(); defer { lock.unlock() }; return String(decoding: stderr, as: UTF8.self) }
    func markTimedOut() { lock.lock(); killed = true; lock.unlock() }
    func appendError(_ data: Data) { lock.lock(); if stderr.count < 2048 { stderr.append(data.prefix(2048 - stderr.count)) }; lock.unlock() }
}

enum Section: String, CaseIterable {
    case dialogue, display, recordings, observations, voice, images, models, jobs, components, diagnostics
    /// «Диалог» is retired from the sidebar: its page stays built (the poll still fills it) but nothing navigates to it.
    var isHidden: Bool { self == .dialogue }
    static var visible: [Section] { allCases.filter { !$0.isHidden } }
    var title: String {
        switch self {
        case .dialogue: return "Диалог"
        case .display: return "Дисплей"
        case .recordings: return "Записи"
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
        case .recordings: return "clock.arrow.circlepath"
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
        case .recordings: return "Сессии, выводы, транскрипты и медиа · только локально и только владельцу."
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

// Owner-local recordings: expandable sessions, individual files and in-panel AVKit playback.
final class RecordingsPane: NSView, NSTableViewDataSource, NSTableViewDelegate {
    weak var panel: Panel?
    let day = NSPopUpButton(), source = NSPopUpButton(), files = NSPopUpButton()
    let table = NSTableView(), detail = NSTextView(), picture = NSImageView(), player = AVPlayerView()
    let status = NSTextField(labelWithString: ""), meeting = NSButton(title: "Сохранять текущую встречу", target: nil, action: nil)
    let more = NSButton(title: "Ещё сессии", target: nil, action: nil)
    let play = NSButton(title: "Открыть / воспроизвести", target: nil, action: nil)
    let preserve = NSButton(title: "Сохранить файл", target: nil, action: nil)
    let remove = NSButton(title: "Удалить файл", target: nil, action: nil)
    var entries: [[String: Any]] = [], rows: [[String: Any]] = [], expanded = Set<String>()
    var selected: [String: Any]?, chosenDay: String?, total = 0, serial = 0, navigationSerial = 0
    var loaded = false, permitted = false, lastPoll = Date.distantPast
    var requestedTime: Double?

    init(panel: Panel) {
        self.panel = panel
        super.init(frame: .zero)
        let refresh = NSButton(title: "Обновить", target: self, action: #selector(reload))
        day.target = self; day.action = #selector(changeDay)
        source.addItems(withTitles: ["Все источники", "Камера", "Экран", "Микрофон", "Звук приложений", "Изображения", "Дисплей"])
        source.target = self; source.action = #selector(reload)
        meeting.target = self; meeting.action = #selector(mark)
        more.target = self; more.action = #selector(loadMore)
        play.target = self; play.action = #selector(openFile)
        preserve.target = self; preserve.action = #selector(preserveFile)
        remove.target = self; remove.action = #selector(deleteFile)
        files.target = self; files.action = #selector(changeFile)
        status.font = Theme.font(11); status.textColor = Theme.dim; status.maximumNumberOfLines = 2
        status.lineBreakMode = .byWordWrapping
        let controls = NSStackView(views: [day, source, refresh]); controls.spacing = 8
        let column = NSTableColumn(identifier: .init("recording")); table.addTableColumn(column)
        table.headerView = nil; table.rowHeight = 44; table.dataSource = self; table.delegate = self
        table.target = self; table.doubleAction = #selector(expandSession)
        table.setAccessibilityLabel("Записи по сессиям; двойной щелчок раскрывает сессию")
        let list = NSScrollView(); list.documentView = table; list.hasVerticalScroller = true
        detail.isEditable = false; detail.isSelectable = true; detail.isRichText = false
        detail.font = Theme.font(12); detail.textColor = Theme.text; detail.backgroundColor = Theme.raised
        detail.textContainerInset = NSSize(width: 8, height: 8)
        detail.isVerticallyResizable = true; detail.isHorizontallyResizable = false
        detail.autoresizingMask = [.width]; detail.textContainer?.widthTracksTextView = true
        let textScroll = NSScrollView(); textScroll.documentView = detail; textScroll.hasVerticalScroller = true
        picture.imageScaling = .scaleProportionallyUpOrDown; picture.isHidden = true
        player.controlsStyle = .inline; player.isHidden = true
        let actions = NSStackView(views: [play, preserve, remove]); actions.spacing = 8
        let right = NSStackView(views: [textScroll, files, actions, picture, player]); right.orientation = .vertical
        right.alignment = .leading; right.spacing = 8
        for view in [textScroll, files, picture, player] {
            view.translatesAutoresizingMaskIntoConstraints = false
            view.widthAnchor.constraint(equalTo: right.widthAnchor).isActive = true
        }
        textScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 190).isActive = true
        picture.heightAnchor.constraint(equalToConstant: 190).isActive = true
        player.heightAnchor.constraint(equalToConstant: 190).isActive = true
        let body = NSStackView(views: [list, right]); body.orientation = .horizontal; body.spacing = 16; body.distribution = .fill
        list.widthAnchor.constraint(equalTo: body.widthAnchor, multiplier: 0.44).isActive = true
        list.heightAnchor.constraint(equalTo: body.heightAnchor).isActive = true
        right.heightAnchor.constraint(equalTo: body.heightAnchor).isActive = true
        let footer = NSStackView(views: [more, meeting]); footer.spacing = 12
        let stack = NSStackView(views: [controls, body, footer, status]); stack.orientation = .vertical; stack.spacing = 10; stack.alignment = .leading
        addSubview(stack); stack.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: leadingAnchor), stack.trailingAnchor.constraint(equalTo: trailingAnchor),
            stack.topAnchor.constraint(equalTo: topAnchor), stack.bottomAnchor.constraint(equalTo: bottomAnchor),
            body.widthAnchor.constraint(equalTo: stack.widthAnchor), status.widthAnchor.constraint(equalTo: stack.widthAnchor),
            status.heightAnchor.constraint(equalToConstant: 38)])
        clear()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func clear() {
        serial += 1; entries = []; rows = []; selected = nil; expanded = []; loaded = false
        table.reloadData(); detail.string = "Записи доступны только подтверждённому владельцу при разрешённой записи."
        player.player?.pause(); player.player = nil; player.isHidden = true; picture.image = nil; picture.isHidden = true
        files.removeAllItems(); play.isEnabled = false; preserve.isEnabled = false; remove.isEnabled = false; meeting.isEnabled = false; more.isHidden = true
        day.removeAllItems()
    }
    func updatePermission(_ value: [String: Any]?) {
        permitted = value?["recordings_allowed"] as? Bool == true
        if !permitted { clear(); return }
        if let navigation = value?["recordings_navigation"] as? [String: Any], let next = navigation["serial"] as? Int, next != navigationSerial {
            navigationSerial = next; chosenDay = navigation["day"] as? String; requestedTime = navigation["at"] as? Double
            panel?.select(.recordings); panel?.show(); load(reset: true)
        } else if panel?.section == .recordings && (!loaded || (Date().timeIntervalSince(lastPoll) > 15 && player.player?.timeControlStatus != .playing)) { load(reset: true) }
    }
    func request(offset: Int = 0) -> [String: Any] {
        var args: [String: Any] = ["empty": true, "limit": 100, "offset": offset]
        if let chosenDay { args["day"] = chosenDay }
        let values = ["all", "camera", "screen", "microphone", "system-audio", "generated", "display"]
        if values.indices.contains(source.indexOfSelectedItem) { args["source"] = values[source.indexOfSelectedItem] }
        return args
    }
    @objc func reload() { expanded = []; load(reset: true) }
    @objc func changeDay() { chosenDay = day.selectedItem?.representedObject as? String; reload() }
    @objc func loadMore() { load(reset: false) }
    func load(reset: Bool) {
        guard permitted, let panel, let json = panel.jsonArgument(request(offset: reset ? 0 : entries.count)) else { return }
        serial += 1; let token = serial; lastPoll = Date(); status.stringValue = "Загружаю записи…"
        panel.command(["ui-recordings", json]) { [weak self] value in
            guard let self, token == self.serial else { return }
            guard let value, value["hidden"] as? Bool != true, self.permitted else { self.clear(); self.status.stringValue = "Записи скрыты или сервис недоступен."; return }
            self.loaded = true; self.chosenDay = value["day"] as? String
            self.day.removeAllItems()
            for entry in value["days"] as? [[String: Any]] ?? [] {
                let title = entry["day"] as? String ?? ""
                self.day.addItem(withTitle: title); self.day.lastItem?.representedObject = title
                if title == self.chosenDay { self.day.select(self.day.lastItem) }
            }
            let page = value["items"] as? [[String: Any]] ?? []
            self.entries = reset ? page : self.entries + page; self.total = value["total"] as? Int ?? self.entries.count
            self.more.isHidden = self.entries.count >= self.total
            let state = value["meeting"] as? [String: Any] ?? [:]
            self.meeting.isEnabled = state["detected"] as? Bool == true && state["marked"] as? Bool != true
            self.meeting.title = state["marked"] as? Bool == true ? "Встреча сохраняется 30 дней" : "Сохранять текущую встречу"
            self.status.stringValue = "Сессий и событий: \(self.entries.count) из \(self.total) · транскрипты и отмеченные встречи: 30 дней."
            if let at = self.requestedTime {
                if let session = self.entries.first(where: { ($0["start"] as? Double ?? 0) <= at && ($0["end"] as? Double ?? 0) >= at }), let id = session["id"] as? String {
                    self.expanded.insert(id)
                } else if self.entries.count < self.total { self.load(reset: false); return }
            }
            self.rebuild()
            for entry in self.entries where self.expanded.contains(entry["id"] as? String ?? "") {
                if (entry["observations"] as? [[String: Any]] ?? []).isEmpty, let id = entry["id"] as? String { self.fetchChildren(id, offset: 0) }
            }
            if let at = self.requestedTime {
                let index = self.rows.indices.filter { self.rows[$0]["type"] as? String != "gap" }.min {
                    abs((self.rows[$0]["start"] as? Double ?? 0)-at) < abs((self.rows[$1]["start"] as? Double ?? 0)-at)
                }
                if let index { self.table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false); self.table.scrollRowToVisible(index) }
                self.requestedTime = nil
            }
        }
    }
    func rebuild() {
        let id = selected?["id"] as? String
        rows = []
        for entry in entries {
            rows.append(entry)
            guard let key = entry["id"] as? String, expanded.contains(key), entry["type"] as? String == "session" else { continue }
            let children = entry["observations"] as? [[String: Any]] ?? []; rows += children
            if children.count < (entry["count"] as? Int ?? 0) { rows.append(["type": "more", "id": key + ":more", "session": key, "offset": children.count]) }
        }
        table.reloadData()
        if let id, let row = rows.firstIndex(where: { $0["id"] as? String == id }) { table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false); showDetail(row) }
        else { showDetail(-1) }
    }
    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { rows.indices.contains(row) && rows[row]["type"] as? String != "gap" }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard rows.indices.contains(row), let panel else { return nil }
        let item = rows[row], kind = item["type"] as? String ?? ""
        let range = "\(panel.clock(item["start"]))–\(panel.clock(item["end"]))"
        let label = NSTextField(wrappingLabelWithString: "")
        label.font = Theme.font(11); label.maximumNumberOfLines = 2; label.lineBreakMode = .byTruncatingTail
        switch kind {
        case "session":
            let open = expanded.contains(item["id"] as? String ?? "")
            let activity = item["activity"] as? String ?? ""
            label.stringValue = "\(open ? "▾" : "▸") \(range) · \(Panel.observationSources[item["source"] as? String ?? ""] ?? "") · \(item["count"] ?? 0)\n\(activity.hasPrefix("call:") ? "Встреча" : activity) · \((item["content"] as? [String] ?? []).joined(separator: ", "))"
        case "gap": label.stringValue = "\(range) · ⚠ \(item["label"] as? String ?? (Panel.gapReasons[item["reason"] as? String ?? ""]?.short ?? "пропуск"))"; label.textColor = Theme.warn
        case "more": label.stringValue = "    Ещё наблюдения… (двойной щелчок)"
        case "observation": label.stringValue = "    " + panel.observationRowTitle(item)
        default: label.stringValue = "\(range) · \(item["title"] as? String ?? "Материал")"
        }
        return label
    }
    func tableViewSelectionDidChange(_ notification: Notification) { showDetail(table.selectedRow) }
    @objc func expandSession() {
        let row = table.clickedRow >= 0 ? table.clickedRow : table.selectedRow
        guard rows.indices.contains(row) else { return }
        let entry = rows[row]
        if entry["type"] as? String == "more", let id = entry["session"] as? String {
            fetchChildren(id, offset: entry["offset"] as? Int ?? 0)
        } else if entry["type"] as? String == "session", let id = entry["id"] as? String {
            if expanded.contains(id) { expanded.remove(id) } else {
                expanded.insert(id)
                if (entry["observations"] as? [[String: Any]] ?? []).isEmpty { fetchChildren(id, offset: 0) }
            }
            rebuild()
        }
    }
    func fetchChildren(_ id: String, offset: Int) {
        guard let panel else { return }
        var args = request(offset: offset); args["session"] = id
        guard let json = panel.jsonArgument(args) else { return }
        let token = serial
        panel.command(["ui-recordings", json]) { [weak self] value in
            guard let self, self.permitted, token == self.serial, let value, value["hidden"] as? Bool != true,
                  let index = self.entries.firstIndex(where: { $0["id"] as? String == id }) else { return }
            let prior = offset == 0 ? [] : (self.entries[index]["observations"] as? [[String: Any]] ?? [])
            self.entries[index]["observations"] = prior + (value["items"] as? [[String: Any]] ?? [])
            self.rebuild()
        }
    }
    func showDetail(_ row: Int) {
        player.player?.pause(); player.player = nil; player.isHidden = true; picture.image = nil; picture.isHidden = true
        files.removeAllItems(); selected = rows.indices.contains(row) ? rows[row] : nil
        play.isEnabled = false; preserve.isEnabled = false; remove.isEnabled = false
        guard let item = selected, let panel else { detail.string = "Выберите сессию; двойной щелчок раскрывает её наблюдения."; return }
        if item["type"] as? String == "observation" {
            let facts = item["facts"] as? [[String: Any]] ?? []
            var text = panel.observationDetail(item).title + "\n\n"
            text += facts.map { fact in
                ["kind", "detail", "topic", "activity", "person", "identity", "confidence", "basis", "identity_basis"].compactMap { key in
                    fact[key].map { value in
                        let labels = ["kind": "Вид", "detail": "Описание", "topic": "Тема", "activity": "Занятие", "person": "Человек", "identity": "Имя", "confidence": "Уверенность", "basis": "Основание", "identity_basis": "Идентификация"]
                        let shown: String
                        if key == "confidence", let number = value as? Double { shown = "\(Int((number*100).rounded())) %" }
                        else if key == "kind", let text = value as? String { shown = Panel.factKinds[text] ?? text }
                        else if value as? String == "model-inference" { shown = "вывод модели" }
                        else if value as? String == "owner-confirmed" { shown = "подтверждено владельцем" }
                        else { shown = "\(value)" }
                        return "\(labels[key] ?? key): \(shown)"
                    }
                }.joined(separator: " · ")
            }.joined(separator: "\n\n")
            let transcripts = item["transcripts"] as? [[String: Any]] ?? []
            text += "\n\nТранскрипт:\n" + (transcripts.isEmpty ? "Не сохранён или истёк срок хранения." : transcripts.compactMap { $0["text"] as? String }.joined(separator: "\n"))
            let media = item["media"] as? [[String: Any]] ?? []
            for file in media {
                files.addItem(withTitle: "\(file["source"] as? String ?? "Файл") · \(panel.clock(file["start"]))" + (file["path"] as? String == nil ? " · недоступен" : "") + (file["preserved"] as? Bool == true ? " · сохранён" : ""))
                files.lastItem?.representedObject = file
            }
            if media.isEmpty || !media.contains(where: { $0["path"] as? String != nil }) { text += "\n\nОригинал недоступен: удалён после анализа или по сроку." }
            detail.string = text
        } else if item["type"] as? String == "session" {
            detail.string = "\(panel.clock(item["start"]))–\(panel.clock(item["end"])) · \(item["count"] ?? 0) наблюдений\nДвойной щелчок раскрывает наблюдения с фактами, текстом и отдельными файлами."
        } else {
            let card = item["card"] as? [String: Any] ?? [:]
            detail.string = (item["title"] as? String ?? "Материал") + "\n\n" + (card["markdown"] as? String ?? card["mermaid"] as? String ?? "")
            if let image = item["image"] as? String { picture.image = NSImage(contentsOfFile: image); picture.isHidden = picture.image == nil }
            play.isEnabled = item["type"] as? String == "display"
            remove.isEnabled = item["artifact_id"] as? String != nil
        }
        if files.numberOfItems > 0 { files.selectItem(at: 0); changeFile() }
    }
    @objc func changeFile() {
        player.player?.pause(); player.player = nil; player.isHidden = true; picture.image = nil; picture.isHidden = true
        let file = files.selectedItem?.representedObject as? [String: Any] ?? [:]
        let available = file["path"] as? String != nil && file["complete"] as? Bool == true
        play.isEnabled = available; preserve.isEnabled = available && file["preserved"] as? Bool != true; remove.isEnabled = available
        if let path = file["path"] as? String, ["jpg", "jpeg", "png", "webp"].contains(URL(fileURLWithPath: path).pathExtension.lowercased()) {
            picture.image = NSImage(contentsOfFile: path); picture.isHidden = picture.image == nil
        }
    }
    @objc func mark() {
        panel?.command(["ui-recording-meeting", "{\"action\":\"mark\"}"]) { [weak self] value in
            self?.load(reset: true); self?.status.stringValue = value?["message"] as? String ?? "Не удалось отметить встречу."
        }
    }
    @objc func preserveFile() { control("preserve") }
    @objc func deleteFile() { control("delete") }
    func control(_ action: String) {
        guard permitted, let panel, let selected else { return }
        if let artifact = selected["artifact_id"] as? String, action == "delete" {
            panel.command(["ui-visual-delete", artifact]) { [weak self] _ in self?.load(reset: true) }; return
        }
        guard let file = files.selectedItem?.representedObject as? [String: Any], let mid = file["id"] as? String,
              let oid = selected["id"] as? String, let json = panel.jsonArgument(["action": action, "observation": oid, "media": mid]) else { return }
        panel.command(["ui-recording-control", json]) { [weak self] value in
            self?.load(reset: true); self?.status.stringValue = value?["message"] as? String ?? "Операция недоступна."
        }
    }
    @objc func openFile() {
        guard permitted, let panel, let selected else { return }
        if let card = selected["card"] as? [String: Any], let id = card["id"] as? String,
           let json = panel.jsonArgument(["id": id]) {
            panel.command(["ui-display-select", json]) { _ in panel.select(.display); panel.refresh() }; return
        }
        guard let file = files.selectedItem?.representedObject as? [String: Any], let mid = file["id"] as? String,
              let oid = selected["id"] as? String, let json = panel.jsonArgument(["observation": oid, "media": mid]) else { return }
        let token = serial
        panel.command(["ui-recording-play", json]) { [weak self] value in
            guard let self, self.permitted, token == self.serial, self.selected?["id"] as? String == oid, (self.files.selectedItem?.representedObject as? [String: Any])?["id"] as? String == mid, let path = value?["path"] as? String else { return }
            if value?["image"] as? Bool == true { self.picture.image = NSImage(contentsOfFile: path); self.picture.isHidden = self.picture.image == nil }
            else { self.player.player = AVPlayer(url: URL(fileURLWithPath: path)); self.player.isHidden = false; self.player.player?.play() }
        }
    }
}

final class Panel: NSObject, NSMenuDelegate, NSWindowDelegate, NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate, NSTextViewDelegate, WKNavigationDelegate {
    let cli: String
    let testing: Bool
    let startHidden: Bool
    let defaults: UserDefaults
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
    let navigation = NSTableView()
    let content = NSView()
    var pages: [Section: NSView] = [:]
    var section = Section.display
    let pageTitle = NSTextField(labelWithString: "")
    let pageDetail = NSTextField(labelWithString: "")
    let heading = NSTextField(labelWithString: "Подключаюсь к Butler…")
    let sources = StableTextField(wrappingLabelWithString: "")
    let history = StableTextField(wrappingLabelWithString: "")
    let message = StableTextField(wrappingLabelWithString: "")
    let image = PreviewView()
    let artifactImage = PreviewView()
    let artifactCaption = NSTextField(wrappingLabelWithString: "Выберите результат для просмотра.")
    let pause = NSButton(title: "Пауза", target: nil, action: nil)
    let voices = NSPopUpButton()
    let voiceState = NSTextField(wrappingLabelWithString: "Голос ещё не выбран. Прослушайте варианты и выберите подходящий.")
    let voiceToggle = NSButton(title: "Выключить голос", target: nil, action: nil)
    let faceWake = NSButton(checkboxWithTitle: "Постоянный разговор при подтверждённом владельце", target: nil, action: nil)
    let feed = FeedView()
    // The written conversation on the Display page: the thread, the message field and its buttons.
    let chatFeed = FeedView()
    let composer = ComposerTextView()
    let sendButton = NSButton(title: "Отправить", target: nil, action: nil)
    let dictateButton = NSButton(title: "Диктовать", target: nil, action: nil)
    let clearChatButton = NSButton(title: "Очистить", target: nil, action: nil)
    let chatStatus = NSTextField(wrappingLabelWithString: "")
    var chatSending = false
    var dictationActive = false
    // Наблюдения: «Сейчас | История»
    let observationsMode = NSSegmentedControl(labels: ["Сейчас", "История"], trackingMode: .selectOne, target: nil, action: nil)
    var recordingsPane: RecordingsPane!
    var nowPane: NSView!
    var historyPane: NSView!
    let nowBanner = NSTextField(wrappingLabelWithString: "")
    let obsBanner = NSTextField(wrappingLabelWithString: "")
    let obsPrev = NSButton(title: "◀", target: nil, action: nil)
    let obsNext = NSButton(title: "▶", target: nil, action: nil)
    let obsDay = NSPopUpButton(frame: .zero, pullsDown: false)
    let obsSource = NSPopUpButton(frame: .zero, pullsDown: false)
    let obsEmpty = NSButton(checkboxWithTitle: "Показать пустые и пропуски", target: nil, action: nil)
    let obsRefresh = NSButton(title: "Обновить", target: nil, action: nil)
    let obsMore = NSButton(title: "Показать ещё", target: nil, action: nil)
    let obsSummary = NSTextField(wrappingLabelWithString: "")
    let obsStatus = NSTextField(labelWithString: "")
    let obsDetailTitle = NSTextField(wrappingLabelWithString: "Выберите запись слева")
    let obsDetailBody = NSTextField(wrappingLabelWithString: "")
    let obsFrame = IntrinsicFreeImageView()
    let observationTable = NSTableView()
    var observationItems: [[String: Any]] = []
    var observationDays: [[String: Any]] = []
    var observationDay: String?
    var observationTotal = 0
    var observationSerial = 0
    var observationSelectedID: String?
    var observationsLoaded = false
    var dictationConsumed: String?      // the dictation whose text already went into the field
    let latencySummary = NSTextField(wrappingLabelWithString: "Модели прогреваются · измерение ответа появится после реплики.")
    let commandReview = StableTextField(wrappingLabelWithString: "Изменения Butler применяются после вашего голосового подтверждения.")
    let meetingStatus = NSTextField(wrappingLabelWithString: "Проверяю режим встречи…")
    let availability = NSSegmentedControl(labels: ["Авто", "Занят", "Свободен"], trackingMode: .selectOne, target: nil, action: nil)
    let availabilityStatus = NSTextField(wrappingLabelWithString: "Занятость определяется автоматически.")
    let meetingChoice = NSPopUpButton()
    let workStatus = StableTextField(wrappingLabelWithString: "Фоновые поручения появятся здесь. Проект, модель и задача требуют вашего подтверждения.")
    let feedbackStatus = NSTextField(wrappingLabelWithString: "Исправления: данных пока нет.")
    static let archiveTitle = "Постоянный анализ разговоров, видео и звука приложений"
    let archiveToggle = NSButton(checkboxWithTitle: Panel.archiveTitle, target: nil, action: nil)
    let storageInfo = StableTextField(wrappingLabelWithString: "Определяю объём временного буфера…")
    let ownerSetupStatus = NSTextField(wrappingLabelWithString: "Эталоны голоса хранятся на этом Mac. Восстановление владельца — через локальное подтверждение macOS.")
    let guestName = NSTextField()
    let debugStatus = StableTextField(wrappingLabelWithString: "Подключаюсь…")
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
    let revealButton = NSButton(title: "Показать в Finder", target: nil, action: nil)
    let componentSave = NSButton(title: "Сохранить компонент", target: nil, action: nil)
    let ownerSetupCancel = NSButton(title: "Отменить регистрацию", target: nil, action: nil)
    let debugStamp = NSTextField(labelWithString: "")
    var pauseMenuItem: NSMenuItem?
    var setupNoticeText = ""
    var setupNoticeUntil = Date.distantPast
    var setupNoticeStage = "idle"
    var testNow: Date?
    var testReplies: [String: [String: Any]] = [:]
    var testNoReply: Set<String> = []
    /// Seconds a command may run before the panel gives up on it. «Остановить» задание chains up to three CLI calls of 12 s
    /// in the service; 15 s would report a failure that is still succeeding.
    static func commandTimeout(_ name: String) -> Double { ["ui-state": 8, "ui-schedule-control": 60, "ui-recording-play": 75][name] ?? 15 }
    var testRevealed: String?
    var roots: [String] = []
    var artifacts: [[String: Any]] = []
    var lastVisualRequest = ""
    var state: [String: Any] = [:]
    var busy = false
    /// Consecutive failed `ui-state` polls and when the service last answered. One slow or dropped poll must not
    /// wipe the screen: the banner appears only after `disconnectAfterFailures` failures in a row.
    var failureStreak = 0
    var lastGoodState: Date?
    let disconnectAfterFailures = 3
    var timer: Timer?
    var profileIDs: [String] = []
    var cameraPreview = true
    var testCommands: [[String]] = []
    var testState: [String: Any]?
    var testSendResult: [String: Any]? = ["queued": true, "id": "synthetic"]
    var testDisplayAction: [String: Any]? = ["status": "синтетически: действие принято"]
    var testObservations: [String: Any]?
    var testSupersedeObservation = false
    var testDictation: [String: Any]? = ["state": "listening", "id": "synthetic-1", "text": "", "partial": ""]
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
    var displayActionButtons: [DisplayActionButton] = []
    var displayRenderedRev = 0
    var diagramCardID: String?
    var svgWeb: WKWebView?
    var svgLoads = 0
    var diagramWeb: WKWebView?
    var diagramReady = false
    var pendingDiagram: String?
    var videoView: AVPlayerView?

    static func preferenceStore() -> UserDefaults {
        // A bundled application's own preferences are the standard domain.
        // macOS can reject adding that same domain as a suite (returning nil).
        .standard
    }
    init(cli: String, testing: Bool = false, defaults: UserDefaults = Panel.preferenceStore(), startHidden: Bool = false) {
        self.startHidden = startHidden
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
        recordingsPane = RecordingsPane(panel: self)
        pages[.recordings] = recordingsPane
        pages[.observations] = observationsView()
        pages[.voice] = voiceView()
        pages[.images] = imagesView()
        pages[.models] = modelsView()
        pages[.jobs] = managementView(isAI: false)
        pages[.components] = managementView(isAI: true)
        let diagnostics = vertical()
        debugStatus.font = Theme.font(12, .regular)
        debugStatus.isSelectable = true
        debugStamp.font = Theme.font(11, .regular)
        debugStamp.textColor = Theme.dim
        fill(diagnostics, debugStamp)
        fill(diagnostics, debugStatus)
        pages[.diagnostics] = scrolling(diagnostics)
        for page in pages.values {
            content.addSubview(page)
            pin(page, to: content)
            page.isHidden = true
        }
        if let root = window.contentView { Theme.apply(root) }
        let stored = defaults.string(forKey: "section").flatMap(Section.init(rawValue:)) ?? .display
        select(stored)                                  // a stored «dialogue» from an older version opens the Display
        if let frame = defaults.string(forKey: "windowFrame"), NSRectFromString(frame).width >= window.minSize.width,
           let clamped = testing ? NSRectFromString(frame) : Panel.clampedToScreen(NSRectFromString(frame)) {
            window.setFrame(clamped, display: false)
        } else { window.center() }
        restoringFrame = false
        let menu = NSMenu()
        for (title, action) in [("Открыть Butler", #selector(show)), ("Модели по задачам…", #selector(showModels)), ("Настройки…", #selector(showDebug)), ("Диагностика…", #selector(showDiagnostics)), ("Пауза / продолжить", #selector(toggle))] {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
            entry.target = self
            menu.addItem(entry)
            if action == #selector(toggle) { pauseMenuItem = entry }
        }
        menu.delegate = self
        item.menu = menu
        item.button?.title = "◉ Butler"
        installAppMenu()
        if !testing {
            DistributedNotificationCenter.default().addObserver(self, selector: #selector(showFromNotification(_:)), name: .init("com.boris.butler-ui.show"), object: NSUserName())
            timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refresh() }
            refresh()
            loadSettings()
            loadManagement()
            if !startHidden {
                if defaults.object(forKey: "windowVisible") == nil { show() }
                else if defaults.bool(forKey: "windowVisible") { window.orderFront(nil) }
            }
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
    /// Cameras, microphones, meters and the readiness lamps: the controls that used to live on the retired «Диалог» page.
    func devicesColumn() -> NSView {
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
        return card(scrolling(device))
    }
    /// The retired «Диалог» page: only the read-only dialogue status and the timeline stay here; nothing navigates to it.
    func dialogueView() -> NSView {
        let conversation = vertical()
        fill(conversation, label("Лента: кто и когда сказал, ответы, снимки и уведомления в Telegram. Новое внизу. История очищается при потере разрешения."))
        fill(conversation, latencySummary)
        fixedLabel(latencySummary, height: 42)
        latencySummary.font = Theme.font(11, .regular)
        latencySummary.textColor = Theme.dim
        fill(conversation, commandReview)
        fixedLabel(commandReview, height: 64)
        commandReview.font = Theme.font(12)
        commandReview.isSelectable = true
        fill(conversation, workStatus)
        fixedLabel(workStatus, height: 160)
        workStatus.isSelectable = true
        fill(conversation, feedbackStatus)
        fixedLabel(feedbackStatus, height: 36)
        feed.onCorrection = { [weak self] entry, kind in self?.correctEpisode(entry, kind: kind) }
        fill(conversation, row([feed.eventsToggle]))
        fill(conversation, feed)
        feed.heightAnchor.constraint(greaterThanOrEqualToConstant: 260).isActive = true
        return card(conversation)
    }
    func observationsView() -> NSView {
        let stack = vertical()
        nowBanner.textColor = Theme.warn
        nowBanner.font = Theme.font(12, .medium)
        nowBanner.isHidden = true
        fill(stack, nowBanner)
        fill(stack, sources)
        stack.addArrangedSubview(row([NSButton(title: "Камера", target: self, action: #selector(camera)), NSButton(title: "Экран", target: self, action: #selector(screen))]))
        fill(stack, image)
        image.heightAnchor.constraint(equalToConstant: 310).isActive = true
        fill(stack, message)
        fill(stack, title("Последние наблюдения"))
        fill(stack, label("Выводы модели могут содержать ошибки. Здесь только наблюдения с выводом; всё остальное, по дням и с пропусками, — во вкладке «История»."))
        fill(stack, history)
        for field in [sources, history, message] { field.isSelectable = true }
        nowPane = scrolling(stack)
        historyPane = observationsHistoryView()
        observationsMode.target = self; observationsMode.action = #selector(changeObservationsMode)
        observationsMode.selectedSegment = defaults.string(forKey: "observationsMode") == "history" ? 1 : 0
        observationsMode.setAccessibilityLabel("Наблюдения: сейчас или история")
        let container = NSView()
        for child in [observationsMode, nowPane!, historyPane!] { container.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([
            observationsMode.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 16), observationsMode.topAnchor.constraint(equalTo: container.topAnchor, constant: 14),
            observationsMode.widthAnchor.constraint(equalToConstant: 240),
            nowPane.leadingAnchor.constraint(equalTo: container.leadingAnchor), nowPane.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            nowPane.topAnchor.constraint(equalTo: observationsMode.bottomAnchor, constant: 6), nowPane.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            historyPane.leadingAnchor.constraint(equalTo: container.leadingAnchor), historyPane.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            historyPane.topAnchor.constraint(equalTo: observationsMode.bottomAnchor, constant: 6), historyPane.bottomAnchor.constraint(equalTo: container.bottomAnchor)])
        showObservationsMode()
        return card(container)
    }
    func observationsHistoryView() -> NSView {
        for (button, action) in [(obsPrev, #selector(olderObservationDay)), (obsNext, #selector(newerObservationDay)), (obsRefresh, #selector(reloadObservations)), (obsMore, #selector(moreObservations))] {
            button.target = self; button.action = action
        }
        obsPrev.toolTip = "Более ранний день"; obsNext.toolTip = "Более поздний день"
        obsPrev.setAccessibilityLabel("Более ранний день"); obsNext.setAccessibilityLabel("Более поздний день")
        obsDay.target = self; obsDay.action = #selector(changeObservationDay)
        obsDay.addItem(withTitle: "Загружаю…")
        obsDay.setAccessibilityLabel("День наблюдений")
        for (title, key) in [("Все источники", ""), ("Камера", "camera"), ("Экран", "screen"), ("Микрофон", "microphone"), ("Звук приложений", "system-audio")] {
            obsSource.addItem(withTitle: title); obsSource.lastItem?.representedObject = key
        }
        obsSource.target = self; obsSource.action = #selector(reloadObservations)
        obsSource.setAccessibilityLabel("Источник")
        obsEmpty.target = self; obsEmpty.action = #selector(reloadObservations)
        obsEmpty.toolTip = "Наблюдения без вывода и интервалы, когда запись или анализ не работали"
        let spacer = NSView(); spacer.setContentHuggingPriority(.init(1), for: .horizontal)
        let toolbar = row([obsPrev, obsDay, obsNext, obsSource, obsEmpty, spacer, obsRefresh])
        obsSummary.textColor = Theme.dim
        obsSummary.font = Theme.font(12)
        obsSummary.isSelectable = true
        obsBanner.textColor = Theme.warn
        obsBanner.font = Theme.font(12, .medium)
        obsBanner.isHidden = true
        let scroll = tableScroll(observationTable, columns: [("title", "Время · источник · вывод", 520)], minHeight: 200)
        scroll.hasHorizontalScroller = false
        observationTable.headerView = nil
        observationTable.rowHeight = 28
        observationTable.setAccessibilityLabel("История наблюдений")
        observationTable.allowsEmptySelection = true
        obsDetailTitle.font = Theme.font(14, .semibold)
        obsDetailBody.font = Theme.font(12)
        obsDetailBody.isSelectable = true
        obsFrame.imageScaling = .scaleProportionallyDown
        obsFrame.imageAlignment = .alignTopLeft
        obsFrame.isHidden = true
        obsFrame.heightAnchor.constraint(equalToConstant: 220).isActive = true
        let detail = vertical(10)
        fill(detail, obsDetailTitle)
        fill(detail, obsDetailBody)
        fill(detail, obsFrame)
        let detailCard = card(scrolling(detail))
        obsStatus.font = Theme.font(11)
        obsStatus.textColor = Theme.dim
        obsStatus.lineBreakMode = .byTruncatingTail
        obsMore.isHidden = true
        let footer = row([obsMore, obsStatus])
        let view = NSView()
        for child in [toolbar, obsSummary, obsBanner, scroll, detailCard, footer] { view.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([
            toolbar.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16), toolbar.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
            toolbar.topAnchor.constraint(equalTo: view.topAnchor, constant: 6),
            obsSummary.leadingAnchor.constraint(equalTo: toolbar.leadingAnchor), obsSummary.trailingAnchor.constraint(equalTo: toolbar.trailingAnchor),
            obsSummary.topAnchor.constraint(equalTo: toolbar.bottomAnchor, constant: 8),
            obsBanner.leadingAnchor.constraint(equalTo: toolbar.leadingAnchor), obsBanner.trailingAnchor.constraint(equalTo: toolbar.trailingAnchor),
            obsBanner.topAnchor.constraint(equalTo: obsSummary.bottomAnchor, constant: 4),
            scroll.leadingAnchor.constraint(equalTo: toolbar.leadingAnchor), scroll.widthAnchor.constraint(equalToConstant: 520),
            scroll.topAnchor.constraint(equalTo: obsBanner.bottomAnchor, constant: 8), scroll.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -8),
            detailCard.leadingAnchor.constraint(equalTo: scroll.trailingAnchor, constant: 14), detailCard.trailingAnchor.constraint(equalTo: toolbar.trailingAnchor),
            detailCard.topAnchor.constraint(equalTo: scroll.topAnchor), detailCard.bottomAnchor.constraint(equalTo: scroll.bottomAnchor),
            footer.leadingAnchor.constraint(equalTo: toolbar.leadingAnchor), footer.trailingAnchor.constraint(equalTo: toolbar.trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -12)])
        return view
    }
    func voiceView() -> NSView {
        let stack = vertical(16)
        heading.font = Theme.font(15, .semibold)
        fixedLabel(heading, height: 22)
        pause.target = self; pause.action = #selector(toggle)
        stack.addArrangedSubview(row([heading, pause]))
        fill(stack, separator())
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
        fill(stack, meetingStatus)
        fixedLabel(meetingStatus, height: 44)
        fill(stack, title("Занятость"))
        availability.target = self; availability.action = #selector(changeAvailability)
        availability.selectedSegment = 0
        stack.addArrangedSubview(availability)
        fill(stack, availabilityStatus)
        fixedLabel(availabilityStatus, height: 36)
        fill(stack, separator())
        fill(stack, title("Язык распознавания"))
        languageChoice.addItems(withTitles: ["Русский", "Автоматически", "English", "Deutsch", "Українська"])
        stack.addArrangedSubview(row([languageChoice, NSButton(title: "Применить язык", target: self, action: #selector(applyLanguage))]))
        faceWake.target = self; faceWake.action = #selector(toggleFaceWake)
        stack.addArrangedSubview(faceWake)
        fill(stack, label("В обычном разговоре имя не обязательно. Владелец подтверждается лицом или зарегистрированным голосом; разрешение на запись проверяется отдельно."))
        stack.addArrangedSubview(NSButton(title: "Проверить на синтетическом образце", target: self, action: #selector(checkVoice)))
        ownerSetupCancel.target = self; ownerSetupCancel.action = #selector(cancelSetup); ownerSetupCancel.isEnabled = false
        stack.addArrangedSubview(row([NSButton(title: "Подтвердить или обновить мой голос…", target: self, action: #selector(ownerSetup)), ownerSetupCancel]))
        guestName.placeholderString = "Имя гостя"
        guestName.widthAnchor.constraint(equalToConstant: 220).isActive = true
        stack.addArrangedSubview(row([guestName,NSButton(title:"Представить гостя…",target:self,action:#selector(guestSetup))]))
        fill(stack,ownerSetupStatus)
        fixedLabel(ownerSetupStatus,height:52)
        fill(stack, label("Проверка образца проверяет распознавание и динамик. Работу микрофона и камеры видно слева."))
        stack.addArrangedSubview(row([NSButton(title: "Правила…", target: self, action: #selector(manageRules)),
                                      NSButton(title: "Отчёт за неделю…", target: self, action: #selector(weeklyFeedback))]))
        fill(stack, separator())
        fill(stack, title("Действующие локальные модели"))
        runtimeInfo.isSelectable = true
        fill(stack, runtimeInfo)
        stack.addArrangedSubview(NSButton(title: "Настроить модели и CLI…", target: self, action: #selector(showComponents)))
        let settings = card(scrolling(stack))
        let devices = devicesColumn()
        devices.widthAnchor.constraint(equalToConstant: 330).isActive = true
        let view = NSView()
        for child in [devices, settings] { view.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([devices.leadingAnchor.constraint(equalTo: view.leadingAnchor), devices.topAnchor.constraint(equalTo: view.topAnchor), devices.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            settings.leadingAnchor.constraint(equalTo: devices.trailingAnchor, constant: 16), settings.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            settings.topAnchor.constraint(equalTo: view.topAnchor), settings.bottomAnchor.constraint(equalTo: view.bottomAnchor)])
        return view
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
        gallery.target = self; gallery.action = #selector(chooseArtifact)
        gallery.widthAnchor.constraint(lessThanOrEqualToConstant: 460).isActive = true
        revealButton.target = self; revealButton.action = #selector(revealArtifact); revealButton.isEnabled = false
        stack.addArrangedSubview(row([gallery, NSButton(title: "Показать", target: self, action: #selector(showArtifact)), revealButton, NSButton(title: "Удалить", target: self, action: #selector(deleteArtifact))]))
        return card(scrolling(stack))
    }
    func tableScroll(_ table: NSTableView, columns: [(String, String, CGFloat)], minHeight: CGFloat = 220) -> NSScrollView {
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
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: minHeight).isActive = true
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
        let tableArea = tableScroll(table, columns: isAI ? [("status", "Состояние", 120), ("title", "Компонент", 340), ("model", "Модель / путь", 320)] : [("status", "Состояние", 110), ("name", "Задание", 260), ("schedule", "Расписание", 240)])
        if isAI {
            table.rowHeight = 50
            table.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
            tableArea.hasHorizontalScroller = false
            tableArea.heightAnchor.constraint(equalToConstant: 300).isActive = true
        }
        fill(stack, tableArea)
        stack.addArrangedSubview(NSButton(title: "Обновить состояние", target: self, action: #selector(loadManagement)))
        if isAI {
            componentModel.isEditable = true
            fill(stack, title("Модель / локальный файл или папка")); fill(stack, componentModel)
            fill(stack, title("CLI / интерпретатор")); fill(stack, componentCLI)
            componentNote.font = Theme.font(12); componentNote.textColor = Theme.dim
            componentNote.maximumNumberOfLines = 0; componentNote.lineBreakMode = .byWordWrapping
            fill(stack, componentNote)
            componentNote.heightAnchor.constraint(greaterThanOrEqualToConstant: 64).isActive = true
            componentNote.isSelectable = true
            componentSave.target = self; componentSave.action = #selector(applyComponent); componentSave.isEnabled = false
            stack.addArrangedSubview(componentSave)
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
        return card(isAI ? scrolling(stack) : stack)
    }

    // MARK: Conversational display

    func displayView() -> NSView {
        // Left: the written conversation with Butler and, under it, the compact history of shown materials.
        chatFeed.markdownRenderer = { [unowned self] in self.renderMarkdown($0) }
        chatFeed.placeholder.stringValue = "Напишите Butler или продиктуйте сообщение: ответ появится здесь. Что вы напишете в Telegram, получит ответ в Telegram."
        chatFeed.heightAnchor.constraint(greaterThanOrEqualToConstant: 160).isActive = true
        let chatTitle = title("Разговор")
        clearChatButton.target = self; clearChatButton.action = #selector(clearChat)
        clearChatButton.toolTip = "Очистить окно разговора. Память Butler об этом разговоре не затрагивается."
        let spacer = NSView(); spacer.setContentHuggingPriority(.init(1), for: .horizontal)
        let chatHeader = row([chatTitle, spacer, clearChatButton])
        chatFeed.eventsToggle.state = .on             // «в очереди» / «без ответа» rows belong to the thread; the toggle is not shown here
        let scroll = tableScroll(displayTable, columns: [("title", "История показов", 250)], minHeight: 96)
        scroll.hasHorizontalScroller = false
        displayTable.headerView = nil
        displayTable.rowHeight = 26
        displayTable.setAccessibilityLabel("История показов")
        let historyLabel = label("История показов · до нажатия «Очистить»")
        let clearDisplayButton = NSButton(title: "Очистить", target: self, action: #selector(clearDisplayHistory))
        let historyTitle = row([historyLabel, NSView(), clearDisplayButton])
        historyTitle.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        let left = NSView()
        for child in [chatHeader, chatFeed, historyTitle, scroll] { left.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([
            chatHeader.leadingAnchor.constraint(equalTo: left.leadingAnchor, constant: 12), chatHeader.trailingAnchor.constraint(equalTo: left.trailingAnchor, constant: -12),
            chatHeader.topAnchor.constraint(equalTo: left.topAnchor, constant: 12),
            chatFeed.leadingAnchor.constraint(equalTo: chatHeader.leadingAnchor), chatFeed.trailingAnchor.constraint(equalTo: chatHeader.trailingAnchor),
            chatFeed.topAnchor.constraint(equalTo: chatHeader.bottomAnchor, constant: 8),
            historyTitle.leadingAnchor.constraint(equalTo: chatHeader.leadingAnchor), historyTitle.trailingAnchor.constraint(equalTo: chatHeader.trailingAnchor),
            historyTitle.topAnchor.constraint(equalTo: chatFeed.bottomAnchor, constant: 10),
            scroll.leadingAnchor.constraint(equalTo: chatHeader.leadingAnchor), scroll.trailingAnchor.constraint(equalTo: chatHeader.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: historyTitle.bottomAnchor, constant: 4), scroll.heightAnchor.constraint(equalToConstant: 108),
            scroll.bottomAnchor.constraint(equalTo: left.bottomAnchor, constant: -12)])
        let leftCard = card(left)
        leftCard.widthAnchor.constraint(equalToConstant: 380).isActive = true
        displayDetail.setAccessibilityLabel("Показанный материал")
        let rightCard = card(displayDetail)
        let bar = composerBar()
        let view = NSView()
        for child in [leftCard, rightCard, bar] { view.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([leftCard.leadingAnchor.constraint(equalTo: view.leadingAnchor), leftCard.topAnchor.constraint(equalTo: view.topAnchor),
            leftCard.bottomAnchor.constraint(equalTo: bar.topAnchor, constant: -12),
            rightCard.leadingAnchor.constraint(equalTo: leftCard.trailingAnchor, constant: 16), rightCard.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            rightCard.topAnchor.constraint(equalTo: view.topAnchor), rightCard.bottomAnchor.constraint(equalTo: bar.topAnchor, constant: -12),
            bar.leadingAnchor.constraint(equalTo: view.leadingAnchor), bar.trailingAnchor.constraint(equalTo: view.trailingAnchor), bar.bottomAnchor.constraint(equalTo: view.bottomAnchor)])
        renderDisplayCard(nil)
        return view
    }
    // The message field with «Отправить» and «Диктовать». Return sends, Shift+Return starts a new line.
    func composerBar() -> NSView {
        composer.isRichText = false
        composer.allowsUndo = true
        composer.isAutomaticQuoteSubstitutionEnabled = false
        composer.isAutomaticDashSubstitutionEnabled = false
        composer.font = Theme.font(13)
        composer.textColor = Theme.bright
        composer.insertionPointColor = Theme.accent
        composer.drawsBackground = true
        composer.backgroundColor = Theme.raised
        composer.textContainerInset = NSSize(width: 6, height: 6)
        composer.isVerticallyResizable = true
        composer.isHorizontallyResizable = false
        composer.autoresizingMask = [.width]
        composer.textContainer?.widthTracksTextView = true
        composer.placeholder = "Сообщение Butler · Return — отправить, Shift+Return — новая строка"
        composer.onSend = { [weak self] in self?.sendChat() }
        composer.setAccessibilityLabel("Сообщение для Butler")
        let field = NSScrollView()
        field.documentView = composer
        field.hasVerticalScroller = true
        field.scrollerStyle = .legacy
        field.autohidesScrollers = true
        field.drawsBackground = true
        field.backgroundColor = Theme.raised
        field.borderType = .lineBorder
        sendButton.target = self; sendButton.action = #selector(sendChat)
        dictateButton.target = self; dictateButton.action = #selector(toggleDictation)
        sendButton.setAccessibilityLabel("Отправить сообщение Butler")
        chatStatus.font = Theme.font(11)
        chatStatus.textColor = Theme.dim
        chatStatus.lineBreakMode = .byTruncatingTail
        chatStatus.maximumNumberOfLines = 1
        let bar = NSView()
        for child in [field, sendButton, dictateButton, chatStatus] { bar.addSubview(child); child.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([
            field.leadingAnchor.constraint(equalTo: bar.leadingAnchor, constant: 12), field.topAnchor.constraint(equalTo: bar.topAnchor, constant: 10),
            field.heightAnchor.constraint(equalToConstant: 66), field.trailingAnchor.constraint(equalTo: sendButton.leadingAnchor, constant: -10),
            sendButton.trailingAnchor.constraint(equalTo: bar.trailingAnchor, constant: -12), sendButton.topAnchor.constraint(equalTo: field.topAnchor),
            sendButton.widthAnchor.constraint(equalToConstant: 140),
            dictateButton.trailingAnchor.constraint(equalTo: sendButton.trailingAnchor), dictateButton.widthAnchor.constraint(equalTo: sendButton.widthAnchor),
            dictateButton.bottomAnchor.constraint(equalTo: field.bottomAnchor),
            chatStatus.leadingAnchor.constraint(equalTo: field.leadingAnchor), chatStatus.trailingAnchor.constraint(equalTo: bar.trailingAnchor, constant: -12),
            chatStatus.topAnchor.constraint(equalTo: field.bottomAnchor, constant: 4), chatStatus.bottomAnchor.constraint(equalTo: bar.bottomAnchor, constant: -8)])
        return card(bar)
    }
    @objc func sendChat() {
        let text = composer.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !chatSending, let json = jsonArgument(["text": text]) else { return }
        chatSending = true
        sendButton.isEnabled = false
        chatStatus.textColor = Theme.dim
        chatStatus.stringValue = "Отправляю…"
        command(["ui-send-text", json]) { result in
            self.chatSending = false
            self.sendButton.isEnabled = true
            if result?["queued"] as? Bool == true {
                self.composer.string = ""
                self.composer.needsDisplay = true
                self.chatStatus.stringValue = ""
                self.refresh()
            } else {
                // The text stays in the field: nothing the owner wrote is lost to a failed send.
                self.chatStatus.textColor = Theme.error
                self.chatStatus.stringValue = (result?["message"] as? String).map { "Не отправлено: \($0)" }
                    ?? "Не отправлено: нет связи с Butler. Текст остался в поле."
            }
        }
    }
    // «Диктовать»: the service listens through its own microphone path (this app has no microphone permission) and
    // returns the recognised words; they land in the field for the owner to read and send. Nothing is sent by itself.
    @objc func toggleDictation() {
        guard let json = jsonArgument(["action": dictationActive ? "stop" : "start"]) else { return }
        dictateButton.isEnabled = false
        command(["ui-dictate", json]) { result in
            self.dictateButton.isEnabled = true
            if result == nil {
                self.chatStatus.textColor = Theme.error
                self.chatStatus.stringValue = "Диктовка недоступна: нет связи с Butler."
            }
            self.applyDictation(result)
        }
    }
    func applyDictation(_ view: [String: Any]?) {
        guard let view else { return }
        let state = view["state"] as? String ?? "idle"
        let id = view["id"] as? String
        switch state {
        case "listening":
            let stopping = view["stopping"] as? Bool == true
            dictationActive = true
            dictateButton.title = stopping ? "Заканчиваю…" : "Остановить"
            dictateButton.isEnabled = !stopping
            chatStatus.textColor = Theme.accent
            let heard = (view["partial"] as? String ?? "").replacingOccurrences(of: "\n", with: " ")
            chatStatus.stringValue = stopping ? "● Дослушиваю последнюю фразу…" : "● Слушаю: говорите" + (heard.isEmpty ? "" : " · слышу: …" + heard.suffix(80))
        case "done", "failed":
            dictationActive = false
            dictateButton.title = "Диктовать"
            dictateButton.isEnabled = true
            guard id != dictationConsumed else { return }
            dictationConsumed = id
            if state == "failed" {
                chatStatus.textColor = Theme.error
                chatStatus.stringValue = view["reason"] as? String ?? "Диктовка не удалась."
                return
            }
            let words = (view["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if words.isEmpty {
                chatStatus.textColor = Theme.dim
                chatStatus.stringValue = view["note"] as? String ?? "Ничего не распознано."
                return
            }
            // Appended to what is in the field now, so anything typed meanwhile is never overwritten.
            let current = composer.string
            let separator = current.isEmpty || current.hasSuffix(" ") || current.hasSuffix("\n") ? "" : " "
            composer.string = current + separator + words
            composer.needsDisplay = true
            composer.setSelectedRange(NSRange(location: (composer.string as NSString).length, length: 0))
            if window.isVisible { window.makeFirstResponder(composer) }
            chatStatus.textColor = Theme.dim
            chatStatus.stringValue = (view["note"] as? String) ?? "Продиктовано. Проверьте текст и нажмите «Отправить»."
        default:
            if dictationActive { dictationActive = false; dictateButton.title = "Диктовать"; dictateButton.isEnabled = true }
        }
    }
    @objc func clearDisplayHistory() {
        command(["ui-display-clear"]) { _ in self.refresh() }
    }
    @objc func clearChat() {
        command(["ui-panel-chat-clear"]) { _ in
            self.chatFeed.show([])
            self.refresh()
        }
    }
    func jsonArgument(_ values: [String: Any]) -> String? {
        guard let data = try? JSONSerialization.data(withJSONObject: values) else { return nil }
        return String(data: data, encoding: .utf8)
    }
    // The service's rows of the written conversation, drawn as thread entries: the owner's lines, then Butler's.
    func updatePanelChat(_ chat: [String: Any]?) {
        let rows = chat?["entries"] as? [[String: Any]] ?? []
        let entries: [FeedEntry] = rows.map { row in
            let state = row["state"] as? String ?? ""
            let text = row["text"] as? String ?? ""
            var entry: [String: Any] = ["id": "chat:\(row["id"] as? String ?? "")", "at": row["at"] ?? 0, "who": row["who"] as? String ?? "Butler"]
            if row["from"] as? String == "owner" {
                entry["kind"] = "heard"; entry["label"] = "СООБЩЕНИЕ"; entry["text"] = text
            } else {
                switch state {
                case "answered": entry["kind"] = "reply"; entry["label"] = "ОТВЕТ"; entry["text"] = text; entry["markdown"] = true
                case "streaming": entry["kind"] = "reply"; entry["label"] = "ПИШЕТ…"; entry["text"] = text; entry["markdown"] = true
                case "silent": entry["kind"] = "event"; entry["label"] = "БЕЗ ОТВЕТА"; entry["text"] = ""
                case "failed": entry["kind"] = "error"; entry["label"] = "ОТВЕТ НЕ ПОЛУЧЕН"; entry["text"] = row["error"] as? String ?? "агент недоступен"
                default: entry["kind"] = "event"; entry["label"] = state == "running" ? "ДУМАЕТ…" : "В ОЧЕРЕДИ"; entry["text"] = ""
                }
            }
            return FeedEntry(entry)
        }
        chatFeed.show(entries)
    }

    // MARK: Наблюдения: «Сейчас | История»
    static let observationSources = ["camera": "Камера", "screen": "Экран", "microphone": "Микрофон", "system-audio": "Звук приложений", "all": "Все источники", "analysis": "Анализ"]
    // Short name for lists and totals, and what the interval really means.
    static let gapReasons: [String: (short: String, detail: String)] = [
        "permission-paused": ("нет разрешения на запись", "Запись и анализ были остановлены: владелец не был подтверждён тремя свежими проверками или согласие не действовало."),
        "owner-paused": ("пауза", "Владелец нажал «Пауза»: камера и микрофон не работали."),
        "capture-gap": ("обрыв захвата", "Источник не передал данные на этом отрезке."),
        "source-unavailable": ("источник недоступен", "Устройство или экран не отвечали."),
        "analysis-failed": ("анализ не удался", "Локальная модель не вернула результат: исходник удалён, вывода нет."),
        "analysis-expired": ("удалено без анализа", "Исходник не успели проанализировать за 24 часа и удалили по сроку хранения: вывода не будет."),
        "analysis-busy": ("анализ занят", "Очередь анализа была заполнена, часть данных не принята."),
        "analysis-missed": ("анализ не успел", "Анализ не обработал исходники вовремя."),
        "analysis-buffer-full": ("буфер заполнен", "Временный буфер анализа был заполнен: новые данные на этом отрезке не записаны."),
        "stale-participant-check": ("проверка участников устарела", "Проверки участников устарели: запись остановлена до новых подтверждений."),
        "participant-check-gap": ("пропуск проверки участников", "Проверка участников не прошла вовремя: запись остановлена до новых подтверждений."),
        "participant-check-unavailable": ("проверка участников недоступна", "Проверку участников не удалось выполнить: запись остановлена."),
        "service-stopped": ("сервис остановлен", "Служба Butler была остановлена или перезапущена: на этом отрезке ничего не записывалось."),
        "settings-change": ("изменены настройки", "Запись остановлена на время смены настроек."),
        "owner-voice-enrollment": ("запись голоса", "Шла запись эталона голоса: обычная запись была остановлена."),
        "owner-reference-updated": ("обновлён образец", "Обновлён образец владельца: проверки участников начаты заново.")]
    static let factKinds = ["activity": "действие", "person": "человек", "dog": "собака", "book": "книга", "appearance": "внешний вид", "work-topic": "тема работы"]

    func clock(_ value: Any?, seconds: Bool = false) -> String {
        guard let number = value as? Double, number.isFinite else { return "—" }
        let formatter = DateFormatter(); formatter.dateFormat = seconds ? "HH:mm:ss" : "HH:mm"
        formatter.timeZone = TimeZone(identifier: "America/Chicago")
        return formatter.string(from: Date(timeIntervalSince1970: number))
    }
    func durationText(_ seconds: Double) -> String {
        if seconds < 60 { return "меньше минуты" }
        let minutes = Int((seconds / 60).rounded())
        if minutes < 60 { return "\(minutes) мин" }
        return minutes % 60 == 0 ? "\(minutes / 60) ч" : "\(minutes / 60) ч \(minutes % 60) мин"
    }
    func dayTitle(_ day: String, today: String? = nil) -> String {
        let parts = day.split(separator: "-")
        let text = parts.count == 3 ? "\(parts[2]).\(parts[1]).\(parts[0])" : day
        return day == today ? text + " (сегодня)" : text
    }
    func observationItemID(_ item: [String: Any]) -> String {
        if let id = item["id"] as? String { return id }
        return "gap:\(item["reason"] as? String ?? ""):\(item["source"] as? String ?? ""):\(item["start"] as? Double ?? 0)"
    }
    func observationRowTitle(_ item: [String: Any]) -> String {
        let source = Panel.observationSources[item["source"] as? String ?? ""] ?? "—"
        if item["type"] as? String == "gap" {
            let reason = item["reason"] as? String ?? ""
            let start = item["start"] as? Double ?? 0, end = item["end"] as? Double ?? start
            let count = item["count"] as? Int ?? 1
            return "\(clock(start))–\(item["open"] as? Bool == true ? "…" : clock(end)) · ⚠ \(Panel.gapReasons[reason]?.short ?? reason)\(count > 1 ? " ×\(count)" : "") · \(durationText(end - start))"
        }
        let details = (item["facts"] as? [[String: Any]] ?? []).compactMap { $0["detail"] as? String }.filter { !$0.isEmpty }
        return "\(clock(item["start"], seconds: true)) · \(source) · \(details.isEmpty ? "без вывода" : details.joined(separator: "; "))"
    }
    func observationDetail(_ item: [String: Any]) -> (title: String, body: String) {
        let source = Panel.observationSources[item["source"] as? String ?? ""] ?? "—"
        let start = item["start"] as? Double ?? 0, end = item["end"] as? Double ?? start
        if item["type"] as? String == "gap" {
            let reason = item["reason"] as? String ?? ""
            let known = Panel.gapReasons[reason]
            var body = (known?.detail ?? "Причина: \(reason).") + "\n\nИнтервал: \(clock(start, seconds: true))–\(item["open"] as? Bool == true ? "ещё идёт" : clock(end, seconds: true)) (\(durationText(end - start)))"
            body += "\nСобытий в интервале: \(item["count"] as? Int ?? 1)\nИсточник: \(source)"
            return ("⚠ " + (known?.short ?? reason), body)
        }
        let facts = item["facts"] as? [[String: Any]] ?? []
        var lines: [String] = []
        for fact in facts {
            var line = "• \(Panel.factKinds[fact["kind"] as? String ?? ""] ?? (fact["kind"] as? String ?? "факт")): \(fact["detail"] as? String ?? "")"
            if let identity = fact["identity"] as? String, !identity.isEmpty {
                line += " · \(identity)" + (fact["identity_basis"] as? String == "owner-confirmed" ? " (подтверждено вами)" : " (предположение модели)")
            }
            if let confidence = fact["confidence"] as? Double { line += " · уверенность \(Int((confidence * 100).rounded())) %" }
            lines.append(line)
        }
        var body = facts.isEmpty ? "Анализ не выделил ничего значимого." : "Выводы модели (могут содержать ошибки):\n" + lines.joined(separator: "\n")
        body += "\n\n" + (item["original"] as? Bool == true ? "Оригинал пока хранится на этом Mac и будет удалён после анализа или по сроку (24 часа)." : "Оригинал удалён после анализа: остались только выводы.")
        return ("\(source) · \(clock(start, seconds: true))–\(clock(end, seconds: true))", body)
    }
    func analysisBannerText(_ analysis: [String: Any]?) -> String? {
        guard let analysis, analysis["stalled"] as? Bool == true else { return nil }
        let last = (analysis["last_at"] as? Double).map { "последний вывод: \(time($0))" } ?? "выводов ещё не было"
        return "Анализ не идёт: \(last); в очереди исходников: \(analysis["pending"] as? Int ?? 0). Оригиналы хранятся не дольше 24 часов и затем удаляются без анализа, поэтому этот период останется без выводов."
    }
    func showObservationsMode() {
        let history = observationsMode.selectedSegment == 1
        nowPane?.isHidden = history
        historyPane?.isHidden = !history
    }
    @objc func changeObservationsMode() {
        showObservationsMode()
        let history = observationsMode.selectedSegment == 1
        defaults.set(history ? "history" : "now", forKey: "observationsMode")
        if history && !observationsLoaded { loadObservations(reset: true) }
    }
    @objc func reloadObservations() { loadObservations(reset: true) }
    @objc func moreObservations() { loadObservations(reset: false) }
    @objc func changeObservationDay() {
        observationDay = obsDay.selectedItem?.representedObject as? String
        loadObservations(reset: true)
    }
    func stepObservationDay(_ delta: Int) {
        guard let day = observationDay, let index = observationDays.firstIndex(where: { $0["day"] as? String == day }), observationDays.indices.contains(index + delta),
              let next = observationDays[index + delta]["day"] as? String else { return }
        observationDay = next
        loadObservations(reset: true)
    }
    @objc func olderObservationDay() { stepObservationDay(1) }       // days are listed newest first
    @objc func newerObservationDay() { stepObservationDay(-1) }
    func observationRequest(offset: Int) -> [String: Any] {
        var request: [String: Any] = ["empty": obsEmpty.state == .on, "offset": offset, "limit": 100]
        if let day = observationDay { request["day"] = day }
        if let source = obsSource.selectedItem?.representedObject as? String, !source.isEmpty { request["source"] = source }
        return request
    }
    func loadObservations(reset: Bool) {
        guard let json = jsonArgument(observationRequest(offset: reset ? 0 : observationItems.count)) else { return }
        observationSerial += 1
        let serial = observationSerial
        obsStatus.textColor = Theme.dim
        obsStatus.stringValue = "Загружаю…"
        command(["ui-observations", json]) { value in
            guard serial == self.observationSerial else { return }       // a newer request replaced this one
            guard let value else {
                self.obsStatus.textColor = Theme.error
                self.obsStatus.stringValue = "История недоступна: нет связи с Butler."
                return
            }
            self.applyObservations(value, append: !reset)
        }
    }
    func applyObservations(_ value: [String: Any], append: Bool) {
        observationsLoaded = true
        observationDay = value["day"] as? String
        observationDays = value["days"] as? [[String: Any]] ?? []
        let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd"
        formatter.timeZone = (value["timezone"] as? String).flatMap(TimeZone.init(identifier:)) ?? .current
        let today = (value["now"] as? Double).map { formatter.string(from: Date(timeIntervalSince1970: $0)) }
        obsDay.removeAllItems()
        for entry in observationDays {
            let day = entry["day"] as? String ?? ""
            let gaps = entry["gaps"] as? Int ?? 0
            obsDay.addItem(withTitle: "\(dayTitle(day, today: today)) · с выводом \(entry["informative"] as? Int ?? 0)" + (gaps > 0 ? " · пропусков \(gaps)" : ""))
            obsDay.lastItem?.representedObject = day
        }
        let index = observationDays.firstIndex { $0["day"] as? String == observationDay }
        if let index { obsDay.selectItem(at: index) }
        obsPrev.isEnabled = index.map { $0 < observationDays.count - 1 } ?? false
        obsNext.isEnabled = index.map { $0 > 0 } ?? false
        let page = value["items"] as? [[String: Any]] ?? []
        observationItems = append ? observationItems + page : page
        observationTotal = value["total"] as? Int ?? observationItems.count
        observationTable.reloadData()
        if let id = observationSelectedID, let row = observationItems.firstIndex(where: { observationItemID($0) == id }) {
            observationTable.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        } else if !append, !observationItems.isEmpty {
            observationTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        } else if observationItems.isEmpty {
            observationTable.deselectAll(nil)
            showObservationDetail(-1)
        }
        let summary = value["summary"] as? [String: Any] ?? [:]
        var text = "\(dayTitle(observationDay ?? "", today: today)): с выводом — \(summary["informative"] as? Int ?? 0), без вывода — \(summary["empty"] as? Int ?? 0)."
        let gaps = summary["gaps"] as? [[String: Any]] ?? []
        text += gaps.isEmpty ? " Пропусков не записано." : " Пропуски: " + gaps.prefix(4).map {
            "\(Panel.gapReasons[$0["reason"] as? String ?? ""]?.short ?? ($0["reason"] as? String ?? "")) \(durationText($0["seconds"] as? Double ?? 0))" }.joined(separator: ", ") + "."
        obsSummary.stringValue = text
        let banner = analysisBannerText(value["analysis"] as? [String: Any])
        obsBanner.stringValue = banner ?? ""
        obsBanner.isHidden = banner == nil
        obsMore.isHidden = observationItems.count >= observationTotal
        obsStatus.textColor = Theme.dim
        obsStatus.stringValue = observationItems.isEmpty
            ? (obsEmpty.state == .on ? "За этот день ничего не записано." : "Нет наблюдений с выводом за этот день. «Показать пустые и пропуски» покажет, что происходило.")
            : "Показано \(observationItems.count) из \(observationTotal) · обновлено \(clock(value["now"], seconds: true))"
    }
    func showObservationDetail(_ row: Int) {
        guard observationItems.indices.contains(row) else {
            observationSelectedID = nil
            obsDetailTitle.stringValue = "Выберите запись слева"
            obsDetailBody.stringValue = ""
            obsFrame.image = nil; obsFrame.isHidden = true
            return
        }
        let item = observationItems[row]
        observationSelectedID = observationItemID(item)
        let detail = observationDetail(item)
        obsDetailTitle.stringValue = detail.title
        obsDetailBody.stringValue = detail.body
        let image = (item["frame"] as? String).flatMap { NSImage(contentsOfFile: $0) }
        obsFrame.image = image
        obsFrame.isHidden = image == nil
    }

    func displayRowTitle(_ card: [String: Any]) -> String {
        let kinds = ["text": "текст", "image": "изображение", "diagram": "схема", "svg": "рисунок", "video": "видео", "email": "письмо"]
        let formatter = DateFormatter(); formatter.dateFormat = "HH:mm"
        let stamp = (card["at"] as? Double).map { formatter.string(from: Date(timeIntervalSince1970: $0)) } ?? "--:--"
        let kind = card["kind"] as? String ?? ""
        return "\(stamp) · \(kinds[kind] ?? (kind.isEmpty ? "материал" : kind)) · \(card["title"] as? String ?? "")" + (Panel.displayRevision(card) > 0 ? " ↻" : "")
    }
    // Poll result → history table and detail area. Rebuilds rows only when ids, titles or statuses changed.
    func updateDisplay(_ display: [String: Any]?) {
        let cards = (display?["cards"] as? [[String: Any]] ?? []).sorted { ($0["at"] as? Double ?? 0) < ($1["at"] as? Double ?? 0) }
        displayHidden = display?["hidden"] as? String
        displayCurrent = display?["current"] as? String
        let signature = cards.map { "\($0["id"] as? String ?? "")|\($0["title"] as? String ?? "")|\($0["status"] as? String ?? "")|\(Panel.displayRevision($0))" }
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
            if Panel.displayRevision(card) != displayRenderedRev {
                renderDisplayCard(card)        // the agent redrew this card in place (a live canvas): follow it, keep the selection
            } else {
                let status = card["status"] as? String
                if status != displayRenderedStatus { displayRenderedStatus = status; showDisplayStatus(status) }
                applyDisplayActionStates(card)
            }
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
        // Retire the OLD player before constructing the replacement video view.
        videoView?.player?.pause(); videoView = nil
        let content: NSStackView
        if let card {
            if let unavailable = card["unavailable"] as? String {
                content = explanation(unavailable == "paused" ? "Изображения и видео скрыты, пока запись наблюдений на паузе." : unavailable == "missing" ? "Файл материала больше не доступен." : "Этот тип материала не поддерживается в этой версии Butler.")
            } else {
                switch card["kind"] as? String ?? "" {
                case "text": content = textDisplayView(card)
                case "image": content = imageDisplayView(card)
                case "diagram": content = diagramDisplayView(card)
                case "svg": content = svgDisplayView(card)
                case "video": content = videoDisplayView(card)
                case "email": content = emailDisplayView(card)
                default: content = explanation("Этот тип материала не поддерживается в этой версии Butler.")
                }
            }
        } else {
            content = explanation(displayHidden == "private" ? "Материалы разговора скрыты: владелец не подтверждён." : "Пока ничего не показано. Butler выведет сюда материал, когда он понадобится в разговоре.")
        }
        showDisplayContent(content)
        displayActionButtons = []
        if let card, card["unavailable"] == nil, let buttons = displayButtonColumn(card) { fill(content, buttons) }
        // The status line comes from the service only (result of an action, delivery state); never invented here.
        let status = label("")
        status.isSelectable = true
        status.setContentCompressionResistancePriority(.init(1), for: .horizontal)
        fill(content, status)
        displayStatusLabel = status
        displayRenderedID = card?["id"] as? String
        displayRenderedRev = card.map(Panel.displayRevision) ?? 0
        displayRenderedStatus = card?["status"] as? String
        displayRenderedKey = card.map { "card:\($0["id"] as? String ?? "")" } ?? "empty:\(displayHidden ?? "")"
        showDisplayStatus(displayRenderedStatus)
    }
    // The detail container holds exactly one child; swapping it stops any playing video.
    func showDisplayContent(_ view: NSView) {
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
    // An optional Markdown caption under a picture or a diagram: a short box that scrolls when the text is long.
    func captionView(_ card: [String: Any]) -> NSScrollView? {
        guard let text = card["markdown"] as? String, !text.isEmpty else { return nil }
        let scroll = NSTextView.scrollableTextView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        if let textView = scroll.documentView as? NSTextView {
            configure(textView)
            textView.textStorage?.setAttributedString(renderMarkdown(text))
        }
        let height = scroll.heightAnchor.constraint(equalToConstant: 96)
        height.priority = .init(900)
        height.isActive = true
        return scroll
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
        if let caption = captionView(card) { fill(stack, caption) }
        fill(stack, label("Двойной щелчок — увеличить или вернуть масштаб."))
        return stack
    }
    @objc func toggleImageZoom(_ sender: NSClickGestureRecognizer) {
        guard let view = sender.view, let scroll = view.enclosingScrollView else { return }
        let point = sender.location(in: view)
        scroll.animator().setMagnification(scroll.magnification > 1.01 ? 1 : 2.5, centeredAt: point)
    }
    // A drawing the agent made. The service already re-wrote it from an allow list (svg_clean.py); this view is the second and third layer:
    // a page that runs no JavaScript, and a Content-Security-Policy that allows nothing but the drawing's own inline presentation.
    static let svgPolicy = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    static func svgPage(_ drawing: String) -> String {
        "<!doctype html><html><head><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"\(svgPolicy)\">"
            + "<style>html,body{margin:0;background:#0b1410}body{min-height:100vh;display:flex;align-items:center}svg{display:block;width:100%;height:auto;max-height:100vh;fill:#d6ffe0;color:#d6ffe0;font-family:ui-monospace,Menlo,monospace}</style>"
            + "</head><body>\(drawing)</body></html>"
    }
    // The only navigation the drawing's page may make is the load of the page itself (loadHTMLString: about:blank, type .other).
    static func svgNavigationAllowed(url: URL?, type: WKNavigationType) -> Bool {
        type == .other && (url == nil || url?.absoluteString == "about:blank")
    }
    func svgWebView() -> WKWebView {
        if let existing = svgWeb { return existing }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let preferences = WKWebpagePreferences()
        preferences.allowsContentJavaScript = false
        configuration.defaultWebpagePreferences = preferences
        let web = WKWebView(frame: .zero, configuration: configuration)
        web.navigationDelegate = self
        web.allowsMagnification = true
        web.underPageBackgroundColor = Theme.panel
        svgWeb = web
        return web
    }
    func svgDisplayView(_ card: [String: Any]) -> NSStackView {
        guard let drawing = card["svg"] as? String, !drawing.isEmpty else { return explanation("Рисунок недоступен.") }
        let stack = detailStack()
        fill(stack, truncating(title(card["title"] as? String ?? "Рисунок")))
        let web = svgWebView()
        web.loadHTMLString(Panel.svgPage(drawing), baseURL: nil)       // a new page also starts at its own scale
        stretch(stack, web)
        if let caption = captionView(card) { fill(stack, caption) }
        fill(stack, label("Масштаб — щипком на трекпаде."))
        return stack
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
            web.allowsMagnification = true
            web.navigationDelegate = self
            web.underPageBackgroundColor = Theme.panel
            diagramWeb = web
            // The page and mermaid.min.js are files inside Butler.app; nothing is ever fetched.
            web.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
        }
        if diagramCardID != card["id"] as? String { web.magnification = 1 }
        diagramCardID = card["id"] as? String
        pendingDiagram = card["mermaid"] as? String ?? ""
        if diagramReady { flushDiagram() }
        stretch(stack, web)
        if let caption = captionView(card) { fill(stack, caption) }
        fill(stack, label("Масштаб — щипком на трекпаде."))
        return stack
    }
    func flushDiagram() {
        guard let web = diagramWeb, let source = pendingDiagram,
              let data = try? JSONSerialization.data(withJSONObject: [source]), let json = String(data: data, encoding: .utf8) else { return }
        pendingDiagram = nil
        web.evaluateJavaScript("renderDiagram(\(json)[0])") { _, _ in }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === svgWeb { svgLoads += 1; return }
        diagramReady = true
        flushDiagram()
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if webView === svgWeb {
            decisionHandler(Panel.svgNavigationAllowed(url: navigationAction.request.url, type: navigationAction.navigationType) ? .allow : .cancel)
            return
        }
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
            if playable {
                let playback = AVPlayer(url: asset.url)
                player.player = playback
                status.isHidden = true
                playback.play()
            }
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
        return stack
    }
    static func displayRevision(_ card: [String: Any]) -> Int { (card["rev"] as? NSNumber)?.intValue ?? 0 }
    static let displayButtonSymbols = ["reply": "paperplane", "link": "arrow.up.right.square", "reveal": "folder"]
    static func displayButtonTip(_ kind: String, _ hint: String) -> String {
        switch kind {
        case "reply": return "Отправить этот ответ в чат Butler"
        case "link": return hint.isEmpty ? "Открыть ссылку" : "Открыть ссылку: \(hint)"
        case "reveal": return hint.isEmpty ? "Показать в Finder" : "Показать в Finder: \(hint)"
        default: return "Открыть ответ в Mail.app"
        }
    }
    // The card's buttons as one column: a long label is cut with «…» (the tooltip has it whole) instead of widening the window.
    func displayButtonColumn(_ card: [String: Any]) -> NSStackView? {
        let actions = (card["actions"] as? [[String: Any]] ?? []).filter { $0["id"] is String }
        guard !actions.isEmpty else { return nil }
        let column = NSStackView()
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 6
        for action in actions {
            let id = action["id"] as? String ?? ""
            let kind = action["kind"] as? String ?? ""
            let label = action["label"] as? String ?? id
            let button = DisplayActionButton(title: label, target: self, action: #selector(runDisplayAction(_:)))
            button.cardID = card["id"] as? String ?? ""
            button.actionID = id
            button.kind = kind
            button.baseTitle = label
            button.lineBreakMode = .byTruncatingTail
            button.setContentCompressionResistancePriority(.init(1), for: .horizontal)
            button.toolTip = Panel.displayButtonTip(kind, action["hint"] as? String ?? "") + (label.count > 40 ? "\n\(label)" : "")
            if let symbol = Panel.displayButtonSymbols[kind], let image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) {
                button.image = image
                button.imagePosition = .imageLeading
            }
            column.addArrangedSubview(button)
            displayActionButtons.append(button)
        }
        applyDisplayActionStates(card)
        return column
    }
    // A reply is single use: once the card was answered every reply button is off and the chosen one is ticked.
    // Applied at render and at every poll, so it survives a refresh and is the service's state, not ours.
    func applyDisplayActionStates(_ card: [String: Any]) {
        let actions = card["actions"] as? [[String: Any]] ?? []
        for button in displayActionButtons where button.cardID == card["id"] as? String {
            guard let action = actions.first(where: { $0["id"] as? String == button.actionID }) else { continue }
            let used = action["used"] as? Bool ?? false
            button.isEnabled = !used
            button.title = (action["chosen"] as? Bool ?? false) ? "✓ " + button.baseTitle : button.baseTitle
        }
    }
    func lockReplyButtons(cardID: String, chosen: String) {
        for button in displayActionButtons where button.cardID == cardID && button.kind == "reply" {
            button.isEnabled = false
            if button.actionID == chosen { button.title = "✓ " + button.baseTitle }
        }
    }
    @objc func runDisplayAction(_ sender: NSButton) {
        guard let button = sender as? DisplayActionButton else { return }
        button.isEnabled = false
        showDisplayStatus("Выполняю…")
        jsonCommand("ui-display-action", ["id": button.cardID, "action": button.actionID]) { value in
            // A sent (or already sent) reply stays off; anything else may be tried again.
            if value?["locked"] as? Bool == true { self.lockReplyButtons(cardID: button.cardID, chosen: button.actionID) } else { button.isEnabled = true }
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
        let diagnostics = NSMenuItem(title: "Диагностика…", action: #selector(showDiagnostics), keyEquivalent: ""); diagnostics.target = self; appMenu.addItem(diagnostics)
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
    func select(_ value: Section, showHiddenPage: Bool = false) {
        let value = value.isHidden && !showHiddenPage ? Section.display : value
        section = value
        for (key, page) in pages { page.isHidden = key != value }
        pageTitle.stringValue = value.title; pageDetail.stringValue = value.detail
        window.subtitle = version.map { "\(value.title) · v\($0)" } ?? value.title
        if value != .display { videoView?.player?.pause() }
        if value != .recordings { recordingsPane?.player.player?.pause() }
        if value == .recordings && recordingsPane?.loaded != true { recordingsPane?.load(reset: true) }
        if let index = Section.visible.firstIndex(of: value) {
            if navigation.selectedRow != index { navigation.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false) }
        } else { navigation.deselectAll(nil) }
        if !value.isHidden { defaults.set(value.rawValue, forKey: "section") }
        if value == .observations && observationsMode.selectedSegment == 1 && !observationsLoaded { loadObservations(reset: true) }
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
        guard !restoringFrame else { return }
        // AppKit may be resizing to satisfy content constraints. Resizing it
        // again from didResize creates a layout loop on small displays.
        defaults.set(NSStringFromRect(window.frame), forKey: "windowFrame")
    }
    static func boundedUserSize(_ proposed: NSSize, visible: NSSize, minimum: NSSize) -> NSSize {
        NSSize(width: max(minimum.width, min(proposed.width, visible.width)),
               height: max(minimum.height, min(proposed.height, visible.height)))
    }
    func windowWillResize(_ sender: NSWindow, to frameSize: NSSize) -> NSSize {
        guard !testing, let area = (sender.screen ?? NSScreen.main)?.visibleFrame else { return frameSize }
        return Panel.boundedUserSize(frameSize, visible: area.size, minimum: sender.minSize)
    }
    func windowDidResize(_ notification: Notification) { saveFrame() }
    func windowDidMove(_ notification: Notification) { saveFrame() }
    @objc func showFromNotification(_ notification: Notification) { show() }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { show(); return true }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    @objc func showModels() { select(.models); show() }
    @objc func showComponents() { select(.components); show() }

    func numberOfRows(in tableView: NSTableView) -> Int {
        if tableView === navigation { return Section.visible.count }
        if tableView === displayTable { return displayCards.count }
        if tableView === observationTable { return observationItems.count }
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
            let section = Section.visible[row]
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
        if tableView === observationTable {
            guard observationItems.indices.contains(row) else { return nil }
            let item = observationItems[row]
            let label = NSTextField(labelWithString: observationRowTitle(item))
            label.lineBreakMode = .byTruncatingTail
            label.setContentCompressionResistancePriority(.init(1), for: .horizontal)
            label.font = Theme.font(12)
            label.textColor = item["type"] as? String == "gap" ? Theme.warn : (item["facts"] as? [[String: Any]] ?? []).isEmpty ? Theme.dim : Theme.text
            label.toolTip = label.stringValue
            return label
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
            let componentState = ["configured": "настроен", "timeout": "таймаут", "cancelled": "отменён", "not-configured": "не настроен"][value["status"] as? String ?? ""]
            label.stringValue = "● " + (running ? "выполняется" : isAI ? (componentState ?? (ready ? "доступен" : "недоступен")) : ["ready": "готов", "ok": "выполнено", "paused": "пауза", "scheduled": "по плану", "disabled": "выключено", "unknown": "неизвестно", "error": "ошибка" ][value["status"] as? String ?? ""] ?? value["status"] as? String ?? "—")
            label.textColor = ready ? Theme.accent : Theme.error
        } else { label.stringValue = key == "title" && tableView === routeTable ? taskTitle(value) : value[key] as? String ?? "—" }
        label.toolTip = label.stringValue
        if tableView === componentTable {
            if key == "title" { label.maximumNumberOfLines = 2; label.lineBreakMode = .byWordWrapping; label.cell?.wraps = true }
            label.setContentCompressionResistancePriority(.init(1), for: .horizontal)
            let cell = NSTableCellView(); cell.wantsLayer = true; cell.layer?.masksToBounds = true
            cell.addSubview(label); cell.textField = label; label.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
                label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
                label.topAnchor.constraint(equalTo: cell.topAnchor, constant: 4), label.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -4)])
            return cell
        }
        return label
    }
    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        tableView === navigation ? NavigationRowView() : nil
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !reconciling, let table = notification.object as? NSTableView else { return }
        if table === navigation { if Section.visible.indices.contains(table.selectedRow) { select(Section.visible[table.selectedRow]) }; return }
        if table === observationTable { showObservationDetail(table.selectedRow); return }
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
            componentSave.isEnabled = value["model_key"] is String || value["cli_key"] is String
            if !componentSave.isEnabled { componentMessage.stringValue = "Только для просмотра: этот компонент здесь не меняется." }
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
            let last = value["last_result"].map { " · \($0)" } ?? ""
            jobMessage.stringValue = "Следующий по плану: \(time(value["next_run"]))\nПоследний подтверждённый: \(time(value["last_run"]))\(last)\nМодель: \(value["model"] ?? "—")\n\(value["error"] ?? "")"
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
        guard let id = componentID, id == selectedID(componentTable), let value = components.first(where: { $0["id"] as? String == id }),
              value["model_key"] is String || value["cli_key"] is String else { return }
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

    func correctEpisode(_ entry: FeedEntry, kind: String) {
        guard let episode = entry.episode else { return }
        let parts = kind.split(separator: ":", maxSplits: 1).map(String.init)
        let action = parts[0]
        var args: [String: Any] = ["id": episode, "revision": entry.revision, "kind": action, "event": UUID().uuidString]
        if parts.count > 1 { args["memory"] = parts[1] }
        if let annotation = entry.annotation { args["annotation"] = annotation }
        if kind == "recognition" {
            let alert = NSAlert(); alert.messageText = "Что вы сказали?"
            let field = NSTextField(string: ""); field.frame = NSRect(x: 0, y: 0, width: 420, height: 28)
            alert.accessoryView = field; alert.addButton(withTitle: "Исправить"); alert.addButton(withTitle: "Отмена")
            guard alert.runModal() == .alertFirstButtonReturn, !field.stringValue.isEmpty else { return }
            args["text"] = field.stringValue
        }
        guard let data = try? JSONSerialization.data(withJSONObject: args), let json = String(data: data, encoding: .utf8) else { return }
        command(["ui-feedback-correct", json]) { result in
            if result == nil { NSSound.beep() }
            self.refresh()
        }
    }
    @objc func manageRules() {
        command(["ui-rule", "{\"action\":\"list\"}"]) { result in
            guard let rules = result?["rules"] as? [[String: Any]] else { NSSound.beep(); return }
            let alert = NSAlert(); alert.messageText = "Правила и предпочтения"
            alert.informativeText = "Действующие правила хранятся в Butler/Rules. Выберите правило для просмотра или изменения."
            let box = NSStackView(); box.orientation = .vertical; box.alignment = .leading
            let choices = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 460, height: 28))
            choices.addItem(withTitle: "Новое предпочтение")
            for rule in rules { choices.addItem(withTitle: "\(rule["title"] as? String ?? "") · \(rule["status"] as? String ?? "")") }
            let wording = NSTextField(string: ""); wording.placeholderString = "Текст нового предпочтения или новая формулировка"
            wording.frame = NSRect(x: 0, y: 0, width: 460, height: 28)
            box.addArrangedSubview(choices); box.addArrangedSubview(wording)
            box.frame = NSRect(x: 0, y: 0, width: 460, height: 70); alert.accessoryView = box
            for title in ["Сохранить", "Посмотреть", "Ограничить…", "Завершить действие", "Удалить", "Отмена"] { alert.addButton(withTitle: title) }
            let response = alert.runModal().rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
            guard response < 5 else { return }
            let chosen = choices.indexOfSelectedItem > 0 ? rules[choices.indexOfSelectedItem - 1] : nil
            if response == 1 {
                let info = NSAlert(); info.messageText = chosen?["title"] as? String ?? "Новое предпочтение"
                info.informativeText = chosen.map { rule in
                    let uses = (rule["uses"] as? [[String: Any]] ?? []).map { use in
                        let date = Date(timeIntervalSince1970: use["at"] as? Double ?? 0).description
                        return "\(date): ревизия \(use["revision"] ?? ""), результат \(use["outcome"] ?? "unknown")"
                    }.joined(separator: "\n")
                    return "\(rule["text"] as? String ?? "")\nОбласть: \(rule["scope"] ?? "")\nИсточник: \(rule["source"] ?? "")\nСтатус: \(rule["status"] ?? "")\nРевизия: \(rule["revision"] ?? "")\nДата: \(rule["updated_at"] ?? "")\nПримеры применения:\n\(uses.isEmpty ? "Пока нет записей применения." : uses)"
                } ?? "Введите новое предпочтение."
                info.runModal(); return
            }
            var args: [String: Any] = ["event": UUID().uuidString]
            if let chosen { args["id"] = chosen["id"] }
            if response == 0 {
                guard !wording.stringValue.isEmpty else { return }
                args["action"] = chosen == nil ? "remember" : "edit"; args["text"] = wording.stringValue
            } else if response == 2 {
                guard let chosen else { return }
                let scope = chosen["scope"] as? [String: String] ?? ["mode": "private"]
                let narrow = NSAlert(); narrow.messageText = "Когда применять правило?"
                let app = NSTextField(string: scope["application"] ?? ""); app.placeholderString = "Приложение (необязательно)"
                let words = NSTextField(string: scope["contains"] ?? ""); words.placeholderString = "Слова в просьбе (необязательно)"
                let fields = NSStackView(views: [app, words]); fields.orientation = .vertical
                fields.frame = NSRect(x: 0, y: 0, width: 420, height: 60); narrow.accessoryView = fields
                narrow.addButton(withTitle: "Применить"); narrow.addButton(withTitle: "Отмена")
                guard narrow.runModal() == .alertFirstButtonReturn else { return }
                var narrowed = ["mode": "private"]
                if !app.stringValue.isEmpty { narrowed["application"] = app.stringValue }
                if !words.stringValue.isEmpty { narrowed["contains"] = words.stringValue }
                args["action"] = "narrow"; args["scope"] = narrowed
            } else { guard chosen != nil else { return }; args["action"] = response == 3 ? "retire" : "delete" }
            guard let data = try? JSONSerialization.data(withJSONObject: args), let json = String(data: data, encoding: .utf8) else { return }
            self.command(["ui-rule", json]) { _ in self.refresh() }
        }
    }
    @objc func changeAvailability() {
        let modes = ["auto", "busy", "free"]
        guard modes.indices.contains(availability.selectedSegment) else { return }
        command(["ui-attention", "{\"mode\":\"" + modes[availability.selectedSegment] + "\"}"]) { _ in self.refresh() }
    }
    @objc func weeklyFeedback() {
        command(["ui-feedback"]) { value in
            guard let report = value?["weekly"] as? [String: Any] else { NSSound.beep(); return }
            func measure(_ key: String) -> String {
                guard let metric = report[key] as? [String: Any], let n = metric["denominator"] as? Int, n > 0 else { return "нет размеченных данных" }
                return "\(metric["errors"] ?? 0) / \(n)"
            }
            let unknownShare: String
            if let share = report["unknown_share"] as? NSNumber, share.doubleValue.isFinite {
                unknownShare = String(format: "%.1f%%", share.doubleValue * 100)
            } else {
                unknownShare = "нет данных"
            }
            let alert = NSAlert(); alert.messageText = "Качество разговора за неделю"
            alert.informativeText = "Лишние вмешательства: \(measure("false_interventions"))\nПропущенные обращения: \(measure("missed_addresses"))\nПо имени: \(measure("named_misses")); без имени: \(measure("unnamed_misses"))\nМедиана до первого слова: \(report["median_first_word_ms"] ?? "нет данных") мс\nВсего решений: \(report["sample_size"] ?? 0); размечено: \(report["labelled"] ?? 0).\nНеоценённых: \(report["unknown_outcomes"] ?? 0) (\(unknownShare)).\nСцены: \(report["scenes"] ?? [:])\nУчёт ложных прерываний шумом или другими голосами пока недоступен.\nАудиопримеры не сохраняются; акустическая проверка по этим меткам недоступна."
            alert.runModal()
        }
    }
    func command(_ arguments: [String], completion: @escaping ([String: Any]?) -> Void) {
        if testing {
            testCommands.append(arguments)
            if let name = arguments.first {
                if testNoReply.contains(name) { completion(nil); return }
                if let reply = testReplies[name] { completion(reply); return }
            }
            switch arguments.first {
            case "ui-components": completion(["components": components])
            case "ui-schedules": completion(["jobs": jobs])
            case "ui-components-update", "ui-schedule-control": completion(["saved": true, "ok": true])
            case "ui-display-select": completion(["ok": true])
            case "ui-display-action": completion(testDisplayAction)
            case "ui-send-text": completion(testSendResult)
            case "ui-panel-chat-clear": completion(["cleared": true])
            case "ui-dictate": completion(testDictation)
            case "ui-observations":
                if testSupersedeObservation { observationSerial += 1 }      // a newer request has replaced this one before it answers
                completion(testObservations)
            case "ui-state": completion(testState)
            default: completion(nil)
            }
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: self.cli)
            process.arguments = arguments
            let output = Pipe(), errors = Pipe()
            let run = CommandRun()
            process.standardOutput = output
            process.standardError = errors
            errors.fileHandleForReading.readabilityHandler = { run.appendError($0.availableData) }
            let name = arguments.first ?? "-"
            do {
                try process.run()
                let timeout = DispatchWorkItem { if process.isRunning { run.markTimedOut(); process.terminate() } }
DispatchQueue.global().asyncAfter(deadline: .now() + Panel.commandTimeout(name), execute: timeout)
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                timeout.cancel()
                errors.fileHandleForReading.readabilityHandler = nil
                let value = process.terminationStatus == 0 ? (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] : nil
                if value == nil {
                    let reason = run.timedOut ? "timeout" : process.terminationStatus != 0 ? "exit \(process.terminationStatus)" : "unreadable output"
                    Panel.writeLog("\(name) failed: \(reason)\(Panel.errorDetail(name, run.errorText))")
                }
                DispatchQueue.main.async { completion(value) }
            } catch {
                errors.fileHandleForReading.readabilityHandler = nil
                Panel.writeLog("\(name) failed: could not start the CLI (\(type(of: error)))")
                DispatchQueue.main.async { completion(nil) }
            }
        }
    }

    /// One line per failed command in a bounded log: time, command name, class of failure. Arguments are never
    /// written (typed messages will pass through `command`), and stderr is kept only for commands whose arguments
    /// cannot carry the owner's text.
    static let logQueue = DispatchQueue(label: "butler.panel.log")
    static let logURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/share/butler-ambient/panel.log")
    static func writeLog(_ line: String) {
        logQueue.async {
            guard let data = "\(ISO8601DateFormatter().string(from: Date())) \(line)\n".data(using: .utf8) else { return }
            let manager = FileManager.default
            try? manager.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let size = (try? manager.attributesOfItem(atPath: logURL.path))?[.size] as? Int, size > 262_144 {
                let old = logURL.appendingPathExtension("1")
                try? manager.removeItem(at: old)
                try? manager.moveItem(at: logURL, to: old)
            }
            if let handle = try? FileHandle(forWritingTo: logURL) {
                handle.seekToEndOfFile(); handle.write(data); try? handle.close()
            } else {
                try? data.write(to: logURL)
            }
        }
    }
    static func errorDetail(_ command: String, _ text: String) -> String {
        guard !text.isEmpty, !command.hasPrefix("ui-send") && !command.hasPrefix("ui-dictate") else { return "" }
        let line = text.split(separator: "\n").last.map(String.init) ?? text
        return " · \(String(line.prefix(200)))"
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
        command(window.isVisible && [.voice, .observations].contains(section) ? ["ui-state", "--preview"] : ["ui-state"]) { result in
            self.busy = false
            guard let value = result else {
                self.recordingsPane?.updatePermission(nil)
                self.updateDisplay(["cards": [], "hidden": "private"])
                self.artifactImage.image = nil
                self.artifacts = []; self.gallery.removeAllItems(); self.revealButton.isEnabled = false
                self.artifactCaption.stringValue = "Изображения скрыты: проверка владельца недоступна."
                self.observationItems = []; self.observationTable.reloadData(); self.showObservationDetail(-1)
                self.history.stringValue = "Наблюдения скрыты: проверка владельца недоступна."
                self.state["recordings_allowed"] = false
                self.failureStreak += 1
                if let heard = self.lastGoodState, self.failureStreak < self.disconnectAfterFailures {
                    // A slow or dropped poll: keep everything on screen and say how old it is.
                    self.window.subtitle = "\(self.section.title) · данные от \(DateFormatter.localizedString(from: heard, dateStyle: .none, timeStyle: .medium))"
                    return
                }
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
            self.recordingsPane?.updatePermission(value)
            self.state = value
            self.lastGoodState = Date()
            if self.failureStreak > 0 {
                self.failureStreak = 0
                self.window.subtitle = self.version.map { "\(self.section.title) · v\($0)" } ?? self.section.title
            }
            if let version = value["version"] as? String, !version.isEmpty, version != self.version {
                self.version = version
                self.tagline.stringValue = "Личный помощник · v\(version)"
                self.window.subtitle = "\(self.section.title) · v\(version)"
            }
            self.updateDisplay(value["display"] as? [String: Any])
            if value["recordings_allowed"] as? Bool == false {
                self.observationItems = []; self.observationTable.reloadData(); self.showObservationDetail(-1)
                self.history.stringValue = "Наблюдения скрыты до подтверждения владельца."
            }
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
            let feedback = value["feedback"] as? [String: Any] ?? [:]
            let weekly = feedback["weekly"] as? [String: Any] ?? [:]
            self.feedbackStatus.stringValue = "Исправления: в очереди \(feedback["backlog"] ?? 0), размечено за неделю \(weekly["labelled"] ?? 0)." +
                ((feedback["error"] as? String) == nil ? "" : "\nСохранение или анализ временно недоступны; исправление разговора выполняется отдельно.")
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
            self.history.stringValue = recent.isEmpty ? "Пока нет наблюдений с выводом." : recent.prefix(8).map {
                "\(self.time($0["at"])) · \(Panel.observationSources[$0["source"] as? String ?? ""] ?? ($0["source"] as? String ?? "")): \(($0["details"] as? [String] ?? []).joined(separator: "; "))"
            }.joined(separator: "\n")
            let banner = self.analysisBannerText(value["analysis"] as? [String: Any])
            self.nowBanner.stringValue = banner ?? ""
            self.nowBanner.isHidden = banner == nil
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
            if setupStage == self.setupNoticeStage && (self.testNow ?? Date()) < self.setupNoticeUntil { self.ownerSetupStatus.stringValue = self.setupNoticeText }
            self.ownerSetupCancel.isEnabled = ["authenticating", "recording", "validating"].contains(setupStage)
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
            let capturePhase = (voice["listening"] as? [String: Any])?["phase"] as? String ?? "waiting"
            let liveCapture = ["capturing-speech": "Слышу речь, сохраняю начало фразы…", "listening": "Слышу вас, фраза продолжается…",
                               "transcription": "Распознаю услышанную фразу…", "awaiting-continuation": "Жду продолжения фразы…"]
            if let captureStatus = liveCapture[capturePhase] {
                self.latencySummary.stringValue = captureStatus + "\n" + self.latencySummary.stringValue.components(separatedBy: "\n").last!
            }
            if let transcript = (voice["listening"] as? [String: Any])?["transcript"] as? [String: Any],
               transcript["state"] as? String == "draft", let text = transcript["text"] as? String, !text.isEmpty {
                self.latencySummary.stringValue = "Черновик: " + String(text.prefix(130)).replacingOccurrences(of: "\n", with: " ") + "\n" + self.latencySummary.stringValue.components(separatedBy: "\n").last!
            }
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
            self.debugStamp.stringValue = "Обновлено \(self.clock((self.testNow ?? Date()).timeIntervalSince1970, seconds: true))"
            let statusLines = ["ЖИВАЯ ОТЛАДКА",
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
            self.updatePanelChat(value["panel_chat"] as? [String: Any])
            self.applyDictation(value["dictation"] as? [String: Any])
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
            let archiveEffective = archive["enabled"] as? Bool == true
            let archiveChosen = archive["preference"] as? Bool ?? archiveEffective
            self.archiveToggle.state = archiveChosen ? .on : .off
            self.archiveToggle.title = Panel.archiveTitle + (archiveChosen && !archiveEffective ? " (на паузе)" : "")
            let work = value["work"] as? [String: Any] ?? [:]
            let workRows = work["jobs"] as? [[String: Any]] ?? []
            let workLabels = ["running":"выполняется", "done":"готово", "failed":"ошибка", "cancelled":"отменено", "interrupted":"прервано"]
            self.workStatus.stringValue = workRows.isEmpty ? "Фоновых поручений пока нет. Можно поручить задачу Codex или Claude в конкретном проекте." : workRows.prefix(2).map {
                "\($0["project"] ?? "Проект") · \(workLabels[$0["status"] as? String ?? ""] ?? "проверяю") · \($0["backend"] ?? "CLI") / \($0["model"] ?? "—")"
            }.joined(separator: "\n")
            if let dialogue = work["voice_requests"] as? [String: Any],
               let requests = dialogue["requests"] as? [[String: Any]], !requests.isEmpty {
                let requestLabels = ["queued":"ожидает обработки", "running":"обрабатываю", "awaiting-answer":"жду вашего ответа",
                                     "completed":"ответ подготовлен", "failed":"ошибка запроса", "silent":"ответ не требуется", "cancelled":"отменён"]
                let deliveryLabels = ["pending":"ожидает озвучивания", "playing":"озвучиваю", "completed":"воспроизведение завершено",
                                      "interrupted":"голос остановлен", "failed":"озвучивание не завершено", "cancelled":"вывод отменён"]
                self.workStatus.stringValue += "\n" + requests.suffix(3).map {
                    let state = requestLabels[$0["state"] as? String ?? ""] ?? "проверяю"
                    let delivery = deliveryLabels[$0["delivery"] as? String ?? ""] ?? ""
                    let request = String(($0["request"] as? String ?? "Личный запрос").prefix(100))
                    let kind = $0["kind"] as? String == "task" ? "Ход работы" : "Диалог"
                    return "\(kind): \(request) · \(state) · \(delivery)"
                }.joined(separator: "\n")
            }
            let meeting = voice["meeting"] as? [String: Any] ?? [:]
            let meetingQuiet = meeting["quiet"] as? Bool == true
            let meetingApps = meeting["apps"] as? [String] ?? []
            self.meetingStatus.stringValue = meetingQuiet
                ? "Встреча / тихий режим · отвечаю только на явное обращение к Butler.\n" + (meetingApps.isEmpty ? "Автоматическая проверка или выбранный вами режим." : meetingApps.joined(separator: ", "))
                : "Обычный разговор · имя не обязательно. Учитываю, к кому вы обращаетесь."
            self.meetingStatus.textColor = meetingQuiet ? Theme.warn : Theme.dim
            let attention = voice["attention"] as? [String: Any] ?? [:]
            let override = attention["override"] as? String ?? "auto"
            self.availability.selectedSegment = override == "busy" ? 1 : override == "free" ? 2 : 0
            let actualBusy = attention["state"] as? String == "busy"
            self.availabilityStatus.stringValue = override == "auto"
                ? "Автоматически: \(actualBusy ? "занят" : "свободен"). Можно изменить вручную."
                : "Вручную: \(override == "busy" ? "занят" : "свободен") — до изменения ситуации."
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
                let titles = self.artifacts.enumerated().map { index, artifact in self.artifactTitle(index, artifact) }
                if previous != titles && !titles.isEmpty {
                    self.gallery.removeAllItems()
                    self.gallery.addItems(withTitles: titles)
                    self.displayArtifact()          // the details below always describe the image the popup shows
                } else if titles.isEmpty {
                    self.gallery.removeAllItems()
                    self.gallery.addItem(withTitle: "Результатов пока нет")
                    self.revealButton.isEnabled = false
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
        if testing { return }                       // layout tests never raise a window (lesson L-2026-10-25-7)
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
    }
    func windowWillClose(_ notification: Notification) { defaults.set(false, forKey: "windowVisible") }
    @objc func showDebug() {
        select(.components)
        show()
        if !settingsLoaded { loadSettings() }
        loadManagement()
    }
    @objc func showDiagnostics() {
        select(.diagnostics)
        show()
    }
    /// The menu says what the click will do, from the state that is on screen.
    func menuNeedsUpdate(_ menu: NSMenu) {
        guard let enabled = state["enabled"] as? Bool else { pauseMenuItem?.title = "Пауза / продолжить"; return }
        pauseMenuItem?.title = enabled ? "Поставить на паузу" : "Продолжить"
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
    func saveSettings(_ values: [String: String], applied: String = "Настройки применены.") {
        guard let data = try? JSONSerialization.data(withJSONObject: values), let json = String(data: data, encoding: .utf8) else { return }
        settingMessage.stringValue = "Применяю. Butler перезапустится и заново проверит участников…"
        command(["ui-settings-update", json]) { value in
            self.settingMessage.stringValue = value?["saved"] as? Bool == true
                ? "\(applied) Butler перезапускается и заново проверяет участников."
                : "Настройки не применены: устройство или локальная модель недоступны."
        }
    }
    @objc func applyDevices() {
        guard cameraOptions.indices.contains(cameraChoice.indexOfSelectedItem), microphoneOptions.indices.contains(microphoneChoice.indexOfSelectedItem),
              let camera = cameraOptions[cameraChoice.indexOfSelectedItem]["id"], let microphone = microphoneOptions[microphoneChoice.indexOfSelectedItem]["id"] else { return }
        let sizes = ["640x480","1280x720","1920x1080"]
        let size = sizes.indices.contains(cameraSizeChoice.indexOfSelectedItem) ? sizes[cameraSizeChoice.indexOfSelectedItem] : "640x480"
        saveSettings(["camera": camera, "microphone": microphone, "camera_size": size], applied: "Устройства применены.")
    }
    @objc func videoEffects() { command(["ui-video-effects"]) { _ in } }
    /// A refusal from the service stays on screen for a while: the 1 s refresh would otherwise replace it with the idle text.
    /// It is dropped when the setup moves to another stage, because the progress text is then the news.
    func noteSetup(_ text: String) {
        setupNoticeText = text
        setupNoticeUntil = (testNow ?? Date()).addingTimeInterval(15)
        setupNoticeStage = ((state["voice"] as? [String: Any])?["owner_setup"] as? [String: Any])?["stage"] as? String ?? "idle"
        ownerSetupStatus.stringValue = text
    }
    @objc func ownerSetup() {
        command(["ui-owner-setup"]) { value in
            if value?["queued"] as? Bool == true { self.refresh(); return }
            self.noteSetup(value?["message"] as? String ?? "Не удалось начать регистрацию: нет связи с Butler.")
        }
    }
    @objc func cancelSetup() {
        command(["ui-owner-setup-cancel"]) { value in
            if value?["cancelled"] as? Bool != true { self.noteSetup("Не удалось отменить регистрацию: нет связи с Butler.") }
            self.refresh()
        }
    }

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
        saveSettings(["speech_language": languages[languageChoice.indexOfSelectedItem]], applied: "Язык распознавания: \(languageChoice.titleOfSelectedItem ?? languages[languageChoice.indexOfSelectedItem]).")
    }
    /// Decides from the state read just now, not from the last poll: a stale «enabled» would flip the wrong way.
    @objc func toggle() {
        command(["ui-state"]) { fresh in
            guard let enabled = fresh?["enabled"] as? Bool else {
                self.message.stringValue = "Нет связи с Butler: пауза не переключена."
                self.refresh()
                return
            }
            self.command([enabled ? "pause" : "resume"]) { _ in self.refresh() }
        }
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
        settingMessage.stringValue = "Играю пример голоса…"
        command(["voice-preview", profileIDs[voices.indexOfSelectedItem]]) { value in
            self.settingMessage.stringValue = value?["played"] != nil ? "Пример голоса воспроизведён." : "Не удалось проиграть пример голоса: проверьте вывод звука и локальный сервис."
        }
    }
    @objc func selectVoice() {
        guard profileIDs.indices.contains(voices.indexOfSelectedItem) else { return }
        let name = voices.titleOfSelectedItem ?? "выбранный"
        command(["voice-select", profileIDs[voices.indexOfSelectedItem]]) { value in
            self.settingMessage.stringValue = value?["selected"] != nil ? "Голос выбран: \(name)." : "Не удалось выбрать голос: проверьте локальный сервис."
            self.refresh()
        }
    }
    @objc func toggleVoice() {
        let voice = state["voice"] as? [String: Any] ?? [:]
        command(["preferences", voice["enabled"] as? Bool == true ? "{\"voice_enabled\":false}" : "{\"voice_enabled\":true}"]) { _ in self.refresh() }
    }
    @objc func guestSetup() {
        let name = guestName.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
        guard !name.isEmpty && name.count <= 80 else { noteSetup("Введите короткое имя гостя."); return }
        command(["ui-guest-setup",name]) { value in
            if value?["queued"] as? Bool == true { self.guestName.stringValue = "" }
            else { self.noteSetup(value?["message"] as? String ?? "Не удалось начать регистрацию гостя: нет связи с Butler.") }
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
    @objc func chooseArtifact() { displayArtifact() }

    // MARK: Изображения: where each image came from, where it is and whether it was sent
    static let artifactOrigins = ["camera": "Снимок камеры", "screen": "Снимок экрана", "my-file": "Мой материал · кандидат", "generated": "Создано моделью"]
    func artifactOrigin(_ artifact: [String: Any]) -> String {
        if let origin = artifact["origin_kind"] as? String, let name = Panel.artifactOrigins[origin] { return name }
        let kind = artifact["kind"] as? String ?? ""
        return ["captured": "Снимок", "imported": "Мой материал · кандидат", "generated": "Создано моделью"][kind] ?? "Изображение"
    }
    func artifactTitle(_ index: Int, _ artifact: [String: Any]) -> String {
        let sent = (artifact["sent"] as? [String: Any])?["state"] as? String == "sent"
        return "\(index + 1). \(artifactOrigin(artifact)) · \(stamp(artifact["at"], short: true))" + (sent ? " · отправлено" : "")
    }
    /// Fixed day.month.year 24-hour stamps, like the Наблюдения page: the system locale would print «Oct 26, 2026 at 8:00:00 PM».
    func stamp(_ value: Any?, short: Bool = false) -> String {
        guard let number = value as? Double, number > 0, number.isFinite else { return "время неизвестно" }
        let formatter = DateFormatter(); formatter.dateFormat = short ? "dd.MM HH:mm" : "dd.MM.yyyy HH:mm:ss"
        return formatter.string(from: Date(timeIntervalSince1970: number))
    }
    func artifactPlace(_ artifact: [String: Any]) -> String? {
        let place = artifact["place"] as? String ?? ""
        switch artifact["origin_kind"] as? String ?? "" {
        case "camera": return "камера" + (place.isEmpty || place == "камера" ? "" : " «\(place)»")
        case "screen": return place.isEmpty ? "экран" : place
        case "my-file": return place.isEmpty ? nil : "папка " + (place as NSString).abbreviatingWithTildeInPath
        case "generated": return place.isEmpty ? "локальная модель" : "локальная модель «\(place)»"
        default: return nil
        }
    }
    func artifactSent(_ artifact: [String: Any]) -> String {
        let sent = artifact["sent"] as? [String: Any] ?? [:]
        switch sent["state"] as? String ?? "" {
        case "sent": return "отправлено вам в Telegram (\(stamp(sent["at"])))"
        case "uncertain": return "отправка была начата, результат неизвестен; повтора не будет"
        case "not-sent": return "должно было уйти в Telegram, но не отправлено"
        default: return "не отправлялось, только в этом окне"
        }
    }
    func artifactNote(_ artifact: [String: Any]) -> String? {
        let caption = artifact["caption"] as? String ?? ""
        if caption.isEmpty { return nil }
        switch artifact["origin_kind"] as? String ?? "" {
        case "my-file": return "Описание модели (может быть неточным): \(caption)"
        case "generated":
            // The service stores «Generated locally with <model>: <request>»; the owner needs the request.
            let request = caption.hasPrefix("Generated locally with ") ? caption.components(separatedBy: ": ").dropFirst().joined(separator: ": ") : caption
            return request.isEmpty ? nil : "Запрос: \(request)"
        default: return caption
        }
    }
    func artifactProvenance(_ artifact: [String: Any], allowed: Bool) -> String {
        var lines = [artifactOrigin(artifact)]
        if let place = artifactPlace(artifact) { lines.append("Откуда: \(place)") }
        let verb = ["my-file": "Файл изменён", "generated": "Создано"][artifact["origin_kind"] as? String ?? ""] ?? "Снято"
        lines.append("\(verb): \(stamp(artifact["at"]))")
        if let path = artifact["path"] as? String, allowed { lines.append("Файл: " + (path as NSString).abbreviatingWithTildeInPath) }
        else if artifact["available"] as? Bool == false { lines.append("Файл: удалён по сроку хранения или недоступен") }
        else { lines.append("Файл: путь скрыт, пока запись на паузе") }
        lines.append("Отправка: " + artifactSent(artifact))
        if let note = artifactNote(artifact) { lines.append(note) }
        return lines.joined(separator: "\n")
    }
    func displayArtifact() {
        guard artifacts.indices.contains(gallery.indexOfSelectedItem) else { artifactImage.image = nil; revealButton.isEnabled = false; return }
        let artifact = artifacts[gallery.indexOfSelectedItem]
        let allowed = state["recording_allowed"] as? Bool == true
        let path = allowed ? artifact["path"] as? String : nil
        artifactImage.image = path.flatMap { NSImage(contentsOfFile: $0) }
        artifactCaption.stringValue = artifactProvenance(artifact, allowed: allowed)
        revealButton.isEnabled = path.map { FileManager.default.fileExists(atPath: $0) } ?? false
    }
    @objc func revealArtifact() {
        guard state["recording_allowed"] as? Bool == true, artifacts.indices.contains(gallery.indexOfSelectedItem),
              let path = artifacts[gallery.indexOfSelectedItem]["path"] as? String, FileManager.default.fileExists(atPath: path) else { return }
        if testing { testRevealed = path; return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
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
    panel.select(.dialogue, showHiddenPage: true)       // the retired page still works; only navigation to it is gone
    layout()
    check("feed-separates-kinds", panel.feed.stack.arrangedSubviews.count == 4
          && (panel.feed.stack.arrangedSubviews[0] as? FeedEntryView)?.stamp.textColor == Theme.notice
          && (panel.feed.stack.arrangedSubviews[1] as? FeedEntryView)?.stamp.textColor == Theme.owner
          && (panel.feed.stack.arrangedSubviews[3] as? FeedEntryView)?.body.stringValue.hasPrefix("Задачи") == true)
    let corrected = FeedEntry(["id": "synthetic-1", "episode": "synthetic-episode", "revision": 3,
                               "at": feedStart + 7, "who": "Борис", "kind": "heard", "label": "РАСПОЗНАНО", "text": "Синтетическая реплика"])
    if let view = panel.feed.stack.arrangedSubviews[1] as? FeedEntryView {
        var bound = ""
        view.onCorrection = { entry, kind in bound = "\(entry.episode ?? ""):\(entry.revision):\(kind)" }
        view.update(corrected)
        let action = NSMenuItem(); action.representedObject = "label_other"; view.correctSelected(action)
        check("correction-binds-exact-episode-revision", !view.correctButton.isHidden && bound == "synthetic-episode:3:label_other")
    }
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
    // The retired page keeps only read-only status and the timeline: every control moved to «Голос и язык».
    func interactive(_ view: NSView) -> Bool { view is NSButton || view is NSSegmentedControl || (view as? NSTextField)?.isEditable == true }
    func controls(_ view: NSView) -> [NSView] { view is FeedEntryView ? [] : (interactive(view) ? [view] : []) + view.subviews.flatMap(controls) }
    check("hidden-dialogue-page-keeps-no-stranded-controls", controls(panel.pages[.dialogue]!).allSatisfy { $0 === panel.feed.eventsToggle })
    panel.select(.voice); layout()
    func inside(_ view: NSView, _ page: NSView) -> Bool { view.isDescendant(of: page) && !view.isHiddenOrHasHiddenAncestor }
    let voicePage = panel.pages[.voice]!
    check("device-controls-moved-to-voice-page", [panel.cameraChoice, panel.cameraSizeChoice, panel.microphoneChoice, panel.microphoneMeter, panel.debugImage, panel.dialogueLamp,
                                                  panel.recordingLamp, panel.voiceLamp, panel.cameraWorking, panel.microphoneWorking, panel.participantSummary,
                                                  panel.pause, panel.availability, panel.meetingStatus].allSatisfy { inside($0, voicePage) })
    func buttonTitles(_ view: NSView) -> [String] { ((view as? NSButton).map { [$0.title] } ?? []) + view.subviews.flatMap(buttonTitles) }
    check("device-and-rule-buttons-are-on-voice-page", ["Применить устройства", "Обновить устройства", "Эффекты камеры macOS…", "Правила…", "Отчёт за неделю…"].allSatisfy { buttonTitles(voicePage).contains($0) })
    let voiceRoot = panel.window.contentView!
    let devicesRect = panel.cameraChoice.convert(panel.cameraChoice.bounds, to: voiceRoot), pauseRect = panel.pause.convert(panel.pause.bounds, to: voiceRoot)
    let contentRect = panel.content.convert(panel.content.bounds, to: voiceRoot)
    check("voice-page-controls-are-inside-the-window", contentRect.contains(devicesRect) && contentRect.contains(pauseRect) && devicesRect.width > 250 && panel.debugImage.bounds.width > 250)
    let initial = geometry()
    check("preview-has-reserved-area", panel.debugImage.bounds.width > 250 && panel.debugImage.bounds.height > 140)
    check("single-main-window", panel.pages.count == Section.allCases.count && panel.window.title.hasPrefix("Butler"))
    for size in [NSSize(width: 1280, height: 720), NSSize(width: 300, height: 900), NSSize(width: 4000, height: 1000)] {
        panel.debugImage.image = syntheticImage(size); layout()
        snapshots.append(["image": "\(size)", "geometry": geometry()])
        check("preview-stable-\(Int(size.width))x\(Int(size.height))", geometry() == initial)
    }
    export("butler-voice-devices")
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

    panel.availability.selectedSegment = 2
    panel.changeAvailability()
    check("manual-free-control-uses-local-attention-command", panel.testCommands.last { $0.first == "ui-attention" } == ["ui-attention", "{\"mode\":\"free\"}"])
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
    check("sidebar-lists-every-section-except-the-retired-dialogue", Section.visible == Section.allCases.filter { $0 != .dialogue } && panel.navigation.numberOfRows == 9
          && !Section.visible.map { $0.title }.contains("Диалог"))
    panel.select(.dialogue); layout()
    check("retired-dialogue-opens-the-display-and-is-not-stored", panel.section == .display && panel.pages[.dialogue]?.isHidden == true && panel.pages[.display]?.isHidden == false
          && prefs.string(forKey: "section") == "display" && panel.navigation.selectedRow == Section.visible.firstIndex(of: .display))
    let legacyDomain = "com.boris.butler-ui.layout-legacy-" + UUID().uuidString
    let legacyPrefs = UserDefaults(suiteName: legacyDomain)!
    legacyPrefs.set("dialogue", forKey: "section")
    let legacy = Panel(cli: "/usr/bin/false", testing: true, defaults: legacyPrefs)
    check("stored-dialogue-section-opens-the-display", legacy.section == .display && legacyPrefs.string(forKey: "section") == "display")
    legacy.window.close(); legacyPrefs.removePersistentDomain(forName: legacyDomain)
    for section in Section.visible {
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
    // Card buttons: reply / link / reveal under every card kind. The panel names a button by card and action id only.
    func reply(_ id: String, _ label: String, used: Bool = false, chosen: Bool = false) -> [String: Any] {
        ["id": id, "label": label, "kind": "reply", "used": used, "chosen": chosen]
    }
    let longLabel = String(repeating: "Очень длинная подпись кнопки ", count: 3)
    func buttonCards(used: Bool = false, chosen: String? = nil) -> [[String: Any]] {
        [["id": "card-buttons", "at": feedStart + 50, "kind": "text", "title": "Очистка кэшей", "origin": "synthetic",
          "markdown": "Нашёл **12 ГБ** кэшей в 14 проектах. Что сделать?", "links": [],
          "actions": [reply("b0", "Да, почистить", used: used, chosen: chosen == "b0"), reply("b1", "Нет, оставить", used: used, chosen: chosen == "b1"),
                      ["id": "b2", "label": "Документация npm", "kind": "link", "hint": "docs.npmjs.com"],
                      ["id": "b3", "label": "Показать отчёт", "kind": "reveal", "hint": "~/projects/report.txt"],
                      ["id": "b4", "label": longLabel, "kind": "link", "hint": "example.com"]]],
         ["id": "card-buttons-diagram", "at": feedStart + 60, "kind": "diagram", "title": "Схема с кнопкой", "origin": "synthetic",
          "mermaid": "graph TD\n  A[Запрос] --> B[Ответ]", "actions": [reply("b0", "Разобрать подробнее")]],
         ["id": "card-withheld", "at": feedStart + 70, "kind": "image", "title": "Скрытое изображение", "origin": "synthetic", "unavailable": "paused",
          "actions": [reply("b0", "Показать")]]]
    }
    func showButtons(_ cards: [[String: Any]], raise: Int? = nil, current: String = "card-buttons") {
        panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic",
                           "display": ["cards": displayCards + cards, "current": current, "raise_serial": raise ?? 1]]
        panel.refresh(); layout()
    }
    showButtons(buttonCards(), raise: 2)
    let buttonsView = panel.displayDetail.subviews.first
    let found = actionButtons(panel.displayDetail)
    check("display-buttons-show-their-labels", found.map { $0.title } == ["Да, почистить", "Нет, оставить", "Документация npm", "Показать отчёт", longLabel])
    check("display-buttons-appear-under-a-text-card", panel.displayRenderedID == "card-buttons" && found.map { $0.actionID } == ["b0", "b1", "b2", "b3", "b4"]
          && found.map { $0.kind } == ["reply", "reply", "link", "reveal", "link"] && found.allSatisfy { $0.cardID == "card-buttons" && $0.isEnabled })
    check("display-buttons-say-what-they-do", found[0].toolTip?.contains("в чат") == true && found[2].toolTip?.contains("docs.npmjs.com") == true
          && found[3].toolTip?.contains("~/projects/report.txt") == true && found[4].toolTip?.contains(longLabel) == true)
    check("display-button-kinds-have-distinct-icons", Set(found.map { $0.image?.tiffRepresentation?.count ?? 0 }).count >= 3)
    check("display-long-label-is-cut-not-widened", found[4].frame.width <= panel.displayDetail.bounds.width && found[4].frame.maxX <= panel.displayDetail.bounds.maxX
          && panel.window.frame.size == displayWindowSize && found[4].title == longLabel)
    export("butler-display-buttons")
    let before = panel.testCommands.count
    panel.runDisplayAction(found[2])
    let linkCommand = panel.testCommands.dropFirst(before).last { $0.first == "ui-display-action" }
    let linkPayload = linkCommand.flatMap { $0.count > 1 ? $0[1].data(using: .utf8) : nil }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    check("display-click-sends-only-card-and-action-ids", linkPayload == ["id": "card-buttons", "action": "b2"] && found[2].isEnabled)
    panel.testDisplayAction = ["status": "Ответ отправлен: «Да, почистить».", "locked": true]
    panel.runDisplayAction(found[0])
    check("display-sent-reply-turns-off-every-reply-of-the-card", !found[0].isEnabled && !found[1].isEnabled && found[0].title.hasPrefix("✓ ") && !found[1].title.hasPrefix("✓")
          && found[2].isEnabled && found[3].isEnabled && panel.displayStatusLabel?.stringValue == "Ответ отправлен: «Да, почистить».")
    showButtons(buttonCards(used: true, chosen: "b1"))
    let after = actionButtons(panel.displayDetail)
    check("display-reply-state-comes-from-the-service-and-is-updated-in-place", panel.displayDetail.subviews.first === buttonsView && after.map { $0.actionID } == found.map { $0.actionID }
          && !after[0].isEnabled && !after[1].isEnabled && after[1].title == "✓ Нет, оставить" && after[0].title == "Да, почистить" && after[2].isEnabled)
    export("butler-display-buttons-answered")
    // A card that is already answered when the panel first draws it (relaunch, reopened history) must not flash live buttons.
    let answeredFirst: [String: Any] = ["id": "card-answered-first", "at": feedStart + 95, "kind": "text", "title": "Уже отвечено", "origin": "synthetic",
                                         "markdown": "Выбор сделан.", "links": [], "actions": [reply("b0", "Да", used: true, chosen: true), reply("b1", "Нет", used: true)]]
    showButtons(buttonCards(used: true, chosen: "b1") + [answeredFirst])
    if let index = panel.displayCards.firstIndex(where: { $0["id"] as? String == "card-answered-first" }) {
        panel.displayTable.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false); layout()
    }
    let firstPaint = actionButtons(panel.displayDetail)
    check("display-answered-card-has-no-live-buttons-on-first-paint", panel.displayRenderedID == "card-answered-first" && firstPaint.count == 2
          && firstPaint.allSatisfy { !$0.isEnabled } && firstPaint[0].title == "✓ Да")
    panel.displayTable.selectRowIndexes(IndexSet(integer: 6), byExtendingSelection: false); layout()
    let diagramButtons = actionButtons(panel.displayDetail)
    check("display-diagram-card-has-its-button", panel.displayRenderedID == "card-buttons-diagram" && diagramButtons.map { $0.actionID } == ["b0"] && diagramButtons[0].isEnabled)
    panel.testDisplayAction = ["status": "Ответ не отправлен: слишком много сообщений ждут ответа."]
    panel.runDisplayAction(diagramButtons[0])
    check("display-failed-reply-can-be-tried-again", diagramButtons[0].isEnabled && panel.displayStatusLabel?.stringValue == "Ответ не отправлен: слишком много сообщений ждут ответа.")
    panel.testDisplayAction = nil
    panel.runDisplayAction(diagramButtons[0])
    check("display-no-answer-from-the-service-is-reported-and-retryable", diagramButtons[0].isEnabled && panel.displayStatusLabel?.stringValue == "Действие не выполнено: нет ответа от Butler.")
    panel.testDisplayAction = ["status": "синтетически: действие принято"]
    panel.displayTable.selectRowIndexes(IndexSet(integer: 7), byExtendingSelection: false); layout()
    check("display-withheld-material-shows-no-buttons", panel.displayRenderedID == "card-withheld" && actionButtons(panel.displayDetail).isEmpty)
    panel.displayTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false); layout()
    check("display-card-without-actions-has-no-buttons", panel.displayRenderedID == "card-text" && actionButtons(panel.displayDetail).isEmpty)
    check("display-email-keeps-its-single-reply-action", { () -> Bool in
        panel.displayTable.selectRowIndexes(IndexSet(integer: 4), byExtendingSelection: false); layout()
        let email = actionButtons(panel.displayDetail)
        return email.map { $0.actionID } == ["open-reply-in-mail"] && email[0].title == "Открыть ответ в Mail.app" && email[0].isEnabled }())
    // Live canvas: the agent redraws a card in place (same id, higher rev). The panel follows it without moving the owner.
    func canvasCard(rev: Int, source: String) -> [String: Any] {
        ["id": "card-canvas", "at": feedStart + 80, "kind": "diagram", "title": "Архитектура", "origin": "synthetic", "mermaid": source, "rev": rev,
         "actions": [reply("b0", "Разобрать подробнее")]]
    }
    showButtons([canvasCard(rev: 0, source: "graph TD\n  A --> B")], raise: 3, current: "card-canvas")
    let canvasView = panel.displayDetail.subviews.first
    let canvasRow = panel.displayTable.selectedRow
    check("display-new-canvas-card-is-shown-and-raises-the-page", panel.section == .display && panel.displayRenderedID == "card-canvas" && panel.displayRenderedRev == 0)
    func historyRowText(_ row: Int) -> String { (panel.displayTable.view(atColumn: 0, row: row, makeIfNecessary: true) as? NSTextField)?.stringValue ?? "" }
    let canvasRowBefore = historyRowText(canvasRow)
    panel.select(.voice)
    showButtons([canvasCard(rev: 1, source: "graph TD\n  A --> B --> C")], raise: 3, current: "card-canvas")
    check("display-redraw-is-followed-in-place", panel.displayRenderedID == "card-canvas" && panel.displayRenderedRev == 1
          && panel.displayDetail.subviews.first !== canvasView && panel.displayTable.selectedRow == canvasRow && panel.displayTable.numberOfRows == 6)
    check("display-quiet-redraw-leaves-the-owner-where-they-are", panel.section == .voice)
    check("display-redrawn-row-is-marked", panel.displayRowTitle(canvasCard(rev: 1, source: "")).hasSuffix(" ↻") && !panel.displayRowTitle(canvasCard(rev: 0, source: "")).contains("↻"))
    check("display-history-row-refreshes-after-a-redraw", !canvasRowBefore.isEmpty && !canvasRowBefore.contains("↻") && historyRowText(canvasRow).hasSuffix(" ↻"))
    let redrawn = panel.displayDetail.subviews.first
    showButtons([canvasCard(rev: 1, source: "graph TD\n  A --> B --> C")], raise: 3, current: "card-canvas")
    check("display-same-revision-is-not-rebuilt", panel.displayDetail.subviews.first === redrawn)
    // Captions under a picture or a diagram, and zoom for big diagrams.
    let longCaption = String(repeating: "Длинная подпись схемы. ", count: 90)
    let captioned: [[String: Any]] = [
        ["id": "card-cap-diagram", "at": feedStart + 90, "kind": "diagram", "title": "Схема с подписью", "origin": "synthetic", "mermaid": "graph TD\n  A --> B",
         "markdown": "Подпись **схемы**: вход идёт в ядро."],
        ["id": "card-cap-image", "at": feedStart + 91, "kind": "image", "title": "Рисунок с подписью", "origin": "synthetic", "image": imagePath.path, "markdown": "Подпись рисунка"],
        ["id": "card-cap-long", "at": feedStart + 92, "kind": "diagram", "title": "Длинная подпись", "origin": "synthetic", "mermaid": "graph TD\n  A --> B", "markdown": longCaption],
        ["id": "card-cap-none", "at": feedStart + 93, "kind": "diagram", "title": "Без подписи", "origin": "synthetic", "mermaid": "graph TD\n  A --> B"],
        ["id": "card-cap-empty", "at": feedStart + 94, "kind": "diagram", "title": "Пустая подпись", "origin": "synthetic", "mermaid": "graph TD\n  A --> B", "markdown": ""]]
    showButtons(captioned, raise: 3, current: "card-cap-diagram")
    func openCard(_ id: String) {
        if let index = panel.displayCards.firstIndex(where: { $0["id"] as? String == id }) {
            panel.displayTable.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false); layout()
        }
    }
    check("display-diagram-shows-its-caption", panel.displayRenderedID == "card-cap-diagram" && textViews(panel.displayDetail).contains { $0.string.contains("Подпись схемы: вход идёт в ядро.") })
    check("display-caption-markdown-is-rendered-not-shown-raw", textViews(panel.displayDetail).allSatisfy { !$0.string.contains("**") })
    check("display-diagram-can-be-zoomed", panel.diagramWeb?.allowsMagnification == true)
    export("butler-display-caption")
    openCard("card-cap-image")
    check("display-image-shows-its-caption", panel.displayRenderedID == "card-cap-image" && textViews(panel.displayDetail).contains { $0.string.contains("Подпись рисунка") })
    openCard("card-cap-long")
    let captionBoxes = textViews(panel.displayDetail).compactMap { $0.enclosingScrollView }
    check("display-long-caption-scrolls-inside-a-bounded-box", captionBoxes.count == 1 && captionBoxes[0].frame.height <= 100 && panel.window.frame.size == displayWindowSize)
    openCard("card-cap-none")
    check("display-card-without-caption-has-no-text-box", panel.displayRenderedID == "card-cap-none" && textViews(panel.displayDetail).isEmpty)
    openCard("card-cap-empty")
    check("display-empty-caption-draws-no-text-box", panel.displayRenderedID == "card-cap-empty" && textViews(panel.displayDetail).isEmpty)
    panel.diagramWeb?.magnification = 2
    openCard("card-cap-diagram")
    check("display-zoom-starts-over-for-another-card", panel.diagramWeb?.magnification == 1)

    // Every template the agent is given must parse and draw in the real bundled Mermaid, inside this WKWebView.
    func spin(_ timeout: TimeInterval, until done: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while !done() && Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.02)) }
        return done()
    }
    func renderInPage(_ web: WKWebView, _ source: String) -> [String: Any]? {
        var outcome: [String: Any]?
        var finished = false
        web.callAsyncJavaScript("""
            const status = await renderDiagram(source);
            const svg = document.querySelector('#diagram svg');
            const box = svg ? svg.getBoundingClientRect() : null;
            return { status: status, width: box ? box.width : 0, height: box ? box.height : 0,
                     text: svg ? svg.textContent.length : 0, syntaxError: svg ? svg.textContent.includes('Syntax error') : false,
                     errorShown: document.getElementById('error').style.display !== 'none',
                     errorText: document.getElementById('error').style.display !== 'none' ? document.getElementById('error').textContent : '',
                     sourceShown: document.getElementById('source').style.display !== 'none' ? document.getElementById('source').textContent : '' };
            """, arguments: ["source": source], in: nil, in: .page) { result in
            if case .success(let value) = result { outcome = value as? [String: Any] }
            finished = true
        }
        _ = spin(20) { finished }
        return outcome
    }
    func snapshotPage(_ web: WKWebView, _ name: String) {
        _ = spin(0.3) { false }
        var done = false
        web.takeSnapshot(with: nil) { image, _ in
            if let tiff = image?.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
               let png = rep.representation(using: .png, properties: [:]) { try? png.write(to: directory.appendingPathComponent(name + ".png")) }
            done = true
        }
        _ = spin(10) { done }
    }
    let templatesData = Bundle.main.url(forResource: "diagram_templates", withExtension: "json").flatMap { try? Data(contentsOf: $0) }
    let templates = templatesData.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [[String: Any]] ?? []
    check("display-templates-ship-in-the-bundle", templates.count >= 10 && templates.allSatisfy { ($0["id"] as? String) != nil && ($0["mermaid"] as? String) != nil })
    openCard("card-cap-diagram")
    check("display-diagram-page-loads", spin(20) { panel.diagramReady })
    var templateReport: [[String: Any]] = []
    if let web = panel.diagramWeb, panel.diagramReady {
        for template in templates {
            let id = template["id"] as? String ?? "?"
            let got = renderInPage(web, template["mermaid"] as? String ?? "")
            let good = got?["status"] as? String == "ok" && (got?["width"] as? Double ?? 0) > 80 && (got?["height"] as? Double ?? 0) > 40
                && (got?["text"] as? Int ?? 0) > 5 && got?["syntaxError"] as? Bool == false && got?["errorShown"] as? Bool == false
            check("display-template-\(id)-renders", good)
            templateReport.append(["id": id, "ok": good, "width": got?["width"] ?? 0, "height": got?["height"] ?? 0, "error": got?["errorText"] ?? "no answer"])
            snapshotPage(web, "butler-template-\(id)")
        }
        // Negative controls: the check above must be able to fail, and a bad diagram must fall back to a visible message plus its source.
        let broken = renderInPage(web, "flowchart LR\n  A --> ")
        check("display-broken-diagram-falls-back-to-message-and-source", broken?["status"] as? String == "error" && broken?["errorShown"] as? Bool == true
              && (broken?["sourceShown"] as? String ?? "").contains("A -->"))
        let unsupported = renderInPage(web, "architecture-beta\n  group api(cloud)[API]\n")
        check("display-unsupported-diagram-type-is-an-error-not-a-blank", unsupported?["status"] as? String == "error" && unsupported?["errorShown"] as? Bool == true)
        if let data = try? JSONSerialization.data(withJSONObject: templateReport, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: directory.appendingPathComponent("templates-report.json"))
        }
        let again = renderInPage(web, "flowchart LR\n  A --> B")
        check("display-page-recovers-after-a-bad-diagram", again?["status"] as? String == "ok" && again?["errorShown"] as? Bool == false)
    }
    // Drawings (svg cards): shown by a page that runs no JavaScript, under a Content-Security-Policy that allows nothing.
    func evaluate(_ web: WKWebView, _ script: String) -> Any? {
        var result: Any?
        var done = false
        web.evaluateJavaScript(script) { value, _ in result = value; done = true }
        _ = spin(10) { done }
        return result
    }
    func textFields(_ view: NSView) -> [NSTextField] { ((view as? NSTextField).map { [$0] } ?? []) + view.subviews.flatMap(textFields) }
    let goodDrawing = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 200\"><rect x=\"10\" y=\"10\" width=\"120\" height=\"60\" fill=\"#143d2a\" stroke=\"#7fbf92\"/>"
        + "<circle cx=\"250\" cy=\"100\" r=\"40\" fill=\"#14263d\"/><text x=\"20\" y=\"45\">Ядро</text><text x=\"215\" y=\"105\" fill=\"#d6e8ff\">Модель</text></svg>"
    // A loopback listener inside this test process: it can tell whether the page tried to fetch something. Nothing outside the process is touched.
    func listenOnLoopback() -> (descriptor: Int32, port: UInt16)? {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return nil }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let size = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(descriptor, $0, size) } }
        var length = size
        let named = withUnsafeMutablePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(descriptor, $0, &length) } }
        guard bound == 0, named == 0, listen(descriptor, 8) == 0 else { close(descriptor); return nil }
        _ = fcntl(descriptor, F_SETFL, O_NONBLOCK)
        return (descriptor, UInt16(bigEndian: address.sin_port))
    }
    // True when a client SENT something (an HTTP request). A bare connection is not enough: WebKit may open a socket ahead of a navigation
    // (preconnect) and then cancel it without sending a byte.
    func requestArrived(_ descriptor: Int32, within seconds: TimeInterval) -> Bool {
        var clients: [Int32] = []
        defer { clients.forEach { close($0) } }
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            let client = accept(descriptor, nil, nil)
            if client >= 0 { _ = fcntl(client, F_SETFL, O_NONBLOCK); clients.append(client) }
            for client in clients {
                var buffer = [UInt8](repeating: 0, count: 64)
                if read(client, &buffer, buffer.count) > 0 { return true }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        return false
    }
    // Every connection the listener has queued, counted and closed over a short window (a page may open several).
    func drainConnections(_ descriptor: Int32, for seconds: TimeInterval) -> Int {
        var count = 0
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            let client = accept(descriptor, nil, nil)
            if client >= 0 { close(client); count += 1 } else { RunLoop.current.run(until: Date().addingTimeInterval(0.02)) }
        }
        return count
    }
    let probe = listenOnLoopback()
    check("display-svg-probe-listener-is-up", probe != nil)
    let probePort = probe?.port ?? 9
    // What a hostile page would do if the service-side cleaning ever failed: run script, run a handler, fetch an address.
    let hostileDrawing = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 100\" onload=\"document.documentElement.setAttribute('data-ran','onload')\">"
        + "<script>document.documentElement.setAttribute('data-ran','script')</script><rect width=\"100\" height=\"100\" fill=\"#143d2a\" onclick=\"1\"/>"
        + "<image href=\"http://127.0.0.1:\(probePort)/hit\" width=\"10\" height=\"10\"/></svg>"
    // The very drawing the agent's playbook shows (shipped in the bundle, cleaned by the same sanitizer in the Python tests).
    let exampleDrawing = Bundle.main.url(forResource: "drawing_example", withExtension: "svg").flatMap { try? String(contentsOf: $0, encoding: .utf8) } ?? ""
    check("display-svg-playbook-example-ships-in-the-bundle", exampleDrawing.contains("<marker") && exampleDrawing.contains("Агент"))
    let drawings: [[String: Any]] = [
        ["id": "card-svg", "at": feedStart + 100, "kind": "svg", "title": "Рисунок ядра", "origin": "synthetic", "svg": goodDrawing,
         "markdown": "Подпись рисунка", "actions": [reply("b0", "Разобрать ядро")]],
        ["id": "card-svg-hostile", "at": feedStart + 101, "kind": "svg", "title": "Враждебный", "origin": "synthetic", "svg": hostileDrawing],
        ["id": "card-svg-empty", "at": feedStart + 102, "kind": "svg", "title": "Пустой", "origin": "synthetic", "svg": ""],
        ["id": "card-svg-example", "at": feedStart + 103, "kind": "svg", "title": "Пример из инструкции", "origin": "synthetic", "svg": exampleDrawing]]
    showButtons(drawings, raise: 4, current: "card-svg")
    func openDrawing(_ id: String) -> WKWebView? {
        let before = panel.svgLoads
        if let index = panel.displayCards.firstIndex(where: { $0["id"] as? String == id }) {
            panel.displayTable.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false); layout()
        }
        guard let web = panel.svgWeb, spin(20, until: { panel.svgLoads > before }) else { return nil }
        return web
    }
    if let web = openDrawing("card-svg") {
        check("display-svg-card-renders-its-drawing", panel.displayRenderedID == "card-svg"
              && (evaluate(web, "document.querySelectorAll('svg rect, svg circle, svg text').length") as? Int) == 4
              && ((evaluate(web, "document.querySelector('svg').getBoundingClientRect().width") as? Double) ?? 0) > 100)
        check("display-svg-card-has-caption-and-button", textViews(panel.displayDetail).contains { $0.string.contains("Подпись рисунка") }
              && actionButtons(panel.displayDetail).map { $0.actionID } == ["b0"])
        check("display-svg-can-be-zoomed", web.allowsMagnification)
        check("display-svg-view-keeps-nothing-on-disk", !web.configuration.websiteDataStore.isPersistent)
        check("display-svg-default-colours-are-readable-on-the-console", (evaluate(web, "getComputedStyle(document.querySelector('svg text')).fill") as? String) == "rgb(214, 255, 224)"
              && (evaluate(web, "getComputedStyle(document.querySelectorAll('svg text')[1]).fill") as? String) == "rgb(214, 232, 255)")
        check("display-svg-page-carries-a-strict-policy", (evaluate(web, "document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]').content") as? String) == Panel.svgPolicy
              && Panel.svgPolicy.hasPrefix("default-src 'none'") && !Panel.svgPolicy.contains("script-src"))
        snapshotPage(web, "butler-display-svg")
    } else { check("display-svg-card-renders-its-drawing", false) }
    let permissivePolicy = "default-src * 'unsafe-inline' 'unsafe-eval'"
    let permissivePage = Panel.svgPage(hostileDrawing).replacingOccurrences(of: Panel.svgPolicy, with: permissivePolicy)
    // A. The payload is real: an unrestricted web view runs it, so "did not run" below is an observation, not an absence of markup.
    let free = WKWebView(frame: NSRect(x: 0, y: 0, width: 200, height: 200), configuration: WKWebViewConfiguration())
    free.loadHTMLString(permissivePage, baseURL: nil)
    _ = spin(20) { (evaluate(free, "document.readyState") as? String) == "complete" }
    check("display-svg-control-hostile-payload-runs-in-an-unrestricted-view", (evaluate(free, "document.documentElement.getAttribute('data-ran')") as? String) != nil)
    // B. Layer: JavaScript off. The drawing's own view with the policy loosened still runs nothing, and the app can still script the page.
    if let web = panel.svgWeb ?? Optional(panel.svgWebView()) {
        let before = panel.svgLoads
        web.loadHTMLString(permissivePage, baseURL: nil)
        _ = spin(20) { panel.svgLoads > before }
        let ran = evaluate(web, "document.documentElement.getAttribute('data-ran')")
        let native = evaluate(web, "document.documentElement.setAttribute('data-native','1'); document.documentElement.getAttribute('data-native')") as? String
        check("display-svg-without-javascript-the-hostile-script-does-not-run", ran == nil || ran is NSNull)
        check("display-svg-control-the-app-can-still-script-the-page", native == "1")
        // C. Layer: the policy. With it loosened the page reaches for the address; with the real policy it never does.
        check("display-svg-control-a-loosened-policy-lets-the-page-fetch", probe.map { requestArrived($0.descriptor, within: 3) } == true)
        if let probe { _ = drainConnections(probe.descriptor, for: 1.5) }          // the control's own extra connections must not count against the real run
    }
    // D. The real path: a hostile drawing selected as a card runs nothing and fetches nothing.
    if let web = openDrawing("card-svg-example") {
        check("display-svg-playbook-example-renders", panel.displayRenderedID == "card-svg-example"
              && (evaluate(web, "document.querySelectorAll('svg rect, svg circle, svg text').length") as? Int) == 9
              && ((evaluate(web, "document.querySelector('svg').getBoundingClientRect().height") as? Double) ?? 0) > 60
              && (evaluate(web, "document.querySelector('svg marker path') !== null") as? Bool) == true)
        snapshotPage(web, "butler-display-svg-example")
    } else { check("display-svg-playbook-example-renders", false) }
    if let web = openDrawing("card-svg-hostile") {
        let ran = evaluate(web, "document.documentElement.getAttribute('data-ran')")
        check("display-svg-hostile-card-runs-nothing", panel.displayRenderedID == "card-svg-hostile" && (ran == nil || ran is NSNull))
        check("display-svg-policy-stops-every-fetch", probe.map { !requestArrived($0.descriptor, within: 1.5) } == true
              && (evaluate(web, "document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]').content") as? String) == Panel.svgPolicy)
        // The view itself refuses to be sent elsewhere (the delegate, not only the pure function). A listener of its own, known only to this
        // navigation, so that nothing else the earlier pages did can be mistaken for it; an unrestricted view shows the listener does see a visit.
        if let navigation = listenOnLoopback(), let url = URL(string: "http://127.0.0.1:\(navigation.port)/navigate") {
            free.load(URLRequest(url: url))
            check("display-svg-control-an-unrestricted-view-does-navigate", requestArrived(navigation.descriptor, within: 3))
            _ = drainConnections(navigation.descriptor, for: 1)
            web.load(URLRequest(url: url))
            check("display-svg-view-refuses-to-navigate-away", !requestArrived(navigation.descriptor, within: 1.5))
            close(navigation.descriptor)
        } else { check("display-svg-view-refuses-to-navigate-away", false) }
    } else { check("display-svg-hostile-card-runs-nothing", false) }
    if let probe { close(probe.descriptor) }
    // Drawings that are only WORDS about markup (cleaned by the service, copied from the sanitizer's own corpus): the real HTML parser must keep them
    // as text inside the drawing, never turn them into elements. One element in the page per element of the drawing, and none of the dangerous kinds.
    let wordsOnly: [(String, Int)] = [
        ("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 200\"><text>&lt;/text&gt;&lt;script&gt;alert(1)&lt;/script&gt;&lt;text&gt;</text></svg>", 2),
        ("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 200\"><title>&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;</title></svg>", 2),
        ("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 200\"><desc>&lt;script&gt;alert(1)&lt;/script&gt;</desc></svg>", 2),
        ("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 200\"><g><title>&lt;/title&gt;&lt;img src=x onerror=alert(1)&gt;</title></g></svg>", 3),
        ("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 200\"><text>&lt;script&gt;alert(1)&lt;/script&gt;</text></svg>", 2),
        ("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 200\"><rect id=\"a\" /><rect aria-label=\"&lt;img src=x onerror=alert(1)&gt;\" /></svg>", 3),
        ("<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 400 200\" />", 1)]
    var parsedAsText = true
    for (index, item) in wordsOnly.enumerated() {
        let words: [String: Any] = ["id": "card-svg-words-\(index)", "at": feedStart + 200 + Double(index), "kind": "svg", "title": "Слова", "origin": "synthetic", "svg": item.0]
        showButtons(drawings + [words], raise: 5, current: "card-svg")
        guard let web = openDrawing("card-svg-words-\(index)") else { parsedAsText = false; continue }
        let elements = evaluate(web, "document.querySelectorAll('body svg, body svg *').length") as? Int
        let dangerous = evaluate(web, "document.querySelectorAll('svg script, svg img, svg iframe, svg style, svg foreignObject, svg a, svg image, svg use, svg [onload], svg [onerror], svg [onclick]').length") as? Int
        if elements != item.1 || dangerous != 0 { parsedAsText = false }
    }
    check("display-svg-markup-words-stay-text-in-the-real-html-parser", parsedAsText && wordsOnly.count == 7)
    // Control for the check above: real markup of a kind the parser keeps (an image element) IS seen by the same query. Script and handler
    // attributes are dropped by WebKit's parser when JavaScript is off, so they are covered by the "did not run" checks, not by this one.
    let markupDrawing = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 100\"><image href=\"about:blank\" width=\"1\" height=\"1\"/></svg>"
    showButtons(drawings + [["id": "card-svg-markup", "at": feedStart + 300, "kind": "svg", "title": "Разметка", "origin": "synthetic", "svg": markupDrawing]], raise: 6, current: "card-svg")
    if let web = openDrawing("card-svg-markup") {
        check("display-svg-control-the-parser-query-sees-real-markup", (evaluate(web, "document.querySelectorAll('svg script, svg img, svg iframe, svg style, svg foreignObject, svg a, svg image, svg use').length") as? Int) == 1)
    } else { check("display-svg-control-the-parser-query-sees-real-markup", false) }
    check("display-svg-navigation-is-limited-to-its-own-page-load", Panel.svgNavigationAllowed(url: URL(string: "about:blank"), type: .other)
          && Panel.svgNavigationAllowed(url: nil, type: .other)
          && !Panel.svgNavigationAllowed(url: URL(string: "https://example.com/"), type: .other)
          && !Panel.svgNavigationAllowed(url: URL(string: "file:///etc/passwd"), type: .other)
          && !Panel.svgNavigationAllowed(url: URL(string: "about:blank"), type: .linkActivated)
          && !Panel.svgNavigationAllowed(url: URL(string: "about:blank"), type: .formSubmitted))
    panel.svgWeb?.magnification = 2.5
    _ = openDrawing("card-svg")
    check("display-svg-zoom-starts-over-for-another-card", panel.svgWeb?.magnification == 1)
    openCard("card-svg-empty")
    check("display-svg-without-a-drawing-explains-itself", panel.displayRenderedID == "card-svg-empty" && textFields(panel.displayDetail).contains { $0.stringValue == "Рисунок недоступен." })
    check("display-svg-row-is-labelled-as-a-drawing", panel.displayRowTitle(drawings[0]).contains("рисунок"))

    panel.testState = ["enabled": true, "display": ["cards": [], "raise_serial": 1, "hidden": "private"]]
    panel.refresh(); layout()
    check("display-private-hides-cards", panel.displayTable.numberOfRows == 0 && panel.displayRenderedID == nil && panel.displayDetail.subviews.count == 1)
    // Written conversation on the Display page: thread, composer, send and dictation. Synthetic rows; no service.
    let chatStart = feedStart + 100
    func chatRow(_ id: String, _ from: String, _ state: String, _ text: String, error: String? = nil) -> [String: Any] {
        var row: [String: Any] = ["id": id, "at": chatStart, "turn": id.split(separator: ".").first.map(String.init) ?? id, "state": state,
                                  "who": from == "owner" ? "Борис" : "Butler", "from": from, "text": text]
        if let error { row["error"] = error }
        return row
    }
    let chatRows: [[String: Any]] = [
        chatRow("t1", "owner", "sent", "Какие у меня задачи на сегодня?"),
        chatRow("t1.r", "butler", "answered", "# Сегодня\n\n- Проверить **расписание**\n- Ответить клиенту\n\nПодробнее [на сайте](https://familybuilder.io)."),
        chatRow("t2", "owner", "sent", "Напомни про звонок в 15:00"),
        chatRow("t2.r", "butler", "streaming", "Хорошо, ставлю напоминание на 15:00"),
        chatRow("t3", "owner", "sent", "Что с деплоем?"),
        chatRow("t3.r", "butler", "failed", "", error: "шлюз OpenClaw не отвечает: проверьте, что он запущен"),
        chatRow("t4", "owner", "sent", "Привет всем"),
        chatRow("t4.r", "butler", "silent", "Butler решил не отвечать на это сообщение.")]
    panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic",
                       "display": ["cards": displayCards, "current": "card-text", "raise_serial": 1],
                       "panel_chat": ["entries": chatRows, "busy": true]]
    panel.select(.display); panel.refresh(); layout(); export("butler-display-conversation")
    func entryViews(_ view: NSView) -> [FeedEntryView] { ((view as? FeedEntryView).map { [$0] } ?? []) + view.subviews.flatMap(entryViews) }
    let threadViews = entryViews(panel.chatFeed)
    func threadRow(_ id: String) -> FeedEntryView? { threadViews.first { $0.entry.id == "chat:" + id } }
    check("chat-thread-shows-every-row", panel.chatFeed.visibleEntries.count == 8 && threadViews.count == 8)
    check("chat-owner-and-butler-rows-are-told-apart", threadRow("t1")?.entry.kind == "heard" && threadRow("t1")?.entry.who == "Борис"
          && threadRow("t1.r")?.entry.kind == "reply" && threadRow("t1.r")?.entry.who == "Butler")
    check("chat-answer-is-rendered-as-markdown", threadRow("t1.r").map { view in
        let shown = view.body.attributedStringValue.string
        var link = false
        view.body.attributedStringValue.enumerateAttribute(.link, in: NSRange(location: 0, length: view.body.attributedStringValue.length)) { value, _, _ in
            if (value as? URL)?.host == "familybuilder.io" { link = true } }
        return shown.contains("Проверить расписание") && !shown.contains("**") && !shown.contains("# ") && link } == true)
    check("chat-streaming-and-failed-and-silent-rows-are-labelled", threadRow("t2.r")?.stamp.stringValue.contains("ПИШЕТ") == true
          && threadRow("t3.r")?.stamp.stringValue.contains("ОТВЕТ НЕ ПОЛУЧЕН") == true && threadRow("t3.r")?.body.stringValue.contains("не отвечает") == true
          && threadRow("t4.r")?.stamp.stringValue.contains("БЕЗ ОТВЕТА") == true)
    let composerRect = panel.composer.convert(panel.composer.bounds, to: nil), detailRect = panel.displayDetail.convert(panel.displayDetail.bounds, to: nil)
    let threadRect = panel.chatFeed.convert(panel.chatFeed.bounds, to: nil), sendRect = panel.sendButton.convert(panel.sendButton.bounds, to: nil)
    check("display-composer-sits-below-the-cards", composerRect.maxY <= detailRect.minY + 1 && composerRect.maxY <= threadRect.minY + 1 && composerRect.width > 300
          && sendRect.maxX <= panel.content.convert(panel.content.bounds, to: nil).maxX && threadRect.height >= 160 && detailRect.height >= 200)
    check("display-history-list-stays-under-the-thread", panel.displayTable.numberOfRows == 5 && panel.displayRenderedID == "card-text")
    let historyRect = panel.displayTable.enclosingScrollView!.convert(panel.displayTable.enclosingScrollView!.bounds, to: nil)
    check("display-history-list-does-not-overlap-the-thread-and-shows-three-rows", historyRect.maxY <= threadRect.minY + 1
          && panel.displayTable.enclosingScrollView!.contentView.bounds.height / panel.displayTable.rowHeight >= 3 && panel.displayTable.headerView == nil)
    check("chat-stamp-labels-are-not-truncated", ["t1", "t1.r", "t2.r", "t3.r", "t4.r"].allSatisfy { id in
        threadRow(id).map { $0.stamp.frame.width + 1 >= $0.stamp.intrinsicContentSize.width } ?? false })

    panel.composer.string = "  Новый вопрос  \n"
    panel.sendChat(); layout()
    let sentCommand = panel.testCommands.last { $0.first == "ui-send-text" }
    let sentPayload = sentCommand.flatMap { $0.count > 1 ? $0[1].data(using: .utf8) : nil }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    check("chat-send-sends-the-trimmed-text-and-clears-the-field", sentPayload == ["text": "Новый вопрос"] && panel.composer.string.isEmpty && panel.sendButton.isEnabled)
    let commandsBefore = panel.testCommands.count
    panel.composer.string = "   \n "; panel.sendChat()
    check("chat-empty-message-is-not-sent", panel.testCommands.count == commandsBefore)
    panel.testSendResult = ["queued": false, "message": "сообщение длиннее 4000 символов"]
    panel.composer.string = "Длинный текст"; panel.sendChat()
    check("chat-refused-message-stays-in-the-field-with-the-reason", panel.composer.string == "Длинный текст" && panel.chatStatus.stringValue.contains("4000")
          && panel.sendButton.isEnabled)
    panel.testSendResult = nil
    panel.sendChat()
    check("chat-no-connection-keeps-the-text-and-says-so", panel.composer.string == "Длинный текст" && panel.chatStatus.stringValue.contains("нет связи"))
    panel.testSendResult = ["queued": true, "id": "synthetic"]
    panel.composer.string = ""; panel.chatStatus.stringValue = ""

    func key(_ code: UInt16, _ modifiers: NSEvent.ModifierFlags = []) -> NSEvent {
        NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: modifiers, timestamp: 0, windowNumber: panel.window.windowNumber, context: nil,
                         characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: code)!
    }
    var returns = 0
    panel.composer.onSend = { returns += 1 }
    panel.composer.string = "a"; panel.composer.setSelectedRange(NSRange(location: 1, length: 0))
    panel.composer.keyDown(with: key(36))
    check("composer-return-sends-and-adds-no-line", returns == 1 && panel.composer.string == "a")
    panel.composer.keyDown(with: key(76))
    check("composer-keypad-enter-sends-too", returns == 2 && panel.composer.string == "a")
    panel.composer.keyDown(with: key(36, .shift))
    check("composer-shift-return-starts-a-new-line", returns == 2 && panel.composer.string == "a\n")
    panel.composer.keyDown(with: key(36, .option))
    check("composer-option-return-starts-a-new-line", returns == 2 && panel.composer.string == "a\n\n")
    panel.composer.onSend = { [weak panel] in panel?.sendChat() }
    panel.composer.string = ""

    // Dictation: the service listens; the panel only starts, stops and receives words.
    func lastDictation() -> [String: String]? {
        panel.testCommands.last { $0.first == "ui-dictate" }.flatMap { $0.count > 1 ? $0[1].data(using: .utf8) : nil }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    }
    panel.composer.string = "Привет,"
    panel.testDictation = ["state": "listening", "id": "d1", "text": "", "partial": ""]
    panel.toggleDictation()
    check("dictation-start-is-requested-and-shown", lastDictation() == ["action": "start"] && panel.dictationActive && panel.dictateButton.title == "Остановить"
          && panel.chatStatus.stringValue.contains("Слушаю"))
    panel.testState?["dictation"] = ["state": "listening", "id": "d1", "text": "", "partial": "это черновик"]
    panel.refresh()
    check("dictation-shows-what-is-being-heard", panel.chatStatus.stringValue.contains("это черновик") && panel.composer.string == "Привет,")
    panel.composer.string = "Привет, друг"                                         // the owner typed while it listened
    panel.testState?["dictation"] = ["state": "done", "id": "d1", "text": "это диктовка", "partial": ""]
    panel.refresh()
    check("dictated-text-is-appended-to-what-is-in-the-field-now", panel.composer.string == "Привет, друг это диктовка" && !panel.dictationActive
          && panel.dictateButton.title == "Диктовать" && panel.chatStatus.stringValue.contains("Проверьте"))
    panel.refresh()
    check("dictated-text-is-inserted-only-once", panel.composer.string == "Привет, друг это диктовка")
    check("dictation-never-sends-by-itself", !panel.testCommands.contains { $0.first == "ui-send-text" && ($0.last ?? "").contains("диктовка") })
    panel.composer.string = ""
    panel.testDictation = ["state": "listening", "id": "d2", "text": "", "partial": ""]
    panel.toggleDictation()
    panel.testDictation = ["state": "done", "id": "d2", "text": "второй", "partial": ""]
    panel.toggleDictation()
    check("dictation-stop-is-requested-and-delivers-the-words", lastDictation() == ["action": "stop"] && panel.composer.string == "второй" && !panel.dictationActive)
    panel.testDictation = ["state": "failed", "id": "d3", "reason": "Наблюдение на паузе: нажмите «Продолжить», чтобы включить микрофон."]
    panel.toggleDictation()
    check("dictation-refusal-is-explained-and-leaves-the-field", panel.chatStatus.stringValue.contains("паузе") && panel.composer.string == "второй" && !panel.dictationActive)
    panel.testDictation = ["state": "done", "id": "d4", "text": "", "partial": "", "note": "Ничего не распознано. Проверьте уровень микрофона и повторите."]
    panel.toggleDictation()
    check("dictation-with-no-words-says-so-and-changes-nothing", panel.chatStatus.stringValue.contains("Ничего не распознано") && panel.composer.string == "второй")
    panel.testDictation = nil
    panel.toggleDictation()
    check("dictation-without-a-service-says-so", panel.chatStatus.stringValue.contains("нет связи") && panel.dictateButton.isEnabled)
    panel.testDictation = ["state": "listening", "id": "d1", "text": "", "partial": ""]
    panel.composer.string = ""; panel.chatStatus.stringValue = ""
    panel.testState?["dictation"] = nil
    panel.testState?["panel_chat"] = ["entries": [], "busy": false]
    panel.refresh(); layout()
    check("chat-cleared-thread-shows-the-hint", panel.chatFeed.visibleEntries.isEmpty && !panel.chatFeed.placeholder.isHidden
          && panel.chatFeed.placeholder.stringValue.contains("Telegram"))
    panel.clearChat()
    check("chat-clear-is-requested", panel.testCommands.contains { $0 == ["ui-panel-chat-clear"] })
    export("butler-display-conversation-empty")

    // Recordings REQ-001…008: synthetic local data, no service or device access.
    let recordings = panel.recordingsPane!
    let recordedAt = 1793134800.0
    let recordingChild: [String: Any] = ["type": "observation", "id": "recording-o1", "source": "camera", "start": recordedAt, "end": recordedAt + 10,
        "facts": [["kind": "activity", "detail": "Работает за столом", "topic": "code", "confidence": 0.8, "basis": "model-inference"]],
        "transcripts": [["text": "Синтетический локальный транскрипт"]], "original": true,
        "media": [["id": "recording-m1", "source": "camera", "path": imagePath.path, "start": recordedAt, "complete": true, "preserved": false],
                  ["id": "recording-m2", "source": "microphone", "start": recordedAt, "complete": true]]]
    let recordingSession: [String: Any] = ["type": "session", "id": "session:recording-o1", "source": "camera", "start": recordedAt, "end": recordedAt + 70,
        "activity": "Работа", "count": 2, "content": ["факты", "текст", "медиа"], "observations": [recordingChild, ["type": "observation", "id": "recording-o2", "source": "camera", "start": recordedAt+60, "end": recordedAt+70, "facts": [], "media": []]]]
    let recordingGap: [String: Any] = ["type": "gap", "start": recordedAt-60, "end": recordedAt, "source": "all", "reason": "service-stopped", "label": "перезапуск"]
    panel.testReplies["ui-recordings"] = ["hidden": false, "day": "2026-10-27", "days": [["day": "2026-10-27"]], "items": [recordingSession, recordingGap], "total": 2, "meeting": ["detected": true, "marked": false]]
    recordings.updatePermission(["recordings_allowed": true]); panel.select(.recordings); recordings.reload(); layout()
    check("recordings-sidebar-and-collapsed-sessions", Section.visible.contains(.recordings) && recordings.rows.count == 2 && recordings.day.titleOfSelectedItem == "2026-10-27")
    recordings.table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false); recordings.expandSession(); layout()
    check("recordings-expands-session-and-gap-is-not-selectable", recordings.rows.count == 4 && !recordings.tableView(recordings.table, shouldSelectRow: 3))
    recordings.table.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false); layout()
    check("recordings-shows-transcript-fact-basis-and-image", recordings.detail.string.contains("Синтетический локальный транскрипт") && recordings.detail.string.contains("вывод модели") && recordings.detail.string.contains("Уверенность: 80 %") && recordings.picture.image != nil && recordings.files.numberOfItems == 2)
    export("butler-recordings")
    recordings.preserveFile()
    let recordingControl = panel.testCommands.last { $0.first == "ui-recording-control" }.flatMap { $0.last?.data(using: .utf8) }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    check("recordings-control-binds-selected-file-and-observation", recordingControl == ["action": "preserve", "observation": "recording-o1", "media": "recording-m1"])
    recordings.updatePermission(["recordings_allowed": false])
    check("recordings-owner-loss-clears-cached-text-media-and-days", recordings.rows.isEmpty && recordings.picture.image == nil && recordings.player.player == nil && recordings.day.numberOfItems == 0 && !recordings.detail.string.contains("транскрипт"))
    recordings.updatePermission(["recordings_allowed": true, "recordings_navigation": ["serial": 1, "day": "2026-10-27", "at": recordedAt]])
    check("recordings-voice-navigation-opens-local-section-at-day", panel.section == .recordings && recordings.chosenDay == "2026-10-27" && recordings.navigationSerial == 1 && recordings.expanded.contains("session:recording-o1"))
    panel.testNoReply.insert("ui-recordings"); recordings.reload(); panel.testNoReply.remove("ui-recordings")
    check("recordings-disconnection-clears-private-cached-state", recordings.rows.isEmpty && recordings.player.player == nil && recordings.picture.image == nil)
    panel.testReplies.removeValue(forKey: "ui-recordings")

    // Наблюдения: «Сейчас» shows only observations that concluded something; «История» browses days, sources, empty rows and gaps.
    let obsNow = 1_793_062_800.0        // 2026-10-26 20:00 America/Chicago: the synthetic days below end on this «today»
    func obsFact(_ detail: String, kind: String = "activity", confidence: Double = 0.8, identity: String = "") -> [String: Any] {
        ["kind": kind, "detail": detail, "confidence": confidence, "identity": identity, "identity_basis": identity.isEmpty ? "" : "owner-confirmed", "basis": "model-inference"]
    }
    let obsDays: [[String: Any]] = [["day": "2026-10-26", "informative": 3, "empty": 2, "gaps": 2], ["day": "2026-10-25", "informative": 40, "empty": 12, "gaps": 0],
                                    ["day": "2026-10-24", "informative": 5, "empty": 1, "gaps": 1]]
    let obsItems: [[String: Any]] = [
        ["type": "observation", "id": "o1", "source": "camera", "start": obsNow, "end": obsNow + 10, "facts": [obsFact("Пьёт кофе за столом", confidence: 0.9), obsFact("Татьяна у двери", kind: "person", identity: "Таня")], "original": true, "frame": imagePath.path],
        ["type": "observation", "id": "o2", "source": "screen", "start": obsNow - 60, "end": obsNow - 50, "facts": [obsFact("Открыт редактор кода", kind: "work-topic")], "original": false, "frame": NSNull()],
        ["type": "observation", "id": "o3", "source": "microphone", "start": obsNow - 120, "end": obsNow - 110, "facts": [], "original": false, "frame": NSNull()],
        ["type": "gap", "source": "all", "reason": "permission-paused", "start": obsNow - 3000, "end": obsNow - 600, "count": 14, "open": true],
        ["type": "gap", "source": "microphone", "reason": "analysis-buffer-full", "start": obsNow - 4000, "end": obsNow - 3900, "count": 1, "open": false]]
    func obsReply(_ items: [[String: Any]], day: String = "2026-10-26", total: Int? = nil, stalled: Bool = false) -> [String: Any] {
        ["day": day, "timezone": "America/Chicago", "days": obsDays, "items": items, "total": total ?? items.count, "now": obsNow, "offset": 0, "limit": 100,
         "summary": ["informative": 3, "empty": 2, "gaps": [["reason": "permission-paused", "seconds": 8700.0, "count": 14], ["reason": "analysis-buffer-full", "seconds": 100.0, "count": 1]]],
         "analysis": ["last_at": obsNow - 7200, "pending": stalled ? 45000 : 0, "stalled": stalled]]
    }
    func lastObservationRequest() -> [String: Any]? {
        panel.testCommands.last { $0.first == "ui-observations" }.flatMap { $0.count > 1 ? $0[1].data(using: .utf8) : nil }.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
    }
    func observationTableLabel(_ row: Int) -> NSTextField? { panel.tableView(panel.observationTable, viewFor: panel.observationTable.tableColumns[0], row: row) as? NSTextField }
    panel.select(.observations)
    panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic",
                       "recent": [["source": "camera", "at": obsNow, "details": ["Пьёт кофе за столом"]], ["source": "screen", "at": obsNow - 60, "details": ["Открыт редактор кода", "Читает документ"]]],
                       "analysis": ["last_at": obsNow - 7200, "pending": 45000, "stalled": true]]
    panel.refresh(); layout()
    check("observations-open-on-now-with-informative-rows", panel.observationsMode.selectedSegment == 0 && !panel.nowPane.isHidden && panel.historyPane.isHidden
          && panel.history.stringValue.split(separator: "\n").count == 2 && panel.history.stringValue.contains("Камера: Пьёт кофе за столом") && panel.history.stringValue.contains("Открыт редактор кода; Читает документ"))
    check("observations-now-says-when-analysis-stalled", !panel.nowBanner.isHidden && panel.nowBanner.stringValue.contains("Анализ не идёт") && panel.nowBanner.stringValue.contains("45000"))
    export("butler-observations-now")
    panel.testState?["analysis"] = ["last_at": obsNow, "pending": 0, "stalled": false]
    panel.testState?["recent"] = []
    panel.refresh()
    check("observations-now-banner-clears-and-empty-state-is-worded", panel.nowBanner.isHidden && panel.history.stringValue == "Пока нет наблюдений с выводом.")

    panel.testObservations = obsReply(obsItems, stalled: true)
    panel.observationsMode.selectedSegment = 1; panel.changeObservationsMode(); layout()
    let firstRequest = lastObservationRequest()
    check("history-first-load-asks-for-the-newest-day-without-empty-rows", (firstRequest?["day"]) == nil && firstRequest?["empty"] as? Bool == false && firstRequest?["offset"] as? Int == 0
          && firstRequest?["limit"] as? Int == 100 && firstRequest?["source"] == nil && panel.defaults.string(forKey: "observationsMode") == "history")
    check("history-pane-replaces-now-pane", panel.nowPane.isHidden && !panel.historyPane.isHidden)
    check("history-list-has-every-row-and-labels-them", panel.observationTable.numberOfRows == 5 && observationTableLabel(0)?.stringValue.contains("Камера · Пьёт кофе за столом; Татьяна у двери") == true
          && observationTableLabel(2)?.stringValue.hasSuffix("Микрофон · без вывода") == true
          && observationTableLabel(3)?.stringValue.contains("⚠ нет разрешения на запись ×14") == true && observationTableLabel(3)?.stringValue.contains("–…") == true
          && observationTableLabel(4)?.stringValue.contains("буфер заполнен") == true)
    check("history-every-gap-reason-seen-in-the-real-store-has-a-label", ["analysis-buffer-full", "analysis-busy", "analysis-expired", "analysis-failed", "analysis-missed", "capture-gap",
          "owner-paused", "owner-reference-updated", "owner-voice-enrollment", "participant-check-gap", "permission-paused", "service-stopped", "settings-change",
          "source-unavailable", "stale-participant-check"].allSatisfy { Panel.gapReasons[$0] != nil })
    check("history-rows-are-coloured-by-meaning", observationTableLabel(0)?.textColor == Theme.text && observationTableLabel(2)?.textColor == Theme.dim && observationTableLabel(3)?.textColor == Theme.warn)
    check("history-day-picker-lists-days-and-marks-today", panel.obsDay.numberOfItems == 3 && panel.obsDay.titleOfSelectedItem == "26.10.2026 (сегодня) · с выводом 3 · пропусков 2"
          && panel.obsDay.item(at: 1)?.representedObject as? String == "2026-10-25" && panel.obsNext.isEnabled == false && panel.obsPrev.isEnabled)
    check("history-summary-states-counts-and-gap-totals", panel.obsSummary.stringValue.contains("с выводом — 3, без вывода — 2") && panel.obsSummary.stringValue.contains("нет разрешения на запись 2 ч 25 мин")
          && panel.obsSummary.stringValue.contains("буфер заполнен 2 мин"))
    check("history-banner-appears-when-analysis-stalled", !panel.obsBanner.isHidden && panel.obsBanner.stringValue.contains("45000"))
    check("history-first-row-is-selected-and-its-facts-are-shown", panel.observationTable.selectedRow == 0 && panel.obsDetailTitle.stringValue.hasPrefix("Камера")
          && panel.obsDetailBody.stringValue.contains("действие: Пьёт кофе за столом · уверенность 90 %") && panel.obsDetailBody.stringValue.contains("человек: Татьяна у двери · Таня (подтверждено вами)")
          && panel.obsDetailBody.stringValue.contains("Оригинал пока хранится") && panel.obsDetailBody.stringValue.contains("могут содержать ошибки"))
    check("history-key-frame-is-shown-only-for-the-row-that-has-one", !panel.obsFrame.isHidden && panel.obsFrame.image != nil)
    export("butler-observations-history-frame")
    panel.observationTable.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
    check("history-deleted-original-is-stated-and-the-frame-is-hidden", panel.obsDetailBody.stringValue.contains("Оригинал удалён после анализа") && panel.obsFrame.isHidden && panel.obsFrame.image == nil)
    panel.observationTable.selectRowIndexes(IndexSet(integer: 2), byExtendingSelection: false)
    check("history-empty-observation-says-nothing-significant", panel.obsDetailBody.stringValue.contains("Анализ не выделил ничего значимого"))
    panel.observationTable.selectRowIndexes(IndexSet(integer: 3), byExtendingSelection: false)
    check("history-gap-explains-the-interval", panel.obsDetailTitle.stringValue == "⚠ нет разрешения на запись" && panel.obsDetailBody.stringValue.contains("не был подтверждён тремя свежими проверками")
          && panel.obsDetailBody.stringValue.contains("Событий в интервале: 14") && panel.obsDetailBody.stringValue.contains("ещё идёт"))
    layout(); export("butler-observations-history")
    let obsListRect = panel.observationTable.enclosingScrollView!.convert(panel.observationTable.enclosingScrollView!.bounds, to: nil)
    let obsDetailRect = panel.obsDetailTitle.convert(panel.obsDetailTitle.bounds, to: nil), obsContentFrame = panel.content.convert(panel.content.bounds, to: nil)
    let obsRefreshRect = panel.obsRefresh.convert(panel.obsRefresh.bounds, to: nil), obsStatusRect = panel.obsStatus.convert(panel.obsStatus.bounds, to: nil)
    check("history-layout-keeps-list-detail-and-controls-inside-the-window", abs(obsListRect.width - 520) < 2 && obsDetailRect.minX > obsListRect.maxX && obsContentFrame.contains(obsRefreshRect) && obsContentFrame.contains(obsStatusRect)
          && obsListRect.height > 200 && panel.obsPrev.frame.width > 0 && panel.obsEmpty.frame.maxX <= obsRefreshRect.minX + 1)

    panel.obsDay.selectItem(at: 1); panel.changeObservationDay()
    check("history-choosing-a-day-requests-exactly-that-day", lastObservationRequest()?["day"] as? String == "2026-10-25")
    panel.testObservations = obsReply([], day: "2026-10-25")
    panel.obsDay.selectItem(at: 1); panel.changeObservationDay()
    check("history-day-request-carries-the-chosen-day", lastObservationRequest()?["day"] as? String == "2026-10-25" && panel.observationTable.numberOfRows == 0
          && panel.obsStatus.stringValue.contains("Нет наблюдений с выводом") && panel.obsDetailTitle.stringValue == "Выберите запись слева")
    panel.olderObservationDay()
    check("history-older-arrow-steps-back-one-day", lastObservationRequest()?["day"] as? String == "2026-10-24")
    panel.testObservations = obsReply([], day: "2026-10-24")
    panel.olderObservationDay()
    check("history-older-arrow-is-disabled-at-the-oldest-day", !panel.obsPrev.isEnabled && panel.obsNext.isEnabled && lastObservationRequest()?["day"] as? String == "2026-10-24")
    panel.newerObservationDay()
    check("history-newer-arrow-steps-forward-one-day", lastObservationRequest()?["day"] as? String == "2026-10-25")
    panel.testObservations = obsReply(obsItems)
    panel.observationDay = "2026-10-26"
    panel.obsSource.selectItem(withTitle: "Камера"); panel.reloadObservations()
    check("history-source-filter-is-sent", lastObservationRequest()?["source"] as? String == "camera" && lastObservationRequest()?["day"] as? String == "2026-10-26")
    panel.obsSource.selectItem(withTitle: "Все источники"); panel.obsEmpty.state = .on; panel.reloadObservations()
    check("history-all-sources-sends-no-source-and-empty-flag-is-sent", lastObservationRequest()?["source"] == nil && lastObservationRequest()?["empty"] as? Bool == true)

    panel.testObservations = obsReply(Array(repeating: obsItems[1], count: 3).enumerated().map { index, item in var copy = item; copy["id"] = "page1-\(index)"; return copy }, total: 7)
    panel.reloadObservations()
    check("history-more-button-appears-when-more-rows-exist", !panel.obsMore.isHidden && panel.obsStatus.stringValue.contains("Показано 3 из 7"))
    panel.testObservations = obsReply(Array(repeating: obsItems[1], count: 4).enumerated().map { index, item in var copy = item; copy["id"] = "page2-\(index)"; return copy }, total: 7)
    panel.moreObservations()
    check("history-more-appends-the-next-page-from-the-right-offset", lastObservationRequest()?["offset"] as? Int == 3 && panel.observationTable.numberOfRows == 7 && panel.obsMore.isHidden)

    panel.testObservations = nil
    let rowsBefore = panel.observationTable.numberOfRows
    panel.reloadObservations()
    check("history-no-connection-keeps-the-list-and-says-so", panel.observationTable.numberOfRows == rowsBefore && panel.obsStatus.stringValue.contains("нет связи"))
    panel.testObservations = obsReply(obsItems)
    panel.testSupersedeObservation = true
    panel.observationItems = []; panel.observationTable.reloadData()
    panel.reloadObservations()
    check("history-answer-to-a-replaced-request-is-ignored", panel.observationItems.isEmpty && panel.observationTable.numberOfRows == 0)
    panel.testSupersedeObservation = false
    panel.obsEmpty.state = .off; panel.reloadObservations()
    let modeDomain = "com.boris.butler-ui.layout-mode-" + UUID().uuidString
    let modePrefs = UserDefaults(suiteName: modeDomain)!
    modePrefs.set("history", forKey: "observationsMode"); modePrefs.set("observations", forKey: "section")
    let modePanel = Panel(cli: "/usr/bin/false", testing: true, defaults: modePrefs)
    modePanel.testObservations = obsReply(obsItems)
    check("history-mode-is-remembered-and-loads-when-the-page-opens", modePanel.observationsMode.selectedSegment == 1 && modePanel.historyPane.isHidden == false && modePanel.nowPane.isHidden)
    modePanel.window.close(); modePrefs.removePersistentDomain(forName: modeDomain)
    panel.observationsMode.selectedSegment = 0; panel.changeObservationsMode()
    check("history-switching-back-shows-now-and-remembers-it", !panel.nowPane.isHidden && panel.historyPane.isHidden && panel.defaults.string(forKey: "observationsMode") == "now")

    // Изображения: every image says what it is, where it came from, where the file is and whether it was sent.
    func artifactEntry(_ kind: String, _ origin: String, place: String, caption: String = "", sent: [String: Any] = ["state": "not-requested"], path: String? = imagePath.path) -> [String: Any] {
        var entry: [String: Any] = ["id": origin + "-id", "kind": kind, "at": obsNow, "available": true, "candidate": kind == "imported", "origin_kind": origin, "place": place, "caption": caption, "sent": sent]
        if let path { entry["path"] = path }
        return entry
    }
    let artifactList = [artifactEntry("captured", "camera", place: "FaceTime HD Camera", sent: ["state": "sent", "at": obsNow + 60]),
                        artifactEntry("captured", "screen", place: "экран (первый дисплей)"),
                        artifactEntry("imported", "my-file", place: "/Users/nobody/Pictures/Holiday", caption: "Закат над озером."),
                        artifactEntry("generated", "generated", place: "sd-test", caption: "Generated locally with sd-test: кот в шляпе", sent: ["state": "uncertain"]),
                        artifactEntry("captured", "camera", place: "камера", sent: ["state": "not-sent"], path: "/nonexistent/gone.jpg")]
    func stampExpected(_ format: String) -> String { let f = DateFormatter(); f.dateFormat = format; return f.string(from: Date(timeIntervalSince1970: obsNow)) }
    panel.select(.images)
    panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic", "visual": ["status": "ready", "request_id": "images-1", "artifacts": artifactList]]
    panel.refresh(); layout()
    check("images-popup-titles-say-what-each-image-is", panel.gallery.numberOfItems == 5 && panel.gallery.item(at: 0)?.title.contains("Снимок камеры · " + stampExpected("dd.MM HH:mm")) == true && panel.gallery.item(at: 0)?.title.hasSuffix("· отправлено") == true
          && panel.gallery.item(at: 1)?.title.contains("Снимок экрана") == true && panel.gallery.item(at: 2)?.title.contains("Мой материал · кандидат") == true
          && panel.gallery.item(at: 3)?.title.contains("Создано моделью") == true && panel.gallery.item(at: 3)?.title.contains("отправлено") == false)
    check("images-first-image-shows-device-time-file-and-sent-state", panel.gallery.indexOfSelectedItem == 0 && panel.artifactImage.image != nil && panel.artifactCaption.stringValue.contains("Откуда: камера «FaceTime HD Camera»")
          && panel.artifactCaption.stringValue.contains("Снято: ") && panel.artifactCaption.stringValue.contains("Файл: ") && panel.artifactCaption.stringValue.contains("Отправка: отправлено вам в Telegram (") && panel.artifactCaption.stringValue.contains("Снято: " + stampExpected("dd.MM.yyyy HH:mm:ss")))
    export("butler-images-camera")
    panel.gallery.selectItem(at: 1); panel.chooseArtifact()
    check("images-choosing-in-the-popup-updates-the-details-at-once", panel.artifactCaption.stringValue.hasPrefix("Снимок экрана") && panel.artifactCaption.stringValue.contains("Откуда: экран (первый дисплей)")
          && panel.artifactCaption.stringValue.contains("Отправка: не отправлялось, только в этом окне"))
    panel.gallery.selectItem(at: 2); panel.chooseArtifact()
    check("images-found-file-names-its-folder-and-marks-the-description-as-fallible", panel.artifactCaption.stringValue.contains("Откуда: папка /Users/nobody/Pictures/Holiday") && panel.artifactCaption.stringValue.contains("Файл изменён: ")
          && panel.artifactCaption.stringValue.contains("Описание модели (может быть неточным): Закат над озером."))
    panel.gallery.selectItem(at: 3); panel.chooseArtifact()
    check("images-generated-names-the-model-and-shows-the-request-not-the-raw-english-caption", panel.artifactCaption.stringValue.contains("Откуда: локальная модель «sd-test»") && panel.artifactCaption.stringValue.contains("Создано: ")
          && panel.artifactCaption.stringValue.contains("Запрос: кот в шляпе") && !panel.artifactCaption.stringValue.contains("Generated locally")
          && panel.artifactCaption.stringValue.contains("результат неизвестен"))
    export("butler-images-generated")
    panel.gallery.selectItem(at: 4); panel.chooseArtifact()
    check("images-failed-delivery-is-not-reported-as-sent", panel.artifactCaption.stringValue.contains("должно было уйти в Telegram, но не отправлено") && !panel.artifactCaption.stringValue.contains("отправлено вам"))
    check("images-finder-button-is-disabled-when-the-file-is-gone", !panel.revealButton.isEnabled && panel.artifactImage.image == nil)
    panel.gallery.selectItem(at: 0); panel.chooseArtifact()
    check("images-finder-button-reveals-exactly-the-shown-file", panel.revealButton.isEnabled && { panel.testRevealed = nil; panel.revealArtifact(); return panel.testRevealed == imagePath.path }())
    panel.testState?["recording_allowed"] = false
    panel.testState?["visual"] = ["status": "ready", "request_id": "images-1", "artifacts": artifactList.map { var copy = $0; copy.removeValue(forKey: "path"); copy.removeValue(forKey: "place"); return copy }]
    panel.refresh(); panel.gallery.selectItem(at: 0); panel.chooseArtifact()
    check("images-pause-hides-the-path-and-the-picture-but-keeps-the-origin", panel.artifactImage.image == nil && !panel.revealButton.isEnabled && panel.artifactCaption.stringValue.contains("путь скрыт")
          && panel.artifactCaption.stringValue.contains("Снимок камеры") && !panel.artifactCaption.stringValue.contains(imagePath.path))
    panel.testRevealed = nil; panel.revealArtifact()
    check("images-finder-button-does-nothing-while-paused", panel.testRevealed == nil)
    panel.testState?["recording_allowed"] = true
    panel.testState?["visual"] = ["status": "idle", "artifacts": []]
    panel.refresh()
    check("images-empty-gallery-disables-finder-and-says-so", panel.gallery.numberOfItems == 1 && panel.gallery.item(at: 0)?.title == "Результатов пока нет" && !panel.revealButton.isEnabled)

    // Settings defects (audit 2026-10-26): menu, pause, read-only save, setup refusals + cancel, archive choice, results, timeouts, statuses, diagnostics text.
    func s6Commands(since count: Int) -> [String] { panel.testCommands.dropFirst(count).compactMap { $0.first } }
    let s6Menu = panel.item.menu?.items.map { $0.title } ?? []
    check("menu-splits-settings-and-diagnostics", s6Menu.contains("Настройки…") && s6Menu.contains("Диагностика…") && !s6Menu.contains("Настройки и диагностика…"))
    func s6Click(_ title: String) { if let entry = panel.item.menu?.items.first(where: { $0.title == title }), let target = entry.target, let action = entry.action { _ = target.perform(action) } }
    panel.select(.display)
    s6Click("Диагностика…")
    check("menu-diagnostics-opens-the-diagnostics-page", panel.section == .diagnostics && !panel.pages[.diagnostics]!.isHidden)
    s6Click("Настройки…")
    check("menu-settings-opens-components", panel.section == .components)
    panel.state = ["enabled": true]; panel.menuNeedsUpdate(panel.item.menu!); let s6Running = panel.pauseMenuItem?.title
    panel.state = ["enabled": false]; panel.menuNeedsUpdate(panel.item.menu!); let s6Paused = panel.pauseMenuItem?.title
    panel.state = [:]; panel.menuNeedsUpdate(panel.item.menu!); let s6Unknown = panel.pauseMenuItem?.title
    check("menu-pause-item-says-what-it-will-do", s6Running == "Поставить на паузу" && s6Paused == "Продолжить" && s6Unknown == "Пауза / продолжить")
    var s6Mark = panel.testCommands.count
    panel.state = ["enabled": false]; panel.testState = ["enabled": true]          // the screen is stale: Butler was resumed meanwhile
    panel.toggle()
    check("pause-decides-from-fresh-state-not-the-stale-one", Array(s6Commands(since: s6Mark).prefix(2)) == ["ui-state", "pause"])
    s6Mark = panel.testCommands.count
    panel.state = ["enabled": true]; panel.testState = ["enabled": false]
    panel.toggle()
    check("resume-decides-from-fresh-state-too", Array(s6Commands(since: s6Mark).prefix(2)) == ["ui-state", "resume"])
    s6Mark = panel.testCommands.count
    panel.testState = nil
    panel.toggle()
    check("pause-without-a-connection-changes-nothing-and-says-so", !s6Commands(since: s6Mark).contains("pause") && !s6Commands(since: s6Mark).contains("resume") && panel.message.stringValue.contains("Нет связи"))
    panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic"]

    let s6SavedComponents = panel.components
    let s6Editable: [String: Any] = ["id": "s6-asr", "title": "Whisper", "model": "/m.bin", "executable": "/w", "model_key": "whisper_model", "cli_key": "whisper_cli", "ready": true]
    let s6ReadOnly: [String: Any] = ["id": "s6-camera", "title": "Камера", "model": "—", "executable": "—", "model_key": NSNull(), "cli_key": NSNull(), "ready": true]
    panel.receiveComponents([s6Editable, s6ReadOnly])
    panel.componentTable.selectRowIndexes(IndexSet(integer: panel.components.firstIndex { $0["id"] as? String == "s6-camera" }!), byExtendingSelection: false)
    check("component-save-is-disabled-for-read-only-rows", !panel.componentSave.isEnabled && panel.componentMessage.stringValue.contains("Только для просмотра"))
    s6Mark = panel.testCommands.count
    panel.applyComponent()
    check("component-save-sends-nothing-for-read-only-rows", !s6Commands(since: s6Mark).contains("ui-components-update"))
    panel.componentTable.selectRowIndexes(IndexSet(integer: panel.components.firstIndex { $0["id"] as? String == "s6-asr" }!), byExtendingSelection: false)
    check("component-save-is-enabled-for-editable-rows", panel.componentSave.isEnabled && panel.componentMessage.stringValue.isEmpty)
    let longComponent: [String: Any] = ["id": "knowledge", "title": "Знания Butler · проекты, память, история", "model": "/synthetic/workspace-butler", "executable": "/synthetic/python", "ready": true, "status": "configured",
        "note": "Настроен; первый поиск ещё не выполнялся. " + String(repeating: "Полное описание компонента и его локальных настроек. ", count: 8)]
    panel.receiveComponents([longComponent]); panel.select(.components)
    for width in [CGFloat(1120), 1280] {
        panel.window.setContentSize(NSSize(width: width, height: 820)); layout()
        let page = panel.pages[.components]!
        let tableArea = panel.componentTable.enclosingScrollView!
        let titleCell = panel.componentTable.view(atColumn: 1, row: 0, makeIfNecessary: true) as? NSTableCellView
        let statusCell = panel.componentTable.view(atColumn: 0, row: 0, makeIfNecessary: true) as? NSTableCellView
        check("components-contain-table-at-\(Int(width))", page.bounds.contains(tableArea.convert(tableArea.bounds, to: page)) && !tableArea.hasHorizontalScroller)
        check("components-wrap-titles-at-\(Int(width))", titleCell?.textField?.maximumNumberOfLines == 2 && titleCell?.textField?.lineBreakMode == .byWordWrapping && titleCell!.bounds.contains(titleCell!.textField!.frame))
        check("components-configured-knowledge-is-not-unavailable-at-\(Int(width))", statusCell?.textField?.stringValue == "● настроен")
        check("components-help-wraps-without-line-pruning-at-\(Int(width))", panel.componentNote.maximumNumberOfLines == 0 && panel.componentNote.lineBreakMode == .byWordWrapping && panel.componentNote.bounds.width < page.bounds.width)
    }
    export("butler-components-fixed")
    panel.window.setContentSize(NSSize(width: 1280, height: 820))
    panel.receiveComponents(s6SavedComponents)

    panel.select(.voice)
    let s6Idle = "Эталоны голоса хранятся на этом Mac."
    let s6Start = Date(timeIntervalSince1970: 1_793_000_000)
    func s6Refresh(stage: String, at date: Date) {
        panel.testNow = date
        panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic", "voice": ["owner_setup": ["stage": stage, "seconds": 3.0]]]
        panel.refresh()
    }
    panel.testReplies["ui-owner-setup"] = ["queued": false, "message": "Для регистрации нужен включённый подтверждённый микрофон. Полная Пауза сохраняется."]
    panel.testNow = s6Start; panel.ownerSetup()
    check("owner-setup-refusal-is-shown", panel.ownerSetupStatus.stringValue.contains("подтверждённый микрофон"))
    s6Refresh(stage: "idle", at: s6Start.addingTimeInterval(5))
    check("owner-setup-refusal-survives-the-next-refresh", panel.ownerSetupStatus.stringValue.contains("подтверждённый микрофон"))
    s6Refresh(stage: "idle", at: s6Start.addingTimeInterval(20))
    check("owner-setup-refusal-fades-back-to-the-idle-text", panel.ownerSetupStatus.stringValue == s6Idle)
    panel.testReplies["ui-owner-setup"] = nil; panel.testNoReply.insert("ui-owner-setup")
    panel.testNow = s6Start; panel.ownerSetup()
    check("owner-setup-without-a-connection-says-so", panel.ownerSetupStatus.stringValue.contains("нет связи с Butler"))
    panel.testNoReply.remove("ui-owner-setup")
    panel.guestName.stringValue = "  "
    panel.guestSetup()
    check("guest-setup-asks-for-a-name-and-the-text-stays", panel.ownerSetupStatus.stringValue == "Введите короткое имя гостя.")
    s6Refresh(stage: "idle", at: s6Start.addingTimeInterval(2))
    check("guest-name-prompt-survives-the-refresh", panel.ownerSetupStatus.stringValue == "Введите короткое имя гостя.")
    panel.testReplies["ui-guest-setup"] = ["queued": false, "message": "Проверка владельца уже открыта."]
    panel.guestName.stringValue = "Аня"; panel.testNow = s6Start; panel.guestSetup()
    check("guest-setup-refusal-is-shown-and-the-name-is-kept", panel.ownerSetupStatus.stringValue == "Проверка владельца уже открыта." && panel.guestName.stringValue == "Аня")
    panel.testReplies["ui-guest-setup"] = ["queued": true]
    panel.guestSetup()
    check("guest-setup-accepted-clears-the-name", panel.guestName.stringValue.isEmpty)
    s6Refresh(stage: "idle", at: s6Start.addingTimeInterval(30))
    check("setup-cancel-is-disabled-when-nothing-is-running", !panel.ownerSetupCancel.isEnabled)
    for stage in ["authenticating", "recording", "validating"] {
        s6Refresh(stage: stage, at: s6Start.addingTimeInterval(31))
        check("setup-cancel-is-enabled-while-\(stage)", panel.ownerSetupCancel.isEnabled)
        if stage == "recording" { panel.ownerSetupStatus.scrollToVisible(panel.ownerSetupStatus.bounds); layout(); export("butler-voice-owner-setup") }
    }
    panel.testReplies["ui-owner-setup-cancel"] = ["cancelled": true]
    s6Mark = panel.testCommands.count
    panel.cancelSetup()
    check("setup-cancel-sends-the-cancel-command", s6Commands(since: s6Mark).first == "ui-owner-setup-cancel")
    panel.testReplies["ui-owner-setup-cancel"] = nil; panel.testNoReply.insert("ui-owner-setup-cancel")
    panel.cancelSetup()
    check("setup-cancel-failure-is-reported-while-the-stage-is-unchanged", panel.ownerSetupStatus.stringValue.contains("Не удалось отменить"))
    s6Refresh(stage: "validating", at: s6Start.addingTimeInterval(33))
    check("setup-notice-survives-a-refresh-in-the-same-stage", panel.ownerSetupStatus.stringValue.contains("Не удалось отменить"))
    s6Refresh(stage: "saved", at: s6Start.addingTimeInterval(34))
    check("setup-notice-is-dropped-when-the-stage-moves-on", panel.ownerSetupStatus.stringValue.contains("Голос обновлён"))
    panel.testNoReply.remove("ui-owner-setup-cancel")

    func s6Archive(preference: Bool?, enabled: Bool) {
        var archive: [String: Any] = ["enabled": enabled]
        if let preference { archive["preference"] = preference }
        panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic", "archive": archive]
        panel.refresh()
    }
    s6Archive(preference: true, enabled: false)
    check("archive-checkbox-shows-the-choice-and-says-when-paused", panel.archiveToggle.state == .on && panel.archiveToggle.title == Panel.archiveTitle + " (на паузе)")
    s6Archive(preference: true, enabled: true)
    check("archive-checkbox-on-and-running-has-no-pause-note", panel.archiveToggle.state == .on && panel.archiveToggle.title == Panel.archiveTitle)
    s6Archive(preference: false, enabled: false)
    check("archive-checkbox-off-stays-off-under-pause", panel.archiveToggle.state == .off && panel.archiveToggle.title == Panel.archiveTitle)
    s6Archive(preference: nil, enabled: true)
    check("archive-checkbox-falls-back-to-the-effective-state-for-an-older-service", panel.archiveToggle.state == .on)

    panel.profileIDs = ["calm"]; panel.voices.removeAllItems(); panel.voices.addItem(withTitle: "Спокойный")
    panel.testReplies["voice-preview"] = ["played": "calm", "synthetic": true]
    panel.preview()
    check("voice-preview-success-is-reported", panel.settingMessage.stringValue == "Пример голоса воспроизведён.")
    panel.testReplies["voice-preview"] = nil; panel.testNoReply.insert("voice-preview")
    panel.preview()
    check("voice-preview-failure-is-reported", panel.settingMessage.stringValue.contains("Не удалось проиграть"))
    panel.testReplies["voice-select"] = ["selected": "calm", "synthetic": true]
    panel.selectVoice()
    check("voice-select-success-names-the-voice", panel.settingMessage.stringValue == "Голос выбран: Спокойный.")
    panel.testReplies["voice-select"] = nil; panel.testNoReply.insert("voice-select")
    panel.profileIDs = ["calm"]                 // the refresh after a success rebuilt the list from a state without profiles
    panel.selectVoice()
    check("voice-select-failure-is-reported", panel.settingMessage.stringValue.contains("Не удалось выбрать голос"))
    panel.testReplies["ui-settings-update"] = ["saved": true, "restart_required": true]
    panel.languageChoice.selectItem(at: 2)
    panel.applyLanguage()
    check("language-apply-success-names-the-language-and-the-restart", panel.settingMessage.stringValue.hasPrefix("Язык распознавания: ") && panel.settingMessage.stringValue.contains("перезапускается"))
    panel.testReplies["ui-settings-update"] = ["saved": false]
    panel.applyLanguage()
    check("language-apply-failure-is-reported", panel.settingMessage.stringValue.contains("Настройки не применены"))

    check("schedule-control-gets-a-long-timeout-and-state-polls-a-short-one", Panel.commandTimeout("ui-schedule-control") >= 45 && Panel.commandTimeout("ui-state") == 8 && Panel.commandTimeout("ui-components-update") == 15 && Panel.commandTimeout("anything") == 15)
    panel.jobs = [["id": "s6-job-ok", "name": "Готовое", "kind": "cron", "schedule": "Ежедневно", "enabled": true, "status": "ok"], ["id": "s6-job-err", "name": "Сбой", "kind": "cron", "schedule": "Ежедневно", "enabled": true, "status": "error"]]
    panel.jobTable.reloadData()
    func s6JobStatus(_ row: Int) -> String? { (panel.tableView(panel.jobTable, viewFor: panel.jobTable.tableColumns[0], row: row) as? NSTextField)?.stringValue }
    check("finished-job-status-is-translated", s6JobStatus(0) == "● выполнено" && s6JobStatus(1) == "● ошибка")

    // Diagnostics: the clock is a label of its own, so the text is only rewritten when something real changed.
    panel.select(.diagnostics)
    func s6Diagnostics(at date: Date, mic: Double) {
        panel.testNow = date
        panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic", "voice": ["audio": ["ready": true], "debug": ["microphone": ["rms_dbfs": mic]]]]
        panel.refresh()
    }
    s6Diagnostics(at: s6Start, mic: -40)
    layout(); export("butler-diagnostics-live")
    let s6Writes = panel.debugStatus.writes, s6Stamp = panel.debugStamp.stringValue
    s6Diagnostics(at: s6Start.addingTimeInterval(5), mic: -40)
    check("diagnostics-text-is-not-rewritten-when-only-the-clock-moved", panel.debugStatus.writes == s6Writes && panel.debugStamp.stringValue != s6Stamp && !panel.debugStatus.stringValue.contains("ЖИВАЯ ОТЛАДКА ·"))
    s6Diagnostics(at: s6Start.addingTimeInterval(6), mic: -20)
    check("diagnostics-text-is-rewritten-when-something-changed", panel.debugStatus.writes == s6Writes + 1 && panel.debugStatus.stringValue.contains("-20"))
    let s6Field = StableTextField(wrappingLabelWithString: "")
    for value in ["a", "a", "a", "b", "b", "a"] { s6Field.stringValue = value }
    check("stable-field-counts-only-real-changes", s6Field.writes == 3 && s6Field.stringValue == "a")
    panel.testNow = nil
    panel.testReplies.removeAll(); panel.testNoReply.removeAll()
    panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic"]

    panel.select(.models)
    let savingFrame = panel.window.frame
    panel.saveFrame()
    check("saving-frame-does-not-resize-and-small-displays-respect-minimum-without-layout-feedback", panel.window.frame == savingFrame && Panel.boundedUserSize(NSSize(width: 1280, height: 820), visible: NSSize(width: 1024, height: 681), minimum: NSSize(width: 1120, height: 730)) == NSSize(width: 1120, height: 730))
    let restored = Panel(cli: "/usr/bin/false", testing: true, defaults: prefs)
    check("window-and-section-restored", restored.section == .models && restored.window.frame == panel.window.frame)
    restored.window.close()
    let chosen = panel.section
    let frameBeforeDisconnect = panel.window.frame
    // One or two dropped polls keep the last state on screen and only mark its age; the third clears it.
    panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic"]
    panel.refresh(); layout()
    panel.voiceState.stringValue = "synthetic transient text"
    check("failure-streak-starts-at-zero", panel.failureStreak == 0 && panel.lastGoodState != nil)
    panel.observationItems = [obsItems[0]]; panel.obsDetailBody.stringValue = "Synthetic stored fact"
    panel.artifacts = artifactList
    panel.testState = nil
    panel.refresh(); layout()
    check("first-failed-poll-clears-every-stored-observation-and-gallery-cache", panel.observationItems.isEmpty && panel.artifacts.isEmpty && !panel.obsDetailBody.stringValue.contains("Synthetic stored fact") && panel.state["recordings_allowed"] as? Bool == false)
    check("first-failed-poll-keeps-last-state", panel.heading.stringValue != "Butler недоступен" && panel.window.subtitle.contains("данные от")
          && panel.voiceState.stringValue.contains("synthetic transient text") && panel.failureStreak == 1)
    panel.refresh(); layout()
    check("second-failed-poll-keeps-last-state", panel.heading.stringValue != "Butler недоступен" && panel.failureStreak == 2)
    panel.testState = ["enabled": true, "recording_allowed": true, "version": "0.0.0-synthetic"]
    panel.refresh(); layout()
    check("answer-resets-the-streak-and-the-age-marker", panel.failureStreak == 0 && !panel.window.subtitle.contains("данные от") && panel.window.subtitle.hasSuffix("v0.0.0-synthetic"))
    panel.voiceState.stringValue = "synthetic transient text"
    panel.testState = nil
    panel.refresh(); panel.refresh(); layout()
    check("two-failures-after-recovery-do-not-clear", panel.heading.stringValue != "Butler недоступен")
    panel.refresh(); layout()
    check("third-failed-poll-shows-disconnect", panel.heading.stringValue == "Butler недоступен" && panel.failureStreak == 3)
    check("disconnect-does-not-navigate-or-resize", panel.section == chosen && panel.window.frame == frameBeforeDisconnect)
    check("disconnect-clears-feed", panel.feed.stack.arrangedSubviews.isEmpty && !panel.feed.placeholder.isHidden)
    check("disconnect-clears-transient-voice-text", !panel.voiceState.stringValue.contains("synthetic transient text"))
    check("disconnect-clears-media", panel.debugImage.image == nil && panel.image.image == nil && panel.artifactImage.image == nil)
    check("test-mode-never-starts-cli", panel.testing && restored.testing && panel.timer == nil && restored.timer == nil)
    // The Dock icon is the system rendition of the bundle's icon: transparent corners, opaque centre, body ≈ 0.84 of
    // the canvas with its shadow (Mail.app measures the same). LaunchServices masks only registered apps, so this is
    // verified against the installed bundle; on a machine without it the check is recorded as skipped, never as passed.
    check("icon-resolves", applicationIcon() != nil)
    let installedBundle = Bundle(path: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications/Butler.app").path)
    if let installedBundle {
        let shape = applicationIcon(bundle: installedBundle).flatMap { iconProfile($0) }
        check("icon-of-installed-bundle-is-a-standard-squircle", shape.map { $0.corner == 0 && $0.centre == 255 && $0.body > 0.76 && $0.body < 0.90 } == true)
    } else {
        check("icon-squircle-check-skipped-no-installed-bundle", true)
    }
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

/// The icon the Dock, Cmd-Tab and alerts draw. The service opens this executable directly, so the icon is set
/// explicitly. `NSWorkspace` returns the system rendition of the bundle's icon (squircle body, margin, shadow:
/// the same image Finder shows). The raw `Butler.icns` is a full-bleed square, and `applicationIconImage` draws
/// whatever it is given unmasked, which made Butler the only square, oversized icon in the Dock. The raw file is
/// the fallback only when the bundle has no icon resource.
func applicationIcon(bundle: Bundle = .main) -> NSImage? {
    guard let portraitURL = bundle.url(forResource: "Butler", withExtension: "icns") else { return nil }
    let system = NSWorkspace.shared.icon(forFile: bundle.bundlePath)
    if !system.representations.isEmpty && system.size.width > 0 { return system }
    return NSImage(contentsOf: portraitURL)
}

/// Where an icon's pixels are: alpha of the corner and the centre, and the width of the visible body as a share
/// of the canvas (a standard macOS icon body is 824 of 1024 = 0.805, plus a soft shadow).
func iconProfile(_ image: NSImage, side: Int = 512) -> (corner: Int, centre: Int, body: Double)? {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: side, pixelsHigh: side, bitsPerSample: 8, samplesPerPixel: 4,
                                        hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
          let context = NSGraphicsContext(bitmapImageRep: bitmap) else { return nil }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    NSColor.clear.set(); NSRect(x: 0, y: 0, width: side, height: side).fill(using: .copy)
    image.draw(in: NSRect(x: 0, y: 0, width: side, height: side), from: .zero, operation: .sourceOver, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    func alpha(_ x: Int, _ y: Int) -> Int { Int((bitmap.colorAt(x: x, y: y)?.alphaComponent ?? 0) * 255 + 0.5) }
    let row = side / 2
    var first = -1, last = -1
    for x in 0..<side where alpha(x, row) >= 8 { if first < 0 { first = x }; last = x }
    guard first >= 0 else { return (alpha(2, 2), alpha(side / 2, side / 2), 0) }
    return (alpha(2, 2), alpha(side / 2, side / 2), Double(last - first + 1) / Double(side))
}

ProcessInfo.processInfo.processName = "Butler"
let app = NSApplication.shared
if let icon = applicationIcon() {
    app.applicationIconImage = icon
    app.dockTile.display()
}
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--test-video-playback" {
    app.setActivationPolicy(.accessory)
    let domain = "com.boris.butler-ui.video-test.\(UUID().uuidString)"
    let prefs = UserDefaults(suiteName: domain)!
    let panel = Panel(cli: "/usr/bin/false", testing: true, defaults: prefs)
    panel.renderDisplayCard(["kind": "video", "id": "playback-test", "video": CommandLine.arguments[2]])
    let deadline = Date().addingTimeInterval(8)
    while Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.1)) }
    let playback = panel.videoView?.player
    let seconds = playback?.currentTime().seconds ?? 0
    let passed = playback?.currentItem?.status == .readyToPlay && seconds > 1
    print("video-playback: \(passed ? "PASS" : "FAIL") time=\(seconds) rate=\(playback?.rate ?? 0)")
    playback?.pause()
    panel.renderDisplayCard(["kind": "text", "id": "stop-test", "markdown": "Done"])
    let stopped = panel.videoView == nil && (playback?.rate ?? 0) == 0
    print("video-retire: \(stopped ? "PASS" : "FAIL")")
    prefs.removePersistentDomain(forName: domain)
    exit(passed && stopped ? 0 : 1)
}
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--test-layout" {
    app.setActivationPolicy(.accessory)
    exit(runLayoutTests(output: CommandLine.arguments[2]))
}
let startHidden = CommandLine.arguments.contains("--background")
let ownerArguments = CommandLine.arguments.dropFirst().filter { $0 != "--background" }
guard ownerArguments.count <= 1 else { exit(2) }
let home = FileManager.default.homeDirectoryForCurrentUser
var uiLock: UILock?
do { uiLock = try UILock(path: home.appendingPathComponent(".local/share/butler-ambient/ui.lock")) }
catch let error as POSIXError where error.code == .EWOULDBLOCK {
    if !startHidden { DistributedNotificationCenter.default().postNotificationName(.init("com.boris.butler-ui.show"), object: NSUserName(), userInfo: nil, deliverImmediately: true) }
    exit(0)
} catch { exit(1) }
app.setActivationPolicy(.regular)
let ownerCLI = ownerArguments.first ?? home.appendingPathComponent(".openclaw/tools/ambient-awareness/bin/butler-ambient").path
let panel = Panel(cli: ownerCLI, startHidden: startHidden)
app.delegate = panel
app.run()

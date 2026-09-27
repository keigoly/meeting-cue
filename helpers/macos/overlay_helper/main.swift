// overlay-helper — Web UI(http://127.0.0.1:PORT)をウィンドウに表示する。3 つの使い方:
//
// 1) 半透明パネル(meetcue run --ui overlay・旧来): 最前面(level .screenSaver + fullScreenAuxiliary)・ドラッグで移動・
//    位置は記憶。⌃⌥L で固定(クリック透過)⇄移動、⌃⌥H で表示/非表示、⌃⌥R で再読込(Carbon・Accessibility 許可不要)。
//    引数: --url <URL> [--width 980] [--height 560] [--opacity 0.94] [--level floating|top]
// 2) --window(meetcue app がデバッグ起動で呼ぶ): タイトルバー付きの通常ウィンドウ。閉じると終了。
//    --caption --url <…/caption.html>: ライブ字幕のウィンドウだけを出す(確かめる用・閉じると終了)。
//    stdin "top on" / "top off" で「常に手前」(全画面の会議アプリの上にも出る)。
// 3) ホスト(Meeting Cue!.app の本体・2026-09-26): Info.plist の MeetcueLaunchScript があれば、Terminal を開かずに
//    `<script> serve`(meetcue app --no-window)を子プロセスで起動し、応答したら画面を読み込む。マイク・システム音声録音の
//    許可はこのアプリ(Meeting Cue!)に付く。ウィンドウを閉じる / ⌘Q では子に SIGINT を送り、録音の保存とサマリを待ってから終わる。
//
//    メニューバー(2026-09-26。2026-09-27 に見た目を角丸のパネルへ = MenuPanel + SwiftUI):
//    ログイン時に起動(無効なら黄色の帯)/ 録音を開始・停止の大きなボタン(LOCAL の行も)/ レコーディング(ウィンドウ)/
//    ライブ字幕(半透明の最前面パネル = /index.html)/ 設定を開く ⌘, / 記録のフォルダ / 終了 ⌘Q。
//    見た目の確認は `overlay-helper --render-menu <dir>`(3 つの状態を PNG に描く・画面は撮らない)。アイコンは Resources の
//    MenuBarTemplate(@2x).png(テンプレート画像)で、録音中は右上に赤い点を重ねる(packaging/icon/)。
//    ホストではウィンドウを閉じてもメニューバーに残る(終了はメニューか ⌘Q)。
//
// 画面からは window.webkit.messageHandlers.meetcue.postMessage({cmd: "top", on: true}) で「常に手前」を切り替えられる。
// stdin "quit" / SIGINT / SIGTERM で終了。stderr に診断 JSON。
// ビルド: swiftc -parse-as-library -O main.swift -o overlay-helper(アプリは packaging/make_mac_app.sh)

import AppKit
import Carbon
import Foundation
import ServiceManagement
import SwiftUI
import WebKit

func diag(_ fields: [String: Any]) {
    var f = fields
    f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000)
    if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        FileHandle.standardError.write(Data((s + "\n").utf8))
    }
}

var gOverlay: Overlay?
var gHost: Host?

/// 外観(2026-09-27 keigoly様: ダークモードも)。auto = macOS に合わせる / light / dark。各ウィンドウは外観を持たずアプリに従う。
func applyAppTheme(_ t: String?) {
    NSApplication.shared.appearance = t == "light" ? NSAppearance(named: .aqua) : t == "dark" ? NSAppearance(named: .darkAqua) : nil
}

/// ウィンドウの地の色(タイトルバーの色)。明るい外観 = 画面の --bg #e9f9f9 / 暗い外観 = #0f1d29。
let meetcueWindowBg = NSColor(name: nil) { ap in
    ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        ? NSColor(calibratedRed: 0.059, green: 0.114, blue: 0.161, alpha: 1)
        : NSColor(calibratedRed: 0.914, green: 0.976, blue: 0.976, alpha: 1)
}

/// 画面(JS)→ ウィンドウの橋渡し。
final class Bridge: NSObject, WKScriptMessageHandler {
    weak var overlay: Overlay?
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let d = m.body as? [String: Any], let cmd = d["cmd"] as? String else { return }
        switch cmd {
        case "top", "pin": overlay?.setTop((d["on"] as? Bool) ?? false)   // pin = ライブ字幕の「常に手前」
        case "drag": overlay?.setDragRegion(height: (d["height"] as? Double) ?? 0, rects: (d["rects"] as? [[Double]]) ?? [])
        case "main": gHost?.showMain()
        case "theme": applyAppTheme(d["value"] as? String)
        default: break
        }
    }
}

final class Overlay: NSObject, NSWindowDelegate {
    let panel: NSPanel
    let web: WKWebView
    let windowMode: Bool
    let captionMode: Bool          // ライブ字幕(2026-09-27・信号機ボタン付きの暗いパネル)
    var hostMode = false
    var onClose: (() -> Void)?
    private var dragHeight: CGFloat = -1   // 画面がまだ知らせていない = 既定の帯。0 = 動かさない(メニューを開いている間)
    private var dragRects: [NSRect] = []
    private var dragMonitor: Any?
    private let bridge = Bridge()
    private(set) var locked = false
    private var hidden = false

    init(url: URL?, width: CGFloat, height: CGFloat, opacity: CGFloat, top: Bool, window: Bool = false, caption: Bool = false) {
        windowMode = window
        captionMode = caption
        let sf = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let w = min(width, sf.width - 40), h = min(height, sf.height - 40)
        let rect = window || caption ? NSRect(x: sf.midX - w / 2, y: sf.midY - h / 2, width: w, height: h)
                          : NSRect(x: sf.maxX - w - 20, y: sf.maxY - h - 20, width: w, height: h)
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(bridge, name: "meetcue")
        if caption {
            // ライブ字幕: 上の帯(40pt・題と状態だけ)は画面(caption.html)が描く。信号機ボタンは帯の縦の中央へ動かし(placeTrafficLights)、
            // 帯の空いている所をつかむと動かせる(押せる部品の位置は画面から "drag" で届く)。アプリを前面に出さないパネル
            // なので、「常に手前」で全画面の会議アプリの上にも出る(旧来の半透明パネルと同じ)
            panel = NSPanel(contentRect: rect, styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView,
                                                           .nonactivatingPanel], backing: .buffered, defer: false)
            panel.title = "ライブ字幕"
            panel.titleVisibility = .hidden
            panel.titlebarAppearsTransparent = true
            panel.backgroundColor = meetcueWindowBg   // 画面の --bg(外観はアプリに従う)
            panel.isReleasedWhenClosed = false
            panel.isFloatingPanel = false
            panel.becomesKeyOnlyIfNeeded = false
            panel.hidesOnDeactivate = false
            panel.setFrameAutosaveName("meetcue-caption")
            panel.minSize = NSSize(width: 600, height: 260)
            web = WKWebView(frame: NSRect(origin: .zero, size: panel.contentView!.bounds.size), configuration: cfg)
            web.autoresizingMask = [.width, .height]
            web.setValue(false, forKey: "drawsBackground")
            panel.contentView?.addSubview(web)
            super.init()
            bridge.overlay = self
            panel.delegate = self
            if let url { web.load(URLRequest(url: url)) }
            setTop(top)
            placeTrafficLights()
            panel.makeKeyAndOrderFront(nil)
            dragMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown]) { [weak self] e in
                guard let self, e.window === self.panel, self.inDragRegion(e.locationInWindow) else { return e }
                self.panel.performDrag(with: e)
                return nil
            }
            diag(["phase": "shown", "mode": "caption", "window_id": panel.windowNumber, "url": url?.absoluteString ?? "",
                  "frame": [Int(panel.frame.minX), Int(panel.frame.minY), Int(panel.frame.width), Int(panel.frame.height)]])
            return
        }
        if window {
            panel = NSPanel(contentRect: rect, styleMask: [.titled, .closable, .miniaturizable, .resizable],
                            backing: .buffered, defer: false)
            panel.title = "Meeting Cue!"
            panel.titlebarAppearsTransparent = true
            panel.backgroundColor = meetcueWindowBg   // 画面の --bg(タイトルバーの色・外観はアプリに従う)
            panel.isFloatingPanel = false
            panel.becomesKeyOnlyIfNeeded = false
            panel.hidesOnDeactivate = false
            panel.setFrameAutosaveName("meetcue-window")
            panel.minSize = NSSize(width: 720, height: 420)
            web = WKWebView(frame: NSRect(origin: .zero, size: panel.contentView!.bounds.size), configuration: cfg)
            web.autoresizingMask = [.width, .height]
            panel.contentView?.addSubview(web)
            super.init()
            bridge.overlay = self
            panel.delegate = self
            if let url { web.load(URLRequest(url: url)) }
            setTop(top)
            panel.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate(ignoringOtherApps: true)
            diag(["phase": "shown", "mode": "window", "url": url?.absoluteString ?? "",
                  "frame": [Int(panel.frame.minX), Int(panel.frame.minY), Int(panel.frame.width), Int(panel.frame.height)]])
            return
        }
        panel = NSPanel(contentRect: rect, styleMask: [.borderless, .nonactivatingPanel, .resizable, .utilityWindow],
                        backing: .buffered, defer: false)
        panel.level = top ? .screenSaver : .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.alphaValue = opacity
        panel.hasShadow = true
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.setFrameAutosaveName("meetcue-overlay")
        panel.minSize = NSSize(width: 420, height: 240)

        web = WKWebView(frame: NSRect(origin: .zero, size: panel.contentView!.bounds.size), configuration: cfg)
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")   // HTML 側の半透明背景を活かす
        web.wantsLayer = true
        web.layer?.cornerRadius = 12
        web.layer?.masksToBounds = true
        panel.contentView?.addSubview(web)
        super.init()
        bridge.overlay = self
        panel.delegate = self
        if let url { web.load(URLRequest(url: url)) }
        panel.orderFrontRegardless()
        diag(["phase": "shown", "url": url?.absoluteString ?? "", "level": top ? "screenSaver" : "floating",
              "frame": [Int(panel.frame.minX), Int(panel.frame.minY), Int(panel.frame.width), Int(panel.frame.height)]])
    }

    func load(_ url: URL) { web.load(URLRequest(url: url)) }

    /// 起動中・終了中・異常時の案内(画面の配色に合わせる)。
    func showMessage(_ title: String, _ detail: String) {
        func esc(_ s: String) -> String {
            s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        }
        let html = """
        <!doctype html><html lang="ja"><meta charset="utf-8"><body style="margin:0;height:100vh;display:flex;align-items:center;
        justify-content:center;background:#232323;color:#ececec;font:14px -apple-system,'Hiragino Sans',sans-serif">
        <div style="text-align:center;max-width:560px;padding:24px"><div style="font-size:20px;font-weight:700;margin-bottom:10px">\(esc(title))</div>
        <div style="color:#9a9a9a;line-height:1.7">\(esc(detail))</div></div></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    func toggleLock() {
        locked.toggle()
        panel.ignoresMouseEvents = locked
        panel.alphaValue = locked ? max(0.5, panel.alphaValue - 0.12) : min(1.0, panel.alphaValue + 0.12)
        diag(["phase": "lock", "locked": locked])
    }

    func toggleHidden() {
        hidden.toggle()
        if hidden { panel.orderOut(nil) } else { panel.orderFrontRegardless() }
        diag(["phase": "hidden", "hidden": hidden])
    }

    func reload() { web.reload() }

    /// 「常に手前」: 全画面の会議アプリの上にも出す(level .screenSaver + fullScreenAuxiliary の前例)。
    /// ホスト(Dock に出る通常アプリ)は、手前にする間だけ accessory にする(他アプリの全画面の上に出すため)。
    func setTop(_ on: Bool) {
        if hostMode {
            NSApplication.shared.setActivationPolicy(on ? .accessory : .regular)
        }
        panel.level = on ? .screenSaver : .normal
        panel.collectionBehavior = on ? [.canJoinAllSpaces, .fullScreenAuxiliary] : []
        panel.orderFrontRegardless()
        if hostMode && !on { NSApplication.shared.activate(ignoringOtherApps: true) }
        diag(["phase": "top", "on": on])
    }

    // ---- ライブ字幕のウィンドウ ----
    static let captionBand: CGFloat = 40   // 上の帯の高さ(caption.html の .top と同じ・2026-09-27 に 52 → 40)

    /// 信号機ボタンを上の帯の縦の中央へ(Electron の trafficLightPosition と同じく、ボタンの入れ物の高さを帯に合わせる)。
    /// 大きさを変える・全画面から戻るとシステムが並べ直すので、そのたびに呼ぶ。
    func placeTrafficLights() {
        guard captionMode, let close = panel.standardWindowButton(.closeButton),
              let mini = panel.standardWindowButton(.miniaturizeButton), let zoom = panel.standardWindowButton(.zoomButton),
              let container = close.superview?.superview else { return }
        let band = Overlay.captionBand
        var r = container.frame
        r.size.height = band
        r.origin.y = panel.frame.height - band
        container.frame = r
        let gap = mini.frame.minX - close.frame.minX
        for (i, b) in [close, mini, zoom].enumerated() {
            b.setFrameOrigin(NSPoint(x: 20 + CGFloat(i) * gap, y: (band - b.frame.height) / 2))
        }
    }

    func setDragRegion(height: Double, rects: [[Double]]) {
        dragHeight = CGFloat(height)
        dragRects = rects.compactMap { $0.count == 4 ? NSRect(x: $0[0], y: $0[1], width: $0[2], height: $0[3]) : nil }
    }

    /// 帯の中で、信号機ボタンと画面の押せる部品の外なら true(座標は画面と同じ左上原点に直して比べる)。
    private func inDragRegion(_ p: NSPoint) -> Bool {
        guard captionMode, let h = panel.contentView?.bounds.height else { return false }
        let q = NSPoint(x: p.x, y: h - p.y)
        let band = dragHeight >= 0 ? dragHeight : Overlay.captionBand
        return q.y <= band && q.x > 90 && !dragRects.contains { $0.insetBy(dx: -2, dy: -2).contains(q) }
    }

    func windowDidResize(_ notification: Notification) { placeTrafficLights() }
    func windowDidExitFullScreen(_ notification: Notification) { placeTrafficLights() }
    func windowDidBecomeKey(_ notification: Notification) { placeTrafficLights() }

    func windowWillClose(_ notification: Notification) {
        if let m = dragMonitor { NSEvent.removeMonitor(m); dragMonitor = nil }
        onClose?()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard windowMode else { return true }
        if hostMode {   // Meeting Cue!.app: 閉じても終わらずメニューバーに残る。終了はメニューか ⌘Q
            panel.orderOut(nil)
            return false
        }
        // デバッグ起動(meetcue app が呼ぶ --window): 閉じる = 終了(meetcue app が後始末)
        NSApplication.shared.terminate(nil)
        return false
    }
}

/// メニューバーの録音中の赤い点。クリックは下のボタンへ通す。
final class RecordingDot: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSColor.systemRed.setFill()
        NSBezierPath(ovalIn: bounds).fill()
    }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

// ---- メニューバーのアイコンを押したときの画面(2026-09-27・同日 見た目を独自に) ------------------------
// NSMenu では角丸のボタンや色付きのタイルが作れないため、半透明の角丸パネル(MenuPanel)に SwiftUI で描く。
// 上から: アイコン・名前・状態(待機中 / 録音中の経過時間 / 保存中)/ 録音と LOCAL で録音の 2 枚のタイル(録音中は停止の
// タイル 1 枚)/ ライブ字幕・記録のフォルダ・ログイン時に起動(スイッチ)/ 設定 ⌘, ・更新を確認・終了 ⌘Q の小さなボタン。
// 地は画面と同じ明るい空色(見た目 B)・目印は基本色(⚙ の色)。

enum MenuAction { case login, start, startLocal, stop, caption, settings, folder, update, quit }

struct MenuState {
    var recording = false
    var saving = false
    var login: SMAppService.Status = .notRegistered
    var captionOn = false
    var accent = Color(red: 0.243, green: 0.839, blue: 0.784)   // 画面の基本色(⚙ の色・既定 #3ed6c8 のミント)
    var startedMs: Double? = nil                                 // 録音の開始(/api/state の started_ms・経過時間の表示)
}

private let menuRed = Color(red: 1.0, green: 0.365, blue: 0.424)    // 画面の --rec #ff5d6c
private let menuRedText = Color(red: 0.851, green: 0.204, blue: 0.275)   // 画面の --rec-text #d93446(明るい地の文字)
private let menuStar = Color(red: 1.0, green: 0.82, blue: 0.40)     // 画面の --star #ffd166(LOCAL の目印・固定)
let menuInk = Color(red: 0.106, green: 0.141, blue: 0.251)          // 画面の --fg #1b2440(明るい地の文字)
let menuBg = Color(red: 0.914, green: 0.976, blue: 0.976)           // 画面の --bg #e9f9f9
let menuBg2 = Color(red: 0.831, green: 0.945, blue: 0.949)          // 画面の --bg2 #d4f1f2

/// 2 色を t : 1-t で混ぜる(画面の color-mix(in srgb, a t, b) と同じ)。
func menuMix(_ a: Color, _ b: Color, _ t: Double) -> Color {
    guard let x = NSColor(a).usingColorSpace(.sRGB), let y = NSColor(b).usingColorSpace(.sRGB) else { return a }
    return Color(red: x.redComponent * t + y.redComponent * (1 - t), green: x.greenComponent * t + y.greenComponent * (1 - t),
                 blue: x.blueComponent * t + y.blueComponent * (1 - t))
}

/// 配色の組(画面の CSS と同じ値)。明るい外観 = 空色の地・白いカード / 暗い外観 = 夜の紺・暗いカード。
struct MenuPal {
    let dark: Bool
    var ink: Color { dark ? Color(red: 0.894, green: 0.945, blue: 0.961) : menuInk }                         // --fg
    var bg: Color { dark ? Color(red: 0.059, green: 0.114, blue: 0.161) : menuBg }                          // --bg
    var bg2: Color { dark ? Color(red: 0.039, green: 0.082, blue: 0.125) : menuBg2 }                        // --bg2
    var card: Color { dark ? Color(red: 0.090, green: 0.157, blue: 0.220) : .white }                        // --panel
    var cardHi: Color { dark ? Color(red: 0.118, green: 0.200, blue: 0.271) : .white }                      // --panel-hi
    var hover: Color { dark ? Color.white.opacity(0.08) : Color.white.opacity(0.75) }
    var rule: Color { dark ? Color.white.opacity(0.10) : Color.black.opacity(0.08) }
    var switchOff: Color { dark ? Color.white.opacity(0.20) : Color.black.opacity(0.14) }
    var shadow: Color { dark ? Color.black.opacity(0.35) : Color(red: 0.1, green: 0.27, blue: 0.4).opacity(0.10) }
    var recText: Color { dark ? Color(red: 1.0, green: 0.553, blue: 0.600) : menuRedText }                  // --rec-text
    /// 基本色の文字(明るい地 = 基本色 45% + #0b3d44 / 暗い地 = 基本色 82% + 白)。
    func accentText(_ c: Color) -> Color {
        dark ? menuMix(c, .white, 0.82) : menuMix(c, Color(red: 0.043, green: 0.239, blue: 0.267), 0.45)
    }
}

/// "#rrggbb" → Color(読めなければ nil)。
func menuColor(hex: String) -> Color? {
    let h = hex.hasPrefix("#") ? String(hex.dropFirst()) : hex
    guard h.count == 6, let v = UInt32(h, radix: 16) else { return nil }
    return Color(red: Double((v >> 16) & 0xff) / 255, green: Double((v >> 8) & 0xff) / 255, blue: Double(v & 0xff) / 255)
}

/// 録音の開始からの経過(mm:ss・1 時間を超えたら h:mm:ss)。
func menuElapsed(_ startedMs: Double?, _ now: Date) -> String {
    guard let ms = startedMs else { return "" }
    let s = max(0, Int((now.timeIntervalSince1970 * 1000 - ms) / 1000))
    return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60) : String(format: "%02d:%02d", s / 60, s % 60)
}

/// マウスが乗っているか。@State は Xcode なし(Command Line Tools だけ)ではマクロの部品が無く使えないため、
/// ObservableObject + @StateObject で持つ。
final class MenuHover: ObservableObject { @Published var on = false }

/// 押せる行。マウスが乗ると薄く明るくする。
struct MenuHoverButton<Label: View>: View {
    @Environment(\.colorScheme) private var scheme
    private var pal: MenuPal { MenuPal(dark: scheme == .dark) }
    let action: () -> Void
    @ViewBuilder let label: () -> Label
    @StateObject private var hover = MenuHover()
    var body: some View {
        Button(action: action) {
            label()
                .padding(.horizontal, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8).fill(hover.on ? pal.hover : .clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover.on = $0 }
    }
}

/// 録音・LOCAL で録音・停止のタイル(記号・題・一言)。solid は塗りつぶし(録音中の停止)。
struct MenuTile: View {
    @Environment(\.colorScheme) private var scheme
    private var pal: MenuPal { MenuPal(dark: scheme == .dark) }
    let symbol: String
    let title: String
    let sub: String
    let tint: Color
    var disabled = false
    var solid = false
    let action: () -> Void
    @StateObject private var hover = MenuHover()
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 2) {
                Image(systemName: symbol).font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(solid ? Color.white : tint).frame(height: 20)
                Spacer(minLength: 6)
                Text(title).font(.system(size: 13, weight: .bold)).foregroundStyle(solid ? Color.white : pal.ink).lineLimit(1)
                Text(sub).font(.system(size: 10.5)).foregroundStyle(solid ? Color.white.opacity(0.85) : Color.secondary).lineLimit(1)
            }
            .padding(EdgeInsets(top: 10, leading: 11, bottom: 9, trailing: 11))
            .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12).fill(solid ? tint.opacity(hover.on ? 0.88 : 1) : (hover.on ? pal.cardHi : pal.card.opacity(0.9))))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(tint.opacity(solid ? 0 : 0.45), lineWidth: 1.2))
            .shadow(color: pal.shadow, radius: 5, y: 2)
            .contentShape(RoundedRectangle(cornerRadius: 11))
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.45 : 1)
        .onHover { hover.on = $0 }
    }
}

/// ログイン時に起動の小さなスイッチ(状態は macOS の登録のまま・押すと切り替え)。
struct MenuSwitch: View {
    @Environment(\.colorScheme) private var scheme
    private var pal: MenuPal { MenuPal(dark: scheme == .dark) }
    let on: Bool
    let tint: Color
    var body: some View {
        ZStack(alignment: on ? .trailing : .leading) {
            Capsule().fill(on ? tint : pal.switchOff).frame(width: 30, height: 18)
            Circle().fill(Color.white).frame(width: 14, height: 14).padding(2).shadow(color: .black.opacity(0.25), radius: 1, y: 0.5)
        }
        .frame(width: 30, height: 18)
    }
}

/// 最下段の小さなボタン(記号の下に題と鍵)。
struct MenuFootButton: View {
    @Environment(\.colorScheme) private var scheme
    private var pal: MenuPal { MenuPal(dark: scheme == .dark) }
    let symbol: String
    let title: String
    let key: String
    let action: () -> Void
    @StateObject private var hover = MenuHover()
    var body: some View {
        Button(action: action) {
            VStack(spacing: 3) {
                Image(systemName: symbol).font(.system(size: 13, weight: .medium))
                HStack(spacing: 4) {
                    Text(title).font(.system(size: 11, weight: .semibold))
                    if !key.isEmpty { Text(key).font(.system(size: 10)).foregroundStyle(Color.secondary) }
                }
                .lineLimit(1).fixedSize()
            }
            .frame(maxWidth: .infinity, minHeight: 46)
            .background(RoundedRectangle(cornerRadius: 10).fill(hover.on ? pal.cardHi : pal.card.opacity(0.7)))
            .contentShape(RoundedRectangle(cornerRadius: 9))
        }
        .buttonStyle(.plain)
        .onHover { hover.on = $0 }
    }
}

struct MenuContent: View {
    @Environment(\.colorScheme) private var scheme
    private var pal: MenuPal { MenuPal(dark: scheme == .dark) }
    let s: MenuState
    let act: (MenuAction) -> Void
    var icon: NSImage? = nil

    private func chip(_ t: String, _ c: Color) -> some View {
        Text(t).font(.system(size: 11, weight: .semibold).monospacedDigit()).foregroundStyle(c)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(RoundedRectangle(cornerRadius: 6).fill(c.opacity(0.14)))
    }

    @ViewBuilder private var status: some View {
        if s.recording {
            TimelineView(.periodic(from: .now, by: 1)) { t in chip("● 録音中 \(menuElapsed(s.startedMs, t.date))", pal.recText) }
        } else if s.saving {
            chip("保存しています…", pal.accentText(s.accent))
        } else {
            chip("待機中", Color.secondary)
        }
    }

    private var head: some View {
        HStack(spacing: 9) {
            if let icon { Image(nsImage: icon).resizable().frame(width: 24, height: 24) }
            Text("Meeting Cue\(Text("!").foregroundStyle(pal.accentText(s.accent)))").font(.system(size: 14, weight: .heavy))
            Spacer(minLength: 8)
            status
        }
        .padding(.horizontal, 4).padding(.top, 2)
    }

    private func row(_ symbol: String, _ title: String, on: Bool = false, _ a: MenuAction) -> some View {
        MenuHoverButton(action: { act(a) }) {
            HStack(spacing: 10) {
                Image(systemName: symbol).font(.system(size: 13, weight: .medium)).foregroundStyle(pal.accentText(s.accent)).frame(width: 18)
                Text(title).font(.system(size: 13))
                Spacer(minLength: 8)
                if on { Text("表示中").font(.system(size: 11, weight: .semibold)).foregroundStyle(pal.accentText(s.accent)) }
            }
            .frame(height: 30)
        }
    }

    private var loginRow: some View {
        MenuHoverButton(action: { act(.login) }) {
            HStack(spacing: 10) {
                Image(systemName: "power").font(.system(size: 13, weight: .medium)).foregroundStyle(Color.secondary).frame(width: 18)
                Text(s.login == .requiresApproval ? "ログイン時に起動(許可が必要)" : "ログイン時に起動").font(.system(size: 13))
                Spacer(minLength: 8)
                MenuSwitch(on: s.login == .enabled, tint: s.accent)
            }
            .frame(height: 30)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            head
            if s.recording {
                MenuTile(symbol: "stop.fill", title: "録音を停止", sub: "回答とまとめを仕上げて保存します", tint: menuRed, solid: true) { act(.stop) }
            } else {
                HStack(spacing: 8) {
                    MenuTile(symbol: "record.circle", title: s.saving ? "保存しています…" : "録音を開始", sub: "会議に参加・題名なし",
                             tint: menuRed, disabled: s.saving) { act(.start) }
                    MenuTile(symbol: "lock.fill", title: "LOCAL で録音", sub: "クラウドへ送らない",
                             tint: menuStar, disabled: s.saving) { act(.startLocal) }
                }
            }
            VStack(spacing: 0) {
                row("captions.bubble", "ライブ字幕", on: s.captionOn, .caption)
                row("folder", "記録のフォルダを開く", .folder)
                loginRow
            }
            Rectangle().fill(pal.rule).frame(height: 1)
            HStack(spacing: 6) {
                MenuFootButton(symbol: "gearshape", title: "設定", key: "⌘,") { act(.settings) }
                MenuFootButton(symbol: "arrow.triangle.2.circlepath", title: "更新を確認", key: "") { act(.update) }
                MenuFootButton(symbol: "power", title: "終了", key: "⌘Q") { act(.quit) }
            }
        }
        .padding(10)
        .frame(width: 296)
        .foregroundStyle(pal.ink)
        .background(LinearGradient(colors: [pal.bg, pal.bg2], startPoint: .top, endPoint: .bottom).opacity(0.93))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(s.accent.opacity(0.35)))
    }
}

// ---- アップデートの画面(2026-09-27) ------------------------------------------------------------
// 形は UpdateView の説明を参照(2026-09-27 に見た目を独自に)。

struct UpdateNote { let subject: String; let detail: String }

struct UpdateInfo {
    var available = false
    var head = ""
    var date = ""
    var notes: [UpdateNote] = []
    var rebuild = false
    var recording = false
    var accent = MenuState().accent
}

/// 「自動で行う」のチェック。@State は Command Line Tools だけでは使えないため ObservableObject で持ち、変えたらすぐ保存する。
final class UpdateModel: ObservableObject {
    static let autoKey = "meetcueAutoUpdate"
    @Published var auto: Bool { didSet { UserDefaults.standard.set(auto, forKey: UpdateModel.autoKey) } }
    init() { auto = UserDefaults.standard.bool(forKey: UpdateModel.autoKey) }
}

/// 基本色で塗った上の文字: 明るい基本色なら濃い紺、暗い基本色なら白(画面の --accent-ink と同じ決め方)。
func inkOn(_ c: Color) -> Color {
    guard let n = NSColor(c).usingColorSpace(.sRGB) else { return .white }
    let l = 0.2126 * n.redComponent + 0.7152 * n.greenComponent + 0.0722 * n.blueComponent
    return l > 0.55 ? Color(red: 0.043, green: 0.075, blue: 0.133) : .white
}

/// アップデートの画面(2026-09-27・同日 見た目を独自に): 上にアイコン・見出し・版の札、中に変更内容(件名 = 太字・
/// 本文の 1 行目 = 説明)、下に「自動で更新する」スイッチ・幅いっぱいの「今すぐ更新して再起動」・「終了時に更新」
/// 「この版はスキップ」の文字ボタン。最新なら見出しと札と「閉じる」だけ。
struct UpdateView: View {
    @Environment(\.colorScheme) private var scheme
    private var pal: MenuPal { MenuPal(dark: scheme == .dark) }
    let info: UpdateInfo
    @ObservedObject var model: UpdateModel
    let icon: NSImage?
    var preview = false            // 画像に描くとき(ScrollView は描けないので並べるだけ)
    let act: (String) -> Void      // skip / onquit / now / ok

    private func chip(_ t: String) -> some View {
        Text(t).font(.system(size: 11, weight: .semibold).monospacedDigit()).foregroundStyle(Color.secondary)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(RoundedRectangle(cornerRadius: 7).fill(pal.card.opacity(0.8)))
            .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(pal.accentText(info.accent).opacity(0.35)))
    }

    private func primary(_ title: String, _ a: String, enabled: Bool = true) -> some View {
        Button { act(a) } label: {
            Text(title).font(.system(size: 13, weight: .bold)).foregroundStyle(inkOn(info.accent))
                .frame(maxWidth: .infinity).frame(height: 36)
                .background(RoundedRectangle(cornerRadius: 10).fill(info.accent))
                .contentShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.4)
    }

    private func link(_ title: String, _ a: String) -> some View {
        Button { act(a) } label: {
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(pal.accentText(info.accent))
                .padding(.horizontal, 6).padding(.vertical, 4).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var notesList: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(info.notes.indices, id: \.self) { i in
                let n = info.notes[i]
                HStack(alignment: .firstTextBaseline, spacing: 9) {
                    Image(systemName: "checkmark.circle.fill").font(.system(size: 12)).foregroundStyle(pal.accentText(info.accent))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(n.subject).font(.system(size: 12.5, weight: .semibold)).fixedSize(horizontal: false, vertical: true)
                        if !n.detail.isEmpty {
                            Text(n.detail).font(.system(size: 11.5)).foregroundStyle(Color.secondary).lineSpacing(2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .center, spacing: 14) {
                if let icon { Image(nsImage: icon).resizable().frame(width: 58, height: 58) }
                VStack(alignment: .leading, spacing: 7) {
                    Text(info.available ? "アップデートがあります" : "最新の版です").font(.system(size: 17, weight: .heavy))
                    HStack(spacing: 6) {
                        chip(info.head)
                        chip(info.date)
                        if info.available { chip("変更 \(info.notes.count) 件") }
                    }
                }
            }
            if info.available {
                VStack(alignment: .leading, spacing: 6) {
                    Text("変更内容").font(.system(size: 10.5, weight: .heavy)).tracking(1.2).foregroundStyle(pal.accentText(info.accent))
                    Group {
                        if preview { notesList } else { ScrollView { notesList }.frame(maxHeight: 220) }
                    }
                    .background(RoundedRectangle(cornerRadius: 14).fill(pal.card))
                    .shadow(color: pal.shadow, radius: 8, y: 3)
                    if info.rebuild {
                        Label("画面の部品を作り直すため、再起動まで 1 分ほどかかります", systemImage: "hammer")
                            .font(.system(size: 11)).foregroundStyle(Color.secondary)
                    }
                }
                Button { model.auto.toggle() } label: {   // 標準の Toggle は画像に描けないので、メニューと同じスイッチ
                    HStack {
                        Text("自動で更新する(終了するときに反映)").font(.system(size: 12.5))
                        Spacer()
                        MenuSwitch(on: model.auto, tint: info.accent)
                    }
                    .padding(.horizontal, 12).frame(height: 38)
                    .background(RoundedRectangle(cornerRadius: 12).fill(pal.card.opacity(0.8)))
                    .contentShape(RoundedRectangle(cornerRadius: 12))
                }
                .buttonStyle(.plain)
                VStack(spacing: 6) {
                    primary(info.recording ? "録音中・保存中は再起動できません" : "今すぐ更新して再起動", "now", enabled: !info.recording)
                    HStack(spacing: 4) {
                        Spacer()
                        link("終了時に更新", "onquit")
                        Text("·").foregroundStyle(Color.secondary)
                        link("この版はスキップ", "skip")
                        Spacer()
                    }
                }
            } else {
                primary("閉じる", "ok")
            }
        }
        .padding(EdgeInsets(top: 40, leading: 24, bottom: 18, trailing: 24))
        .frame(width: 480)
        .foregroundStyle(pal.ink)
    }
}

/// 見た目の確認用(--render-update <dir>): 新しい版がある / 最新 の 2 つを PNG に描いて終わる。画面は撮らない。
@MainActor func renderUpdatePreview(to dir: String) {
    let icon = NSImage(contentsOfFile: "packaging/icon/AppIcon.icns")
    let notes = [UpdateNote(subject: "質問の行の時刻ずれを直す(長い無音の後の質問が最大 60 s 早く記録されていた)",
                            detail: "主因(セグメンター): 長さで切った final の切れ端を繰り越し、単独で出すとき、最後の partial の範囲を使っていた。"),
                 UpdateNote(subject: "置き換え辞書: 音声認識がよく間違える固有名詞を正しい語へ(⚙ で編集)",
                            detail: "掛ける場所: 確定した発言(記録・質問の判定・回答候補・サマリ・書き出し)と途中の表示。")]
    let cases: [(String, UpdateInfo)] = [
        ("available", UpdateInfo(available: true, head: "7bce2a8", date: "2026-09-27 03:10", notes: notes, rebuild: true)),
        ("latest", UpdateInfo(available: false, head: "7bce2a8", date: "2026-09-27 03:10")),
    ]
    for (name0, info) in cases { for dark in [false, true] {
        let name = dark ? name0 + "-dark" : name0, pal = MenuPal(dark: dark)
        let v = UpdateView(info: info, model: UpdateModel(), icon: icon, preview: true, act: { _ in })
            .background(LinearGradient(colors: [pal.bg, pal.bg2], startPoint: .top, endPoint: .bottom))
            .environment(\.colorScheme, dark ? .dark : .light)
        let r = ImageRenderer(content: v)
        r.scale = 2
        if let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("update-\(name).png"))
        }
    } }
}

/// 鍵を受け取れる(Esc で閉じる)が、アプリを前面に出さないパネル。
final class MenuPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

/// パネルがキーでなくても 1 回目のクリックで押せるようにする。
final class MenuHostingView: NSHostingView<MenuContent> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// 見た目の確認用(--render-menu <dir>): 3 つの状態を PNG に描いて終わる。画面は撮らない。
@MainActor func renderMenuPreview(to dir: String) {
    let icon = NSImage(contentsOfFile: "packaging/icon/AppIcon.icns")
    let started = Date().timeIntervalSince1970 * 1000 - 192_000
    let states: [(String, MenuState)] = [
        ("idle", MenuState(recording: false, saving: false, login: .notRegistered, captionOn: false)),
        ("recording", MenuState(recording: true, saving: false, login: .enabled, captionOn: true, startedMs: started)),
        ("saving", MenuState(recording: false, saving: true, login: .enabled, captionOn: false)),
    ]
    for (name0, s) in states { for dark in [false, true] {
        let name = dark ? name0 + "-dark" : name0
        let v = MenuContent(s: s, act: { _ in }, icon: icon)
            .background(dark ? Color(red: 0.08, green: 0.10, blue: 0.13) : Color.white)   // 半透明の下地(実物は背景がぼけて透ける)
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .environment(\.colorScheme, dark ? .dark : .light)
        let r = ImageRenderer(content: v)
        r.scale = 2
        if let img = r.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("menu-\(name).png"))
        }
    } }
}

/// Meeting Cue!.app の本体: meetcue app を子プロセスで動かし、その画面を出す(Terminal を開かない)。
final class Host: NSObject, NSApplicationDelegate {
    let script: String
    let url: URL
    private var child: Process?
    private var quitting = false
    private var statusItem: NSStatusItem?
    private var pollTimer: Timer?
    private var recording = false
    private var saving = false          // 停止後、回答とサマリを仕上げている間(/api/state の saving)
    private var menuPanel: MenuPanel?
    private var menuHost: MenuHostingView?
    private var menuMonitors: [Any] = []
    private var accent = MenuState().accent   // 画面の基本色(⚙ で変えられる)。パネルを開くたびに /api/settings から読む
    private var startedMs: Double?            // 録音の開始(パネルの経過時間)
    private(set) var caption: Overlay?   // ライブ字幕(半透明の最前面パネル)

    init(script: String, url: URL) {
        self.script = script
        self.url = url
    }

    func start() {
        gOverlay?.showMessage("起動しています…", "Meeting Cue! の本体を起動しています。")
        DispatchQueue.global().async {
            if self.serverUp() {   // デバッグ起動の本体が既に動いている → つなぐだけ(閉じても本体は止めない)
                DispatchQueue.main.async { gOverlay?.load(self.url) }
                return
            }
            DispatchQueue.main.async { self.spawn() }
        }
    }

    private func spawn() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script, "serve"]
        p.standardInput = FileHandle.nullDevice
        p.terminationHandler = { pr in
            // main queue ではなく run loop に積む(2026-09-27): main queue のブロックの中から終了(terminateLater)に入ると、
            // 待つ間の入れ子の run loop は main queue の次のブロックを実行できず、子の終了を受け取れずに固まっていた
            // (SIGTERM の DispatchSource の中・main queue で runModal したアラートを開いたままの ⌘Q / osascript quit)。
            // run loop に積んだブロックは入れ子の run loop(modalPanel)でも実行される
            RunLoop.main.perform(inModes: [.common, .modalPanel]) { self.childExited(pr.terminationStatus) }
            CFRunLoopWakeUp(CFRunLoopGetMain())
        }
        do { try p.run() } catch {
            gOverlay?.showMessage("起動できませんでした", "\(error)")
            return
        }
        child = p
        diag(["phase": "host_spawn", "pid": p.processIdentifier])
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(90)
            while Date() < deadline {
                if self.child?.isRunning != true { return }
                if self.serverUp() {
                    DispatchQueue.main.async { gOverlay?.load(self.url) }
                    return
                }
                Thread.sleep(forTimeInterval: 0.3)
            }
            DispatchQueue.main.async {
                gOverlay?.showMessage("起動に時間がかかっています", "90 秒待っても本体が応答しません。ログ: ~/.meeting-cue/logs/")
            }
        }
    }

    private func serverUp() -> Bool {
        var req = URLRequest(url: url.appendingPathComponent("api/state"))
        req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.5)
        return ok
    }

    private func childExited(_ status: Int32) {
        diag(["phase": "host_child_exit", "status": status])
        child = nil
        if quitting {
            NSApplication.shared.reply(toApplicationShouldTerminate: true)
        } else {
            gOverlay?.showMessage("本体が終了しました",
                                  "終了コード \(status)。ログ: ~/.meeting-cue/logs/ 。ウィンドウを閉じて、もう一度起動してください。")
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let c = child, c.isRunning else { return .terminateNow }
        if quitting { return .terminateLater }
        quitting = true
        gOverlay?.showMessage("終了しています…", "録音中なら止めて、記録とサマリを保存しています(最大 2 分)。")
        c.interrupt()   // SIGINT → meetcue app が録音を正規に止める(Ctrl-C と同じ後始末)
        DispatchQueue.main.asyncAfter(deadline: .now() + 120) {
            if c.isRunning { c.terminate() }
        }
        return .terminateLater
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMain()
        return true
    }

    // ---- メニューバー --------------------------------------------------------------------
    // 形は Resources の MenuBarTemplate.png / @2x(18pt のテンプレート画像・2026-09-26 PixivStudio の C-D-01d-02 を
    // packaging/icon/make_icons.sh で作ったもの)。録音中は右上に赤い点を重ねる: 形は点の周りを丸く抜いたテンプレートの
    // まま(明暗はシステムが塗る)、点は上に置いた別の view。画像が無い(アプリの外で動かした)ときは SF Symbols。
    private static let glyph: NSImage? = {
        guard let img = Bundle.main.image(forResource: "MenuBarTemplate") else { return nil }   // @2x も一緒に読む
        img.isTemplate = true
        img.accessibilityDescription = "Meeting Cue!"
        return img
    }()
    private static let dotDiameter: CGFloat = 6, dotGap: CGFloat = 1.25   // pt。点は 18pt の枠の右上に接する
    private static let punchedGlyph: NSImage? = glyph.map { g in
        let img = NSImage(size: g.size, flipped: false) { rect in
            g.draw(in: rect)
            let r = dotDiameter / 2 + dotGap
            let cx = rect.maxX - dotDiameter / 2, cy = rect.maxY - dotDiameter / 2
            NSGraphicsContext.current?.compositingOperation = .clear
            NSBezierPath(ovalIn: NSRect(x: cx - r, y: cy - r, width: 2 * r, height: 2 * r)).fill()
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = "Meeting Cue! — 録音中"
        return img
    }
    private var recDot: NSView?

    func installStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.target = self      // 押したら角丸のパネル(MenuPanel)を開く・閉じる
        item.button?.action = #selector(toggleMenuPanel)
        item.button?.sendAction(on: [.leftMouseDown, .rightMouseDown])   // メニューと同じく押した瞬間に開く
        statusItem = item
        if let g = Host.glyph, let b = item.button {
            b.imagePosition = .imageOnly
            let d = Host.dotDiameter
            let dot = RecordingDot()
            dot.isHidden = true
            dot.translatesAutoresizingMaskIntoConstraints = false
            b.addSubview(dot)
            NSLayoutConstraint.activate([   // 形はボタンの中央に置かれる → 中央から右上へずらす
                dot.widthAnchor.constraint(equalToConstant: d),
                dot.heightAnchor.constraint(equalToConstant: d),
                dot.centerXAnchor.constraint(equalTo: b.centerXAnchor, constant: (g.size.width - d) / 2),
                dot.centerYAnchor.constraint(equalTo: b.centerYAnchor, constant: -(g.size.height - d) / 2),
            ])
            recDot = dot
        }
        updateIcon()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in self?.pollState() }
    }

    private func updateIcon() {
        guard let b = statusItem?.button else { return }
        b.toolTip = recording ? "Meeting Cue! — 録音中" : "Meeting Cue!"
        if let g = Host.glyph {
            b.image = recording ? Host.punchedGlyph : g
            recDot?.isHidden = !recording
            return
        }
        let img = NSImage(systemSymbolName: recording ? "record.circle.fill" : "waveform", accessibilityDescription: "Meeting Cue!")
        img?.isTemplate = !recording
        b.image = img
        b.contentTintColor = recording ? .systemRed : nil
    }

    private func pollState() {
        var req = URLRequest(url: url.appendingPathComponent("api/state"))
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let rec = (obj["recording"] as? Bool) ?? false
            let sav = (obj["saving"] as? Bool) ?? false
            let started = obj["started_ms"] as? Double
            DispatchQueue.main.async {
                let changed = rec != self.recording || sav != self.saving || started != self.startedMs
                self.saving = sav
                self.startedMs = started
                if rec != self.recording { self.recording = rec; self.updateIcon() }
                if changed && self.menuPanel?.isVisible == true { self.layoutMenuPanel() }   // 開いている間も状態に合わせる
            }
        }.resume()
    }

    // ---- 角丸のパネル(MenuPanel) ----
    private var menuState: MenuState {
        MenuState(recording: recording, saving: saving, login: SMAppService.mainApp.status, captionOn: caption != nil,
                  accent: accent, startedMs: startedMs)
    }

    /// ⚙ の基本色(colors.accent)を読み、変わっていれば開いているパネルを描き直す。
    private func fetchAccent() {
        var req = URLRequest(url: url.appendingPathComponent("api/settings"))
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let hex = (obj["colors"] as? [String: Any])?["accent"] as? String, let c = menuColor(hex: hex) else { return }
            DispatchQueue.main.async {
                guard c != self.accent else { return }
                self.accent = c
                if self.menuPanel?.isVisible == true { self.layoutMenuPanel() }
            }
        }.resume()
    }

    @objc func toggleMenuPanel() {
        if menuPanel?.isVisible == true { closeMenuPanel() } else { openMenuPanel() }
    }

    private func openMenuPanel() {
        if menuPanel == nil {
            let host = MenuHostingView(rootView: MenuContent(s: menuState, act: { [weak self] in self?.menuAct($0) }, icon: NSApplication.shared.applicationIconImage))
            let effect = NSVisualEffectView()
            effect.material = .popover
            effect.blendingMode = .behindWindow
            effect.state = .active
            effect.maskImage = Host.roundedMask(14)
            host.translatesAutoresizingMaskIntoConstraints = false
            effect.addSubview(host)
            NSLayoutConstraint.activate([
                host.leadingAnchor.constraint(equalTo: effect.leadingAnchor),
                host.trailingAnchor.constraint(equalTo: effect.trailingAnchor),
                host.topAnchor.constraint(equalTo: effect.topAnchor),
                host.bottomAnchor.constraint(equalTo: effect.bottomAnchor),
            ])
            let p = MenuPanel(contentRect: NSRect(x: 0, y: 0, width: 296, height: 320),
                              styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isOpaque = false
            p.backgroundColor = .clear
            p.hasShadow = true
            p.level = .popUpMenu
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
            p.hidesOnDeactivate = false
            p.contentView = effect
            menuPanel = p
            menuHost = host
        }
        layoutMenuPanel()
        fetchAccent()
        menuPanel?.makeKeyAndOrderFront(nil)
        statusItem?.button?.highlight(true)
        // 外を押したら閉じる(他のアプリ = global・このアプリの別のウィンドウ = local)。Esc でも閉じる。
        // パネルはアプリを前面に出さないので、⌘, / ⌘Q もここで受ける
        let g = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            self?.closeMenuPanel()
        }
        let l = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .keyDown]) { [weak self] e in
            guard let self, let p = self.menuPanel else { return e }
            if e.type == .keyDown {
                if e.keyCode == 53 { self.closeMenuPanel(); return nil }   // Esc
                if e.modifierFlags.contains(.command) {
                    if e.charactersIgnoringModifiers == "q" { self.menuAct(.quit); return nil }
                    if e.charactersIgnoringModifiers == "," { self.menuAct(.settings); return nil }
                }
                return e
            }
            if e.window === p || e.window === self.statusItem?.button?.window { return e }   // アイコンは toggle が受ける
            self.closeMenuPanel()
            return e
        }
        menuMonitors = [g, l].compactMap { $0 }
    }

    /// 状態に合わせて中身を描き直し、大きさと位置(アイコンの真下・画面の端からはみ出さない)を決める。
    private func layoutMenuPanel() {
        guard let p = menuPanel, let host = menuHost, let bw = statusItem?.button?.window else { return }
        host.rootView = MenuContent(s: menuState, act: { [weak self] in self?.menuAct($0) }, icon: NSApplication.shared.applicationIconImage)
        host.layoutSubtreeIfNeeded()
        let size = host.fittingSize
        let bf = bw.frame
        let vf = (bw.screen ?? NSScreen.main)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let x = min(max(bf.midX - size.width / 2, vf.minX + 6), vf.maxX - size.width - 6)
        p.setFrame(NSRect(x: x, y: bf.minY - size.height - 5, width: size.width, height: size.height), display: true)
    }

    private func closeMenuPanel() {
        menuMonitors.forEach { NSEvent.removeMonitor($0) }
        menuMonitors = []
        menuPanel?.orderOut(nil)
        statusItem?.button?.highlight(false)
    }

    private func menuAct(_ a: MenuAction) {
        closeMenuPanel()
        switch a {
        case .login: toggleLogin()
        case .start: startRecording()
        case .startLocal: startRecordingLocal()
        case .stop: stopRecording()
        case .update: checkUpdate()
        case .caption: toggleCaption()
        case .settings: openSettings()
        case .folder: openSessionsFolder()
        case .quit: quitApp()
        }
    }

    /// 角丸の切り抜き(NSVisualEffectView の maskImage。9 分割で伸ばす)。
    private static func roundedMask(_ r: CGFloat) -> NSImage {
        let img = NSImage(size: NSSize(width: 2 * r + 1, height: 2 * r + 1), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: r, yRadius: r).fill()
            return true
        }
        img.capInsets = NSEdgeInsets(top: r, left: r, bottom: r, right: r)
        img.resizingMode = .stretch
        return img
    }

    private func post(_ body: [String: Any]) {
        var req = URLRequest(url: url.appendingPathComponent("api/action"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { _, _, _ in
            DispatchQueue.main.async { self.pollState() }
        }.resume()
    }

    @objc func startRecording() { post(["action": "start", "mode": "participant", "privacy": "private"]) }
    @objc func startRecordingLocal() { post(["action": "start", "mode": "participant", "privacy": "local"]) }
    @objc func stopRecording() { post(["action": "stop"]) }
    @objc func quitApp() { NSApplication.shared.terminate(nil) }

    @objc func showMain() {
        NSApplication.shared.activate(ignoringOtherApps: true)
        gOverlay?.panel.makeKeyAndOrderFront(nil)
    }

    @objc func openSettings() {
        showMain()
        gOverlay?.web.evaluateJavaScript("document.getElementById('setbtn')?.click()", completionHandler: nil)
    }

    @objc func openSessionsFolder() {
        let dir = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".meeting-cue/sessions")
        NSWorkspace.shared.open(dir)
    }

    /// ライブ字幕: 文字起こしと回答候補を流す半透明の最前面パネル(/index.html・全画面の会議アプリの上にも出る)。
    /// ⌃⌥L でクリック透過の固定 ⇄ 移動、⌃⌥H で表示/非表示。
    @objc func toggleCaption() {
        if let c = caption {
            c.panel.close()   // windowWillClose → captionClosed
            return
        }
        // 2026-09-27: ライブ字幕の画面(caption.html)。録音していなくても字幕だけを動かす(本体が保存なしで動かす)
        let c = Overlay(url: url.appendingPathComponent("caption.html"), width: 860, height: 480, opacity: 1.0, top: false,
                        caption: true)
        c.onClose = { [weak self] in self?.captionClosed() }
        caption = c
    }

    /// 字幕のウィンドウを閉じた: 本体の字幕だけの動きも止める(画面の合図が途絶えても 20 s で止まるが、すぐ止める)。
    private func captionClosed() {
        caption = nil
        post(["action": "caption", "on": false])
    }

    // ---- アップデートを確認(2026-09-27): 手元の repo に、動いている版より新しいコミットがあるか ----
    // メニューから(manual)は必ず画面を出す(最新なら「最新の版です」)。起動の 30 s 後と 30 分ごとの確認(自動)は、新しい版が
    // あって録音中でなく、スキップした版でも一度出した版でもないときだけ出す。「自動で行う」なら出さずに終了時に反映する
    static let skipKey = "meetcueSkipHead"
    private let updateModel = UpdateModel()
    private var updateWin: NSWindow?
    private var promptedHead = ""
    private var installOnQuit = false
    private var rebuildOnQuit = false
    private var updateTimer: Timer?

    @objc func checkUpdate() { checkUpdate(manual: true) }

    func startUpdateChecks() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self] in self?.checkUpdate(manual: false) }
        updateTimer = Timer.scheduledTimer(withTimeInterval: 1800, repeats: true) { [weak self] _ in self?.checkUpdate(manual: false) }
    }

    private func checkUpdate(manual: Bool) {
        let build = Bundle.main.object(forInfoDictionaryKey: "MeetcueBuildCommit") as? String ?? ""
        var comps = URLComponents(url: url.appendingPathComponent("api/version"), resolvingAgainstBaseURL: false)
        comps?.queryItems = [URLQueryItem(name: "app", value: build)]
        guard let u = comps?.url else { return }
        var req = URLRequest(url: u)
        req.timeoutInterval = 10
        URLSession.shared.dataTask(with: req) { data, _, _ in
            let v = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { self.handleUpdate(v, manual: manual) }
        }.resume()
    }

    private func updateInfo(_ v: [String: Any]) -> UpdateInfo {
        let notes = (v["notes"] as? [[String: Any]] ?? []).map {
            UpdateNote(subject: $0["subject"] as? String ?? "", detail: $0["detail"] as? String ?? "")
        }
        let behind = v["behind"] as? Int ?? 0, rebuild = v["rebuild"] as? Bool ?? false
        return UpdateInfo(available: behind > 0 || rebuild, head: v["head"] as? String ?? "", date: v["date"] as? String ?? "",
                          notes: notes, rebuild: rebuild, recording: v["recording"] as? Bool ?? false, accent: accent)
    }

    private func handleUpdate(_ v: [String: Any]?, manual: Bool) {
        guard let v else {
            if manual {
                let a = NSAlert()
                a.messageText = "アップデートを確認できませんでした"
                a.informativeText = "本体が応答しません。少し待ってからもう一度お試しください。"
                NSApplication.shared.activate(ignoringOtherApps: true)
                a.runModal()
            }
            return
        }
        let info = updateInfo(v)
        diag(["phase": "update_check", "manual": manual, "available": info.available, "head": info.head, "rebuild": info.rebuild])
        if !manual {
            guard info.available, !info.recording else { return }
            if updateModel.auto {   // 自動で行う: 画面は出さず、終了時に反映する
                installOnQuit = true
                rebuildOnQuit = info.rebuild
                return
            }
            if info.head == UserDefaults.standard.string(forKey: Host.skipKey) || info.head == promptedHead { return }
            promptedHead = info.head
        }
        presentUpdate(info)
    }

    private func presentUpdate(_ info: UpdateInfo) {
        let view = UpdateView(info: info, model: updateModel, icon: NSApplication.shared.applicationIconImage,
                              act: { [weak self] a in self?.updateAct(a, info) })
        let host = NSHostingView(rootView: view)
        let size = host.fittingSize
        let w = updateWin ?? {
            let w = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.titled, .closable, .fullSizeContentView],
                             backing: .buffered, defer: false)
            w.titlebarAppearsTransparent = true
            w.titleVisibility = .hidden
            w.title = "アップデート"
            w.backgroundColor = meetcueWindowBg
            w.isReleasedWhenClosed = false
            w.standardWindowButton(.miniaturizeButton)?.isEnabled = false
            w.standardWindowButton(.zoomButton)?.isEnabled = false
            return w
        }()
        updateWin = w
        w.contentView = host
        w.setContentSize(size)
        w.center()
        NSApplication.shared.activate(ignoringOtherApps: true)
        w.makeKeyAndOrderFront(nil)
    }

    private func updateAct(_ a: String, _ info: UpdateInfo) {
        updateWin?.close()
        switch a {
        case "skip":
            UserDefaults.standard.set(info.head, forKey: Host.skipKey)
        case "onquit":
            installOnQuit = true
            rebuildOnQuit = info.rebuild
        case "now":
            restartForUpdate(rebuild: info.rebuild)
        default:
            break
        }
        diag(["phase": "update_action", "action": a, "head": info.head])
    }

    /// packaging/update_app.sh をアプリと切り離して起動する(アプリの終了を待って、要るなら作り直し、relaunch なら開き直す)。
    private func spawnUpdater(rebuild: Bool, relaunch: Bool) -> Bool {
        let upd = URL(fileURLWithPath: script).deletingLastPathComponent().appendingPathComponent("update_app.sh").path
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-c", "nohup \"$0\" \"$1\" \"$2\" \"$3\" >/dev/null 2>&1 &", upd, String(getpid()),
                       rebuild ? "rebuild" : "", relaunch ? "" : "norelaunch"]
        do {
            try p.run()
            p.waitUntilExit()
        } catch {
            diag(["phase": "update_error", "error": "\(error)"])
            return false
        }
        return true
    }

    private func restartForUpdate(rebuild: Bool) {
        guard spawnUpdater(rebuild: rebuild, relaunch: true) else { return }
        diag(["phase": "update_restart", "rebuild": rebuild])
        installOnQuit = false
        NSApplication.shared.terminate(nil)
    }

    /// 「終了時にインストール」: 本体(Python)の変更は次の起動で読み込まれるので何もしない。画面の部品(Swift)の変更だけ、
    /// 終了を待って作り直す(開き直しはしない)
    func applicationWillTerminate(_ notification: Notification) {
        if installOnQuit && rebuildOnQuit {
            _ = spawnUpdater(rebuild: true, relaunch: false)
            diag(["phase": "update_on_quit", "rebuild": true])
        }
    }

    @objc func toggleLogin() {
        let svc = SMAppService.mainApp
        do {
            if svc.status == .enabled { try svc.unregister() } else { try svc.register() }
        } catch {
            diag(["phase": "login_item_error", "error": "\(error)"])
        }
        diag(["phase": "login_item", "status": svc.status.rawValue])
        if svc.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }   // システム設定で許可が要る
    }

    /// ⌘Q と、Web 画面の入力欄で ⌘C / ⌘V を効かせるための最小のメニュー。
    static func installMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        let settings = appMenu.addItem(withTitle: "設定を開く…", action: #selector(Host.openSettings), keyEquivalent: ",")
        settings.target = gHost
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Meeting Cue! を終了", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "編集")
        edit.addItem(withTitle: "取り消す", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "やり直す", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "カット", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "コピー", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "ペースト", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "すべてを選択", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.addItem(.separator())
        edit.addItem(withTitle: "ウィンドウを閉じる", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        editItem.submenu = edit
        NSApplication.shared.mainMenu = main
    }
}

@main
struct OverlayHelper {
    static func main() {
        let args = CommandLine.arguments
        func opt(_ name: String, _ def: String) -> String {
            if let i = args.firstIndex(of: name), i + 1 < args.count { return args[i + 1] }
            return def
        }
        if args.contains("--render-menu") {   // 見た目の確認用: メニューのパネルを PNG に描いて終わる
            MainActor.assumeIsolated { renderMenuPreview(to: opt("--render-menu", ".")) }
            exit(0)
        }
        if args.contains("--render-update") {   // 見た目の確認用: アップデートの画面を PNG に描いて終わる
            MainActor.assumeIsolated { renderUpdatePreview(to: opt("--render-update", ".")) }
            exit(0)
        }
        let hostScript = Bundle.main.object(forInfoDictionaryKey: "MeetcueLaunchScript") as? String
        guard let url = URL(string: opt("--url", "http://127.0.0.1:8765/")) else { diag(["phase": "abort", "reason": "bad_url"]); exit(2) }
        let window = args.contains("--window") || hostScript != nil
        let captionOnly = hostScript == nil && args.contains("--caption")   // ライブ字幕のウィンドウだけ(確かめる用・--url は caption.html)
        let width = CGFloat(Double(opt("--width", window ? "1180" : "980")) ?? 980)
        let height = CGFloat(Double(opt("--height", window ? "760" : "560")) ?? 560)
        let opacity = CGFloat(Double(opt("--opacity", "0.94")) ?? 0.94)
        let top = window ? false : opt("--level", "top") == "top"

        let app = NSApplication.shared
        if let hostScript {
            app.setActivationPolicy(.regular)     // Dock に出る通常のアプリ
            gHost = Host(script: hostScript, url: url)
            Host.installMenu()
            app.delegate = gHost
        } else {
            app.setActivationPolicy(.accessory)   // Dock に出さない・フォーカスを奪わない
        }

        // ホットキー ⌃⌥L(固定)/ ⌃⌥H(表示切替)/ ⌃⌥R(再読込)
        let handler: EventHandlerUPP = { (_, event, _) -> OSStatus in
            var hk = EventHotKeyID()
            let st = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                       nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
            if st == noErr {
                DispatchQueue.main.async {
                    switch hk.id {
                    case 1: (gHost?.caption ?? gOverlay)?.toggleLock()
                    case 2: (gHost?.caption ?? gOverlay)?.toggleHidden()
                    case 3: gOverlay?.reload(); gHost?.caption?.reload()
                    default: break
                    }
                }
            }
            return noErr
        }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetEventDispatcherTarget(), handler, 1, &spec, nil, nil)
        let mods = UInt32(controlKey | optionKey)
        var refs: [EventHotKeyRef?] = [nil, nil, nil]
        for (id, key) in [(UInt32(1), kVK_ANSI_L), (UInt32(2), kVK_ANSI_H), (UInt32(3), kVK_ANSI_R)] {
            let hid = EventHotKeyID(signature: OSType(0x4D43_4F56), id: id)  // 'MCOV'
            _ = RegisterEventHotKey(UInt32(key), mods, hid, GetEventDispatcherTarget(), 0, &refs[Int(id) - 1])
        }

        gOverlay = captionOnly
            ? Overlay(url: url, width: 860, height: 480, opacity: 1.0, top: false, caption: true)
            : Overlay(url: hostScript == nil ? url : nil, width: width, height: height,
                      opacity: window ? 1.0 : opacity, top: top, window: window)
        if captionOnly { gOverlay?.onClose = { NSApplication.shared.terminate(nil) } }
        if hostScript != nil {
            gOverlay?.hostMode = true
            gHost?.installStatusItem()
            gHost?.start()
            gHost?.startUpdateChecks()
        }

        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let onSig: @convention(block) () -> Void = { NSApplication.shared.terminate(nil) }
        let si = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let sg = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        si.setEventHandler(handler: onSig); sg.setEventHandler(handler: onSig)
        si.resume(); sg.resume()
        if hostScript == nil {
            Thread.detachNewThread {
                while let line = readLine(strippingNewline: true) {
                    let cmd = line.trimmingCharacters(in: .whitespaces)
                    if cmd == "quit" {
                        DispatchQueue.main.async { NSApplication.shared.terminate(nil) }
                        return
                    }
                    if cmd == "top on" || cmd == "top off" {
                        DispatchQueue.main.async { gOverlay?.setTop(cmd == "top on") }
                    }
                }
            }
        }
        _ = si; _ = sg; _ = refs
        app.run()
    }
}

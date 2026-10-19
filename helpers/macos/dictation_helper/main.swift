// dictation-helper — 音声入力(REQUIREMENTS FR-14・2026-10-17 / FR-14b・2026-10-19)の Mac 側。OS に触る所だけを受け持つ:
//   キー(Carbon のホットキー。Fn・右 ⌘ など修飾キーだけのキーは CGEventTap)・マイク(AVAudioEngine → 16 kHz モノラル s16le・
//   マイクの選択とテスト)・貼り付け(NSPasteboard + ⌘V・送信キー)・前面のアプリと画面の文脈(アクセシビリティ)・音・
//   フローティングバー(NSPanel・前面のアプリから入力を奪わない)・ほかの音の消音(CoreAudio)・ナギのペット(FR-14c)。
// Soniox への送信・仕上げ・記録・計測・設定は本体(meetcue.dictation)が受け持つ。
//
// stdout(1 行 1 JSON・どれも t_ms 付き):
//   {"type":"ready","bindings":{役: [{spec,label,ok,via,status,error}]},"hotkey":"⌃⌥V","status":0,"mic":"authorized",
//    "ax":true,"listen":true,"source":"mic","paste":true}
//   {"type":"key","action":"activate"|"handsfree"|"lock"|"cancel"|"paste_last","state":"down"|"up"|"abort","index":0,"via":…}
//     abort = 修飾キーだけのキーを押したまま別のキーを押した(⌥ + 文字など。録り始めた分は捨ててよい)
//   {"type":"bindings","bindings":{…},"listen":bool} — bindings / suspend のあと
//   {"type":"context","app":…,"bundle":…,"title":…,"selected":…,"before":…,"role":…,"secure":bool,"ms":…}
//   {"type":"rec_start","in_hz":48000,"source":"mic","device":"…"} → 最初の音が届いたら {"type":"rec_first"}
//   {"type":"audio","seq":n,"pcm":"<base64>"}   約 100 ms ごと(16 kHz モノラル s16le)
//   {"type":"rec_stop","audio_s":…,"app":…,"bundle":…}   止めた時の前面のアプリ(貼り先)も添える
//   {"type":"pasted","id":n,"ok":true,"chars":n,"ms":…,"sent":"enter"|null,"app":…,"bundle":…} /
//   {"type":"pasted","ok":false,"reason":"ax"|"disabled"|"empty"|"app_changed","clipboard":…}
//   {"type":"clipboard_restored","items":n} / {"type":"devices","devices":[{uid,name,default}]} / {"type":"apps","apps":[…]}
//   {"type":"level","rms":…,"db":…}(マイクのテストの間)/ {"type":"meter","on":bool}
//   {"type":"mute","on":bool,"ok":bool,"how":"mute"|"volume"|null}
//   {"type":"resource","cpu_pct":…,"cpu_s":…,"interval_s":…,"footprint_mb":…,"peak_mb":…,"wakeups":n,"idle_wakeups":n}
//     60 s ごとの自分の重さ(CPU はその間の平均・メモリは実際の使用量 phys_footprint・起こされた回数はその間の数)
//   {"type":"pet","action":"set","pet":…|"pet_size":…} / {"type":"pet","action":"open"}(ペットの右クリックの操作)
//   {"type":"error","phase":"mic"|"hotkey"|"file"|"tap"|"mute"|"bindings"|"pet","error":"…"} / {"type":"bye"}
// stdin(1 行 1 コマンド): start / stop / quit / ax_prompt / listen_prompt、または JSON の {"cmd": …}:
//   start {context: "app"|"full", selection: bool} / paste {text, send: "enter"|"cmd_enter"|null, id, expect: bundle} /
//   clip {text}(クリップボードに入れるだけ)/
//   bindings {bindings: {activate: [...], handsfree: [...], lock, paste_last, cancel}} / suspend {on} /
//   bar {state: idle|rec|busy|done|cancel|error, text, handsfree} / bar_mode {mode: recording|always|off, hint, live} /
//   live {text}(バーの上の、話している言葉の窓。空で閉じる)/
//   sound {name: start|stop|cancel|error} / sounds {on, set} / mute {on, delay_ms} / mic {uid} / devices / apps /
//   pet {mode: always|recording|off, size: s|m|l, meeting: idle|listen|think, event: cue|done}(ナギのペット。表情は bar の
//   状態からも変わる)/ meter {on, seconds} / clipboard {avoid_history} / capture {on}(キーを決める間: 次に押したキーを {"type":"captured","spec"} で返す)。stdin が閉じたら終わる(本体が落ちたとき、マイクとキーを握ったままにしない)。
// 引数: [--bindings <JSON>] [--key V --mods ctrl,opt](旧・有効化 1 つ)[--file <音声>](試験: start でマイクの代わりに
//       ファイルを実時間で流す)[--emit "activate.down@1,activate.up@4"](試験: キーの知らせを秒後に出す)[--no-paste]
//       [--no-sound] [--no-bar] [--state-dir <dir>](消音の印)[--render-bar <dir>](バーの見た目を PNG に描く・画面は使わない)
//       [--resource-s <秒>](重さを出す間隔・既定 60)[--pet-dir <絵>](ペットの 6 表情の PNG・無ければペットを出さない)
//       [--no-pet] [--render-pet <dir>](ペットの窓の見え方を PNG に描く・--pet-dir と一緒に)
//       [--pet-debug](確かめる用: カーソルの出入り・押す / 離す・見えているかの変化を {"type":"pet_debug"} で出す)
// 権限: マイク・アクセシビリティ(⌘V と送信キーを送る・画面の文脈。無いときは文をクリップボードに入れて reason=ax)・
//       入力監視(修飾キーだけのキーのときだけ)。Carbon のホットキーは許可が要らない。
// キャンセル(Esc)は録っている間だけ、ハンズフリーに切り替えるキー(Space)は有効化を押している間だけ登録する(ふだんの
// Esc と Space は奪わない)。押しっぱなしの見落としに備え、0.25 s ごとに照らして外す(修飾キーだけのキーは修飾の状態・
// 修飾 + キーは 330 s の上限)。
// ビルド: swiftc -parse-as-library -O main.swift -o dictation-helper(helpers/macos/Makefile)

import AppKit
import ApplicationServices
import AudioToolbox
import AVFoundation
import Carbon
import CoreAudio
import Foundation

let outQ = DispatchQueue(label: "dictation.out")

func emit(_ obj: [String: Any]) {
    var f = obj
    if f["t_ms"] == nil { f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000) }
    outQ.async {
        if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
           let s = String(data: d, encoding: .utf8) {
            print(s)
            fflush(stdout)
        }
    }
}

// ---- キーの書き方(meetcue/dictation_settings.py と同じ) ----------------------------------------------------------
let KEYS: [String: Int] = {
    var m: [String: Int] = [
        "A": kVK_ANSI_A, "B": kVK_ANSI_B, "C": kVK_ANSI_C, "D": kVK_ANSI_D, "E": kVK_ANSI_E, "F": kVK_ANSI_F,
        "G": kVK_ANSI_G, "H": kVK_ANSI_H, "I": kVK_ANSI_I, "J": kVK_ANSI_J, "K": kVK_ANSI_K, "L": kVK_ANSI_L,
        "M": kVK_ANSI_M, "N": kVK_ANSI_N, "O": kVK_ANSI_O, "P": kVK_ANSI_P, "Q": kVK_ANSI_Q, "R": kVK_ANSI_R,
        "S": kVK_ANSI_S, "T": kVK_ANSI_T, "U": kVK_ANSI_U, "V": kVK_ANSI_V, "W": kVK_ANSI_W, "X": kVK_ANSI_X,
        "Y": kVK_ANSI_Y, "Z": kVK_ANSI_Z,
        "0": kVK_ANSI_0, "1": kVK_ANSI_1, "2": kVK_ANSI_2, "3": kVK_ANSI_3, "4": kVK_ANSI_4, "5": kVK_ANSI_5,
        "6": kVK_ANSI_6, "7": kVK_ANSI_7, "8": kVK_ANSI_8, "9": kVK_ANSI_9,
        "SPACE": kVK_Space, "ESCAPE": kVK_Escape, "RETURN": kVK_Return, "TAB": kVK_Tab, "DELETE": kVK_Delete,
        "LEFT": kVK_LeftArrow, "RIGHT": kVK_RightArrow, "UP": kVK_UpArrow, "DOWN": kVK_DownArrow,
        "MINUS": kVK_ANSI_Minus, "EQUAL": kVK_ANSI_Equal, "LBRACKET": kVK_ANSI_LeftBracket,
        "RBRACKET": kVK_ANSI_RightBracket, "BACKSLASH": kVK_ANSI_Backslash, "SEMICOLON": kVK_ANSI_Semicolon,
        "QUOTE": kVK_ANSI_Quote, "COMMA": kVK_ANSI_Comma, "PERIOD": kVK_ANSI_Period, "SLASH": kVK_ANSI_Slash,
        "GRAVE": kVK_ANSI_Grave,
    ]
    let fkeys = [kVK_F1, kVK_F2, kVK_F3, kVK_F4, kVK_F5, kVK_F6, kVK_F7, kVK_F8, kVK_F9, kVK_F10, kVK_F11, kVK_F12,
                 kVK_F13, kVK_F14, kVK_F15, kVK_F16, kVK_F17, kVK_F18, kVK_F19, kVK_F20]
    for (i, c) in fkeys.enumerated() { m["F\(i + 1)"] = c }
    return m
}()
let KEY_NAME: [Int: String] = Dictionary(KEYS.map { ($0.value, $0.key) }, uniquingKeysWith: { a, _ in a })
let SPECIAL_LABEL: [String: String] = ["SPACE": "Space", "ESCAPE": "Esc", "RETURN": "Return", "TAB": "Tab",
                                       "DELETE": "Delete", "LEFT": "←", "RIGHT": "→", "UP": "↑", "DOWN": "↓",
                                       "MINUS": "-", "EQUAL": "=", "LBRACKET": "[", "RBRACKET": "]", "BACKSLASH": "\\",
                                       "SEMICOLON": ";", "QUOTE": "'", "COMMA": ",", "PERIOD": ".", "SLASH": "/",
                                       "GRAVE": "`"]
let MODS: [(String, Int, String)] = [("ctrl", controlKey, "⌃"), ("opt", optionKey, "⌥"), ("shift", shiftKey, "⇧"),
                                     ("cmd", cmdKey, "⌘")]
let MOD_ALIAS: [String: String] = ["control": "ctrl", "alt": "opt", "option": "opt", "command": "cmd", "meta": "cmd"]

/// 修飾キーだけのキー: (キーコード, その修飾の総称の印, 左右を分ける印(device の印・0 = Fn), Carbon の修飾, 表示名)
struct ModOnly {
    let code: Int
    let generic: UInt64
    let device: UInt64
    let carbon: Int
    let label: String
}
let MOD_ONLY: [String: ModOnly] = [
    "fn": ModOnly(code: kVK_Function, generic: CGEventFlags.maskSecondaryFn.rawValue, device: 0, carbon: 0, label: "Fn"),
    "lctrl": ModOnly(code: kVK_Control, generic: CGEventFlags.maskControl.rawValue, device: 0x1, carbon: controlKey, label: "左⌃"),
    "rctrl": ModOnly(code: kVK_RightControl, generic: CGEventFlags.maskControl.rawValue, device: 0x2000, carbon: controlKey, label: "右⌃"),
    "lopt": ModOnly(code: kVK_Option, generic: CGEventFlags.maskAlternate.rawValue, device: 0x20, carbon: optionKey, label: "左⌥"),
    "ropt": ModOnly(code: kVK_RightOption, generic: CGEventFlags.maskAlternate.rawValue, device: 0x40, carbon: optionKey, label: "右⌥"),
    "lcmd": ModOnly(code: kVK_Command, generic: CGEventFlags.maskCommand.rawValue, device: 0x8, carbon: cmdKey, label: "左⌘"),
    "rcmd": ModOnly(code: kVK_RightCommand, generic: CGEventFlags.maskCommand.rawValue, device: 0x10, carbon: cmdKey, label: "右⌘"),
    "lshift": ModOnly(code: kVK_Shift, generic: CGEventFlags.maskShift.rawValue, device: 0x2, carbon: shiftKey, label: "左⇧"),
    "rshift": ModOnly(code: kVK_RightShift, generic: CGEventFlags.maskShift.rawValue, device: 0x4, carbon: shiftKey, label: "右⇧"),
]
let GENERIC_MODS: UInt64 = CGEventFlags.maskControl.rawValue | CGEventFlags.maskAlternate.rawValue
    | CGEventFlags.maskCommand.rawValue | CGEventFlags.maskShift.rawValue | CGEventFlags.maskSecondaryFn.rawValue

struct KeySpec {
    let raw: String
    let mods: [String]
    let key: String?
    let only: String?

    static func parse(_ s: String) -> KeySpec? {
        let t = s.trimmingCharacters(in: .whitespaces)
        if t.isEmpty { return nil }
        if MOD_ONLY[t.lowercased()] != nil { return KeySpec(raw: t.lowercased(), mods: [], key: nil, only: t.lowercased()) }
        let parts = t.split(separator: "+", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
        if parts.contains(where: { $0.isEmpty }) { return nil }
        var mods = Set<String>()
        for p in parts.dropLast() {
            let m = MOD_ALIAS[p.lowercased()] ?? p.lowercased()
            guard MODS.contains(where: { $0.0 == m }) else { return nil }
            mods.insert(m)
        }
        let key = parts.last!.uppercased()
        guard KEYS[key] != nil else { return nil }
        return KeySpec(raw: t, mods: MODS.map { $0.0 }.filter { mods.contains($0) }, key: key, only: nil)
    }

    var keyCode: Int? { key.flatMap { KEYS[$0] } }
    var carbonMods: UInt32 { UInt32(MODS.filter { mods.contains($0.0) }.reduce(0) { $0 | $1.1 }) }
    /// いつも登録しておいてよいキー(⌃・⌥・⌘ のどれかと組む / F1〜F20 / 修飾キーだけ)。ふだんの入力(文字・⇧ + 文字)は奪わない
    var safeStatic: Bool {
        if only != nil { return true }
        if !Set(mods).isDisjoint(with: ["ctrl", "opt", "cmd"]) { return true }
        guard let k = key, k.hasPrefix("F"), let n = Int(k.dropFirst()) else { return false }
        return (1...20).contains(n)
    }
    var modOnly: ModOnly? { only.flatMap { MOD_ONLY[$0] } }
    var label: String {
        if let m = modOnly { return m.label }
        return MODS.filter { mods.contains($0.0) }.map { $0.2 }.joined() + (SPECIAL_LABEL[key ?? ""] ?? key ?? "")
    }
    /// 有効化を押している間に、ハンズフリーへの切り替えのキーを登録するときの修飾(押している修飾と同じにする)
    var heldCarbonMods: UInt32 { modOnly.map { UInt32($0.carbon) } ?? carbonMods }
}

struct Bindings {
    var activate: [KeySpec] = []
    var handsfree: [KeySpec] = []
    var lock: KeySpec? = nil
    var pasteLast: KeySpec? = nil
    var cancel: KeySpec? = nil
    var raw: [String: Any] = [:]
    var bad: [String] = []

    static func from(_ obj: [String: Any]) -> Bindings {
        var b = Bindings()
        b.raw = obj
        func one(_ v: Any?) -> KeySpec? {
            guard let s = v as? String, !s.isEmpty else { return nil }
            if let k = KeySpec.parse(s) { return k }
            b.bad.append(s)
            return nil
        }
        b.activate = ((obj["activate"] as? [String]) ?? []).compactMap { one($0) }
        b.handsfree = ((obj["handsfree"] as? [String]) ?? []).compactMap { one($0) }
        b.lock = one(obj["lock"])
        b.pasteLast = one(obj["paste_last"])
        b.cancel = one(obj["cancel"])
        return b
    }
}

// ---- キー(Carbon のホットキー + 修飾キーだけのキーの見張り) -------------------------------------------------------------
final class Keys {
    struct Reg {
        let action: String
        let index: Int
        let spec: KeySpec
        let ref: EventHotKeyRef?
    }
    struct ModState {
        var down = false
        var at = Date.distantPast
        var tainted = false
    }

    var regs: [UInt32: Reg] = [:]
    var nextId: UInt32 = 1
    var b = Bindings()
    var suspended = false
    var recording = false
    var cancelIds: [UInt32] = []
    var lockIds: [UInt32] = []
    var held: (spec: KeySpec, index: Int)? = nil       // 有効化を押している間
    var heldSince = Date.distantPast
    var wdSawHeld = false   // 見張りの問い合わせで「押している」と一度でも読めたか(読めない仕組みなら、問い合わせでは離したと見なさない)
    var modState: [String: ModState] = [:]              // "activate#0" など
    var results: [String: [[String: Any]]] = [:]
    var tap: CFMachPort? = nil
    var tapSource: CFRunLoopSource? = nil
    var watchdog: Timer? = nil

    /// 静的な登録(有効化・ハンズフリーで始める・最後の文字起こしを貼り付け)をやり直す。キャンセルとハンズフリーへの
    /// 切り替えは、録っている間・押している間だけ(dynamic)。
    func apply(_ nb: Bindings) -> [String: Any] {
        unregisterAll()
        b = nb
        if suspended {   // キーを決めている間は覚えるだけ(再開したときに登録する)
            return ["bindings": results, "listen": CGPreflightListenEventAccess(), "suspended": true]
        }
        results = [:]
        var needTap = false
        for (action, list) in [("activate", b.activate), ("handsfree", b.handsfree)] {
            for (i, spec) in list.enumerated() {
                if spec.modOnly != nil {
                    needTap = true
                    results[action, default: []].append(["spec": spec.raw, "label": spec.label, "via": "tap", "ok": false])
                } else {
                    results[action, default: []].append(register(spec, action: action, index: i))
                }
            }
        }
        if let p = b.pasteLast { results["paste_last"] = [register(p, action: "paste_last", index: 0)] }
        if let l = b.lock { results["lock"] = [["spec": l.raw, "label": l.label, "via": "dynamic", "ok": true]] }
        if let c = b.cancel { results["cancel"] = [["spec": c.raw, "label": c.label, "via": "dynamic", "ok": true]] }
        let tapOK = needTap ? ensureTap() : { removeTap(); return true }()
        for action in ["activate", "handsfree"] {
            guard var rows = results[action] else { continue }
            for i in rows.indices where (rows[i]["via"] as? String) == "tap" {
                rows[i]["ok"] = tapOK
                if !tapOK { rows[i]["error"] = "listen_permission" }
            }
            results[action] = rows
        }
        for s in b.bad { emit(["type": "error", "phase": "bindings", "error": "bad_spec", "spec": s]) }
        if recording { setRecording(true) }
        return ["bindings": results, "listen": CGPreflightListenEventAccess(), "suspended": suspended]
    }

    func register(_ spec: KeySpec, action: String, index: Int) -> [String: Any] {
        var row: [String: Any] = ["spec": spec.raw, "label": spec.label, "via": "carbon"]
        guard let code = spec.keyCode else {
            row["ok"] = false
            row["error"] = "bad_key"
            return row
        }
        guard spec.safeStatic else {   // 本体も検めるが、ここでも文字のキーを常に奪う登録はしない
            row["ok"] = false
            row["error"] = "unsafe"
            emit(["type": "error", "phase": "hotkey", "error": "unsafe", "spec": spec.raw])
            return row
        }
        let id = nextId
        nextId += 1
        var ref: EventHotKeyRef? = nil
        let st = RegisterEventHotKey(UInt32(code), spec.carbonMods, EventHotKeyID(signature: OSType(0x4D43_4454), id: id),
                                     GetEventDispatcherTarget(), 0, &ref)
        row["status"] = Int(st)
        row["ok"] = st == noErr
        if st == noErr {
            regs[id] = Reg(action: action, index: index, spec: spec, ref: ref)
        } else {
            row["error"] = st == OSStatus(eventHotKeyExistsErr) ? "in_use" : "register_failed"
            emit(["type": "error", "phase": "hotkey", "error": row["error"]!, "status": Int(st), "spec": spec.raw])
        }
        return row
    }

    func registerDynamic(_ code: Int, _ mods: UInt32, action: String, spec: KeySpec) -> UInt32? {
        let id = nextId
        nextId += 1
        var ref: EventHotKeyRef? = nil
        let st = RegisterEventHotKey(UInt32(code), mods, EventHotKeyID(signature: OSType(0x4D43_4454), id: id),
                                     GetEventDispatcherTarget(), 0, &ref)
        guard st == noErr else { return nil }
        regs[id] = Reg(action: action, index: 0, spec: spec, ref: ref)
        return id
    }

    func unregister(_ id: UInt32) {
        if let r = regs.removeValue(forKey: id), let ref = r.ref { UnregisterEventHotKey(ref) }
    }

    func unregisterAll() {
        if let h = held {   // 押している最中に登録し直す: 離しの知らせはもう来ないので、本体の「押している間」を終わらせる
            emit(["type": "key", "action": "activate", "state": "up", "index": h.index, "via": "reset"])
        }
        for id in Array(regs.keys) { unregister(id) }
        cancelIds = []
        lockIds = []
        held = nil
        modState = [:]
        removeTap()
    }

    var suspendGen = 0

    func suspend(_ on: Bool) -> [String: Any] {
        suspended = on
        suspendGen += 1
        if on {
            unregisterAll()
            let g = suspendGen
            // 画面が閉じた・読み直されたなどで再開の知らせが来なくても、キーを止めたままにしない
            DispatchQueue.main.asyncAfter(deadline: .now() + 120) { [weak self] in
                guard let self = self, self.suspended, g == self.suspendGen else { return }
                var r = self.suspend(false)
                r["type"] = "bindings"
                r["auto_resumed"] = true
                emit(r)
            }
            return ["bindings": results, "listen": CGPreflightListenEventAccess(), "suspended": true]
        }
        return apply(b)
    }

    /// 録っている間だけキャンセルのキー(既定 Esc)を登録する。
    func setRecording(_ on: Bool) {
        recording = on
        for id in cancelIds { unregister(id) }
        cancelIds = []
        if on, !suspended, let c = b.cancel, let code = c.keyCode,
           let id = registerDynamic(code, c.carbonMods, action: "cancel", spec: c) {
            cancelIds = [id]
        }
        updateWatchdog()
    }

    func registerLock(for spec: KeySpec) {
        unregisterLock()
        guard !suspended, let l = b.lock, let code = l.keyCode else { return }
        if let id = registerDynamic(code, spec.heldCarbonMods, action: "lock", spec: l) { lockIds = [id] }
        updateWatchdog()
    }

    func unregisterLock() {
        for id in lockIds { unregister(id) }
        lockIds = []
        updateWatchdog()
    }

    func onCarbon(id: UInt32, pressed: Bool) {
        guard let r = regs[id] else { return }
        switch r.action {
        case "activate":
            if pressed {
                held = (r.spec, r.index)
                heldSince = Date()
                wdSawHeld = false
                registerLock(for: r.spec)
                emit(["type": "key", "action": "activate", "state": "down", "index": r.index, "via": "carbon"])
            } else {
                released(r.index)
            }
        case "handsfree", "paste_last", "cancel", "lock":
            if pressed { emit(["type": "key", "action": r.action, "state": "down", "index": r.index, "via": "carbon"]) }
        default:
            break
        }
    }

    func released(_ index: Int, via: String = "carbon") {
        held = nil
        unregisterLock()
        emit(["type": "key", "action": "activate", "state": "up", "index": index, "via": via])
    }

    // --- 修飾キーだけのキー(CGEventTap・入力監視の許可) ---
    func ensureTap() -> Bool {
        if tap != nil { return true }
        let mask = CGEventMask(1 << CGEventType.flagsChanged.rawValue) | CGEventMask(1 << CGEventType.keyDown.rawValue)
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
                                        eventsOfInterest: mask, callback: tapCallback, userInfo: nil) else {
            emit(["type": "error", "phase": "tap", "error": "tap_create_failed", "listen": CGPreflightListenEventAccess()])
            return false
        }
        let src = CFMachPortCreateRunLoopSource(nil, t, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        tap = t
        tapSource = src
        return true
    }

    func removeTap() {
        if let t = tap { CGEvent.tapEnable(tap: t, enable: false) }
        if let s = tapSource { CFRunLoopRemoveSource(CFRunLoopGetMain(), s, .commonModes) }
        tap = nil
        tapSource = nil
    }

    func reenableTap() {
        if let t = tap { CGEvent.tapEnable(tap: t, enable: true) }
        if let t = capTap { CGEvent.tapEnable(tap: t, enable: true) }
    }

    // --- キーを決める間(画面の「キーを押してください」): 次に押したキーを、実際に見張るのと同じ仕組みで拾う ---
    // ブラウザは Fn(🌐)などを知らせないことがある(2026-10-19 keigoly様: Fn だけを押しても決まらなかった)。入力監視の許可が要る
    var capTap: CFMachPort? = nil
    var capSource: CFRunLoopSource? = nil
    var capPending: Int? = nil       // 押している修飾キーのキーコード(ほかのキーを押さずに離したら、修飾キーだけのキー)
    var capOther = false
    var capGen = 0

    func startCapture(seconds: Double = 30) -> Bool {
        stopCapture()
        let mask = CGEventMask(1 << CGEventType.flagsChanged.rawValue) | CGEventMask(1 << CGEventType.keyDown.rawValue)
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
                                        eventsOfInterest: mask, callback: captureCallback, userInfo: nil) else { return false }
        let src = CFMachPortCreateRunLoopSource(nil, t, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        capTap = t
        capSource = src
        capPending = nil
        capOther = false
        capGen += 1
        let g = capGen
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            if let self = self, self.capGen == g { self.stopCapture() }
        }
        return true
    }

    func stopCapture() {
        if let t = capTap { CGEvent.tapEnable(tap: t, enable: false) }
        if let s = capSource { CFRunLoopRemoveSource(CFRunLoopGetMain(), s, .commonModes) }
        capTap = nil
        capSource = nil
        capPending = nil
    }

    /// 拾ったキー(spec)を返す。nil = まだ決まらない。flagsChanged は修飾キー、keyDown はふつうのキー(修飾との組み合わせ)
    func captureStep(_ type: CGEventType, code: Int, flags: UInt64, autorepeat: Bool) -> String? {
        if type == .flagsChanged {
            guard let (name, m) = MOD_ONLY.first(where: { $0.value.code == code }) else { return nil }
            if Keys.isHeld(m, flags) {
                if capPending == nil {
                    capPending = code
                    capOther = false
                } else {
                    capOther = true   // 修飾キーを 2 つ押した = 組み合わせの途中
                }
            } else if capPending == code {
                defer { capPending = nil }
                if !capOther { return name }   // ほかのキーを押さずに離した = 修飾キーだけのキー(Fn・右 ⌘ など)
            }
            return nil
        }
        guard type == .keyDown, !autorepeat else { return nil }
        capOther = true
        guard let name = KEY_NAME[code] else { return "?\(code)" }
        var mods: [String] = []
        if flags & CGEventFlags.maskControl.rawValue != 0 { mods.append("ctrl") }
        if flags & CGEventFlags.maskAlternate.rawValue != 0 { mods.append("opt") }
        if flags & CGEventFlags.maskShift.rawValue != 0 { mods.append("shift") }
        if flags & CGEventFlags.maskCommand.rawValue != 0 { mods.append("cmd") }
        return (mods + [name]).joined(separator: "+")
    }

    func onCapture(_ type: CGEventType, _ event: CGEvent) {
        guard capTap != nil else { return }
        if event.getIntegerValueField(.eventSourceUnixProcessID) == Int64(getpid()) { return }
        let spec = captureStep(type, code: Int(event.getIntegerValueField(.keyboardEventKeycode)), flags: event.flags.rawValue,
                               autorepeat: event.getIntegerValueField(.keyboardEventAutorepeat) != 0)
        guard let sp = spec else { return }
        stopCapture()
        emit(["type": "captured", "spec": sp, "label": KeySpec.parse(sp)?.label ?? sp])
    }

    func modOnlyBindings() -> [(String, Int, KeySpec)] {
        var out: [(String, Int, KeySpec)] = []
        for (i, s) in b.activate.enumerated() where s.modOnly != nil { out.append(("activate", i, s)) }
        for (i, s) in b.handsfree.enumerated() where s.modOnly != nil { out.append(("handsfree", i, s)) }
        return out
    }

    static func isHeld(_ m: ModOnly, _ flags: UInt64) -> Bool {
        m.device == 0 ? (flags & m.generic) != 0 : (flags & m.device) != 0
    }

    /// その修飾キーだけを押したか(ほかの修飾を押したままなら、ふつうの組み合わせとして見送る)。
    static func alone(_ m: ModOnly, _ flags: UInt64) -> Bool {
        if (flags & (GENERIC_MODS & ~m.generic)) != 0 { return false }
        if m.device != 0 {   // 同じ種類の反対側(左 ⌘ を押したまま右 ⌘ など)
            let sides: [UInt64] = [0x1, 0x2000, 0x20, 0x40, 0x8, 0x10, 0x2, 0x4]
            let pair = MOD_ONLY.values.filter { $0.generic == m.generic && $0.device != m.device }.map { $0.device }
            for d in sides where pair.contains(d) && (flags & d) != 0 { return false }
        }
        return true
    }

    func onTap(_ type: CGEventType, _ event: CGEvent) {
        guard !suspended else { return }
        // 貼り付けで自分が押した ⌘V・Return は、次の 1 回の「修飾キーを組み合わせに使った」に数えない
        if event.getIntegerValueField(.eventSourceUnixProcessID) == Int64(getpid()) { return }
        let code = Int(event.getIntegerValueField(.keyboardEventKeycode))
        let flags = event.flags.rawValue
        if type == .flagsChanged {
            for (action, i, spec) in modOnlyBindings() {
                guard let m = spec.modOnly, m.code == code else { continue }
                let key = "\(action)#\(i)"
                var st = modState[key] ?? ModState()
                let down = Keys.isHeld(m, flags)
                if down && !st.down {
                    guard Keys.alone(m, flags) else { continue }
                    st = ModState(down: true, at: Date(), tainted: false)
                    modState[key] = st
                    if action == "activate" {
                        held = (spec, i)
                        heldSince = Date()
                        wdSawHeld = false
                        registerLock(for: spec)
                    }
                    emit(["type": "key", "action": action, "state": "down", "index": i, "via": "tap"])
                } else if !down && st.down {
                    modState[key] = ModState(down: false, at: st.at, tainted: st.tainted)
                    if action == "activate" {
                        if st.tainted {
                            held = nil
                            unregisterLock()
                        } else {
                            released(i, via: "tap")
                        }
                    }
                }
            }
            updateWatchdog()
        } else if type == .keyDown {
            for (key, st) in modState where st.down && !st.tainted {
                if let l = b.lock, code == l.keyCode { continue }                 // ハンズフリーへの切り替え(Carbon が受ける)
                if recording, let c = b.cancel, code == c.keyCode { continue }    // 取り消し
                guard Date().timeIntervalSince(st.at) < 1.0 else { continue }     // 長く話している途中の打鍵は見送る
                modState[key]?.tainted = true
                let parts = key.split(separator: "#")
                emit(["type": "key", "action": String(parts[0]), "state": "abort", "index": Int(parts[1]) ?? 0, "via": "tap"])
            }
        }
    }

    // --- 見落としへの備え: 押している間・録っている間だけ 0.25 s ごとに実際の状態と照らす ---
    func updateWatchdog() {
        let need = !lockIds.isEmpty || !cancelIds.isEmpty || held != nil || modState.values.contains { $0.down }
        if need && watchdog == nil {
            let t = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in self?.check() }
            RunLoop.main.add(t, forMode: .common)
            watchdog = t
        } else if !need, let t = watchdog {
            t.invalidate()
            watchdog = nil
        }
    }

    func check() {
        let flags = CGEventSource.flagsState(.combinedSessionState).rawValue
        if let h = held {
            var still = true
            if let m = h.spec.modOnly {
                // 見張りは左右を分けない(問い合わせの値に左右の印が載るかは確かめていない)。Fn の印が問い合わせに載るかも
                // 確かめていないので、「押している」と一度でも読めたときだけ、読めなくなったら離したと見なす(読めない仕組みなら、
                // キーの知らせ(CGEventTap)だけに任せる。押している間だけ録る動きを 0.25 s で切らないため・10-19)
                let now = (flags & m.generic) != 0
                if now { wdSawHeld = true }
                still = (now || !wdSawHeld) && Date().timeIntervalSince(heldSince) < 330
            } else {
                // 修飾 + キーは Carbon の離しの知らせに任せる(キーの状態を問い合わせると、入力監視の許可が無いときや
                // ホットキーとして受けたキーで「離した」と読むおそれがあり、押している間だけ録る動きを壊す)。
                // 知らせを取りこぼしたときに切り替えのキーを奪い続けないよう、録る上限(本体の max_s 300 s)を過ぎたら外す
                still = Date().timeIntervalSince(heldSince) < 330
            }
            if !still {
                let key = "activate#\(h.index)"
                let tainted = modState[key]?.tainted ?? false
                modState[key]?.down = false
                if tainted {
                    held = nil
                    unregisterLock()
                } else {
                    released(h.index, via: "watchdog")
                }
            }
        }
        for (key, st) in modState where st.down && !key.hasPrefix("activate") {
            let parts = key.split(separator: "#")
            let i = Int(parts[1]) ?? 0
            if i < b.handsfree.count, let m = b.handsfree[i].modOnly, (flags & m.generic) == 0 { modState[key]?.down = false }
        }
        if !recording && !cancelIds.isEmpty { setRecording(false) }
        updateWatchdog()
    }
}

var gKeys: Keys!

let hotkeyHandler: EventHandlerUPP = { _, event, _ in
    guard let event = event else { return OSStatus(eventNotHandledErr) }
    var hk = EventHotKeyID()
    let st = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                               MemoryLayout<EventHotKeyID>.size, nil, &hk)
    if st != noErr { return st }
    gKeys.onCarbon(id: hk.id, pressed: GetEventKind(event) == UInt32(kEventHotKeyPressed))
    return noErr
}

let captureCallback: CGEventTapCallBack = { _, type, event, _ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        gKeys.reenableTap()
    } else {
        gKeys.onCapture(type, event)
    }
    return Unmanaged.passUnretained(event)
}

let tapCallback: CGEventTapCallBack = { _, type, event, _ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        gKeys.reenableTap()
    } else {
        gKeys.onTap(type, event)
    }
    return Unmanaged.passUnretained(event)
}

// ---- 前面のアプリと画面の文脈(アクセシビリティ) ---------------------------------------------------------------------
func axCopy(_ el: AXUIElement, _ name: String) -> CFTypeRef? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func axElement(_ v: CFTypeRef?) -> AXUIElement? {
    guard let v = v, CFGetTypeID(v) == AXUIElementGetTypeID() else { return nil }
    return (v as! AXUIElement)
}

func frontApp() -> [String: Any] {
    guard let a = NSWorkspace.shared.frontmostApplication else { return [:] }
    return ["app": a.localizedName ?? "", "bundle": a.bundleIdentifier ?? ""]
}

/// level: "app"(アプリ名だけ・許可いらない)/ "full"(+ ウィンドウの題名・カーソルの前の文 1,000 字)。selection: 選んだ文も。
func captureContext(level: String, selection: Bool) -> [String: Any] {
    let t0 = Date()
    var out: [String: Any] = ["type": "context"]
    out.merge(frontApp()) { a, _ in a }
    if (level == "full" || selection), AXIsProcessTrusted(), let a = NSWorkspace.shared.frontmostApplication {
        let app = AXUIElementCreateApplication(a.processIdentifier)
        AXUIElementSetMessagingTimeout(app, 0.25)
        if level == "full", let win = axElement(axCopy(app, kAXFocusedWindowAttribute)),
           let title = axCopy(win, kAXTitleAttribute) as? String {
            out["title"] = String(title.prefix(300))
        }
        if let el = axElement(axCopy(app, kAXFocusedUIElementAttribute)) {
            AXUIElementSetMessagingTimeout(el, 0.25)
            out["role"] = axCopy(el, kAXRoleAttribute) as? String ?? ""
            if (axCopy(el, kAXSubroleAttribute) as? String) == (kAXSecureTextFieldSubrole as String) {
                out["secure"] = true   // パスワードの欄は読まない
            } else {
                if selection, let sel = axCopy(el, kAXSelectedTextAttribute) as? String, !sel.isEmpty {
                    out["selected"] = String(sel.prefix(8000))
                }
                if level == "full", let val = axCopy(el, kAXValueAttribute) as? String,
                   let rv = axCopy(el, kAXSelectedTextRangeAttribute), CFGetTypeID(rv) == AXValueGetTypeID() {
                    var range = CFRange()
                    if AXValueGetValue(rv as! AXValue, .cfRange, &range) {
                        let ns = val as NSString
                        let end = min(max(range.location, 0), ns.length)
                        let start = max(0, end - 1000)
                        out["before"] = ns.substring(with: NSRange(location: start, length: end - start))
                    }
                }
            }
        }
    }
    out["ms"] = Int(Date().timeIntervalSince(t0) * 1000)
    return out
}

// ---- マイク(デバイスの一覧・選択) ------------------------------------------------------------------------------------
func audioDeviceProp(_ id: AudioObjectID, _ sel: AudioObjectPropertySelector) -> AudioDeviceID? {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var v = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    return AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &v) == noErr ? v : nil
}

func audioString(_ id: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var ref: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &ref) == noErr, let r = ref else { return nil }
    return r.takeRetainedValue() as String
}

func inputChannels(_ id: AudioDeviceID) -> Int {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
                                          mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func allDevices() -> [AudioDeviceID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    let sys = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(sys, &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(sys, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func defaultDevice(_ sel: AudioObjectPropertySelector) -> AudioDeviceID {
    audioDeviceProp(AudioObjectID(kAudioObjectSystemObject), sel) ?? 0
}

func inputDevices() -> [[String: Any]] {
    let def = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
    return allDevices().compactMap { id in
        guard inputChannels(id) > 0, let uid = audioString(id, kAudioDevicePropertyDeviceUID) else { return nil }
        return ["uid": uid, "name": audioString(id, kAudioObjectPropertyName) ?? uid, "default": id == def]
    }
}

func deviceID(uid: String) -> AudioDeviceID? {
    allDevices().first { audioString($0, kAudioDevicePropertyDeviceUID) == uid && inputChannels($0) > 0 }
}

// ---- 取り込み: マイク(またはファイル)の音を 16 kHz モノラル s16le にして、100 ms ずつ stdout へ ----------------------
final class Capture {
    let dst = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    let chunkBytes = 3200
    let file: String?
    var engine: AVAudioEngine? = nil
    var running = false
    var meterOnly = false
    var fileStop = false
    var micUID = ""
    let q = DispatchQueue(label: "dictation.capture")
    var pending = Data()
    var seq = 0
    var bytes = 0
    var first = false
    var onLevel: ((Float) -> Void)? = nil
    var onMic: ((String) -> Void)? = nil   // 録り始めたマイクの名前(話している言葉の窓に出す)

    init(file: String?) { self.file = file }

    func convert(_ inBuf: AVAudioPCMBuffer, _ conv: AVAudioConverter) -> Data? {
        let ratio = dst.sampleRate / inBuf.format.sampleRate
        let cap = AVAudioFrameCount(Double(inBuf.frameLength) * ratio) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: dst, frameCapacity: cap) else { return nil }
        var used = false
        var err: NSError?
        _ = conv.convert(to: out, error: &err) { _, status in
            if used { status.pointee = .noDataNow; return nil }
            used = true
            status.pointee = .haveData
            return inBuf
        }
        guard err == nil, out.frameLength > 0, let ch = out.int16ChannelData else { return nil }
        return Data(bytes: ch[0], count: Int(out.frameLength) * 2)
    }

    static func rms(_ data: Data) -> Float {
        let n = data.count / 2
        guard n > 0 else { return 0 }
        var acc: Double = 0
        data.withUnsafeBytes { (p: UnsafeRawBufferPointer) in
            let s = p.bindMemory(to: Int16.self)
            for i in 0..<n { let v = Double(s[i]) / 32768.0; acc += v * v }
        }
        return Float((acc / Double(n)).squareRoot())
    }

    func push(_ data: Data) {
        let level = Capture.rms(data)
        if let f = onLevel { DispatchQueue.main.async { f(level) } }
        q.async {
            guard self.running else { return }
            if self.meterOnly {
                self.bytes += data.count
                if self.bytes >= self.chunkBytes {
                    self.bytes = 0
                    let db = level > 0 ? 20 * log10(level) : -120
                    emit(["type": "level", "rms": Double(level), "db": (Double(db) * 10).rounded() / 10])
                }
                return
            }
            if !self.first {
                self.first = true
                emit(["type": "rec_first"])
            }
            self.pending.append(data)
            self.bytes += data.count
            while self.pending.count >= self.chunkBytes {
                self.flushChunk(self.chunkBytes)
            }
        }
    }

    func flushChunk(_ n: Int) {
        let part = pending.prefix(n)
        pending.removeFirst(part.count)
        seq += 1
        emit(["type": "audio", "seq": seq, "pcm": part.base64EncodedString()])
    }

    func start(meter: Bool = false) {
        guard !running else { return }
        q.sync {
            running = true
            meterOnly = meter
            fileStop = false
            pending = Data()
            seq = 0
            bytes = 0
            first = false
        }
        if let path = file, !meter {
            startFile(path)
            return
        }
        let eng = AVAudioEngine()
        let input = eng.inputNode
        var device = "default"
        if !micUID.isEmpty {
            if let dev = deviceID(uid: micUID), let au = input.audioUnit {
                var d = dev
                let st = AudioUnitSetProperty(au, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &d,
                                              UInt32(MemoryLayout<AudioDeviceID>.size))
                if st == noErr {
                    device = micUID
                } else {
                    emit(["type": "error", "phase": "mic", "error": "set_device_failed", "status": Int(st)])
                }
            } else {
                emit(["type": "error", "phase": "mic", "error": "device_not_found"])   // 外した機器: 既定のマイクで録る
            }
        }
        let inFmt = input.outputFormat(forBus: 0)
        guard inFmt.sampleRate > 0, let conv = AVAudioConverter(from: inFmt, to: dst) else {
            q.sync { running = false }
            emit(["type": "error", "phase": "mic", "error": "no_input"])
            return
        }
        input.installTap(onBus: 0, bufferSize: AVAudioFrameCount(inFmt.sampleRate / 20), format: inFmt) { buf, _ in
            if let d = self.convert(buf, conv) { self.push(d) }
        }
        eng.prepare()
        do {
            try eng.start()
        } catch {
            input.removeTap(onBus: 0)
            q.sync { running = false }
            emit(["type": "error", "phase": "mic", "error": "\(error)"])
            return
        }
        engine = eng
        if meter {
            emit(["type": "meter", "on": true, "device": device])
        } else {
            emit(["type": "rec_start", "in_hz": inFmt.sampleRate, "source": "mic", "device": device])
            let dev = device == "default" ? defaultDevice(kAudioHardwarePropertyDefaultInputDevice) : (deviceID(uid: micUID) ?? 0)
            let name = dev != 0 ? (audioString(dev, kAudioObjectPropertyName) ?? "") : ""
            if let f = onMic { DispatchQueue.main.async { f(name) } }
        }
    }

    func startFile(_ path: String) {
        let f: AVAudioFile
        do { f = try AVAudioFile(forReading: URL(fileURLWithPath: path)) } catch {
            q.sync { running = false }
            emit(["type": "error", "phase": "file", "error": "\(error)"])
            return
        }
        guard let conv = AVAudioConverter(from: f.processingFormat, to: dst) else {
            q.sync { running = false }
            emit(["type": "error", "phase": "file", "error": "no_converter"])
            return
        }
        emit(["type": "rec_start", "in_hz": f.processingFormat.sampleRate, "source": "file"])
        let frames = AVAudioFrameCount(f.processingFormat.sampleRate * 0.1)
        DispatchQueue.global().async {
            let t0 = Date()
            var idx = 0
            while f.framePosition < f.length && !self.q.sync(execute: { self.fileStop }) {
                guard let buf = AVAudioPCMBuffer(pcmFormat: f.processingFormat, frameCapacity: frames) else { break }
                do { try f.read(into: buf, frameCount: frames) } catch { break }
                if buf.frameLength == 0 { break }
                if let d = self.convert(buf, conv) { self.push(d) }
                idx += 1
                let wait = t0.addingTimeInterval(Double(idx) * 0.1).timeIntervalSinceNow
                if wait > 0 { Thread.sleep(forTimeInterval: wait) }
            }
        }
    }

    func stop() {
        guard running else { return }
        if let eng = engine {
            eng.inputNode.removeTap(onBus: 0)
            eng.stop()
            engine = nil
        }
        q.sync {
            fileStop = true
            if meterOnly {
                running = false
                meterOnly = false
                emit(["type": "meter", "on": false])
                return
            }
            if !pending.isEmpty { flushChunk(pending.count) }
            running = false
            var ev: [String: Any] = ["type": "rec_stop", "audio_s": (Double(bytes) / 32000 * 100).rounded() / 100]
            ev.merge(frontApp()) { a, _ in a }
            emit(ev)
        }
    }
}

// ---- 貼り付け: 文をクリップボードに入れて ⌘V を送り、少し後に元のクリップボードを戻す ----------------------------------
final class Paster {
    let enabled: Bool
    var avoidHistory = true
    var saved: [[(NSPasteboard.PasteboardType, Data)]]? = nil   // 続けて貼る間(リアルタイム)は最初の 1 回の前の中身
    var ours = 0
    var gen = 0
    static let markers = ["org.nspasteboard.TransientType", "org.nspasteboard.ConcealedType",
                          "org.nspasteboard.AutoGeneratedType"]

    init(enabled: Bool) { self.enabled = enabled }

    func snapshot(_ pb: NSPasteboard) -> [[(NSPasteboard.PasteboardType, Data)]] {
        (pb.pasteboardItems ?? []).map { item in item.types.compactMap { t in item.data(forType: t).map { (t, $0) } } }
    }

    func postKey(_ code: Int, _ flags: CGEventFlags) {
        let src = CGEventSource(stateID: .combinedSessionState)
        let down = CGEvent(keyboardEventSource: src, virtualKey: CGKeyCode(code), keyDown: true)
        let up = CGEvent(keyboardEventSource: src, virtualKey: CGKeyCode(code), keyDown: false)
        down?.flags = flags
        up?.flags = flags
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
    }

    static func sendFlags(_ send: String) -> CGEventFlags? {
        switch send {
        case "enter": return []
        case "cmd_enter": return .maskCommand
        default: return nil
        }
    }

    /// id: 本体が付けた番号(返事の pasted にそのまま付ける。送信キーの返事は遅れて出るので、順番ではなく番号で突き合わせる)。
    /// expect: 貼ってよい前面のアプリ(bundle id)。違えば貼らず・送信キーも押さない(離した後に別のアプリへ移ったとき、
    /// 文と Enter がそのアプリに入らないように・読み直しの指摘 10-19)
    func paste(_ text: String, send: String?, id: Int? = nil, expect: String? = nil) {
        func out(_ f: [String: Any]) {
            var ev = f
            ev["type"] = "pasted"
            if let id = id { ev["id"] = id }
            emit(ev)
        }
        guard enabled else { out(["ok": false, "reason": "disabled", "clipboard": false]); return }
        if let e = expect, !e.isEmpty, let front = NSWorkspace.shared.frontmostApplication?.bundleIdentifier, front != e {
            out(["ok": false, "reason": "app_changed", "clipboard": false, "chars": text.count, "front": front])
            return
        }
        let t0 = Date()
        let sendFlags = send.flatMap { Paster.sendFlags($0) }
        if text.isEmpty {   // 「送信して」だけ: 送信キーだけ押す
            guard let f = sendFlags else { out(["ok": false, "reason": "empty", "clipboard": false]); return }
            guard AXIsProcessTrusted() else { out(["ok": false, "reason": "ax", "clipboard": false]); return }
            postKey(kVK_Return, f)
            var ev: [String: Any] = ["ok": true, "chars": 0, "sent": send!, "ms": 0]
            ev.merge(frontApp()) { a, _ in a }
            out(ev)
            return
        }
        let pb = NSPasteboard.general
        // 戻す中身: 続けて貼る間は最初の 1 回の前の中身。その間にほかの物がコピーされていたら、それを戻す
        if saved == nil || pb.changeCount != ours { saved = snapshot(pb) }
        pb.clearContents()
        let item = NSPasteboardItem()
        item.setString(text, forType: .string)
        if avoidHistory { for m in Paster.markers { item.setData(Data(), forType: NSPasteboard.PasteboardType(m)) } }
        pb.writeObjects([item])
        ours = pb.changeCount
        guard AXIsProcessTrusted() else {
            // ⌘V を送れない: 文はクリップボードに残す(手で貼れる)。戻さない
            saved = nil
            out(["ok": false, "reason": "ax", "clipboard": true, "chars": text.count])
            return
        }
        postKey(kVK_ANSI_V, .maskCommand)
        var ev: [String: Any] = ["ok": true, "chars": text.count]
        ev.merge(frontApp()) { a, _ in a }
        if let f = sendFlags {
            // 貼った文が入り終わってから送る
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                self.postKey(kVK_Return, f)
                ev["sent"] = send!
                ev["ms"] = Int(Date().timeIntervalSince(t0) * 1000)
                out(ev)
            }
        } else {
            ev["ms"] = Int(Date().timeIntervalSince(t0) * 1000)
            out(ev)
        }
        scheduleRestore(after: sendFlags == nil ? 1.0 : 1.5)
    }

    /// 文をクリップボードに入れるだけ(貼らない・戻さない)。貼り先のアプリが変わり、取り戻しのキーも無いとき
    func clip(_ text: String) {
        guard enabled, !text.isEmpty else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
        ours = pb.changeCount
        saved = nil
    }

    static let secretTypes: Set<String> = ["org.nspasteboard.ConcealedType", "org.nspasteboard.TransientType"]

    /// 貼り付け先が読み終わるのを待ってから戻す(続けて貼る間は最後の 1 回の後・送信キーのときは長めに待つ)。その間に別の物が
    /// コピーされていたら戻さない。パスワードなど隠す印の付いた中身は戻さない(戻すとパスワード管理の自動消去が効かなくなる)
    func scheduleRestore(after: Double) {
        gen += 1
        let g = gen
        DispatchQueue.main.asyncAfter(deadline: .now() + after) {
            guard g == self.gen, let saved = self.saved else { return }
            self.saved = nil
            let pb = NSPasteboard.general
            guard pb.changeCount == self.ours else { return }
            if saved.contains(where: { pairs in pairs.contains { Paster.secretTypes.contains($0.0.rawValue) } }) {
                emit(["type": "clipboard_restored", "items": 0, "skipped": "concealed"])
                return
            }
            pb.clearContents()
            let items: [NSPasteboardItem] = saved.map { pairs in
                let it = NSPasteboardItem()
                for (t, d) in pairs { it.setData(d, forType: t) }
                if self.avoidHistory { it.setData(Data(), forType: NSPasteboard.PasteboardType("org.nspasteboard.AutoGeneratedType")) }
                return it
            }
            if !items.isEmpty { pb.writeObjects(items) }
            emit(["type": "clipboard_restored", "items": items.count])
        }
    }
}

// ---- 音 ---------------------------------------------------------------------------------------------------------
final class Sounds {
    var on: Bool
    var set = "default"
    let sets: [String: [String: String]] = [
        "default": ["start": "Tink", "stop": "Pop", "cancel": "Funk", "error": "Basso"],
        "soft": ["start": "Morse", "stop": "Bottle", "cancel": "Purr", "error": "Basso"],
        "classic": ["start": "Ping", "stop": "Glass", "cancel": "Submarine", "error": "Sosumi"],
    ]
    init(on: Bool) { self.on = on }

    func play(_ name: String) {
        guard on, let n = sets[set]?[name] ?? sets["default"]?[name],
              let s = NSSound(named: NSSound.Name(n))?.copy() as? NSSound else { return }
        s.play()
    }
}

// ---- ほかの音を消す(既定の出力機器の消音。戻し忘れないよう印のファイルを残す) --------------------------------------------
final class Muter {
    let mark: URL?
    var active: (dev: AudioDeviceID, how: String, prev: Float32)? = nil

    init(stateDir: String?) {
        mark = stateDir.map { URL(fileURLWithPath: $0).appendingPathComponent("muted.json") }
    }

    static func addr(_ sel: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioDevicePropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
    }

    static func settable(_ dev: AudioDeviceID, _ sel: AudioObjectPropertySelector) -> Bool {
        var a = addr(sel)
        var ok: DarwinBoolean = false
        return AudioObjectHasProperty(dev, &a) && AudioObjectIsPropertySettable(dev, &a, &ok) == noErr && ok.boolValue
    }

    static func getMute(_ dev: AudioDeviceID) -> Float32? {
        var a = addr(kAudioDevicePropertyMute)
        var v: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &v) == noErr ? Float32(v) : nil
    }

    static func setMute(_ dev: AudioDeviceID, _ on: Float32) -> Bool {
        var a = addr(kAudioDevicePropertyMute)
        var v: UInt32 = on > 0 ? 1 : 0
        return AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<UInt32>.size), &v) == noErr
    }

    static func getVolume(_ dev: AudioDeviceID) -> Float32? {
        var a = addr(kAudioHardwareServiceDeviceProperty_VirtualMainVolume)
        var v: Float32 = 0
        var size = UInt32(MemoryLayout<Float32>.size)
        return AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &v) == noErr ? v : nil
    }

    static func setVolume(_ dev: AudioDeviceID, _ vol: Float32) -> Bool {
        var a = addr(kAudioHardwareServiceDeviceProperty_VirtualMainVolume)
        var v = vol
        return AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<Float32>.size), &v) == noErr
    }

    func writeMark() {
        guard let m = mark, let a = active else { return }
        try? FileManager.default.createDirectory(at: m.deletingLastPathComponent(), withIntermediateDirectories: true)
        let obj: [String: Any] = ["dev": Int(a.dev), "how": a.how, "prev": Double(a.prev)]
        if let d = try? JSONSerialization.data(withJSONObject: obj) { try? d.write(to: m) }
    }

    /// 前回の消音が戻っていなければ戻す(ヘルパーが落ちたとき)。
    func recover() {
        guard let m = mark, let d = try? Data(contentsOf: m),
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let dev = obj["dev"] as? Int, let how = obj["how"] as? String, let prev = obj["prev"] as? Double else { return }
        active = (AudioDeviceID(dev), how, Float32(prev))
        _ = set(false)
        emit(["type": "mute", "on": false, "recovered": true])
    }

    func set(_ on: Bool) -> [String: Any] {
        if on {
            guard active == nil else { return ["ok": true, "how": active!.how] }
            let dev = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
            guard dev != 0 else { return ["ok": false, "error": "no_output"] }
            if Muter.settable(dev, kAudioDevicePropertyMute), let prev = Muter.getMute(dev) {
                if prev > 0 { return ["ok": true, "how": "already"] }
                active = (dev, "mute", prev)
                writeMark()
                return ["ok": Muter.setMute(dev, 1), "how": "mute"]
            }
            if Muter.settable(dev, kAudioHardwareServiceDeviceProperty_VirtualMainVolume), let prev = Muter.getVolume(dev) {
                active = (dev, "volume", prev)
                writeMark()
                return ["ok": Muter.setVolume(dev, 0), "how": "volume"]
            }
            return ["ok": false, "error": "not_supported"]
        }
        guard let a = active else { return ["ok": true, "how": NSNull()] }
        let ok = a.how == "mute" ? Muter.setMute(a.dev, a.prev) : Muter.setVolume(a.dev, a.prev)
        active = nil
        if let m = mark { try? FileManager.default.removeItem(at: m) }
        return ["ok": ok, "how": a.how]
    }
}

// ---- 自分の重さ(ナギのペット v0 の Step 1・2026-10-19 keigoly様「メモリとか CPU とかも極力食わないように」) ------------------
// CPU 時間(getrusage)・実際のメモリ(phys_footprint = アクティビティモニタの「メモリ」)・起こされた回数(task_power_info)を
// 決まった間隔で出す。間隔のタイマーは 1 本だけ(許容のずれを 10% 付けて、ほかの起床とまとめてもらう)
final class Usage {
    let every: Double
    var timer: Timer? = nil
    var last: (t: Double, cpu: Double, wake: UInt64, idle: UInt64)? = nil
    var extra: (() -> [String: Any])? = nil   // ペットの状態など、一緒に出すもの

    init(every: Double) { self.every = every }

    static func cpuSeconds() -> Double {
        var ru = rusage()
        getrusage(RUSAGE_SELF, &ru)
        return Double(ru.ru_utime.tv_sec) + Double(ru.ru_utime.tv_usec) / 1e6
            + Double(ru.ru_stime.tv_sec) + Double(ru.ru_stime.tv_usec) / 1e6
    }

    /// (今, 最大) MB
    static func footprint() -> (Double, Double)? {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        guard kr == KERN_SUCCESS else { return nil }
        return (Double(info.phys_footprint) / 1_048_576, Double(info.ledger_phys_footprint_peak) / 1_048_576)
    }

    /// (割り込みで起こされた回数, 眠っていた CPU を起こした回数)。起動してからの合計
    static func wakeups() -> (UInt64, UInt64) {
        var info = task_power_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_power_info_data_t>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_POWER_INFO), $0, &count)
            }
        }
        return kr == KERN_SUCCESS ? (info.task_interrupt_wakeups, info.task_platform_idle_wakeups) : (0, 0)
    }

    func start() {
        guard timer == nil, every > 0 else { return }
        let (w, i) = Usage.wakeups()
        last = (ProcessInfo.processInfo.systemUptime, Usage.cpuSeconds(), w, i)
        let t = Timer(timeInterval: every, repeats: true) { [weak self] _ in self?.sample() }
        t.tolerance = every * 0.1
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func sample() {
        let now = ProcessInfo.processInfo.systemUptime
        let cpu = Usage.cpuSeconds()
        let (w, i) = Usage.wakeups()
        var out: [String: Any] = ["type": "resource", "cpu_s": (cpu * 1000).rounded() / 1000]
        if let l = last, now > l.t {
            let dt: Double = now - l.t
            let pct: Double = (cpu - l.cpu) / dt * 100
            out["interval_s"] = (dt * 10).rounded() / 10
            out["cpu_pct"] = (pct * 1000).rounded() / 1000
            out["wakeups"] = w >= l.wake ? Int(w - l.wake) : 0
            out["idle_wakeups"] = i >= l.idle ? Int(i - l.idle) : 0
        }
        if let (fp, peak) = Usage.footprint() {
            out["footprint_mb"] = (fp * 10).rounded() / 10
            out["peak_mb"] = (peak * 10).rounded() / 10
        }
        if let e = extra?() { out.merge(e) { a, _ in a } }
        last = (now, cpu, w, i)
        emit(out)
    }
}

// ---- フローティングバー(画面の下・前面のアプリから入力を奪わない) ---------------------------------------------------------
final class BarPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class BarView: NSView {
    var state = "idle"        // idle | rec | busy | done | cancel | error
    var handsfree = false
    var text = ""             // 状態の文(ハンズフリーの間は終わり方の案内)
    var hint = ""             // 待ち受け(常に出すとき)の一言
    var levels: [CGFloat] = Array(repeating: 0, count: 16)
    var level: CGFloat = 0
    var started = Date()
    var tick = 0
    var onClick: (() -> Void)? = nil
    var onMenu: ((NSEvent, NSView) -> Void)? = nil

    static let accent = NSColor(calibratedRed: 0.243, green: 0.839, blue: 0.784, alpha: 1)   // #3ed6c8(アプリの基本色)
    static let rec = NSColor(calibratedRed: 1.0, green: 0.365, blue: 0.424, alpha: 1)        // #ff5d6c
    static let warn = NSColor(calibratedRed: 1.0, green: 0.82, blue: 0.4, alpha: 1)

    override var isFlipped: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { onClick?() }
    override func rightMouseDown(with event: NSEvent) { onMenu?(event, self) }

    func caption() -> String {
        switch state {
        case "rec":
            let s = Int(Date().timeIntervalSince(started))
            let t = String(format: "%d:%02d", s / 60, s % 60)
            return handsfree ? "ハンズフリー  \(t)" + (text.isEmpty ? "" : "   \(text)") : t
        case "idle": return hint
        default: return text
        }
    }

    static func font() -> NSFont { NSFont.systemFont(ofSize: 12.5, weight: .semibold) }

    func preferredWidth() -> CGFloat {
        let w = (caption() as NSString).size(withAttributes: [.font: BarView.font()]).width
        let lead: CGFloat = state == "rec" ? 22 + CGFloat(levels.count) * 4 + 10 : 30
        return min(560, max(state == "idle" ? 120 : 150, ceil(lead + w + 22)))
    }

    override func draw(_ dirtyRect: NSRect) {
        let r = bounds.insetBy(dx: 1, dy: 1)
        let pill = NSBezierPath(roundedRect: r, xRadius: r.height / 2, yRadius: r.height / 2)
        NSColor(calibratedRed: 0.059, green: 0.114, blue: 0.161, alpha: state == "idle" ? 0.72 : 0.93).setFill()
        pill.fill()
        NSColor(calibratedWhite: 1, alpha: 0.12).setStroke()
        pill.lineWidth = 1
        pill.stroke()
        let midY = r.midY
        var x: CGFloat = 16
        switch state {
        case "rec":
            let blink = (tick / 6) % 2 == 0 || handsfree == false
            (blink ? BarView.rec : BarView.rec.withAlphaComponent(0.45)).setFill()
            NSBezierPath(ovalIn: NSRect(x: x, y: midY - 4.5, width: 9, height: 9)).fill()
            x += 18
            for (i, lv) in levels.enumerated() {
                let h = max(3, min(18, 3 + lv * 18))
                BarView.accent.withAlphaComponent(0.55 + 0.45 * CGFloat(i) / CGFloat(levels.count)).setFill()
                NSBezierPath(roundedRect: NSRect(x: x, y: midY - h / 2, width: 2.5, height: h), xRadius: 1.25, yRadius: 1.25).fill()
                x += 4
            }
            x += 8
        case "busy":
            for i in 0..<3 {
                let on = (tick / 3) % 3 == i
                NSColor(calibratedWhite: 1, alpha: on ? 0.95 : 0.35).setFill()
                NSBezierPath(ovalIn: NSRect(x: x + CGFloat(i) * 7, y: midY - 2.5, width: 5, height: 5)).fill()
            }
            x += 28
        case "done", "cancel", "error", "idle":
            let glyph = ["done": "✓", "cancel": "×", "error": "!", "idle": "●"][state] ?? ""
            let color = ["done": BarView.accent, "cancel": NSColor(calibratedWhite: 1, alpha: 0.7), "error": BarView.warn,
                         "idle": BarView.accent.withAlphaComponent(0.8)][state] ?? .white
            let g = NSAttributedString(string: glyph, attributes: [.font: NSFont.systemFont(ofSize: state == "idle" ? 9 : 14, weight: .bold),
                                                                   .foregroundColor: color])
            let gs = g.size()
            g.draw(at: NSPoint(x: x, y: midY - gs.height / 2))
            x += 18
        default:
            break
        }
        let cap = NSAttributedString(string: caption(), attributes: [
            .font: BarView.font(), .foregroundColor: NSColor(calibratedWhite: 1, alpha: state == "idle" ? 0.75 : 0.95)])
        let cs = cap.size()
        cap.draw(at: NSPoint(x: x, y: midY - cs.height / 2))
    }
}

// ---- 話している言葉(フローティングバーの上の窓・2026-10-19 keigoly様「文字起こしの字幕が出ていなかった」) ----------------------
// Aqua Voice の窓と同じ並び: 話しているそばの文(末尾に青い点)・左下 × = 取り消し・真ん中 = マイクの名前・右下 ■ = 止める。
// 長い文は、今話しているところ(末尾)が見えるように頭を削る。前面のアプリから入力を奪わない(BarPanel)
final class LiveView: NSView {
    var text = ""
    var mic = ""
    var recording = true
    var onCancel: (() -> Void)? = nil
    var onStop: (() -> Void)? = nil
    var cancelRect = NSRect.zero
    var stopRect = NSRect.zero
    static let font = NSFont.systemFont(ofSize: 16, weight: .medium)
    static let padX: CGFloat = 22
    static let padTop: CGFloat = 16
    static let row: CGFloat = 34
    static let maxText: CGFloat = 112   // 5 行ほど(それより長い文は頭を「…」で削る)
    static let blue = NSColor(calibratedRed: 0.29, green: 0.56, blue: 1.0, alpha: 1)

    override var isFlipped: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    func attributed(_ str: String) -> NSAttributedString {
        let para = NSMutableParagraphStyle()
        para.lineBreakMode = .byWordWrapping
        para.lineSpacing = 4
        let a = NSMutableAttributedString(string: str, attributes: [.font: LiveView.font, .foregroundColor: NSColor.white,
                                                                    .paragraphStyle: para])
        if recording {
            a.append(NSAttributedString(string: "  ●", attributes: [.font: NSFont.systemFont(ofSize: 12), .foregroundColor: LiveView.blue,
                                                                    .paragraphStyle: para]))
        }
        return a
    }

    func height(of str: String, width: CGFloat) -> CGFloat {
        ceil(attributed(str).boundingRect(with: NSSize(width: width, height: 10_000),
                                          options: [.usesLineFragmentOrigin, .usesFontLeading]).height)
    }

    /// 長い文は、末尾(今話しているところ)が見えるように頭を削る
    func fitted(width: CGFloat) -> (String, CGFloat) {
        var str = text
        var h = height(of: str, width: width)
        let chars = Array(text)
        var cut = 0
        while h > LiveView.maxText && cut < chars.count {
            cut = min(chars.count, cut + 6)
            str = "…" + String(chars[cut...])
            h = height(of: str, width: width)
        }
        return (str, min(h, LiveView.maxText))
    }

    func preferredHeight(width: CGFloat) -> CGFloat {
        LiveView.padTop + max(fitted(width: width - 2 * LiveView.padX).1, 20) + 8 + LiveView.row
    }

    override func draw(_ dirtyRect: NSRect) {
        let r = bounds.insetBy(dx: 1, dy: 1)
        let bg = NSBezierPath(roundedRect: r, xRadius: 22, yRadius: 22)
        NSColor(calibratedWhite: 0.03, alpha: 0.94).setFill()
        bg.fill()
        NSColor(calibratedWhite: 1, alpha: 0.12).setStroke()
        bg.lineWidth = 1
        bg.stroke()
        let w = bounds.width - 2 * LiveView.padX
        let (str, h) = fitted(width: w)
        attributed(str).draw(with: NSRect(x: LiveView.padX, y: LiveView.padTop, width: w, height: h + 4),
                             options: [.usesLineFragmentOrigin, .usesFontLeading])
        let cy = bounds.height - LiveView.row / 2 - 4
        cancelRect = NSRect(x: 14, y: cy - 11, width: 22, height: 22)
        NSColor(calibratedWhite: 0.55, alpha: 0.9).setFill()
        NSBezierPath(ovalIn: cancelRect).fill()
        let x = NSBezierPath()
        x.lineWidth = 1.7
        x.lineCapStyle = .round
        x.move(to: NSPoint(x: cancelRect.minX + 7.5, y: cancelRect.minY + 7.5))
        x.line(to: NSPoint(x: cancelRect.maxX - 7.5, y: cancelRect.maxY - 7.5))
        x.move(to: NSPoint(x: cancelRect.maxX - 7.5, y: cancelRect.minY + 7.5))
        x.line(to: NSPoint(x: cancelRect.minX + 7.5, y: cancelRect.maxY - 7.5))
        NSColor(calibratedWhite: 0.1, alpha: 1).setStroke()
        x.stroke()
        if !mic.isEmpty {
            let m = NSAttributedString(string: mic, attributes: [.font: NSFont.systemFont(ofSize: 11.5, weight: .medium),
                                                                 .foregroundColor: NSColor(calibratedWhite: 1, alpha: 0.55)])
            let ms = m.size()
            m.draw(at: NSPoint(x: (bounds.width - ms.width) / 2, y: cy - ms.height / 2))
        }
        if recording {
            stopRect = NSRect(x: bounds.width - 36, y: cy - 11, width: 22, height: 22)
            BarView.rec.withAlphaComponent(0.9).setFill()
            NSBezierPath(roundedRect: stopRect, xRadius: 6, yRadius: 6).fill()
            NSColor.white.setFill()
            NSBezierPath(roundedRect: stopRect.insetBy(dx: 7, dy: 7), xRadius: 1.5, yRadius: 1.5).fill()
        } else {
            stopRect = .zero
        }
    }

    override func mouseDown(with event: NSEvent) {
        let pt = convert(event.locationInWindow, from: nil)
        if cancelRect.insetBy(dx: -6, dy: -6).contains(pt) {
            onCancel?()
        } else if stopRect != .zero && stopRect.insetBy(dx: -6, dy: -6).contains(pt) {
            onStop?()
        }
    }
}

final class Bar: NSObject {
    let enabled: Bool
    var mode = "recording"     // recording(録っている間と結果だけ)| always | off
    var panel: BarPanel? = nil
    let view = BarView(frame: NSRect(x: 0, y: 0, width: 160, height: 34))
    var timer: Timer? = nil
    var hideWork: DispatchWorkItem? = nil
    var onAction: ((String) -> Void)? = nil
    var livePanel: BarPanel? = nil
    let liveView = LiveView(frame: NSRect(x: 0, y: 0, width: 520, height: 90))
    var liveOn = true          // 話している言葉の窓を出すか(設定の「話している言葉を出す」)

    init(enabled: Bool) {
        self.enabled = enabled
        super.init()
        view.onClick = { [weak self] in self?.onAction?("handsfree") }
        view.onMenu = { [weak self] ev, v in self?.menu(ev, v) }
        liveView.onCancel = { [weak self] in self?.onAction?("cancel") }
        liveView.onStop = { [weak self] in self?.onAction?("stop") }
    }

    static func makePanel(_ v: NSView) -> BarPanel {
        let p = BarPanel(contentRect: v.frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        p.isFloatingPanel = true
        p.level = .statusBar
        p.hasShadow = true
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hidesOnDeactivate = false
        p.becomesKeyOnlyIfNeeded = true
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        p.contentView = v
        return p
    }

    /// 話している言葉(空なら窓を閉じる)。録っている間・文字にしている間・貼り終えた直後だけ出す
    func setLive(_ text: String) {
        guard enabled else { return }
        liveView.text = text
        guard liveOn, mode != "off", !text.isEmpty, ["rec", "busy", "done"].contains(view.state) else {
            livePanel?.orderOut(nil)
            return
        }
        if livePanel == nil { livePanel = Bar.makePanel(liveView) }
        placeLive()
        liveView.needsDisplay = true
        livePanel?.orderFrontRegardless()
    }

    func placeLive() {
        guard let lp = livePanel, let p = panel, let vf = (p.screen ?? NSScreen.main)?.visibleFrame else { return }
        let w = min(520, vf.width - 40)
        let h = liveView.preferredHeight(width: w)
        lp.setFrame(NSRect(x: vf.midX - w / 2, y: p.frame.maxY + 8, width: w, height: h), display: true)
        liveView.frame = NSRect(x: 0, y: 0, width: w, height: h)
    }

    func ensurePanel() -> BarPanel {
        if let p = panel { return p }
        let p = Bar.makePanel(view)
        panel = p
        return p
    }

    func place() {
        guard let p = panel else { return }
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        guard let vf = screen?.visibleFrame else { return }
        let w = view.preferredWidth()
        let f = NSRect(x: vf.midX - w / 2, y: vf.minY + 18, width: w, height: 34)
        p.setFrame(f, display: true)
        view.frame = NSRect(x: 0, y: 0, width: w, height: 34)
        if livePanel?.isVisible == true { placeLive() }
    }

    func set(_ state: String, text: String = "", handsfree: Bool = false) {
        guard enabled else { return }
        hideWork?.cancel()
        hideWork = nil
        let wasRec = view.state == "rec"
        view.state = state
        view.text = text
        view.handsfree = handsfree
        if state == "rec" && !wasRec {
            view.started = Date()
            view.levels = Array(repeating: 0, count: view.levels.count)
            liveView.text = ""   // 新しい 1 回: 前の文を消す(届くまで窓は出さない)
            liveView.recording = true
            livePanel?.orderOut(nil)
        }
        if state != "rec" { liveView.recording = false }
        if ["idle", "cancel", "error"].contains(state) { livePanel?.orderOut(nil) } else { liveView.needsDisplay = true }
        if mode == "off" || (state == "idle" && mode != "always") {
            panel?.orderOut(nil)
            livePanel?.orderOut(nil)
            stopTimer()
            return
        }
        let p = ensurePanel()
        place()
        view.needsDisplay = true
        p.orderFrontRegardless()
        if state == "rec" || state == "busy" { startTimer() } else { stopTimer() }
        if ["done", "cancel", "error"].contains(state) {
            let w = DispatchWorkItem { [weak self] in self?.set("idle") }
            hideWork = w
            DispatchQueue.main.asyncAfter(deadline: .now() + (state == "done" ? 1.4 : 2.6), execute: w)
        }
    }

    func setLevel(_ rms: Float) {
        guard view.state == "rec" else { return }
        let db = rms > 0 ? 20 * log10(rms) : -80
        let v = CGFloat(max(0, min(1, (db + 55) / 45)))
        view.level = max(v, view.level * 0.7)
    }

    func startTimer() {
        guard timer == nil else { return }
        let t = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.view.tick += 1
            if self.view.state == "rec" {
                self.view.levels.removeFirst()
                self.view.levels.append(self.view.level)
                self.view.level *= 0.6
            }
            self.place()
            self.view.needsDisplay = true
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func stopTimer() {
        timer?.invalidate()
        timer = nil
    }

    func menu(_ ev: NSEvent, _ v: NSView) {
        let m = NSMenu()
        let items: [(String, String)] = view.state == "rec"
            ? [("終わる(貼り付け)", "handsfree"), ("取り消す", "cancel"), ("最後の文字起こしを貼り付け", "paste_last")]
            : [("最後の文字起こしを貼り付け", "paste_last"), ("ハンズフリーで話す", "handsfree")]
        for (title, action) in items {
            let it = NSMenuItem(title: title, action: #selector(menuItem(_:)), keyEquivalent: "")
            it.target = self
            it.representedObject = action
            m.addItem(it)
        }
        NSMenu.popUpContextMenu(m, with: ev, for: v)
    }

    @objc func menuItem(_ sender: NSMenuItem) {
        if NSApp.isActive { NSApp.deactivate() }   // 貼り付けを前面のアプリへ届ける
        if let a = sender.representedObject as? String { onAction?(a) }
    }
}

// ---- ナギのペット(FR-14c v0・2026-10-19 keigoly様「常時表示させて、動作も邪魔にならない、メモリとかも CPU とかも極力食わないように」) ----
// 設計: Vault 01_Projects/Meeting Cue/Nagi_Pet_Brief.md。v0 は今の 6 表情の絵(meetcue/ui/static/nagi/*.png・236×320)だけで作る。
// ・窓: 縁なし・背景なし・前面を奪わない小さな NSPanel。ignoresMouseEvents は触らない(= 透明な画素のクリックは OS が下の窓へ
//   通す)。絵の上はドラッグで動かせて、位置を state-dir/pet.json に覚える。カーソルが重なると薄くなる。
// ・絵: 6 表情を合わせた「絵のある範囲」で切り詰め、表示する大きさの画素に縮めて 1 回だけ読む(中で 1 枚 約 0.2 MB)。
//   胸から下の切れ目は窓の下に沈めて隠す(画面の下からのぞく形。上下に動いても切れ目が見えない)。
// ・動き: Core Animation の層の変形だけ(絵は描き直さない)。コマ送りは OS の描画側(WindowServer)が行い、ヘルパーは寝ている。
//   描画側の負担はコマの数に比例する(10-19 の実測: うなずき続けると WindowServer の CPU が 1 コアの約 2.5% 増えた)ので、
//   続けて動くのは音声入力で録っている間(うなずく)と文字にしている間(揺れる)だけ。待っている間は止まった絵で 12〜24 s に
//   1 回、会議の録音中は 8〜16 s に 1 回だけ小さく動く(次の 1 回をその都度予約する。回り続けるタイマーは持たない)。
//   ゆっくりした動きは毎秒 20 コマ・速い 1 回の動き(跳ねる・指さす・困る)は 30 コマ。
// ・止める: 窓が見えない(ほかの窓に隠れた・画面が眠った・ロック)/ 視差効果を減らす → 変形を外して止まった絵。
//   低電力モードでは止めずに控えめにする(ゆっくりした動きは毎秒 15 コマ・時々の動きの間をおよそ 1.5 倍に。10-19 の実測で、
//   この Mac は電源につないだままでも低電力モードがオンで、止める決まりではナギが一度も動かなかった)。
final class PetPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class PetView: NSView {
    weak var pet: Pet?
    var downAt: NSPoint? = nil
    var origin: NSPoint? = nil
    var dragged = false
    var area: NSTrackingArea? = nil

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override var mouseDownCanMoveWindow: Bool { false }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let a = area { removeTrackingArea(a) }
        let a = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self, userInfo: nil)
        addTrackingArea(a)
        area = a
    }

    override func mouseEntered(with event: NSEvent) { pet?.hover(true) }
    override func mouseExited(with event: NSEvent) { pet?.hover(false) }

    /// 絵の上(不透明な画素)だけでクリックを受ける
    override func hitTest(_ point: NSPoint) -> NSView? {
        let p = superview.map { convert(point, from: $0) } ?? point
        return pet?.opaque(at: p) == true ? self : nil
    }

    override func mouseDown(with event: NSEvent) {
        downAt = NSEvent.mouseLocation
        origin = window?.frame.origin
        dragged = false
        pet?.pressed(true)
    }

    override func mouseDragged(with event: NSEvent) {
        guard let d = downAt, let o = origin, let w = window else { return }
        let m = NSEvent.mouseLocation
        if !dragged && hypot(m.x - d.x, m.y - d.y) < 3 { return }
        dragged = true
        w.setFrameOrigin(NSPoint(x: (o.x + m.x - d.x).rounded(), y: (o.y + m.y - d.y).rounded()))
    }

    override func mouseUp(with event: NSEvent) {
        pet?.pressed(false)
        if dragged { pet?.moved() } else if downAt != nil { pet?.poke() }
        downAt = nil
        dragged = false
    }

    override func rightMouseDown(with event: NSEvent) { pet?.menu(event, self) }
}

final class Pet: NSObject {
    static let faces = ["idle", "listen", "think", "cue", "done", "alert"]
    static let heights: [String: CGFloat] = ["s": 92, "m": 120, "l": 156]   // 絵の高さ(pt)。v0 の絵は縦 320 px なので「大」は少しぼける
    static let faded: CGFloat = 0.3
    let enabled: Bool
    let dir: URL?
    let posFile: URL?
    var mode = "off"            // always | recording | off(本体の設定 pet。届くまでは出さない)
    var size = "m"              // s | m | l(本体の設定 pet_size)
    var state = "idle"          // 今の表情
    var dict = "idle"           // 音声入力(バーの状態): idle | rec | busy | done | cancel | error
    var meeting = "idle"        // 会議: idle | listen(録音中)| think(保存・サマリ)
    var panel: PetPanel? = nil
    let view = PetView(frame: NSRect(x: 0, y: 0, width: 100, height: 120))
    let root = CALayer()
    let clip = CALayer()        // 窓の大きさで切る(下に沈めた切れ目を隠す)
    let body = CALayer()        // ナギの絵(足もとを支点に動かす)
    var images: [String: CGImage] = [:]
    var masks: [String: [UInt8]] = [:]
    var px = (w: 0, h: 0)
    var loaded = ""             // 読んだ大きさと倍率("m@2.0")。変わったら読み直す
    var loadFailed = false      // 絵が読めなかった(知らせは 1 回だけ)
    var bodyFrame = CGRect.zero
    var shown = false           // 出すと決めている(フェードの途中を含む)
    var visible = false         // OS の occlusionState(実際に見えている)
    var asleep = false          // 画面が眠った・ほかの人に切り替えた
    var hovering = false
    var holding = false
    var idleWork: DispatchWorkItem? = nil
    var backWork: DispatchWorkItem? = nil   // 1 回だけの表情(cue・done・alert)から戻す
    var fadeWork: DispatchWorkItem? = nil
    var onEvent: (([String: Any]) -> Void)? = nil
    var debug = false

    func note(_ what: String, _ extra: [String: Any] = [:]) {
        guard debug else { return }
        emit(["type": "pet_debug", "event": what, "state": state].merging(extra) { a, _ in a })
    }

    init(enabled: Bool, dir: String?, stateDir: String?) {
        self.enabled = enabled && dir != nil
        self.dir = dir.map { URL(fileURLWithPath: $0) }
        posFile = stateDir.map { URL(fileURLWithPath: $0).appendingPathComponent("pet.json") }
        super.init()
        view.pet = self
        view.layer = root            // 層をこちらで持つ(AppKit に描き直させない)
        view.wantsLayer = true
        root.addSublayer(clip)
        clip.masksToBounds = true
        clip.addSublayer(body)
        body.anchorPoint = CGPoint(x: 0.5, y: 0)
        body.contentsGravity = .resize
        let ws = NSWorkspace.shared.notificationCenter
        for n in [NSWorkspace.screensDidSleepNotification, NSWorkspace.screensDidWakeNotification,
                  NSWorkspace.sessionDidResignActiveNotification, NSWorkspace.sessionDidBecomeActiveNotification] {
            ws.addObserver(self, selector: #selector(sleepChanged(_:)), name: n, object: nil)
        }
        ws.addObserver(self, selector: #selector(motionChanged), name: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(motionChanged), name: Notification.Name.NSProcessInfoPowerStateDidChange, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged), name: NSApplication.didChangeScreenParametersNotification, object: nil)
    }

    var reduceMotion: Bool { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
    var lowPower: Bool { ProcessInfo.processInfo.isLowPowerModeEnabled }
    var canAnimate: Bool { shown && visible && !asleep && !reduceMotion }
    var fps: Float { lowPower ? 15 : 20 }   // ゆっくりした小さな動き(うなずき・揺れ・時々の動き)
    let quick: Float = 30                   // 速い 1 回の動き(跳ねる・指さす・困る・クリック)

    // ---- 本体からの知らせ ----
    /// 設定(pet・pet_size)
    func configure(mode m: String?, size s: String?) {
        if let m = m, ["always", "recording", "off"].contains(m) { mode = m }
        if let s = s, Pet.heights[s] != nil, s != size {
            size = s
            if shown {
                layout(keepBottomCenter: true)
                if let f = posFile, FileManager.default.fileExists(atPath: f.path) { moved() }   // 動かしてあれば、足もとの位置で覚え直す
            }
        }
        apply()
    }

    /// 音声入力の状態(バーと同じ知らせ)
    func setDictation(_ s: String) {
        dict = s
        switch s {
        case "rec", "busy":
            backWork?.cancel()
            backWork = nil
            show(base())
        case "done": flash("done", 1.6)
        case "error": flash("alert", 2.6)
        default:   // idle・cancel。1 回だけの表情の途中なら、それが終わってから戻る
            if backWork == nil { show(base()) }
        }
        if shown { motion(restart: false) }   // 表情が同じでも動き方が変わる(会議の録音中 → 音声入力で録る)
        apply()
    }

    /// 会議の状態(idle | listen | think)
    func setMeeting(_ m: String) {
        meeting = ["listen", "think"].contains(m) ? m : "idle"
        if backWork == nil { show(base()) }
        if shown { motion(restart: false) }
        apply()
    }

    /// 会議の出来事(cue = 自分への質問を検知・done = 保存した)。音声入力の最中は割り込まない
    func meetingEvent(_ e: String) {
        guard !["rec", "busy"].contains(dict) else { return }
        switch e {
        case "cue": flash("cue", 2.4)
        case "done": flash("done", 1.6)
        default: return
        }
        apply()
    }

    func base() -> String {
        if dict == "rec" { return "listen" }
        if dict == "busy" { return "think" }
        return meeting == "idle" ? "idle" : meeting
    }

    /// 1 回だけの表情。secs の後に、そのときの状態の表情へ戻す
    func flash(_ face: String, _ secs: Double) {
        backWork?.cancel()
        show(face, force: true)
        let w = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            self.backWork = nil
            self.show(self.base())
            self.apply()
        }
        backWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + secs, execute: w)
    }

    // ---- 出す・隠す ----
    func apply() {
        let active = ["rec", "busy"].contains(dict) || meeting != "idle" || backWork != nil
        let want = enabled && (mode == "always" || (mode == "recording" && active))
        if want { open() } else { close() }
    }

    func open() {
        if shown && panel?.isVisible == true { return }
        let p = ensurePanel()
        guard layout(keepBottomCenter: false) else { return }
        shown = true
        p.alphaValue = 0
        p.orderFrontRegardless()
        visible = p.occlusionState.contains(.visible)
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.25
            p.animator().alphaValue = hovering ? Pet.faded : 1
        }
        motion(restart: true)
    }

    func close() {
        guard shown, let p = panel else { return }
        shown = false
        motion(restart: true)   // canAnimate = false なので、動きを外すだけ
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.2
            p.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self = self, !self.shown else { return }
            p.orderOut(nil)
        })
    }

    func ensurePanel() -> PetPanel {
        if let p = panel { return p }
        let p = PetPanel(contentRect: view.frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        p.isFloatingPanel = true
        p.level = .floating
        p.hasShadow = false
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hidesOnDeactivate = false
        p.becomesKeyOnlyIfNeeded = true
        p.isMovable = false   // 動かすのはこちら(絵のドラッグ)だけ
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        p.contentView = view
        NotificationCenter.default.addObserver(self, selector: #selector(occlusionChanged(_:)),
                                               name: NSWindow.didChangeOcclusionStateNotification, object: p)
        panel = p
        return p
    }

    /// 6 表情を読み、合わせた「絵のある範囲」で切り詰めて、高さ pxH の画素に縮める。(絵, 透明度, 幅, 高さ)
    static func prepare(dir: URL, pxH: Int) -> ([String: CGImage], [String: [UInt8]], Int, Int)? {
        var full: [String: CGImage] = [:]
        for f in faces {
            guard let src = CGImageSourceCreateWithURL(dir.appendingPathComponent("\(f).png") as CFURL, nil),
                  let img = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCache: false] as CFDictionary) else { continue }
            full[f] = img
        }
        guard full["idle"] != nil, pxH > 0 else { return nil }
        var box = CGRect.null
        for img in full.values {
            if let b = opaqueBox(img) { box = box.union(b) }
        }
        guard !box.isNull, box.height > 0 else { return nil }
        let pxW = max(1, Int((CGFloat(pxH) * box.width / box.height).rounded()))
        let space = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()
        var images: [String: CGImage] = [:]
        var masks: [String: [UInt8]] = [:]
        for (f, img) in full {
            guard let c = img.cropping(to: box),
                  let ctx = CGContext(data: nil, width: pxW, height: pxH, bitsPerComponent: 8, bytesPerRow: pxW * 4, space: space,
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { continue }
            ctx.interpolationQuality = .high
            ctx.draw(c, in: CGRect(x: 0, y: 0, width: pxW, height: pxH))
            guard let out = ctx.makeImage(), let data = ctx.data else { continue }
            let p = data.bindMemory(to: UInt8.self, capacity: pxW * pxH * 4)
            var m = [UInt8](repeating: 0, count: pxW * pxH)
            for i in 0..<(pxW * pxH) { m[i] = p[i * 4 + 3] }
            images[f] = out
            masks[f] = m
        }
        return images["idle"] == nil ? nil : (images, masks, pxW, pxH)
    }

    /// 不透明な画素の範囲(画像の左上が原点。画素の並びは上の行から)
    static func opaqueBox(_ img: CGImage) -> CGRect? {
        let w = img.width, h = img.height
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { return nil }
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
        guard let data = ctx.data else { return nil }
        let p = data.bindMemory(to: UInt8.self, capacity: w * h * 4)
        var minX = w, minY = h, maxX = -1, maxY = -1
        for y in 0..<h {
            for x in 0..<w where p[(y * w + x) * 4 + 3] > 8 {
                minX = min(minX, x)
                maxX = max(maxX, x)
                minY = min(minY, y)
                maxY = max(maxY, y)
            }
        }
        guard maxX >= minX, maxY >= minY else { return nil }
        return CGRect(x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1)
    }

    /// 窓の中の絵の置き方(pt)。sink = 窓の下に沈める分・top = 跳ねる分の余白・pad = 揺れる分の余白
    static func geometry(height h: CGFloat, px: (w: Int, h: Int)) -> (win: NSSize, w: CGFloat, sink: CGFloat) {
        let w = (h * CGFloat(px.w) / CGFloat(max(1, px.h))).rounded()
        let sink = (h * 0.09).rounded()
        let top = (h * 0.1).rounded()
        let pad: CGFloat = 8
        return (NSSize(width: w + pad * 2, height: h - sink + top), w, sink)
    }

    func load(scale: CGFloat) -> Bool {
        let key = "\(size)@\(scale)"
        if key == loaded && !images.isEmpty { return true }
        guard let dir = dir, let r = Pet.prepare(dir: dir, pxH: Int(((Pet.heights[size] ?? 120) * scale).rounded())) else {
            if !loadFailed { emit(["type": "error", "phase": "pet", "error": "no_images", "dir": dir?.path ?? ""]) }
            loadFailed = true
            return false
        }
        images = r.0
        masks = r.1
        px = (r.2, r.3)
        loaded = key
        return true
    }

    /// 大きさと位置。絵が読めなければ false
    @discardableResult
    func layout(keepBottomCenter: Bool) -> Bool {
        let p = ensurePanel()
        let scale = (p.screen ?? NSScreen.main)?.backingScaleFactor ?? 2
        guard load(scale: scale) else { return false }
        let h = Pet.heights[size] ?? 120
        let g = Pet.geometry(height: h, px: px)
        let old = p.frame
        let origin = keepBottomCenter && p.isVisible
            ? NSPoint(x: (old.midX - g.win.width / 2).rounded(), y: old.minY)
            : (savedOrigin(g.win) ?? defaultOrigin(g.win))
        p.setFrame(NSRect(origin: origin, size: g.win), display: false)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        root.frame = CGRect(origin: .zero, size: g.win)
        clip.frame = root.bounds
        body.bounds = CGRect(x: 0, y: 0, width: g.w, height: h)
        body.position = CGPoint(x: g.win.width / 2, y: -g.sink)
        body.contentsScale = scale
        CATransaction.commit()
        bodyFrame = CGRect(x: (g.win.width - g.w) / 2, y: -g.sink, width: g.w, height: h)
        setFace(animated: false)
        return true
    }

    func defaultOrigin(_ s: NSSize) -> NSPoint {
        let vf = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        return NSPoint(x: (vf.maxX - s.width - 24).rounded(), y: vf.minY.rounded())   // 右下(Dock の上)からのぞく
    }

    func savedOrigin(_ s: NSSize) -> NSPoint? {
        guard let f = posFile, let d = try? Data(contentsOf: f),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let x = o["x"] as? Double, let y = o["y"] as? Double else { return nil }
        let c = NSPoint(x: x + s.width / 2, y: y + s.height / 2)
        guard NSScreen.screens.contains(where: { NSMouseInRect(c, $0.frame, false) }) else { return nil }   // 外したディスプレイなど
        return NSPoint(x: x, y: y)
    }

    /// ドラッグし終えた: 画面の下の端の近くなら下の端にそろえて(切れ目を隠す)、位置を覚える
    func moved() {
        guard let p = panel else { return }
        var o = p.frame.origin
        let c = NSPoint(x: p.frame.midX, y: p.frame.midY)
        if let sc = NSScreen.screens.first(where: { NSMouseInRect(c, $0.frame, false) }) ?? NSScreen.main {
            let vf = sc.visibleFrame
            if abs(o.y - vf.minY) < 18 { o.y = vf.minY } else if abs(o.y - sc.frame.minY) < 18 { o.y = sc.frame.minY }
            o.x = min(max(o.x, sc.frame.minX - p.frame.width / 2), sc.frame.maxX - p.frame.width / 2)
            o.y = min(max(o.y, sc.frame.minY), sc.frame.maxY - p.frame.height / 2)
        }
        p.setFrameOrigin(o)
        if let f = posFile, let d = try? JSONSerialization.data(withJSONObject: ["x": Double(o.x), "y": Double(o.y)]) {
            try? d.write(to: f, options: .atomic)
        }
    }

    func resetPosition() {
        if let f = posFile { try? FileManager.default.removeItem(at: f) }
        guard let p = panel else { return }
        p.setFrameOrigin(defaultOrigin(p.frame.size))
    }

    // ---- 絵と動き ----
    /// 表情を変える(絵の差し替えは 0.18 s のフェード)。動きは表情ごと
    func show(_ face: String, force: Bool = false) {
        guard force || face != state else { return }
        state = face
        guard shown else { return }   // 出していない間は、出す時に合わせる
        setFace(animated: true)
        motion(restart: true)
    }

    func setFace(animated: Bool) {
        guard let img = images[state] ?? images["idle"] else { return }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        if animated && visible && !asleep {
            let t = CATransition()
            t.type = .fade
            t.duration = 0.18
            body.add(t, forKey: "face")
        }
        body.contents = img
        CATransaction.commit()
    }

    static func anim(_ key: String, _ values: [Double], _ dur: Double, times: [Double]? = nil, forever: Bool = false,
                     fps: Float = 30) -> CAKeyframeAnimation {
        let a = CAKeyframeAnimation(keyPath: key)
        a.values = values
        if let t = times { a.keyTimes = t.map { NSNumber(value: $0) } }
        a.timingFunctions = Array(repeating: CAMediaTimingFunction(name: .easeInEaseOut), count: max(1, values.count - 1))
        a.duration = dur
        if forever { a.repeatCount = .infinity }
        if #available(macOS 12.0, *) { a.preferredFrameRateRange = CAFrameRateRange(minimum: 10, maximum: fps, preferred: fps) }
        return a
    }

    static func hop(fps: Float = 30) -> CAAnimation {
        let g = CAAnimationGroup()
        g.animations = [anim("transform.translation.y", [0, 10, 0], 0.55, times: [0, 0.4, 1], fps: fps),
                        anim("transform.scale.y", [1, 1.03, 0.97, 1], 0.55, times: [0, 0.35, 0.8, 1], fps: fps)]
        g.duration = 0.55
        if #available(macOS 12.0, *) { g.preferredFrameRateRange = CAFrameRateRange(minimum: 10, maximum: fps, preferred: fps) }
        return g
    }

    /// 今の表情の動きを付け直す(動かせないときは外して、止まった絵にする)
    func motion(restart: Bool) {
        idleWork?.cancel()
        idleWork = nil
        guard canAnimate else {
            for k in ["loop", "once", "gesture", "poke"] { body.removeAnimation(forKey: k) }
            return
        }
        if restart { for k in ["loop", "once", "gesture"] { body.removeAnimation(forKey: k) } }
        if !((state == "listen" && dict == "rec") || state == "think") { body.removeAnimation(forKey: "loop") }
        switch state {
        case "listen" where dict == "rec":   // 音声入力で録っている間(短い): 小さくうなずき続ける
            if body.animation(forKey: "loop") == nil {
                body.add(Pet.anim("transform.translation.y", [0, -2.5, 0], 1.3, forever: true, fps: fps), forKey: "loop")
            }
        case "think":    // 考えている: ゆっくり左右に揺れる
            if body.animation(forKey: "loop") == nil {
                body.add(Pet.anim("transform.rotation.z", [0, 0.022, 0, -0.022, 0], 3.2, forever: true, fps: fps), forKey: "loop")
            }
        case "cue":
            if restart { body.add(Pet.anim("transform.translation.y", [0, 9, 0, 4, 0], 0.7, times: [0, 0.3, 0.55, 0.75, 1], fps: quick), forKey: "once") }
        case "done":
            if restart { body.add(Pet.hop(fps: quick), forKey: "once") }
        case "alert":
            if restart { body.add(Pet.anim("transform.translation.x", [0, -3, 3, -3, 3, 0], 0.45, fps: quick), forKey: "once") }
        default:         // 待ち受け・会議の録音中(長い): 時々だけ動く
            scheduleIdle()
        }
    }

    /// 時々の動き: 待っている間は 12〜24 s(呼吸・背伸び・首をかしげる)、会議の録音中は 8〜16 s(2 回うなずく)後に 1 回だけ。
    /// 低電力モードは間を 1.5 倍に。動いたら次の 1 回を予約する
    func scheduleIdle() {
        idleWork?.cancel()
        let face = state
        guard face == "idle" || face == "listen" else { return }
        let w = DispatchWorkItem { [weak self] in
            guard let self = self, self.state == face, self.canAnimate else { return }
            self.idleWork = nil
            let g: CAAnimation
            if face == "listen" {
                g = Pet.anim("transform.translation.y", [0, -2.5, 0, -2.5, 0], 1.8, fps: self.fps)
            } else {
                switch Int.random(in: 0..<3) {
                case 0: g = Pet.anim("transform.scale.y", [1, 1.02, 1], 2.6, fps: self.fps)
                case 1: g = Pet.anim("transform.translation.y", [0, 4, 0], 1.6, fps: self.fps)
                default: g = Pet.anim("transform.rotation.z", [0, 0.018, -0.012, 0], 2.4, fps: self.fps)
                }
            }
            self.body.add(g, forKey: "gesture")
            self.scheduleIdle()
        }
        idleWork = w
        let base = face == "listen" ? Double.random(in: 8...16) : Double.random(in: 12...24)
        DispatchQueue.main.asyncAfter(deadline: .now() + base * (lowPower ? 1.5 : 1), execute: w)
    }

    /// 左クリック: 小さく跳ねるだけ(録音などは始めない)
    func poke() {
        guard canAnimate, body.animation(forKey: "once") == nil else { return }
        body.add(Pet.anim("transform.translation.y", [0, 6, 0], 0.35, fps: quick), forKey: "poke")
    }

    // ---- マウス ----
    func opaque(at p: NSPoint) -> Bool {
        guard let m = masks[state] ?? masks["idle"], px.w > 0, bodyFrame.contains(p) else { return false }
        let x = Int((p.x - bodyFrame.minX) / bodyFrame.width * CGFloat(px.w))
        let y = Int((bodyFrame.maxY - p.y) / bodyFrame.height * CGFloat(px.h))
        guard x >= 0, x < px.w, y >= 0, y < px.h else { return false }
        return m[y * px.w + x] > 24
    }

    /// カーソルが重なったら薄く(すぐ通り過ぎたときは変えない)
    func hover(_ on: Bool) {
        hovering = on
        note("hover", ["on": on])
        fadeWork?.cancel()
        let w = DispatchWorkItem { [weak self] in self?.fade() }
        fadeWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + (on ? 0.25 : 0), execute: w)
    }

    func pressed(_ on: Bool) {
        holding = on
        note("press", ["on": on])
        fade()
    }

    func fade() {
        guard shown, let p = panel else { return }
        let a: CGFloat = hovering && !holding ? Pet.faded : 1
        guard abs(p.alphaValue - a) > 0.01 else { return }
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.15
            p.animator().alphaValue = a
        }
    }

    func menu(_ ev: NSEvent, _ v: NSView) {
        let m = NSMenu()
        func item(_ title: String, _ action: String, on: Bool = false) -> NSMenuItem {
            let it = NSMenuItem(title: title, action: #selector(menuItem(_:)), keyEquivalent: "")
            it.target = self
            it.representedObject = action
            it.state = on ? .on : .off
            return it
        }
        m.addItem(item("隠す(音声入力の設定で戻せます)", "hide"))
        m.addItem(item("録っている間だけ出す", "recording", on: mode == "recording"))
        m.addItem(.separator())
        for (s, t) in [("s", "小さく"), ("m", "ふつう"), ("l", "大きく")] { m.addItem(item(t, "size:" + s, on: size == s)) }
        m.addItem(item("位置を戻す", "reset"))
        m.addItem(.separator())
        m.addItem(item("音声入力の画面を開く", "open"))
        NSMenu.popUpContextMenu(m, with: ev, for: v)
    }

    @objc func menuItem(_ sender: NSMenuItem) {
        guard let a = sender.representedObject as? String else { return }
        if NSApp.isActive { NSApp.deactivate() }   // 前面のアプリを奪ったままにしない
        switch a {
        case "hide":
            configure(mode: "off", size: nil)
            onEvent?(["action": "set", "pet": "off"])
        case "recording":
            let m = mode == "recording" ? "always" : "recording"
            configure(mode: m, size: nil)
            onEvent?(["action": "set", "pet": m])
        case "reset": resetPosition()
        case "open": onEvent?(["action": "open"])
        default:
            guard a.hasPrefix("size:") else { return }
            let s = String(a.dropFirst(5))
            configure(mode: nil, size: s)
            onEvent?(["action": "set", "pet_size": s])
        }
    }

    // ---- 見えている間だけ動かす ----
    @objc func occlusionChanged(_ n: Notification) {
        visible = panel?.occlusionState.contains(.visible) ?? false
        note("occlusion", ["visible": visible])
        motion(restart: false)
    }

    @objc func sleepChanged(_ n: Notification) {
        asleep = n.name == NSWorkspace.screensDidSleepNotification || n.name == NSWorkspace.sessionDidResignActiveNotification
        motion(restart: false)
    }

    @objc func motionChanged() {
        DispatchQueue.main.async { self.motion(restart: false) }   // 低電力モードの知らせは別のスレッドから来る
    }

    @objc func screensChanged() {
        guard shown else { return }
        layout(keepBottomCenter: false)   // 覚えた位置が今の画面の外なら既定へ・倍率が変われば読み直す
    }

    /// 重さの記録に添える(Usage.extra)
    func info() -> [String: Any] {
        ["mode": mode, "size": size, "state": state, "shown": shown, "visible": visible, "moving": canAnimate, "low_power": lowPower,
         "reduce_motion": reduceMotion, "loop": body.animation(forKey: "loop") != nil]
    }
}

/// `--render-pet <dir> --pet-dir <絵>`: ペットの窓の見え方(切り詰め・沈め)を、大きさごとに 6 表情を並べた PNG へ
/// (上の段 = 明るい地・下の段 = 暗い地・枠 = 窓の大きさ。画面は使わない)
func renderPet(_ out: String, petDir: String?) {
    guard let pd = petDir else {
        print("{\"type\":\"error\",\"phase\":\"pet\",\"error\":\"no_pet_dir\"}")
        return
    }
    let url = URL(fileURLWithPath: out)
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    let scale: CGFloat = 2
    for (size, h) in Pet.heights.sorted(by: { $0.value < $1.value }) {
        guard let r = Pet.prepare(dir: URL(fileURLWithPath: pd), pxH: Int(h * scale)) else { continue }
        let g = Pet.geometry(height: h, px: (r.2, r.3))
        let cw = Int(g.win.width * scale), ch = Int(g.win.height * scale)
        let W = cw * Pet.faces.count, H = ch * 2
        guard let ctx = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: W * 4,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { continue }
        for row in 0..<2 {
            ctx.setFillColor(row == 1 ? CGColor(gray: 0.93, alpha: 1) : CGColor(red: 0.09, green: 0.11, blue: 0.14, alpha: 1))
            ctx.fill(CGRect(x: 0, y: row * ch, width: W, height: ch))
            for (i, f) in Pet.faces.enumerated() {
                guard let img = r.0[f] else { continue }
                let cell = CGRect(x: i * cw, y: row * ch, width: cw, height: ch)
                ctx.saveGState()
                ctx.clip(to: cell)
                ctx.draw(img, in: CGRect(x: cell.minX + (CGFloat(cw) - g.w * scale) / 2, y: cell.minY - g.sink * scale,
                                         width: g.w * scale, height: h * scale))
                ctx.restoreGState()
                ctx.setStrokeColor(CGColor(red: 1, green: 0.36, blue: 0.42, alpha: 0.8))
                ctx.stroke(cell.insetBy(dx: 0.5, dy: 0.5), width: 1)
            }
        }
        if let img = ctx.makeImage(), let png = NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:]) {
            try? png.write(to: url.appendingPathComponent("pet_\(size).png"))
        }
        print("{\"type\":\"rendered\",\"size\":\"\(size)\",\"window_pt\":[\(g.win.width),\(g.win.height)],\"px\":[\(r.2),\(r.3)]}")
    }
}

/// `--render-bar <dir>`: バーの見た目を状態ごとに PNG へ(確かめる用・画面は使わない)。
func renderBar(_ dir: String) {
    let url = URL(fileURLWithPath: dir)
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    let cases: [(String, String, String, Bool, String)] = [
        ("idle", "idle", "", false, "⌃⌥V で話す"),
        ("rec_hold", "rec", "", false, ""),
        ("rec_handsfree", "rec", "⌃⌥V で終わる・Esc で取り消し", true, ""),
        ("busy", "busy", "文字にしています…", false, ""),
        ("done", "done", "貼り付けました", false, ""),
        ("cancel", "cancel", "取り消しました(⌃⌥⇧V で貼り付け)", false, ""),
        ("error", "error", "Soniox につながりません", false, ""),
    ]
    for (name, state, text, hf, hint) in cases {
        let v = BarView(frame: NSRect(x: 0, y: 0, width: 160, height: 34))
        v.state = state
        v.text = text
        v.handsfree = hf
        v.hint = hint
        v.started = Date().addingTimeInterval(-12)
        v.levels = (0..<v.levels.count).map { CGFloat(abs(sin(Double($0) * 0.7))) * 0.8 }
        v.frame = NSRect(x: 0, y: 0, width: v.preferredWidth(), height: 34)
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(v.frame.width * 2), pixelsHigh: 68,
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { continue }
        rep.size = v.frame.size
        guard let base = NSGraphicsContext(bitmapImageRep: rep) else { continue }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = base
        NSColor(calibratedWhite: 0.93, alpha: 1).setFill()
        NSRect(origin: .zero, size: v.frame.size).fill()
        let cg = base.cgContext   // BarView は上から下へ描く(isFlipped)ので、文字の向きも合わせて裏返した文脈で描く
        cg.translateBy(x: 0, y: v.frame.height)
        cg.scaleBy(x: 1, y: -1)
        NSGraphicsContext.current = NSGraphicsContext(cgContext: cg, flipped: true)
        v.draw(v.bounds)
        NSGraphicsContext.restoreGraphicsState()
        if let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: url.appendingPathComponent("bar_\(name).png"))
        }
    }
    let liveCases: [(String, String, Bool)] = [
        ("live_rec", "フローティングバーが出まして、ちゃんと録り始めと終わりの音が鳴っていました。ただその上に、文字起こしの文が出ていなかったので", true),
        ("live_long", String(repeating: "長い口述でも、今話しているところが見えるように、頭を削って末尾を出します。", count: 6), true),
        ("live_busy", "本日はお時間をいただき、ありがとうございます。", false),
    ]
    for (name, text, rec) in liveCases {
        let v = LiveView(frame: NSRect(x: 0, y: 0, width: 520, height: 90))
        v.text = text
        v.mic = "MacBook Airのマイク"
        v.recording = rec
        let h = v.preferredHeight(width: 520)
        v.frame = NSRect(x: 0, y: 0, width: 520, height: h)
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1040, pixelsHigh: Int(h * 2), bitsPerSample: 8,
                                         samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                         bytesPerRow: 0, bitsPerPixel: 0) else { continue }
        rep.size = v.frame.size   // 先に大きさ(点)を決めてから文脈を作る(2 倍で描く)
        guard let base = NSGraphicsContext(bitmapImageRep: rep) else { continue }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = base
        NSColor(calibratedWhite: 0.93, alpha: 1).setFill()
        NSRect(origin: .zero, size: v.frame.size).fill()
        let cg = base.cgContext
        cg.translateBy(x: 0, y: v.frame.height)
        cg.scaleBy(x: 1, y: -1)
        NSGraphicsContext.current = NSGraphicsContext(cgContext: cg, flipped: true)
        v.draw(v.bounds)
        NSGraphicsContext.restoreGraphicsState()
        if let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: url.appendingPathComponent("\(name).png"))
        }
    }
    print("{\"type\":\"rendered\",\"dir\":\"\(dir)\"}")
}

/// `--selftest-capture`: 「次に押したキーを拾う」の判定を、作った知らせで確かめる(システムへは何も送らない・キーも登録しない)
func selftestCapture() {
    let k = Keys()
    let fnDown = CGEventFlags.maskSecondaryFn.rawValue
    let cases: [(String, [(CGEventType, Int, UInt64)], String?)] = [
        ("Fn だけ", [(.flagsChanged, kVK_Function, fnDown), (.flagsChanged, kVK_Function, 0)], "fn"),
        ("右 ⌘ だけ", [(.flagsChanged, kVK_RightCommand, CGEventFlags.maskCommand.rawValue | 0x10), (.flagsChanged, kVK_RightCommand, 0)], "rcmd"),
        ("⌃⌥V", [(.flagsChanged, kVK_Control, CGEventFlags.maskControl.rawValue | 0x1),
                 (.flagsChanged, kVK_Option, CGEventFlags.maskControl.rawValue | CGEventFlags.maskAlternate.rawValue | 0x21),
                 (.keyDown, kVK_ANSI_V, CGEventFlags.maskControl.rawValue | CGEventFlags.maskAlternate.rawValue | 0x21)], "ctrl+opt+V"),
        ("Fn を押したまま ←(Fn は組み合わせに使った)", [(.flagsChanged, kVK_Function, fnDown), (.keyDown, kVK_LeftArrow, fnDown),
                                                (.flagsChanged, kVK_Function, 0)], "LEFT"),
        ("F5", [(.keyDown, kVK_F5, 0)], "F5"),
        ("Esc", [(.keyDown, kVK_Escape, 0)], "ESCAPE"),
    ]
    var ok = true
    for (name, steps, want) in cases {
        k.capPending = nil
        k.capOther = false
        var got: String? = nil
        for (t, code, flags) in steps {
            if let s = k.captureStep(t, code: code, flags: flags, autorepeat: false) { got = s; break }
        }
        let pass = got == want
        ok = ok && pass
        print("\(pass ? "ok  " : "NG  ") \(name): \(got ?? "nil")(期待 \(want ?? "nil"))")
    }
    print(ok ? "selftest ok" : "selftest NG")
}

func micStatus() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "undetermined"
    @unknown default: return "unknown"
    }
}

struct Args {
    var bindings: [String: Any] = ["activate": ["ctrl+opt+V"], "lock": "SPACE", "cancel": "ESCAPE"]
    var file: String? = nil
    var emits: [(String, String, Double)] = []
    var paste = true
    var sound = true
    var bar = true
    var stateDir: String? = nil
    var renderBar: String? = nil
    var resourceS: Double = 60
    var petDir: String? = nil
    var pet = true
    var renderPet: String? = nil
    var petDebug = false

    static func parse() -> Args {
        var a = Args()
        var key: String? = nil
        var mods: [String]? = nil
        let argv = CommandLine.arguments
        var i = 1
        while i < argv.count {
            let next: String? = i + 1 < argv.count ? argv[i + 1] : nil
            switch argv[i] {
            case "--bindings":
                if let n = next, let d = n.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { a.bindings = obj }
                i += 1
            case "--key": key = next?.uppercased(); i += 1
            case "--mods": mods = next?.split(separator: ",").map { String($0) }; i += 1
            case "--file": a.file = next; i += 1
            case "--state-dir": a.stateDir = next; i += 1
            case "--render-bar": a.renderBar = next; i += 1
            case "--resource-s": a.resourceS = Double(next ?? "") ?? a.resourceS; i += 1
            case "--pet-dir": a.petDir = next; i += 1
            case "--render-pet": a.renderPet = next; i += 1
            case "--no-pet": a.pet = false
            case "--pet-debug": a.petDebug = true
            case "--emit":
                for item in (next ?? "").split(separator: ",") {
                    let p = item.split(separator: "@")
                    guard p.count == 2, let t = Double(p[1]) else { continue }
                    let what = p[0].split(separator: ".")
                    if what.count == 2 {
                        a.emits.append((String(what[0]), String(what[1]), t))
                    } else {
                        a.emits.append(("activate", String(what[0]), t))   // 旧: "down@1,up@4"
                    }
                }
                i += 1
            case "--no-paste": a.paste = false
            case "--no-sound": a.sound = false
            case "--no-bar": a.bar = false
            default: break
            }
            i += 1
        }
        if key != nil || mods != nil {   // 旧の引数: 有効化 1 つ
            a.bindings["activate"] = [((mods ?? ["ctrl", "opt"]) + [key ?? "V"]).joined(separator: "+")]
        }
        return a
    }
}

@main
struct DictationHelper {
    static func main() {
        let args = Args.parse()
        if let dir = args.renderBar {
            renderBar(dir)
            return
        }
        if let dir = args.renderPet {
            renderPet(dir, petDir: args.petDir)
            return
        }
        if CommandLine.arguments.contains("--selftest-capture") {
            selftestCapture()
            return
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let capture = Capture(file: args.file)
        let paster = Paster(enabled: args.paste)
        let sounds = Sounds(on: args.sound)
        let bar = Bar(enabled: args.bar)
        let muter = Muter(stateDir: args.stateDir)
        muter.recover()
        let usage = Usage(every: args.resourceS)
        let pet = Pet(enabled: args.pet, dir: args.petDir, stateDir: args.stateDir)
        pet.debug = args.petDebug
        pet.onEvent = { ev in
            var e = ev
            e["type"] = "pet"
            emit(e)
        }
        usage.extra = { ["pet": pet.info()] }
        gKeys = Keys()
        capture.onLevel = { bar.setLevel($0) }
        capture.onMic = { bar.liveView.mic = $0 }
        bar.onAction = { action in emit(["type": "key", "action": action, "state": "down", "index": 0, "via": "bar"]) }

        var specs = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
                     EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))]
        InstallEventHandler(GetEventDispatcherTarget(), hotkeyHandler, 2, &specs, nil, nil)
        let res = gKeys.apply(Bindings.from(args.bindings))
        let first = (res["bindings"] as? [String: [[String: Any]]])?["activate"]?.first ?? [:]
        emit(["type": "ready", "bindings": res["bindings"] ?? [:], "listen": res["listen"] ?? false,
              "hotkey": first["label"] ?? "", "status": first["status"] ?? ((first["ok"] as? Bool) == true ? 0 : -1),
              "mic": micStatus(), "ax": AXIsProcessTrusted(), "source": args.file == nil ? "mic" : "file", "paste": args.paste])
        usage.start()

        func quit() {
            capture.stop()
            _ = muter.set(false)
            gKeys.unregisterAll()
            emit(["type": "bye"])
            outQ.sync {}
            exit(0)
        }
        var takeGen = 0
        var muteGen = 0
        func startTake(_ obj: [String: Any]) {
            if capture.running && capture.meterOnly { capture.stop() }
            let level = obj["context"] as? String ?? "app"
            let selection = obj["selection"] as? Bool ?? false
            // 選んだ文と画面の文脈は、マイクより先に読む(マイクの起動が遅いと届くのが遅れ、編集モードを取り違える)
            let early = level == "full" || selection
            if early { emit(captureContext(level: level, selection: selection)) }
            capture.start()
            gKeys.setRecording(capture.running)
            if !early { emit(captureContext(level: level, selection: selection)) }
            takeGen += 1
            let g = takeGen
            // 本体が止まっても録り続けない(本体の上限 max_s 300 s の後ろの備え)
            DispatchQueue.main.asyncAfter(deadline: .now() + 330) {
                guard g == takeGen, capture.running, !capture.meterOnly else { return }
                stopTake()
                emit(["type": "error", "phase": "mic", "error": "max_duration"])
            }
        }
        func stopTake() {
            capture.stop()
            gKeys.setRecording(false)
        }
        func handleJSON(_ obj: [String: Any]) {
            switch obj["cmd"] as? String ?? "" {
            case "start": startTake(obj)
            case "stop": stopTake()
            case "paste": paster.paste(obj["text"] as? String ?? "", send: obj["send"] as? String, id: obj["id"] as? Int,
                                       expect: obj["expect"] as? String)
            case "clip": paster.clip(obj["text"] as? String ?? "")
            case "capture":
                if obj["on"] as? Bool ?? false {
                    let ok = gKeys.startCapture()
                    emit(["type": "capture", "on": ok, "ok": ok, "listen": CGPreflightListenEventAccess()])
                } else {
                    gKeys.stopCapture()
                    emit(["type": "capture", "on": false, "ok": true])
                }
            case "bindings":
                var r = gKeys.apply(Bindings.from(obj["bindings"] as? [String: Any] ?? [:]))
                r["type"] = "bindings"
                emit(r)
            case "suspend":
                var r = gKeys.suspend(obj["on"] as? Bool ?? false)
                r["type"] = "bindings"
                emit(r)
            case "bar":
                let st = obj["state"] as? String ?? "idle"
                bar.set(st, text: obj["text"] as? String ?? "", handsfree: obj["handsfree"] as? Bool ?? false)
                pet.setDictation(st)   // バーを出さない設定でも、ペットの表情は変える
            case "pet":
                pet.configure(mode: obj["mode"] as? String, size: obj["size"] as? String)
                if let m = obj["meeting"] as? String { pet.setMeeting(m) }
                if let e = obj["event"] as? String { pet.meetingEvent(e) }
            case "live": bar.setLive(obj["text"] as? String ?? "")
            case "bar_mode":
                bar.mode = obj["mode"] as? String ?? "recording"
                bar.liveOn = obj["live"] as? Bool ?? bar.liveOn
                bar.view.hint = obj["hint"] as? String ?? bar.view.hint
                if bar.view.state == "idle" { bar.set("idle") }
            case "sound": sounds.play(obj["name"] as? String ?? "")
            case "sounds":
                sounds.on = obj["on"] as? Bool ?? sounds.on
                sounds.set = obj["set"] as? String ?? sounds.set
            case "mute":
                let on = obj["on"] as? Bool ?? false
                let delay = Double(obj["delay_ms"] as? Int ?? 0) / 1000
                muteGen += 1
                let g = muteGen
                DispatchQueue.main.asyncAfter(deadline: .now() + (on ? delay : 0)) {
                    guard g == muteGen else { return }   // 後の指示を優先(すぐ終わった 1 回の消音が、戻した後に効かないように)
                    var r = muter.set(on)
                    r["type"] = "mute"
                    r["on"] = on
                    emit(r)
                }
            case "mic": capture.micUID = obj["uid"] as? String ?? ""
            case "devices": emit(["type": "devices", "devices": inputDevices()])
            case "apps":
                let apps = NSWorkspace.shared.runningApplications
                    .filter { $0.activationPolicy == .regular && $0.bundleIdentifier != nil }
                    .map { ["bundle": $0.bundleIdentifier!, "name": $0.localizedName ?? $0.bundleIdentifier!] }
                emit(["type": "apps", "apps": apps])
            case "meter":
                if obj["on"] as? Bool ?? false {
                    guard !capture.running else { emit(["type": "meter", "on": false, "error": "busy"]); return }
                    capture.start(meter: true)
                    let secs = Double(obj["seconds"] as? Int ?? 8)
                    DispatchQueue.main.asyncAfter(deadline: .now() + secs) { if capture.meterOnly { capture.stop() } }
                } else if capture.meterOnly {
                    capture.stop()
                }
            case "clipboard": paster.avoidHistory = obj["avoid_history"] as? Bool ?? true
            default: break
            }
        }
        func handle(_ line: String) {
            if line.hasPrefix("{") {
                guard let d = line.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
                handleJSON(obj)
                return
            }
            switch line {
            case "start": startTake([:])
            case "stop": stopTake()
            case "ax_prompt":
                let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
                emit(["type": "ax", "trusted": AXIsProcessTrustedWithOptions(opts)])
            case "listen_prompt": emit(["type": "listen", "granted": CGRequestListenEventAccess()])
            case "quit": quit()
            default: break
            }
        }

        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let si = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let sg = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        si.setEventHandler { quit() }
        sg.setEventHandler { quit() }
        si.resume()
        sg.resume()
        Thread.detachNewThread {
            while let line = readLine(strippingNewline: true) {
                let s = line.trimmingCharacters(in: .whitespaces)
                DispatchQueue.main.async { handle(s) }
            }
            DispatchQueue.main.async { quit() }   // stdin が閉じた = 本体が終わった
        }
        for (action, state, t) in args.emits {
            DispatchQueue.main.asyncAfter(deadline: .now() + t) {
                emit(["type": "key", "action": action, "state": state, "index": 0, "via": "test", "test": true])
            }
        }
        _ = si
        _ = sg
        app.run()
    }
}

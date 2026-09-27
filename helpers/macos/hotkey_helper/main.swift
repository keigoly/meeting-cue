// hotkey-helper — グローバルホットキー(Carbon RegisterEventHotKey・Accessibility 許可不要)。
// 押下ごとに stdout へ {"type":"hotkey","name":"pause"|"deepdive"|"mode"} を 1 行出力する。
//   ⌃⌥P = pause(一時停止/再開)  ⌃⌥D = deepdive(直近の発話を強制的にキュー生成)  ⌃⌥M = mode(切替)
// stdin "quit" / SIGINT / SIGTERM で終了。テスト用: --emit "pause@2,deepdive@4"(秒後に自動発火)。
// ビルド: swiftc -parse-as-library -O main.swift -o hotkey-helper

import Carbon
import Foundation

func emit(_ obj: [String: Any]) {
    var f = obj
    f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000)
    if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

let NAMES: [UInt32: String] = [1: "pause", 2: "deepdive", 3: "mode"]

@main
struct HotkeyHelper {
    static func main() {
        let args = CommandLine.arguments
        var tests: [(String, Double)] = []
        if let i = args.firstIndex(of: "--emit"), i + 1 < args.count {
            for item in args[i + 1].split(separator: ",") {
                let p = item.split(separator: "@")
                if p.count == 2, let t = Double(p[1]) { tests.append((String(p[0]), t)) }
            }
        }
        let handler: EventHandlerUPP = { (_, event, _) -> OSStatus in
            var hk = EventHotKeyID()
            let st = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                       nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
            if st == noErr, let name = NAMES[hk.id] { emit(["type": "hotkey", "name": name]) }
            return noErr
        }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetEventDispatcherTarget(), handler, 1, &spec, nil, nil)
        let mods = UInt32(controlKey | optionKey)
        var refs: [EventHotKeyRef?] = [nil, nil, nil]
        var statuses: [String: Int] = [:]
        for (id, key) in [(UInt32(1), kVK_ANSI_P), (UInt32(2), kVK_ANSI_D), (UInt32(3), kVK_ANSI_M)] {
            let hid = EventHotKeyID(signature: OSType(0x4D43_5545), id: id)  // 'MCUE'
            let st = RegisterEventHotKey(UInt32(key), mods, hid, GetEventDispatcherTarget(), 0, &refs[Int(id) - 1])
            statuses[NAMES[id]!] = Int(st)
        }
        emit(["type": "ready", "hotkeys": ["pause": "⌃⌥P", "deepdive": "⌃⌥D", "mode": "⌃⌥M"], "status": statuses])

        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let onSig: @convention(block) () -> Void = { CFRunLoopStop(CFRunLoopGetMain()) }
        let si = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let sg = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        si.setEventHandler(handler: onSig); sg.setEventHandler(handler: onSig)
        si.resume(); sg.resume()
        Thread.detachNewThread {
            while let line = readLine(strippingNewline: true) {
                if line.trimmingCharacters(in: .whitespaces) == "quit" { CFRunLoopStop(CFRunLoopGetMain()); return }
            }
        }
        for (name, t) in tests {
            DispatchQueue.main.asyncAfter(deadline: .now() + t) { emit(["type": "hotkey", "name": name, "test": true]) }
        }
        _ = si; _ = sg; _ = refs
        CFRunLoopRun()
        emit(["type": "bye"])
        exit(0)
    }
}

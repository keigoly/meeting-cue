// mix-helper — 録音の音声(相手 = system / 自分 = mic)を 1 本の .m4a に重ねる(記録の「書き出す」用・2026-09-26)。
//
//   mix-helper --out <出力.m4a> --in <入力.m4a>[@<開始のずれ 秒>] [--in …]
//
// 各入力を「開始のずれ」だけ後ろにずらして重ね(meta.json の audio.<ch>.t0_ms の差 = 画面の再生と同じ頭合わせ)、
// AAC(.m4a)で書き出す。AVFoundation だけ(追加依存なし)。出力が既にあれば上書きする。
// stdout に結果を JSON 1 行: {"ok": true, "ms": …, "duration_ms": …, "inputs": n} / {"ok": false, "error": "…"}。終了コード 0 / 1。
// ビルド: swiftc -parse-as-library -O main.swift -o mix-helper(helpers/macos で make)

import AVFoundation
import Foundation

func emit(_ fields: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        print(s)
    }
}

func fail(_ msg: String) -> Never {
    emit(["ok": false, "error": msg])
    exit(1)
}

@main
struct MixHelper {
    static func main() async {
        let t0 = Date()
        var out: URL?
        var inputs: [(URL, Double)] = []
        var args = CommandLine.arguments.dropFirst().makeIterator()
        while let a = args.next() {
            switch a {
            case "--out":
                guard let p = args.next() else { fail("--out に値が無い") }
                out = URL(fileURLWithPath: p)
            case "--in":
                guard let spec = args.next() else { fail("--in に値が無い") }
                var path = spec, off = 0.0
                if let at = spec.lastIndex(of: "@"), let v = Double(spec[spec.index(after: at)...]) {
                    path = String(spec[..<at]); off = max(0, v)
                }
                inputs.append((URL(fileURLWithPath: path), off))
            default:
                fail("知らない引数: \(a)")
            }
        }
        guard let out else { fail("--out が要る") }
        guard !inputs.isEmpty else { fail("--in が 1 つも無い") }

        let comp = AVMutableComposition()
        var used = 0
        for (url, off) in inputs {
            let asset = AVURLAsset(url: url)
            do {
                guard let track = try await asset.loadTracks(withMediaType: .audio).first else { continue }   // 音の無い入力は飛ばす
                let dur = try await asset.load(.duration)
                guard let ct = comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) else {
                    fail("トラックを作れない")
                }
                try ct.insertTimeRange(CMTimeRange(start: .zero, duration: dur), of: track,
                                       at: CMTime(seconds: off, preferredTimescale: 1000))
                used += 1
            } catch {
                fail("読めない: \(url.path): \(error.localizedDescription)")
            }
        }
        guard used > 0 else { fail("音声のある入力が無い") }

        try? FileManager.default.removeItem(at: out)
        guard let ex = AVAssetExportSession(asset: comp, presetName: AVAssetExportPresetAppleM4A) else {
            fail("書き出しの準備ができない")
        }
        do {
            if #available(macOS 15, *) {
                try await ex.export(to: out, as: .m4a)   // 複数の音声トラックは 1 本に重ねて書かれる
            } else {
                ex.outputURL = out
                ex.outputFileType = .m4a
                await ex.export()
                if ex.status != .completed { throw ex.error ?? NSError(domain: "mix", code: 1) }
            }
        } catch {
            fail("書き出せない: \(error.localizedDescription)")
        }
        emit(["ok": true, "ms": Int(Date().timeIntervalSince(t0) * 1000),
              "duration_ms": Int(comp.duration.seconds * 1000), "inputs": used])
    }
}

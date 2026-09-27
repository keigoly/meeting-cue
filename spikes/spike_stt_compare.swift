// 自分の声(マイク)の文字起こし精度の比較(2026-09-26 Step 1)。録音済みの音声 1 本を、認識の方式を変えて文字にする。
// 本番の stt-helper には手を入れない(比較で勝った方式だけを、あとでヘルパーの任意の引数として足す)。
//
//   swiftc -parse-as-library -O spikes/spike_stt_compare.swift -o <scratch>/stt-compare
//   <scratch>/stt-compare --file mic.m4a --mode st-fast [--vocab words.txt] [--locale ja-JP]
//
// --mode
//   st-fast   SpeechTranscriber + fastResults(今の stt-helper と同じ設定)
//   st        SpeechTranscriber(fastResults なし)
//   dict      DictationTranscriber(システムの音声入力と同じモデル・句読点あり)
//   dict-atyp DictationTranscriber + atypicalSpeech(聞き取りにくい話し方の指定)
// --vocab: 1 行 1 語の語彙(AnalysisContext.contextualStrings・最大 100)。SpeechTranscriber に効くかも確かめる。
// 出力: stdout に確定結果の JSONL({"type":"final","text","start_s","end_s"})と最後に {"type":"done","text","ms"}。
import AVFoundation
import CoreMedia
import Foundation
import Speech

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(2)
}

@main
struct STTCompare {
    static func main() async {
        var file = "", mode = "st-fast", vocabPath = "", locale = "ja-JP"
        var it = CommandLine.arguments.dropFirst().makeIterator()
        while let a = it.next() {
            switch a {
            case "--file": file = it.next() ?? ""
            case "--mode": mode = it.next() ?? mode
            case "--vocab": vocabPath = it.next() ?? ""
            case "--locale": locale = it.next() ?? locale
            default: fail("unknown arg: \(a)")
            }
        }
        if file.isEmpty { fail("--file が要る") }
        let loc = Locale(identifier: locale)
        let module: any SpeechModule
        let results: AsyncThrowingStream<(String, CMTimeRange, Bool, Double?), Error>
        // 最初の語の時刻(2026-09-27 時刻ずれの調査: 結果の範囲は前の確定の終わりから始まり、間の無音を含む)
        func tokStart(_ t: AttributedString) -> Double? {
            for run in t.runs { if let r = run.audioTimeRange { return CMTimeGetSeconds(r.start) } }
            return nil
        }
        switch mode {
        case "st-fast", "st":
            let t = SpeechTranscriber(locale: loc, transcriptionOptions: [],
                                      reportingOptions: mode == "st-fast" ? [.volatileResults, .fastResults] : [.volatileResults],
                                      attributeOptions: [.audioTimeRange])
            module = t
            results = AsyncThrowingStream { c in
                Task {
                    do { for try await r in t.results { c.yield((String(r.text.characters), r.range, r.isFinal, tokStart(r.text))) }; c.finish() }
                    catch { c.finish(throwing: error) }
                }
            }
        case "dict", "dict-atyp":
            let t = DictationTranscriber(locale: loc, contentHints: mode == "dict-atyp" ? [.atypicalSpeech] : [],
                                         transcriptionOptions: [.punctuation], reportingOptions: [],
                                         attributeOptions: [.audioTimeRange])
            module = t
            results = AsyncThrowingStream { c in
                Task {
                    do { for try await r in t.results { c.yield((String(r.text.characters), r.range, r.isFinal, tokStart(r.text))) }; c.finish() }
                    catch { c.finish(throwing: error) }
                }
            }
        default:
            fail("unknown mode: \(mode)")
        }
        do {
            if let req = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
                try await req.downloadAndInstall()
            }
            let ctx = AnalysisContext()
            if !vocabPath.isEmpty {
                let words = try String(contentsOfFile: vocabPath, encoding: .utf8)
                    .split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                ctx.contextualStrings[.general] = Array(words.prefix(100))
            }
            let t0 = Date()
            let collector = Task { () -> [String] in
                var finals: [String] = []
                for try await (text, range, isFinal, tok) in results where isFinal {
                    finals.append(text)
                    emit(["type": "final", "text": text,
                          "start_s": (CMTimeGetSeconds(range.start) * 100).rounded() / 100,
                          "end_s": (CMTimeGetSeconds(range.end) * 100).rounded() / 100,
                          "tok_start_s": tok.map { ($0 * 100).rounded() / 100 } ?? -1])
                }
                return finals
            }
            let audio = try AVAudioFile(forReading: URL(fileURLWithPath: file))
            let analyzer = try await SpeechAnalyzer(inputAudioFile: audio, modules: [module], analysisContext: ctx,
                                                    finishAfterFile: true)
            _ = analyzer
            let finals = try await collector.value
            emit(["type": "done", "mode": mode, "vocab": !vocabPath.isEmpty, "text": finals.joined(),
                  "ms": Int(Date().timeIntervalSince(t0) * 1000)])
        } catch {
            fail("error: \(error)")
        }
    }
}

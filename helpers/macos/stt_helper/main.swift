// stt-helper — マイク入力(既定の音声入力)の日本語ストリーミング STT(macOS 26 SpeechAnalyzer)。
// 前例(作者の別プロジェクトのリアルタイム字幕)の stt_helper と同じプロトコルの写し。
//
// stdout(1 行 1 JSON・純粋なイベント列):
//   {"type":"ready","locale":"ja-JP","channel":"mic","dst_hz":16000}
//   {"type":"partial","text":"…","start_s":0.0,"end_s":1.2,"t_ms":…}
//   {"type":"final","text":"…","start_s":0.0,"end_s":1.2,"t_ms":…}
//   {"type":"bye"}
// stderr: 診断(JSON)。0.1 s ごとの音量 {"phase":"level","db":…,"peak_db":…} もここに流す。
// stdin(1 行 1 コマンド): "finalize"(強制確定・ja は自動 final が出ない) / "quit"。
//
// 引数: [--locale ja-JP] [--channel mic] [--file <audio>] [--pace 1.0] [--record <out.m4a>]
//   --file はテスト用。ファイルを --pace 倍速(1.0=実時間)で供給し、終わったら自動で quit する。
// ビルド: swiftc -parse-as-library -O main.swift -o stt-helper(helpers/macos/Makefile)
// 権限: 初回にマイク許可のダイアログ。--file はマイクを開かない。

import AVFoundation
import CoreMedia
import Foundation
import Speech

func emit(_ fields: [String: Any]) {
    var f = fields
    f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000)
    if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

func diag(_ fields: [String: Any]) {
    var f = fields
    f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000)
    if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        FileHandle.standardError.write(Data((s + "\n").utf8))
    }
}

final class TimeBox: @unchecked Sendable {
    private let lock = NSLock()
    private var seconds: Double = 0
    func add(_ s: Double) { lock.lock(); seconds += s; lock.unlock() }
    func value() -> Double { lock.lock(); let s = seconds; lock.unlock(); return s }
    func cmtime() -> CMTime { CMTime(seconds: value(), preferredTimescale: 1000) }
}

struct Args {
    var locale = "ja-JP"
    var channel = "mic"
    var file: String? = nil
    var pace = 1.0
    static func parse() -> Args {
        var a = Args()
        let argv = CommandLine.arguments
        var i = 1
        while i < argv.count {
            switch argv[i] {
            case "--locale": if i + 1 < argv.count { a.locale = argv[i + 1]; i += 1 }
            case "--channel": if i + 1 < argv.count { a.channel = argv[i + 1]; i += 1 }
            case "--file": if i + 1 < argv.count { a.file = argv[i + 1]; i += 1 }
            case "--pace": if i + 1 < argv.count { a.pace = Double(argv[i + 1]) ?? 1.0; i += 1 }
            case "--record": i += 1   // gRecorder が読む
            default: if !argv[i].hasPrefix("--") { a.locale = argv[i] }
            }
            i += 1
        }
        return a
    }
}

// 音量メーター: analyzer 形式に変換した音声の 0.1 s ごとの RMS / ピーク(dBFS)を stderr の診断で流す
// ({"phase":"level","db":-42.1,"peak_db":-30.2})。stdout の文字起こしの契約は変えない。
// 波形表示と「相手側が無音」の検知に使う。書き出しは直列キューで行い、音声スレッドを止めない。
final class LevelMeter: @unchecked Sendable {
    private let lock = NSLock()
    private let out = DispatchQueue(label: "meetcue.level")
    private var sumSq: Double = 0
    private var peak: Double = 0
    private var n: Int = 0
    func add(_ buf: AVAudioPCMBuffer) {
        let frames = Int(buf.frameLength)
        guard frames > 0 else { return }
        let window = max(1, Int(buf.format.sampleRate * 0.1))
        var ready: [(Double, Double)] = []
        lock.lock()
        for i in 0..<frames {
            let v: Double
            if let f = buf.floatChannelData { v = Double(f[0][i]) }
            else if let s = buf.int16ChannelData { v = Double(s[0][i]) / 32768.0 }
            else { lock.unlock(); return }
            sumSq += v * v
            peak = max(peak, abs(v))
            n += 1
            if n >= window {
                ready.append(((sumSq / Double(n)).squareRoot(), peak))
                sumSq = 0; peak = 0; n = 0
            }
        }
        lock.unlock()
        if ready.isEmpty { return }
        out.async {
            func db(_ x: Double) -> Double { x > 0 ? max(-90, (20 * log10(x) * 10).rounded() / 10) : -90 }
            for (rms, pk) in ready { diag(["phase": "level", "db": db(rms), "peak_db": db(pk)]) }
        }
    }
}
let gMeter = LevelMeter()

// 録音(2026-09-26 U2): analyzer に渡すのと同じ音声(16 kHz モノラル)を AAC の m4a に保存する。
// 文字起こしの start_s / end_s と同じ時間軸なので、画面から発言の位置へ正確に飛べる。
// 書き込みは直列キューで行い、音声スレッドを止めない。最初の書き込みで record_start(t0_ms = その音声の頭の壁時計)を
// stderr に出す(マイクとスピーカーの頭合わせに使う)。正規の停止で close() し、ファイルを完結させる。
final class Recorder: @unchecked Sendable {
    private let q = DispatchQueue(label: "meetcue.record")
    private let url: URL
    private var file: AVAudioFile?
    private var failed = false
    private var frames: Int64 = 0
    init(path: String) { url = URL(fileURLWithPath: path) }
    func write(_ buf: AVAudioPCMBuffer) {
        let nowMs = Int(Date().timeIntervalSince1970 * 1000)
        q.async {
            if self.failed { return }
            if self.file == nil {
                do {
                    try FileManager.default.createDirectory(at: self.url.deletingLastPathComponent(),
                                                            withIntermediateDirectories: true)
                    let base: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: buf.format.sampleRate,
                                               AVNumberOfChannelsKey: 1]
                    var settings = base
                    settings[AVEncoderBitRateKey] = 32000
                    do {
                        self.file = try AVAudioFile(forWriting: self.url, settings: settings,
                                                    commonFormat: buf.format.commonFormat, interleaved: buf.format.isInterleaved)
                    } catch {   // ビットレートが合わない環境では既定に任せる
                        self.file = try AVAudioFile(forWriting: self.url, settings: base,
                                                    commonFormat: buf.format.commonFormat, interleaved: buf.format.isInterleaved)
                    }
                    let headMs = Int(Double(buf.frameLength) / buf.format.sampleRate * 1000)
                    diag(["phase": "record_start", "path": self.url.path, "hz": buf.format.sampleRate, "t0_ms": nowMs - headMs])
                } catch {
                    self.failed = true
                    diag(["phase": "record_error", "error": "\(error)"])
                    return
                }
            }
            do {
                try self.file?.write(from: buf)
                self.frames += Int64(buf.frameLength)
            } catch {
                self.failed = true
                diag(["phase": "record_error", "error": "\(error)"])
            }
        }
    }
    func close() {
        q.sync {
            guard let f = self.file else { return }
            f.close()
            diag(["phase": "record_done", "path": self.url.path, "frames": self.frames])
            self.file = nil
        }
    }
}
var gRecorder: Recorder? = {
    let a = CommandLine.arguments
    if let i = a.firstIndex(of: "--record"), i + 1 < a.count { return Recorder(path: a[i + 1]) }
    return nil
}()

// 入力バッファを analyzer の形式へ変換して供給する(マイク・ファイル共通)。
func convertAndYield(_ inBuf: AVAudioPCMBuffer, conv: AVAudioConverter, dstFmt: AVAudioFormat,
                     cont: AsyncStream<AnalyzerInput>.Continuation, timeBox: TimeBox) {
    let ratio = dstFmt.sampleRate / inBuf.format.sampleRate
    let cap = AVAudioFrameCount(Double(inBuf.frameLength) * ratio) + 1024
    guard let outBuf = AVAudioPCMBuffer(pcmFormat: dstFmt, frameCapacity: cap) else { return }
    var used = false
    var convErr: NSError?
    _ = conv.convert(to: outBuf, error: &convErr) { _, status in
        if used { status.pointee = .noDataNow; return nil }
        used = true
        status.pointee = .haveData
        return inBuf
    }
    if convErr == nil && outBuf.frameLength > 0 {
        cont.yield(AnalyzerInput(buffer: outBuf))
        timeBox.add(Double(outBuf.frameLength) / dstFmt.sampleRate)
        gMeter.add(outBuf)
        gRecorder?.write(outBuf)
    }
}

@main
struct STTHelper {
    static func main() async {
        let args = Args.parse()
        do {
            let supported = await SpeechTranscriber.supportedLocales
            func bcp(_ l: Locale) -> String { l.identifier(.bcp47) }
            guard supported.contains(where: { bcp($0) == args.locale }) else {
                diag(["phase": "abort", "reason": "locale_not_supported", "target": args.locale])
                exit(2)
            }
            let transcriber = SpeechTranscriber(
                locale: Locale(identifier: args.locale),
                transcriptionOptions: [],
                reportingOptions: [.volatileResults, .fastResults],
                attributeOptions: [.audioTimeRange])
            let tA = Date()
            if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                diag(["phase": "asset_download_start"])
                try await req.downloadAndInstall()
            }
            diag(["phase": "asset_ready", "ms": Int(Date().timeIntervalSince(tA) * 1000)])
            guard let dstFmt = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
                diag(["phase": "abort", "reason": "no_format"])
                exit(3)
            }

            let (inputStream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
            let analyzer = SpeechAnalyzer(modules: [transcriber])
            let timeBox = TimeBox()

            let consumer = Task {
                var partials = 0
                var finals = 0
                do {
                    for try await res in transcriber.results {
                        // 始まりは最初の語の時刻(2026-09-27): 結果の範囲は前の確定の終わりから始まり、間の無音を含むことがある
                        // (録音開始直後の発言が 5 s 早く記録された)。語の時刻が無ければ従来どおり範囲の始まり
                        var startS = CMTimeGetSeconds(res.range.start)
                        for run in res.text.runs {
                            if let r = run.audioTimeRange {
                                let t = CMTimeGetSeconds(r.start)
                                if t.isFinite && t > startS && t <= CMTimeGetSeconds(res.range.end) { startS = t }
                                break
                            }
                        }
                        let endS = CMTimeGetSeconds(res.range.end)
                        if res.isFinal { finals += 1 } else { partials += 1 }
                        emit(["type": res.isFinal ? "final" : "partial",
                              "channel": args.channel,
                              "text": String(res.text.characters),
                              "start_s": (startS * 100).rounded() / 100,
                              "end_s": (endS * 100).rounded() / 100,
                              "fed_s": (timeBox.value() * 100).rounded() / 100])
                    }
                } catch {
                    diag(["phase": "results_error", "error": "\(error)"])
                }
                diag(["phase": "results_done", "partials": partials, "finals": finals])
            }

            try await analyzer.start(inputSequence: inputStream)

            let stop = DispatchSemaphore(value: 0)
            signal(SIGINT, SIG_IGN)
            signal(SIGTERM, SIG_IGN)
            let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
            let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
            sigint.setEventHandler { stop.signal() }
            sigterm.setEventHandler { stop.signal() }
            sigint.resume()
            sigterm.resume()
            Thread.detachNewThread {
                while let line = readLine(strippingNewline: true) {
                    let cmd = line.trimmingCharacters(in: .whitespaces)
                    if cmd == "quit" { stop.signal(); return }
                    if cmd == "finalize" {
                        let through = timeBox.cmtime()
                        Task {
                            do { try await analyzer.finalize(through: through) }
                            catch { diag(["phase": "finalize_error", "error": "\(error)"]) }
                        }
                    }
                }
                // stdin EOF では止めない(orchestrator 起動時に閉じることがある)。停止は quit / SIGTERM。
            }

            var engine: AVAudioEngine? = nil
            if let path = args.file {
                // --file: 実時間(×pace)で供給。終わったら少し待って停止(末尾の final を拾う)。
                let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
                let srcFmt = file.processingFormat
                guard let conv = AVAudioConverter(from: srcFmt, to: dstFmt) else {
                    diag(["phase": "abort", "reason": "no_converter"]); exit(4)
                }
                emit(["type": "ready", "locale": args.locale, "channel": args.channel,
                      "dst_hz": dstFmt.sampleRate, "source": "file:\(path)", "pace": args.pace])
                let chunkS = 0.1
                let chunkFrames = AVAudioFrameCount(srcFmt.sampleRate * chunkS)
                let feedStart = Date()
                var idx = 0
                Task.detached {
                    while file.framePosition < file.length {
                        guard let inBuf = AVAudioPCMBuffer(pcmFormat: srcFmt, frameCapacity: chunkFrames) else { break }
                        do { try file.read(into: inBuf, frameCount: chunkFrames) } catch { break }
                        if inBuf.frameLength == 0 { break }
                        convertAndYield(inBuf, conv: conv, dstFmt: dstFmt, cont: cont, timeBox: timeBox)
                        idx += 1
                        if args.pace > 0 {
                            let target = feedStart.addingTimeInterval(Double(idx) * chunkS / args.pace)
                            let sleepS = target.timeIntervalSinceNow
                            if sleepS > 0 { try? await Task.sleep(nanoseconds: UInt64(sleepS * 1e9)) }
                        }
                    }
                    diag(["phase": "feed_done", "audio_s": (timeBox.value() * 100).rounded() / 100,
                          "wall_ms": Int(Date().timeIntervalSince(feedStart) * 1000)])
                    try? await analyzer.finalize(through: timeBox.cmtime())
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    stop.signal()
                }
            } else {
                let eng = AVAudioEngine()
                let inputNode = eng.inputNode
                let inFmt = inputNode.outputFormat(forBus: 0)
                guard let conv = AVAudioConverter(from: inFmt, to: dstFmt) else {
                    diag(["phase": "abort", "reason": "no_converter"]); exit(4)
                }
                inputNode.installTap(onBus: 0, bufferSize: 4096, format: inFmt) { inBuf, _ in
                    convertAndYield(inBuf, conv: conv, dstFmt: dstFmt, cont: cont, timeBox: timeBox)
                }
                eng.prepare()
                try eng.start()
                engine = eng
                emit(["type": "ready", "locale": args.locale, "channel": args.channel,
                      "dst_hz": dstFmt.sampleRate, "source": "mic", "in_hz": inFmt.sampleRate])
            }

            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                DispatchQueue.global().async { stop.wait(); c.resume() }
            }
            _ = sigint
            _ = sigterm

            if let eng = engine {
                eng.stop()
                eng.inputNode.removeTap(onBus: 0)
            }
            gRecorder?.close()   // 録音を完結させてから後始末(flush の打ち切り exit より前)
            cont.finish()
            let flush = Task { try? await analyzer.finalizeAndFinishThroughEndOfInput(); _ = await consumer.value }
            Task { try? await Task.sleep(nanoseconds: 1_500_000_000); emit(["type": "bye", "note": "flush_timeout"]); exit(0) }
            _ = await flush.value
            emit(["type": "bye"])
            exit(0)
        } catch {
            diag(["phase": "error", "error": "\(error)"])
            exit(1)
        }
    }
}

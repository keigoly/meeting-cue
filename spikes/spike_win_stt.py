"""Windows の日本語 STT 候補を同じ条件で流して記録する(Step 1・計測専用。結果は spikes/WINDOWS_STT.md)。

本体の依存ではない: 計測用の仮想環境にだけ sherpa-onnx / faster-whisper / nvidia-cublas-cu12 /
nvidia-cudnn-cu12 / psutil / numpy を入れて動かす(手順は WINDOWS_STT.md §再現)。meetcue からは import しない。

WAV(16 kHz・モノラル・16 bit)を 0.1 s ずつ実時間(--pace 倍)で供給し、各エンジンの結果を
ヘルパーの契約(docs/REQUIREMENTS.md FR-2)と同じ形のイベントで JSONL に書く:
  {"type":"ready"|"partial"|"final"|"bye","channel","text","start_s","end_s","t_ms","fed_s"}(+ 計測用に "decode_ms")
診断は "phase" の行(load / res_base / res / feed_start / feed_done)。採点は spike_win_stt_score.py。

エンジン:
  sherpa-stream   sherpa-onnx の真のストリーミング Zipformer(8 言語・日本語を含む)。区切りはモデル自身のエンドポイント
  sherpa-reazon   sherpa-onnx の日本語 Zipformer(ReazonSpeech・非ストリーミング)+ Silero VAD。partial は区間の再認識
  whisper-turbo   faster-whisper large-v3-turbo(CUDA・float16)+ 同じ VAD と再認識
  whisper-kotoba  faster-whisper kotoba-whisper-v2.0(日本語の蒸留版・CUDA・float16)+ 同じ VAD と再認識
  sapi            Windows 内蔵の音声認識(spike_win_stt_sapi.ps1 を子プロセスで起動し、その stdout をそのまま記録)

モデルの置き場: 環境変数 MEETCUE_SPIKE_HOME(既定 ~/.meeting-cue/spike)の models/ と hf/(Hugging Face のキャッシュ)。
使い方: python spikes/spike_win_stt.py --engine sherpa-reazon --wav tests/fixtures/wav/meeting_ja.wav
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("MEETCUE_SPIKE_HOME", Path.home() / ".meeting-cue" / "spike"))
MODELS = HOME / "models"
SR = 16000
CHUNK_S = 0.1


def now_ms() -> int:
    return int(time.time() * 1000)


class Log:
    """契約の形のイベントと診断を 1 本の JSONL に書く(書き込みは直列)。"""

    def __init__(self, path: Path, echo: bool):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "w", encoding="utf-8")
        self.lock = threading.Lock()
        self.echo = echo

    def write(self, ev: dict) -> None:
        ev.setdefault("t_ms", now_ms())
        line = json.dumps(ev, ensure_ascii=False, sort_keys=True)
        with self.lock:
            self.f.write(line + "\n")
            self.f.flush()
            if self.echo:
                print(line, flush=True)

    def emit(self, **fields) -> None:
        self.write(fields)

    def close(self) -> None:
        self.f.close()


def read_wav(path: Path):
    import numpy as np
    with wave.open(str(path)) as w:
        if (w.getnchannels(), w.getframerate(), w.getsampwidth()) != (1, SR, 2):
            raise SystemExit(f"{path}: 16 kHz・モノラル・16 bit の WAV が要る")
        pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


class ResSampler(threading.Thread):
    """0.5 s ごとに対象プロセスの CPU%(1 コア = 100)・RSS と、GPU 全体の使用率・メモリを記録する。"""

    def __init__(self, log: Log, pid: int, phase: str = "res"):
        super().__init__(daemon=True)
        self.log, self.pid, self.phase = log, pid, phase
        self.stop_ev = threading.Event()
        self.smi: subprocess.Popen | None = None

    def run(self) -> None:
        import psutil
        proc = psutil.Process(self.pid)
        procs = {self.pid: proc}
        proc.cpu_percent(None)
        smi = self.smi = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits", "-lms", "500"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            assert smi.stdout
            for line in smi.stdout:
                if self.stop_ev.is_set():
                    break
                try:
                    util, mem = (float(x) for x in line.strip().split(","))
                except ValueError:
                    continue
                cpu = rss = 0.0
                try:   # 子プロセス(SAPI の PowerShell)も含める
                    for c in proc.children(recursive=True):
                        if c.pid not in procs:
                            procs[c.pid] = c
                            c.cpu_percent(None)
                    for p in list(procs.values()):
                        try:
                            cpu += p.cpu_percent(None)
                            rss += p.memory_info().rss
                        except psutil.NoSuchProcess:
                            procs.pop(p.pid, None)
                except psutil.NoSuchProcess:
                    break
                self.log.emit(phase=self.phase, cpu=round(cpu, 1), rss_mb=round(rss / 2**20, 1),
                              gpu_util=util, gpu_mem_mb=mem)
        finally:
            smi.terminate()

    def stop(self) -> None:
        """nvidia-smi も止める(daemon スレッドは終了時に finally を通らないため、ここで止めないと残る)。"""
        self.stop_ev.set()
        if self.smi and self.smi.poll() is None:
            self.smi.terminate()
        self.join(timeout=2)


class Feeder(threading.Thread):
    """チャンク i を、その音声が終わる時刻(t0 + (i+1)·0.1 s / pace)に渡す(実マイクと同じ遅れ方)。末尾に無音を足す。"""

    def __init__(self, samples, q: queue.Queue, log: Log, pace: float, tail_s: float):
        super().__init__(daemon=True)
        import numpy as np
        self.audio_s = len(samples) / SR
        self.total = np.concatenate([samples, np.zeros(int(tail_s * SR), dtype=np.float32)])
        self.q, self.log, self.pace = q, log, pace

    def run(self) -> None:
        n = int(SR * CHUNK_S)
        t0 = time.perf_counter()
        self.log.emit(phase="feed_start", t0_ms=now_ms())
        done_sent = False
        for i, off in enumerate(range(0, len(self.total), n)):
            target = t0 + (i + 1) * CHUNK_S / self.pace
            dt = target - time.perf_counter()
            if dt > 0:
                time.sleep(dt)
            self.q.put(self.total[off:off + n])
            if not done_sent and off + n >= self.audio_s * SR:
                self.log.emit(phase="feed_done", audio_s=round(self.audio_s, 3),
                              wall_ms=int((time.perf_counter() - t0) * 1000))
                done_sent = True
        self.q.put(None)


class Engine:
    name = ""

    def __init__(self, log: Log, channel: str):
        self.log, self.channel = log, channel
        self.fed = 0   # 受け取った標本数(絶対位置)

    def emit(self, typ: str, text: str, start: float, end: float, decode_ms: float | None = None) -> None:
        ev = {"type": typ, "channel": self.channel, "text": text, "start_s": round(start, 2),
              "end_s": round(end, 2), "fed_s": round(self.fed / SR, 2)}
        if decode_ms is not None:
            ev["decode_ms"] = round(decode_ms, 1)
        self.log.write(ev)

    def accept(self, chunk) -> None:
        raise NotImplementedError

    def step(self) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError


class SherpaStream(Engine):
    """真のストリーミング。partial は復号のたびに文字が変わったら出し、final はエンドポイント(末尾の無音)で出す。"""

    name = "sherpa-stream"
    DIR = "sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10"

    def __init__(self, log: Log, channel: str, threads: int, endpoint_s: float):
        super().__init__(log, channel)
        import sherpa_onnx
        d = MODELS / self.DIR
        tag = "epoch-75-avg-11-chunk-16-left-128"
        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(d / "tokens.txt"), encoder=str(d / f"encoder-{tag}.int8.onnx"),
            decoder=str(d / f"decoder-{tag}.onnx"), joiner=str(d / f"joiner-{tag}.int8.onnx"),
            num_threads=threads, sample_rate=SR, feature_dim=80, decoding_method="greedy_search",
            enable_endpoint_detection=True, rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=endpoint_s, rule3_min_utterance_length=20.0, provider="cpu")
        self.stream = self.rec.create_stream()
        self.seg_start = 0.0
        self.last = ""

    def accept(self, chunk) -> None:
        self.stream.accept_waveform(SR, chunk)
        self.fed += len(chunk)

    def _decode(self) -> float:
        t = time.perf_counter()
        while self.rec.is_ready(self.stream):
            self.rec.decode_stream(self.stream)
        return (time.perf_counter() - t) * 1000

    def step(self) -> None:
        ms = self._decode()
        text = self.rec.get_result(self.stream).strip()
        if text and text != self.last:
            self.emit("partial", text, self.seg_start, self.fed / SR, ms)
            self.last = text
        if self.rec.is_endpoint(self.stream):
            if text:
                self.emit("final", text, self.seg_start, self.fed / SR, ms)
            self.rec.reset(self.stream)
            self.seg_start = self.fed / SR
            self.last = ""

    def flush(self) -> None:
        import numpy as np
        self.stream.accept_waveform(SR, np.zeros(int(0.5 * SR), dtype=np.float32))
        self.stream.input_finished()
        ms = self._decode()
        text = self.rec.get_result(self.stream).strip()
        if text:
            self.emit("final", text, self.seg_start, self.fed / SR, ms)


class VadReDecode(Engine):
    """非ストリーミングのモデルを擬似ストリーミングで使う(sherpa-onnx の simulate-streaming 例と同じ形)。

    Silero VAD が話し始めを見つけたら、区間の頭から今までを partial_every 秒ごとに認識し直して partial を出す。
    VAD が区間を閉じたら(無音 min_silence 秒)区間全体を認識して final を出す。max_speech 秒を超えたら VAD が切る。
    """

    def __init__(self, log: Log, channel: str, partial_every: float, min_silence: float, max_speech: float):
        super().__init__(log, channel)
        import numpy as np
        import sherpa_onnx
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(MODELS / "silero_vad.onnx")
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = max_speech
        cfg.sample_rate = SR
        self.vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=120)
        self.win = cfg.silero_vad.window_size
        self.np = np
        self.buf = np.zeros(0, dtype=np.float32)
        self.buf_start = 0   # buf[0] の絶対位置(標本)
        self.offset = 0      # buf のうち VAD に渡し終えた長さ
        self.started = False
        self.next_partial = 0.0
        self.partial_every = partial_every
        self.last = ""

    def decode(self, samples, final: bool) -> str:
        raise NotImplementedError

    def _timed(self, samples, final: bool) -> tuple[str, float]:
        t = time.perf_counter()
        text = self.decode(samples, final).strip()
        return text, (time.perf_counter() - t) * 1000

    def accept(self, chunk) -> None:
        np = self.np
        self.buf = np.concatenate([self.buf, chunk])
        self.fed += len(chunk)
        while self.offset + self.win <= len(self.buf):
            self.vad.accept_waveform(self.buf[self.offset:self.offset + self.win])
            self.offset += self.win
            if not self.started and self.vad.is_speech_detected():
                self.started = True
                self.next_partial = time.perf_counter() + self.partial_every
        if not self.started and len(self.buf) > 10 * self.win:   # 話し始め前は 0.32 s だけ残す
            drop = len(self.buf) - 10 * self.win
            self.buf = self.buf[drop:]
            self.offset -= drop
            self.buf_start += drop

    def _pop_finals(self) -> None:
        while not self.vad.empty():
            seg = self.vad.front
            samples = self.np.array(seg.samples, dtype=self.np.float32)
            start = seg.start
            self.vad.pop()
            text, ms = self._timed(samples, final=True)
            if text:
                self.emit("final", text, start / SR, (start + len(samples)) / SR, ms)
            # VAD に渡し終えた分を捨て、渡していない端数だけ残す
            self.buf = self.buf[self.offset:]
            self.buf_start += self.offset
            self.offset = 0
            self.started = False
            self.last = ""

    def step(self) -> None:
        self._pop_finals()
        if self.started and time.perf_counter() >= self.next_partial:
            text, ms = self._timed(self.buf[:self.offset], final=False)
            if text and text != self.last:
                self.emit("partial", text, self.buf_start / SR, (self.buf_start + self.offset) / SR, ms)
                self.last = text
            self.next_partial = time.perf_counter() + self.partial_every

    def flush(self) -> None:
        self.vad.flush()
        self._pop_finals()


class SherpaReazon(VadReDecode):
    name = "sherpa-reazon"
    DIR = "sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01"

    def __init__(self, log, channel, threads, int8, pad_s, **kw):
        super().__init__(log, channel, **kw)
        import sherpa_onnx
        d = MODELS / self.DIR
        q = ".int8" if int8 else ""
        # ReazonSpeech 公式の transcribe() は前後に 0.9 s の無音を足して認識する。足さないと区間の頭の句を落としやすい
        self.pad = self.np.zeros(int(pad_s * SR), dtype=self.np.float32)
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / f"encoder-epoch-99-avg-1{q}.onnx"), decoder=str(d / f"decoder-epoch-99-avg-1{q}.onnx"),
            joiner=str(d / f"joiner-epoch-99-avg-1{q}.onnx"), tokens=str(d / "tokens.txt"),
            num_threads=threads, sample_rate=SR, feature_dim=80, decoding_method="greedy_search", provider="cpu")

    def decode(self, samples, final: bool) -> str:
        s = self.rec.create_stream()
        s.accept_waveform(SR, self.np.concatenate([self.pad, samples, self.pad]) if len(self.pad) else samples)
        self.rec.decode_stream(s)
        return s.result.text


def _add_cuda_dlls() -> None:
    """pip の nvidia-cublas-cu12 / nvidia-cudnn-cu12 の DLL を CTranslate2 から見えるようにする(Windows)。"""
    import importlib.util
    for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        spec = importlib.util.find_spec(pkg)
        if not spec or not spec.submodule_search_locations:
            continue
        b = Path(list(spec.submodule_search_locations)[0]) / "bin"
        if b.is_dir():
            os.add_dll_directory(str(b))
            os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


class FasterWhisper(VadReDecode):
    REPOS = {"whisper-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
             "whisper-kotoba": "kotoba-tech/kotoba-whisper-v2.0-faster"}

    def __init__(self, log, channel, name, device, compute_type, beam_final, **kw):
        super().__init__(log, channel, **kw)
        self.name = name
        os.environ.setdefault("HF_HOME", str(HOME / "hf"))
        os.environ["HF_HUB_OFFLINE"] = "1"   # 計測中はネットへ出ない(モデルは事前に取得済み)
        _add_cuda_dlls()
        from faster_whisper import WhisperModel
        from huggingface_hub import snapshot_download
        path = snapshot_download(self.REPOS[name])
        self.model = WhisperModel(path, device=device, compute_type=compute_type)
        self.beam_final = beam_final

    def decode(self, samples, final: bool) -> str:
        segs, _ = self.model.transcribe(
            samples, language="ja", task="transcribe", beam_size=self.beam_final if final else 1,
            temperature=0.0, vad_filter=False, condition_on_previous_text=False, without_timestamps=True)
        return "".join(s.text for s in segs)


def run_sapi(args, log: Log) -> None:
    ps1 = REPO / "spikes" / "spike_win_stt_sapi.ps1"
    proc = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Wav", str(args.wav),
         "-Pace", str(args.pace), "-Channel", args.channel, "-TailS", str(args.tail_s)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    res = ResSampler(log, os.getpid())
    res.start()
    assert proc.stdout
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            log.write(json.loads(line))
        except json.JSONDecodeError:
            log.emit(phase="raw", text=line)
    proc.wait()
    res.stop()
    err = proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
    if err:
        log.emit(phase="stderr", text=err[:2000])


def build(args, log: Log) -> Engine:
    kw = dict(partial_every=args.partial_every, min_silence=args.min_silence, max_speech=args.max_speech)
    if args.engine == "sherpa-stream":
        return SherpaStream(log, args.channel, args.threads, args.min_silence)
    if args.engine == "sherpa-reazon":
        return SherpaReazon(log, args.channel, args.threads, not args.fp32, args.pad_s, **kw)
    return FasterWhisper(log, args.channel, args.engine, args.device, args.compute_type, args.beam, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--engine", required=True,
                    choices=["sherpa-stream", "sherpa-reazon", "whisper-turbo", "whisper-kotoba", "sapi"])
    ap.add_argument("--wav", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="既定: spikes/logs/win_stt_<engine>_<wav>_<時刻>.jsonl")
    ap.add_argument("--channel", default="system")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--tail-s", type=float, default=1.5, help="末尾に足す無音(区切りを出させる)")
    ap.add_argument("--partial-every", type=float, default=0.3, help="擬似ストリーミングの再認識の間隔 s")
    ap.add_argument("--min-silence", type=float, default=0.5, help="区間を閉じる無音 s(VAD / sherpa-stream の rule2)")
    ap.add_argument("--max-speech", type=float, default=20.0, help="VAD が区間を強制で切る長さ s")
    ap.add_argument("--threads", type=int, default=4, help="sherpa-onnx の CPU スレッド数")
    ap.add_argument("--fp32", action="store_true", help="sherpa-reazon を int8 でなく fp32 で")
    ap.add_argument("--pad-s", type=float, default=0.0, help="sherpa-reazon の認識の前後に足す無音 s(公式は 0.9)")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--beam", type=int, default=5, help="Whisper の final の beam(partial は常に 1)")
    ap.add_argument("--tag", default="", help="ログ名に足す印")
    ap.add_argument("--echo", action="store_true")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = f"_{args.tag}" if args.tag else ""
    out = args.out or REPO / "spikes" / "logs" / f"win_stt_{args.engine}{tag}_{args.wav.stem}_{stamp}.jsonl"
    log = Log(out, args.echo)
    log.emit(phase="run", engine=args.engine, wav=args.wav.name, pace=args.pace,
             params={k: str(v) for k, v in vars(args).items() if k not in ("wav", "out", "echo")})

    base = ResSampler(log, os.getpid(), phase="res_base")   # 読み込み前の GPU・CPU(他のアプリの分を引くため)
    base.start()
    time.sleep(3.0)
    base.stop()

    if args.engine == "sapi":
        run_sapi(args, log)
        log.close()
        print(out)
        return 0

    t = time.perf_counter()
    eng = build(args, log)
    log.emit(phase="load", ms=int((time.perf_counter() - t) * 1000))
    samples = read_wav(args.wav)
    if isinstance(eng, VadReDecode):   # 最初の認識で重い初期化(CUDA のカーネル読み込み等)が走るのを計測の外に出す
        t = time.perf_counter()
        eng.decode(samples[:SR], final=True)
        log.emit(phase="warmup", ms=int((time.perf_counter() - t) * 1000))

    q: queue.Queue = queue.Queue()
    feeder = Feeder(samples, q, log, args.pace, args.tail_s)
    res = ResSampler(log, os.getpid())
    log.emit(type="ready", locale="ja-JP", channel=args.channel, dst_hz=SR, source=f"file:{args.wav.name}",
             pace=args.pace, engine=args.engine)
    res.start()
    feeder.start()
    end = False
    while not end:
        items = [q.get()]
        while True:
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        for c in items:
            if c is None:
                end = True
                break
            eng.accept(c)
        eng.step()
    eng.flush()
    log.emit(type="bye")
    res.stop()
    log.close()
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""stt_helper(Windows)— faster-whisper large-v3-turbo(CUDA)+ Silero VAD の日本語ストリーミング STT。

macOS の stt_helper(helpers/macos/stt_helper/main.swift)と同じ契約(docs/REQUIREMENTS.md FR-2):
  stdout(1 行 1 JSON・純粋なイベント列):
    {"type":"ready","locale":"ja-JP","channel":"mic","dst_hz":16000,...}
    {"type":"partial","text":"…","start_s":0.0,"end_s":1.2,"fed_s":…,"t_ms":…}
    {"type":"final","text":"…","start_s":0.0,"end_s":1.2,"fed_s":…,"t_ms":…}
    {"type":"bye"}
  stderr: 診断(JSON)。0.1 s ごとの音量 {"phase":"level","db":…,"peak_db":…} もここに流す。
  stdin(1 行 1 コマンド): "finalize"(今の区間をその場で確定)/ "quit"。EOF では止めない。

方式(spikes/WINDOWS_STT.md・REQUIREMENTS FR-12): faster-whisper に同梱の Silero VAD を 32 ms ごとに回し、声の区間の頭から
今までを partial_every 秒ごとに認識し直して partial を出す(文字が変わったときだけ)。VAD が区間を閉じたら(無音 min_silence 秒)
区間全体を beam 5 で認識して final。区間は max_speech 秒で切る。start_s は区間の始まり(話し始め)。

引数: [--locale ja-JP] [--channel mic] [--file <audio> | --loopback all | (なし = マイク)] [--pace 1.0] [--record <out.m4a>]
      [--loopback-device <名前の一部>] [--mic-device <名前の一部>] [--model <dir>] [--device cuda] [--compute-type float16]
      [--partial-every 0.3] [--min-silence 0.3] [--max-speech 20] [--list-devices]
  取り込み(REQUIREMENTS §7): PyAudioWPatch で WASAPI から。--loopback は出力機器のループバック(再生中の音をまとめて・
  Mac の tap-all と同じ範囲。会議アプリだけを選ぶことはできない)、無ければマイク。機器は既定か、名前の一部で選ぶ。
  --file はテスト用。音声ファイル(WAV / AIFF / m4a など)を 16 kHz モノラルにして --pace 倍速(0 = 待たない)で供給し、
  終わったら自動で終わる。
  --setup-model はセットアップ専用(モデルを Hugging Face から取る。会議中には使わない)。--self-test は読み込みの確認。
専用の仮想環境(packaging/windows/setup.ps1 が作る ~/.meeting-cue/stt-venv)の python で動かす。本体(meetcue)は import しない。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

SR = 16000
CHUNK_S = 0.1
WIN = 512   # Silero VAD の窓(32 ms)
CTX = 64    # 窓の前に付ける直前の標本(faster-whisper の一括処理と同じ)
APP_DIR = Path(os.environ.get("MEETCUE_HOME") or (Path.home() / ".meeting-cue"))
MODEL_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
MODEL_REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"   # 2026-09-27 に計測した版(spikes/WINDOWS_STT.md)
DEFAULT_MODEL = APP_DIR / "models" / "faster-whisper-large-v3-turbo"

_out_lock = threading.Lock()


def now_ms() -> int:
    return int(time.time() * 1000)


def emit(fields: dict) -> None:
    line = json.dumps(dict(fields, t_ms=now_ms()), ensure_ascii=False, sort_keys=True)
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def diag(fields: dict) -> None:
    sys.stderr.write(json.dumps(dict(fields, t_ms=now_ms()), ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


def add_cuda_dlls() -> None:
    """pip の nvidia-cublas-cu12 / nvidia-cudnn-cu12 の DLL を CTranslate2 から見えるようにする。"""
    import importlib.util
    for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        try:
            spec = importlib.util.find_spec(pkg)
        except ModuleNotFoundError:
            spec = None
        if not spec or not spec.submodule_search_locations:
            continue
        b = Path(list(spec.submodule_search_locations)[0]) / "bin"
        if b.is_dir():
            os.add_dll_directory(str(b))
            os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


def load_audio(path: str):
    """音声ファイルを 16 kHz・モノラル・float32 にする(PyAV = faster-whisper の依存)。"""
    import av
    import numpy as np
    parts = []
    with av.open(path) as c:
        r = av.AudioResampler(format="s16", layout="mono", rate=SR)
        for frame in c.decode(audio=0):
            parts += [f.to_ndarray().reshape(-1) for f in r.resample(frame)]
        parts += [f.to_ndarray().reshape(-1) for f in r.resample(None)]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32) / 32768.0


class LevelMeter:
    """0.1 s ごとの RMS / ピーク(dBFS)を stderr の診断で流す(Mac の stt_helper と同じ形)。"""

    def __init__(self):
        import numpy as np
        self.np = np
        self.buf = np.zeros(0, dtype=np.float32)

    def add(self, x) -> None:
        np = self.np
        self.buf = np.concatenate([self.buf, x])
        n = SR // 10
        while len(self.buf) >= n:
            w, self.buf = self.buf[:n], self.buf[n:]
            rms, peak = float(np.sqrt(np.mean(w * w))), float(np.max(np.abs(w)))
            diag({"phase": "level", "db": self._db(rms), "peak_db": self._db(peak)})

    def _db(self, v: float) -> float:
        import math
        return max(-90.0, round(20 * math.log10(v), 1)) if v > 0 else -90.0


class Recorder(threading.Thread):
    """認識に渡すのと同じ音声(16 kHz モノラル)を AAC の m4a に保存する(Mac の --record と同じ)。

    書き込みは別スレッドで行い、認識を止めない。最初の書き込みで record_start(t0_ms = その音声の頭の壁時計)を出す。
    """

    def __init__(self, path: str):
        super().__init__(daemon=True)
        self.path = Path(path)
        self.q: queue.Queue = queue.Queue()

    def write(self, x, head_ms: int) -> None:
        self.q.put((x, head_ms))

    def run(self) -> None:
        import av
        import numpy as np
        container = stream = None
        frames = 0
        failed = False
        while True:
            item = self.q.get()
            if item is None:
                break
            if failed:
                continue
            x, head_ms = item
            try:
                if container is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    container = av.open(str(self.path), "w", format="mp4")
                    stream = container.add_stream("aac", rate=SR)
                    stream.layout = "mono"
                    stream.bit_rate = 32000
                    diag({"phase": "record_start", "path": str(self.path), "hz": SR, "t0_ms": head_ms})
                pcm = np.ascontiguousarray((np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)[None, :])
                frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
                frame.sample_rate = SR
                frame.pts = frames
                frames += pcm.shape[1]
                for p in stream.encode(frame):
                    container.mux(p)
            except Exception as e:   # 録音の失敗で文字起こしは止めない
                failed = True
                diag({"phase": "record_error", "error": f"{type(e).__name__}: {e}"})
        if container is not None:
            try:
                for p in stream.encode(None):
                    container.mux(p)
                container.close()
                diag({"phase": "record_done", "path": str(self.path), "frames": frames})
            except Exception as e:
                diag({"phase": "record_error", "error": f"{type(e).__name__}: {e}"})

    def close(self) -> None:
        self.q.put(None)
        self.join(timeout=10)


class FileFeeder(threading.Thread):
    """チャンク i を、その音声が終わる時刻(t0 + (i+1)·0.1 s / pace)に渡す(実マイクと同じ遅れ方)。終わりに None。"""

    def __init__(self, samples, out: queue.Queue, pace: float):
        super().__init__(daemon=True)
        self.samples, self.out, self.pace = samples, out, pace

    def run(self) -> None:
        n = int(SR * CHUNK_S)
        t0 = time.perf_counter()
        t0_ms = now_ms()
        diag({"phase": "feed_start", "t0_ms": t0_ms})
        for i, off in enumerate(range(0, len(self.samples), n)):
            if self.pace > 0:
                dt = t0 + (i + 1) * CHUNK_S / self.pace - time.perf_counter()
                if dt > 0:
                    time.sleep(dt)
            chunk = self.samples[off:off + n]
            self.out.put((chunk, now_ms() - int(len(chunk) / SR * 1000)))
        diag({"phase": "feed_done", "audio_s": round(len(self.samples) / SR, 2),
              "wall_ms": int((time.perf_counter() - t0) * 1000)})
        self.out.put(None)


class WasapiCapture(threading.Thread):
    """PyAudioWPatch で WASAPI から取り込み、16 kHz モノラル float32 にして渡す(REQUIREMENTS §7)。

    loopback=True は出力機器のループバック(再生中の音をまとめて取る。Mac の tap-all と同じ範囲)。機器は名前の一部で選べる
    (無ければ既定の出力 / 既定のマイク)。機器の元の形式(44.1 / 48 kHz・2〜8 ch)で取り、平均して 1 ch にしてから PyAV で 16 kHz へ。
    何も鳴っていない間、WASAPI のループバックはデータを渡さないので、壁時計に合わせて無音を足す(足さないと VAD が
    無音を見られず区間が閉じない)。足した長さは capture_done の filled_s に出す。
    """

    FILL_AFTER_S = 0.15   # これだけデータが来なければ、遅れた分を無音で埋める

    def __init__(self, out: queue.Queue, *, loopback: bool, device: str | None):
        super().__init__(daemon=True)
        self.out, self.loopback, self.device = out, loopback, device
        self.raw: queue.Queue = queue.Queue()
        self.stop_ev = threading.Event()
        self.filled = 0
        self.info: dict = {}

    def open(self) -> None:
        import pyaudiowpatch as pa
        self.pa = pa
        self.p = pa.PyAudio()
        dev = self._pick()
        self.rate, self.ch = int(dev["defaultSampleRate"]), int(dev["maxInputChannels"])
        self.stream = self.p.open(format=pa.paInt16, channels=self.ch, rate=self.rate, input=True,
                                  input_device_index=dev["index"], frames_per_buffer=self.rate // 50,
                                  stream_callback=self._callback)
        self.info = {"device": dev["name"], "in_hz": self.rate, "in_ch": self.ch, "loopback": self.loopback}
        diag({"phase": "capture_start", **self.info})

    def _pick(self) -> dict:
        p, pa = self.p, self.pa
        api = p.get_host_api_info_by_type(pa.paWASAPI)
        if self.device:
            for i in range(api["deviceCount"]):
                d = p.get_device_info_by_host_api_device_index(api["index"], i)
                if (self.device.lower() in d["name"].lower() and d["maxInputChannels"] > 0
                        and bool(d.get("isLoopbackDevice")) == self.loopback):
                    return d
            raise RuntimeError(f"機器が見つからない: {self.device}(--list-devices で一覧)")
        if self.loopback:
            return p.get_default_wasapi_loopback()
        return p.get_device_info_by_index(api["defaultInputDevice"])

    def _callback(self, data, frames, time_info, status):
        self.raw.put(data)
        return (None, self.pa.paContinue)

    def run(self) -> None:
        import av
        import numpy as np
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
        t0 = None
        pushed = 0
        last_data = time.perf_counter()
        while not self.stop_ev.is_set():
            try:
                data = self.raw.get(timeout=0.05)
            except queue.Empty:
                if t0 is not None and time.perf_counter() - last_data > self.FILL_AFTER_S:
                    lag = int((time.perf_counter() - t0) * SR) - pushed - int(0.05 * SR)
                    if lag > 0:
                        self.out.put((np.zeros(lag, dtype=np.float32), now_ms() - int(lag / SR * 1000)))
                        pushed += lag
                        self.filled += lag
                continue
            pcm = np.frombuffer(data, dtype=np.int16).reshape(-1, self.ch).mean(axis=1).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(pcm[None, :], format="s16", layout="mono")
            frame.sample_rate = self.rate
            x = np.concatenate([f.to_ndarray().reshape(-1) for f in resampler.resample(frame)] or
                               [np.zeros(0, dtype=np.int16)]).astype(np.float32) / 32768.0
            if t0 is None:
                t0 = time.perf_counter() - len(pcm) / self.rate
            last_data = time.perf_counter()
            if len(x):
                self.out.put((x, now_ms() - int(len(x) / SR * 1000)))
                pushed += len(x)

    def close(self) -> None:
        self.stop_ev.set()
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.p.terminate()
        self.join(timeout=2)
        diag({"phase": "capture_done", "filled_s": round(self.filled / SR, 2), **self.info})


def list_devices() -> int:
    """WASAPI の機器の一覧(1 行 1 JSON)。--loopback-device / --mic-device に名前の一部を渡す。"""
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        api = p.get_host_api_info_by_type(pa.paWASAPI)
        for i in range(api["deviceCount"]):
            d = p.get_device_info_by_host_api_device_index(api["index"], i)
            kind = "loopback" if d.get("isLoopbackDevice") else ("mic" if d["maxInputChannels"] else "speaker")
            print(json.dumps({"kind": kind, "name": d["name"], "hz": int(d["defaultSampleRate"]),
                              "default": d["index"] in (api["defaultInputDevice"], api["defaultOutputDevice"])},
                             ensure_ascii=False), flush=True)
    finally:
        p.terminate()
    return 0


class StreamVAD:
    """faster-whisper 同梱の Silero(v6)を 1 窓ずつ呼ぶ。状態(h, c)と直前 64 標本を持ち回す(一括処理と同じ値・0.12 ms/窓)。"""

    def __init__(self):
        import numpy as np
        from faster_whisper.vad import get_vad_model
        self.np = np
        self.session = get_vad_model().session
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.ctx = np.zeros(CTX, dtype=np.float32)

    def prob(self, win) -> float:
        inp = self.np.concatenate([self.ctx, win])[None, :].astype(self.np.float32)
        out, self.h, self.c = self.session.run(None, {"input": inp, "h": self.h, "c": self.c})
        self.ctx = win[-CTX:]
        return float(self.np.asarray(out).reshape(-1)[0])


class Streamer:
    """VAD で声の区間を見つけ、partial(再認識)と final(区間全体)を契約の形で出す。"""

    PREROLL_S = 0.2   # 区間の頭の前に付けて認識する音(話し始めの子音を切らない)
    PAD_S = 0.1       # 区間の終わりの後ろに付ける音
    MIN_SPEECH_S = 0.25
    # finalize を受けたときにまだ声が続いていたら、次の短い切れ目(GAP_S の無音 = 語と語の間)まで閉じるのを待つ(最長 DEFER_MAX_S)。
    # Whisper は語の終わりを先回りして書くので、話している最中でも partial の文字が止まり、セグメンターが間と取り違えて
    # finalize を送る(2026-09-27 合成会議: 「…よくわかりま」の途中で切れ、残りから「わかりました。」が重複した)。
    # 上限は、セグメンターが final を待つ safety_ms(最短 2.5 s)から認識 1 回と処理の間隔(計 約 0.6 s)を引いて余裕を見た値
    GAP_S = 0.10
    DEFER_MAX_S = 1.5

    def __init__(self, model, lang: str, channel: str, *, partial_every: float, min_silence: float,
                 max_speech: float, threshold: float = 0.5):
        import numpy as np
        self.np = np
        self.model, self.lang, self.channel = model, lang, channel
        self.vad = StreamVAD()
        self.partial_every, self.threshold = partial_every, threshold
        self.min_silence = int(min_silence * SR)
        self.max_speech = int(max_speech * SR)
        self.audio = np.zeros(0, dtype=np.float32)
        self.base = 0          # self.audio[0] の絶対位置(標本)
        self.vad_pos = 0       # VAD に渡し終えた絶対位置
        self.fed = 0           # 受け取った標本数
        self.in_speech = False
        self.seg_start = 0     # 区間の始まり(絶対位置)
        self.last_voice = 0    # 最後に声だった窓の終わり(絶対位置)
        self.next_partial = 0.0
        self.last_text = ""
        self.pending_finalize: float | None = None   # 待っている finalize の受付時刻(perf_counter)
        self.counts = {"partials": 0, "finals": 0}

    # ---- 音声と区間 ---------------------------------------------------------------
    def accept(self, x) -> list[tuple]:
        """音声を受け取り VAD を進める。閉じた区間 (start, end, 音声) を返す(認識は呼び出し側で)。"""
        self.audio = self.np.concatenate([self.audio, x])
        self.fed += len(x)
        closed = []
        while self.vad_pos + WIN <= self.fed:
            i = self.vad_pos - self.base
            p = self.vad.prob(self.audio[i:i + WIN])
            w_end = self.vad_pos + WIN
            if not self.in_speech:
                if p >= self.threshold:
                    self.in_speech = True
                    self.seg_start, self.last_voice = self.vad_pos, w_end
                    self.next_partial = time.perf_counter() + self.partial_every
            else:
                if p >= self.threshold - 0.15:   # 声の続き(Silero の標準のヒステリシス)
                    self.last_voice = w_end
                if self.pending_finalize is not None and w_end - self.last_voice >= self.GAP_S * SR:
                    self._finalize_done("gap")
                    closed.append(self._close(self.last_voice))
                elif w_end - self.last_voice >= self.min_silence:
                    closed.append(self._close(self.last_voice))
                elif w_end - self.seg_start >= self.max_speech:
                    closed.append(self._close(w_end))
            self.vad_pos = w_end
        if not self.in_speech:   # 話していない間は、次の区間の頭に付ける分だけ残す
            keep_from = max(self.base, self.vad_pos - int(self.PREROLL_S * SR))
            self.audio = self.audio[keep_from - self.base:]
            self.base = keep_from
        return [c for c in closed if c is not None]

    def request_finalize(self) -> tuple | None:
        """stdin の finalize。声が止んでいればその場で閉じ、続いていれば次の短い切れ目まで待つ(DEFER_MAX_S まで)。"""
        if not self.in_speech:
            return None
        if self.vad_pos - self.last_voice >= self.GAP_S * SR:
            diag({"phase": "finalize", "how": "now", "wait_ms": 0})
            return self._close(self.last_voice)
        if self.pending_finalize is None:
            self.pending_finalize = time.perf_counter()
        return None

    def finalize_deadline(self) -> tuple | None:
        """待っている finalize が DEFER_MAX_S を過ぎたら、声が続いていてもその場で閉じる。"""
        if self.pending_finalize is None or time.perf_counter() - self.pending_finalize < self.DEFER_MAX_S:
            return None
        self._finalize_done("deadline")
        return self.force_close()

    def _finalize_done(self, how: str) -> None:
        diag({"phase": "finalize", "how": how, "wait_ms": round((time.perf_counter() - self.pending_finalize) * 1000)})
        self.pending_finalize = None

    def _close(self, end: int) -> tuple | None:
        """区間を閉じ、その音声をこの時点で写し取る(accept の終わりで古い音声を切り詰めるため)。"""
        start, spoke = self.seg_start, bool(self.last_text)
        self.in_speech = False
        self.pending_finalize = None   # VAD の間で閉じたときも、待っていた finalize はこれで済む
        self.last_text = ""
        if end - start < self.MIN_SPEECH_S * SR and not spoke:   # partial も出していない短い音は捨てる
            return None
        return start, end, self._slice(start, end).copy()

    def force_close(self) -> tuple | None:
        """finalize / 終わり: 今の区間をその場で閉じる(VAD の状態はそのまま)。"""
        if not self.in_speech:
            return None
        return self._close(self.fed)

    def _slice(self, start: int, end: int):
        a = max(self.base, start - int(self.PREROLL_S * SR))
        b = min(self.fed, end + int(self.PAD_S * SR))
        return self.audio[a - self.base:b - self.base]

    # ---- 認識 ---------------------------------------------------------------------
    def recognize(self, samples, final: bool) -> tuple[str, float]:
        t = time.perf_counter()
        segs, _ = self.model.transcribe(
            samples, language=self.lang, task="transcribe", beam_size=5 if final else 1,
            temperature=(0.0, 0.2, 0.4) if final else 0.0, vad_filter=False,
            condition_on_previous_text=False, without_timestamps=True)
        text = "".join(s.text for s in segs).strip()
        return text, (time.perf_counter() - t) * 1000

    def final(self, seg: tuple) -> None:
        start, end, samples = seg
        text, ms = self.recognize(samples, final=True)
        if text:
            self.counts["finals"] += 1
            emit({"type": "final", "channel": self.channel, "text": text, "start_s": round(start / SR, 2),
                  "end_s": round(end / SR, 2), "fed_s": round(self.fed / SR, 2), "decode_ms": round(ms)})

    def maybe_partial(self) -> None:
        if not self.in_speech or time.perf_counter() < self.next_partial:
            return
        text, ms = self.recognize(self._slice(self.seg_start, self.vad_pos), final=False)
        if text and text != self.last_text:
            self.last_text = text
            self.counts["partials"] += 1
            emit({"type": "partial", "channel": self.channel, "text": text, "start_s": round(self.seg_start / SR, 2),
                  "end_s": round(self.vad_pos / SR, 2), "fed_s": round(self.fed / SR, 2), "decode_ms": round(ms)})
        self.next_partial = time.perf_counter() + self.partial_every


def read_commands(cq: queue.Queue) -> None:
    for line in sys.stdin:
        cmd = line.strip()
        if cmd in ("finalize", "quit"):
            cq.put(cmd)
            if cmd == "quit":
                return
    # stdin の EOF では止めない(起動側が閉じることがある)。停止は quit / Ctrl+C


def load_model(args):
    add_cuda_dlls()
    os.environ["HF_HUB_OFFLINE"] = "1"   # 会議中はネットへ出ない(モデルはセットアップで取得済み)
    from faster_whisper import WhisperModel
    import numpy as np
    model_dir = Path(args.model)
    if not (model_dir / "model.bin").exists():
        diag({"phase": "abort", "reason": "model_missing", "model": str(model_dir),
              "hint": "packaging\\windows\\setup.ps1 を実行してモデルを取得する"})
        sys.exit(3)
    t = time.perf_counter()
    try:
        model = WhisperModel(str(model_dir), device=args.device, compute_type=args.compute_type)
        segs, _ = model.transcribe(np.zeros(SR, dtype=np.float32), language="ja", beam_size=1,
                                   without_timestamps=True, vad_filter=False)
        list(segs)   # 最初の認識で CUDA の初期化が走るのを ready の前に済ませる
    except Exception as e:
        diag({"phase": "abort", "reason": "model_load_failed", "device": args.device,
              "error": f"{type(e).__name__}: {e}"[:500]})
        sys.exit(2)
    diag({"phase": "asset_ready", "ms": int((time.perf_counter() - t) * 1000), "model": model_dir.name,
          "device": args.device, "compute_type": args.compute_type})
    return model


def setup_model(dest: Path) -> int:
    """セットアップ専用: モデルを固定した版で取る(会議中の経路では呼ばない)。"""
    from huggingface_hub import snapshot_download
    if (dest / "model.bin").exists():
        print(json.dumps({"phase": "setup_model", "ok": True, "skipped": True, "path": str(dest)}), flush=True)
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    snapshot_download(repo_id=MODEL_REPO, revision=MODEL_REVISION, local_dir=str(dest))
    ok = (dest / "model.bin").exists()
    print(json.dumps({"phase": "setup_model", "ok": ok, "path": str(dest),
                      "s": round(time.perf_counter() - t, 1)}), flush=True)
    return 0 if ok else 1


def main() -> int:
    for s in (sys.stdout, sys.stderr, sys.stdin):   # 本体は UTF-8 で読む(Windows の既定の cp932 にしない)
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Meeting Cue! の Windows 用 STT ヘルパー(契約は FR-2)")
    ap.add_argument("--locale", default="ja-JP")
    ap.add_argument("--channel", default="mic")
    ap.add_argument("--file")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--record")
    ap.add_argument("--loopback", help="再生中の音を取る(all / name:X / pid:N。Windows は機器単位なので、どれでも出力機器の全体)")
    ap.add_argument("--exclude-pid", help="Mac の tap と同じ引数(Windows の機器単位のループバックでは使わない)")
    ap.add_argument("--loopback-device", help="ループバックする出力機器の名前の一部(既定: 既定の出力)")
    ap.add_argument("--mic-device", help="マイクの名前の一部(既定: 既定の入力)")
    ap.add_argument("--list-devices", action="store_true", help="WASAPI の機器の一覧を出して終わる")
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--partial-every", type=float, default=0.3)
    ap.add_argument("--min-silence", type=float, default=0.3)
    ap.add_argument("--max-speech", type=float, default=20.0)
    ap.add_argument("--setup-model", action="store_true", help="セットアップ専用: モデルを取得して終わる")
    ap.add_argument("--self-test", action="store_true", help="モデルを GPU に読み込めるかだけ確かめて終わる")
    args = ap.parse_args()

    if args.setup_model:
        return setup_model(Path(args.model))
    try:   # 専用の仮想環境の python でないと入っていない(本体の python で動かされたときに理由を返す)
        import av, numpy, pyaudiowpatch, faster_whisper  # noqa: F401,E401
    except ImportError as e:
        diag({"phase": "abort", "reason": "packages_missing", "error": str(e),
              "hint": "packaging\\windows\\setup.ps1 が作る専用の仮想環境の python で動かす"})
        return 7
    if args.list_devices:
        return list_devices()

    model = load_model(args)
    if args.self_test:
        print(json.dumps({"phase": "self_test", "ok": True, "device": args.device}), flush=True)
        return 0
    lang = args.locale.split("-")[0].lower()
    aq: queue.Queue = queue.Queue()
    capture = None
    samples = None
    if args.file:
        try:
            samples = load_audio(args.file)
        except Exception as e:
            diag({"phase": "abort", "reason": "file_open_failed", "error": f"{type(e).__name__}: {e}"[:500]})
            return 4
        source = {"source": f"file:{args.file}", "pace": args.pace}
    else:
        loopback = args.loopback is not None
        if loopback and args.loopback != "all":   # Windows のループバックは機器単位(会議アプリだけは取れない)
            diag({"phase": "loopback_scope", "requested": args.loopback, "used": "device"})
        capture = WasapiCapture(aq, loopback=loopback, device=args.loopback_device if loopback else args.mic_device)
        try:
            capture.open()
        except Exception as e:
            diag({"phase": "abort", "reason": "capture_open_failed", "loopback": loopback,
                  "error": f"{type(e).__name__}: {e}"[:500]})
            return 6
        source = {"source": ("loopback:" if loopback else "mic:") + capture.info["device"], "in_hz": capture.info["in_hz"]}

    streamer = Streamer(model, lang, args.channel, partial_every=args.partial_every,
                        min_silence=args.min_silence, max_speech=args.max_speech)
    meter = LevelMeter()
    recorder = Recorder(args.record) if args.record else None
    if recorder:
        recorder.start()
    cq: queue.Queue = queue.Queue()
    threading.Thread(target=read_commands, args=(cq,), daemon=True).start()
    stop = threading.Event()
    for sig in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: stop.set())

    emit({"type": "ready", "locale": args.locale, "channel": args.channel, "dst_hz": SR, **source,
          "engine": "faster-whisper", "model": Path(args.model).name})
    if capture:
        capture.start()
    else:
        FileFeeder(samples, aq, args.pace).start()

    ended = False
    while not ended:
        try:
            items = [aq.get(timeout=0.05)]
        except queue.Empty:
            items = []
        while True:
            try:
                items.append(aq.get_nowait())
            except queue.Empty:
                break
        closed = []
        for it in items:
            if it is None:
                ended = True
                continue
            x, head_ms = it
            meter.add(x)
            if recorder:
                recorder.write(x, head_ms)
            closed += streamer.accept(x)
        quit_ = stop.is_set()
        finalize = False
        while True:
            try:
                cmd = cq.get_nowait()
            except queue.Empty:
                break
            finalize |= cmd == "finalize"
            quit_ |= cmd == "quit"
        if finalize and not (quit_ or ended):
            closed.append(streamer.request_finalize())
        closed.append(streamer.finalize_deadline())
        if quit_ or ended:   # 終わりはその場で閉じる
            closed.append(streamer.force_close())
        for seg in closed:
            if seg:
                streamer.final(seg)
        if quit_:
            break
        streamer.maybe_partial()

    if capture:
        capture.close()
    diag({"phase": "results_done", **streamer.counts})
    if recorder:
        recorder.close()
    emit({"type": "bye"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

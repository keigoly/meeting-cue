"""修正 A の Step 1(2026-10-16): 録音後に音声 1 本を丸ごと認識し直すと何秒かかり、どんな文になるか(Windows・faster-whisper)。

専用の仮想環境の python で動かす(本体は import しない・ヘルパーの load_audio と同じ読み方をする)。

    %USERPROFILE%\\.meeting-cue\\stt-venv\\Scripts\\python.exe spikes\\spike_redo.py ^
        --audio mic=<記録>\\audio\\mic.m4a --audio system=<記録>\\audio\\system.m4a --out <出力> --variants seq,batch8,seqw

方式(variants):
  seq     VAD で無音を除いてつなぎ、区間ごとの時刻(beam 5・前の文に寄せない)
  batchN  faster-whisper の BatchedInferencePipeline(VAD の区間をまとめて N 本ずつ)
  seqw    seq + 語ごとの時刻。語と語の間が --gap 秒以上なら行を分け、行の時刻を語から取る
  noVADw  VAD なし + 語ごとの時刻(参照の whisper.cpp と同じく音声をそのまま渡す)
出力: <out>/<variant>_<channel>.jsonl(記録の transcript.jsonl と同じ形: channel / text / start_seconds / end_seconds
と、区間の avg_logprob / no_speech_prob / compression_ratio)と、標準出力に 1 行 1 JSON の計測
(load_ms・認識の ms・行数・字数・GPU メモリの最大)。採点は eval/redo_cer.py。

2026-10-18(FR-2b の Step 1 の続き・端末内の候補): `--model <モデルのフォルダ>`(既定は本番の large-v3-turbo)と
`--hotwords <語の一覧.txt>`(1 行 1 語・# は注釈。faster-whisper の hotwords として毎回の窓の頭に渡す。
前の文に寄せない設定では initial_prompt は最初の窓にしか効かないため)。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HELPER_DIR = Path(__file__).resolve().parents[1] / "helpers" / "windows" / "stt_helper"


class GpuPeak(threading.Thread):
    """nvidia-smi で GPU メモリの使用量を 0.5 s ごとに見て最大を取る(全プロセスの合計)。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.peak = 0
        self.stop_ev = threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            self.peak = max(self.peak, gpu_used())
            self.stop_ev.wait(0.5)


def gpu_used() -> int:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=5)
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return 0


def lines_of(seg, gap: float | None) -> list[tuple[float, float, str]]:
    """区間を行にする。語の時刻があれば、語と語の間が gap 秒以上のところで分け、行の時刻を語から取る。"""
    words = list(seg.words or [])
    if not words or gap is None:
        return [(seg.start, seg.end, seg.text.strip())]
    out, cur = [], [words[0]]
    for w in words[1:]:
        if w.start - cur[-1].end >= gap:
            out.append(cur)
            cur = [w]
        else:
            cur.append(w)
    out.append(cur)
    return [(ws[0].start, ws[-1].end, "".join(w.word for w in ws).strip()) for ws in out]


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", action="append", required=True, help="channel=path(複数可)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--variants", default="seq,seqw")
    ap.add_argument("--gap", type=float, default=1.5, help="seqw / noVADw で行を分ける語の間(秒)")
    ap.add_argument("--model", help="モデルのフォルダ(既定: 本番の large-v3-turbo)")
    ap.add_argument("--hotwords", help="語の一覧(1 行 1 語・# は注釈)。faster-whisper の hotwords に渡す")
    args = ap.parse_args()
    sys.path.insert(0, str(HELPER_DIR))
    import stt_helper as h   # CUDA の DLL の場所・音声の読み方・モデルの場所を本番と同じにする
    h.add_cuda_dlls()
    os.environ["HF_HUB_OFFLINE"] = "1"
    import numpy as np
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = gpu_used()
    t = time.perf_counter()
    model_dir = Path(args.model) if args.model else h.DEFAULT_MODEL
    model = WhisperModel(str(model_dir), device="cuda", compute_type="float16")
    list(model.transcribe(np.zeros(h.SR, dtype=np.float32), language="ja", beam_size=1,
                          without_timestamps=True, vad_filter=False)[0])
    terms = []
    if args.hotwords:
        for line in Path(args.hotwords).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                terms.append(line)
    hotwords = "、".join(terms) or None
    print(json.dumps({"phase": "load", "ms": round((time.perf_counter() - t) * 1000), "gpu_base_mib": base,
                      "model": model_dir.name, "hotwords": len(terms)}), flush=True)

    tracks = []
    for spec in args.audio:
        ch, path = spec.split("=", 1)
        t = time.perf_counter()
        samples = h.load_audio(path)
        tracks.append((ch, samples))
        print(json.dumps({"phase": "load_audio", "channel": ch, "audio_s": round(len(samples) / h.SR, 1),
                          "ms": round((time.perf_counter() - t) * 1000)}), flush=True)

    for variant in args.variants.split(","):
        for ch, samples in tracks:
            gp = GpuPeak()
            gp.start()
            t = time.perf_counter()
            common = dict(language="ja", task="transcribe", beam_size=5, condition_on_previous_text=False, hotwords=hotwords)
            gap = None
            if variant == "seq":
                it, info = model.transcribe(samples, vad_filter=True, **common)
            elif variant == "seqw":
                it, info = model.transcribe(samples, vad_filter=True, word_timestamps=True, **common)
                gap = args.gap
            elif variant == "noVADw":
                it, info = model.transcribe(samples, vad_filter=False, word_timestamps=True, **common)
                gap = args.gap
            elif variant.startswith("batch"):
                it, info = BatchedInferencePipeline(model=model).transcribe(
                    samples, batch_size=int(variant[5:] or 8), without_timestamps=False, **common)
            else:
                raise SystemExit(f"知らない方式: {variant}")
            rows = []
            for seg in it:
                stats = {"avg_logprob": round(seg.avg_logprob, 3), "no_speech_prob": round(seg.no_speech_prob, 3),
                         "compression_ratio": round(seg.compression_ratio, 2)}
                for a, b, text in lines_of(seg, gap):
                    if text:
                        rows.append({"channel": ch, "text": text, "start_seconds": round(a, 2), "end_seconds": round(b, 2),
                                     **stats})
            ms = round((time.perf_counter() - t) * 1000)
            gp.stop_ev.set()
            gp.join()
            with (out / f"{variant}_{ch}.jsonl").open("w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            chars = sum(len(r["text"]) for r in rows)
            print(json.dumps({"phase": "redo", "variant": variant, "channel": ch, "model": model_dir.name, "hotwords": len(terms),
                              "audio_s": round(len(samples) / h.SR, 1),
                              "speech_s": round(getattr(info, "duration_after_vad", 0) or 0, 1), "ms": ms,
                              "lines": len(rows), "chars": chars, "gpu_peak_mib": gp.peak}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

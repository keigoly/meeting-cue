"""mix_helper(Windows)— 録音の音声(相手 = system / 自分 = mic)を 1 本の .m4a に重ねる(記録の「書き出す」用)。

Mac の mix-helper(helpers/macos/mix_helper/main.swift)と同じ約束:
  mix_helper.py --out <出力.m4a> --in <入力.m4a>[@<開始のずれ 秒>] [--in …]
各入力を「開始のずれ」だけ後ろにずらして重ね(meta.json の audio.<ch>.t0_ms の差 = 画面の再生と同じ頭合わせ)、
AAC(.m4a・16 kHz モノラル)で書き出す。出力が既にあれば上書きする。重ねて 1.0 を超えるところは切る。
stdout に結果を JSON 1 行: {"ok": true, "ms": 処理 ms, "duration_ms": …, "inputs": n} / {"ok": false, "error": "…"}。終了コード 0 / 1。
STT ヘルパー専用の仮想環境(PyAV・numpy)の python で動かす(本体は import しない)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SR = 16000


def decode(path: str):
    import av
    import numpy as np
    parts = []
    with av.open(path) as c:
        r = av.AudioResampler(format="s16", layout="mono", rate=SR)
        for frame in c.decode(audio=0):
            parts += [f.to_ndarray().reshape(-1) for f in r.resample(frame)]
        parts += [f.to_ndarray().reshape(-1) for f in r.resample(None)]
    return (np.concatenate(parts).astype(np.float32) / 32768.0) if parts else np.zeros(0, dtype=np.float32)


def encode(path: Path, x) -> None:
    import av
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="mp4") as c:
        s = c.add_stream("aac", rate=SR)
        s.layout = "mono"
        s.bit_rate = 32000
        pcm = (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)
        step = SR   # 1 s ずつ渡す
        for i in range(0, len(pcm), step):
            chunk = np.ascontiguousarray(pcm[i:i + step][None, :])
            frame = av.AudioFrame.from_ndarray(chunk, format="s16", layout="mono")
            frame.sample_rate = SR
            frame.pts = i
            for p in s.encode(frame):
                c.mux(p)
        for p in s.encode(None):
            c.mux(p)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="録音の音声を 1 本の m4a に重ねる(Windows)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--in", dest="inputs", action="append", default=[], help="<入力.m4a>[@<開始のずれ 秒>]")
    args = ap.parse_args()
    t0 = time.perf_counter()
    try:
        import numpy as np
        tracks = []
        for spec in args.inputs:
            path, off = spec, 0.0
            if "@" in spec:
                path, _, v = spec.rpartition("@")
                off = max(0.0, float(v))
            tracks.append((int(off * SR), decode(path)))
        if not tracks:
            raise ValueError("入力がありません")
        n = max(o + len(x) for o, x in tracks)
        mix = np.zeros(n, dtype=np.float32)
        for o, x in tracks:
            mix[o:o + len(x)] += x
        encode(Path(args.out), mix)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps({"ok": True, "ms": int((time.perf_counter() - t0) * 1000), "duration_ms": int(n / SR * 1000),
                      "inputs": len(tracks)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Soniox のリアルタイム認識を、録音のファイルで測る(REQUIREMENTS FR-2b の Step 1・2026-10-17)。

    python3 spikes/spike_soniox.py <音声> --out <dir> [--name <名前>] [--pace 1.0] [--terms <file>] [--region us] [--endpoint]

音声は afconvert(macOS)で 16 kHz モノラルの WAV にしてから、--pace 倍の速さ(1.0 = 実時間)で 100 ms ずつ流す。
会議中と同じく、流しながら語が確定していく。最後に finalize を送り、残りを確定させる。
--endpoint は Soniox の話の切れ目の検出(enable_endpoint_detection)を使う。無いと、流し続ける音声では語が確定するまで
5〜6 s かかる(2026-10-17 Jev の解説で実測・途中の文字はすぐ届く)。会議で使う形に近いのは --endpoint。
出力:
  <dir>/<名前>.soniox.jsonl  確定した語ごとに 1 行 {"type":"token", text, start_ms, end_ms, recv_s, lag_s}
                             (lag_s = 届いた時刻 − その語の音を送り終えた時刻。実時間で流したときの「確定の遅れ」)
                             最後に {"type":"done", text, audio_s, connect_ms, finalize_s, lag_p50, lag_p90, languages, error}
  <dir>/<名前>.soniox.txt    確定した文だけ
鍵は meetcue.secrets(キーチェーン local.meetcue/soniox → 環境変数 SONIOX_API_KEY)。鍵は表示も保存もしない。
語彙(--terms)は 1 行 1 語。答えを知っている語を渡すと有利になるので、渡したときは結果にその旨を書く。
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meetcue import secrets, soniox  # noqa: E402

CHUNK = soniox.BYTES_PER_S // 10   # 100 ms


def to_pcm(path: Path) -> bytes:
    """afconvert で 16 kHz モノラル 16 bit にし、WAV の data の塊だけを取り出す。
    afconvert は元の音によって WAVE_FORMAT_EXTENSIBLE(65534)で書き、Python 3.9 の wave は読めないため、塊は自分でたどる。"""
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(path), str(wav)], check=True)
        raw = wav.read_bytes()
    pos = 12
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        if cid == b"data":
            return raw[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    raise ValueError(f"WAV に data の塊が無い: {path}")


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))], 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--terms")
    ap.add_argument("--region", default="us")
    ap.add_argument("--endpoint", action="store_true")
    a = ap.parse_args()
    key, _ = secrets.api_key("soniox")
    if not key:
        print("Soniox の鍵がありません(キーチェーン local.meetcue/soniox か SONIOX_API_KEY)", file=sys.stderr)
        return 1
    terms = [ln.strip() for ln in open(a.terms, encoding="utf-8") if ln.strip() and not ln.startswith("#")] if a.terms else []
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    name = a.name or Path(a.audio).stem
    pcm = to_pcm(Path(a.audio))

    s = soniox.Session(key, terms=terms, region=a.region, endpoint_detection=a.endpoint)
    if not s.start():
        print(json.dumps({"type": "done", "error": s.error}, ensure_ascii=False))
        return 1
    t0 = time.monotonic()
    for i in range(0, len(pcm), CHUNK):
        if not s.send_audio(pcm[i:i + CHUNK]):
            break
        if a.pace > 0:
            time.sleep(max(0.0, t0 + (i + CHUNK) / soniox.BYTES_PER_S / a.pace - time.monotonic()))
    s.finalize()
    s.wait_fin(30.0)
    s.close(10.0)

    # 語の音を送り終えた時刻(流し始めからの秒)= end_ms / pace。届いた時刻 recv_s は接続を開いた時からなので、流し始めとの差を足す
    shift = t0 - (s.t_open or t0)
    lags = []
    with open(out / f"{name}.soniox.jsonl", "w", encoding="utf-8") as f:
        for t in s.final_tokens:
            lag = None
            if t.get("end_ms") is not None:
                lag = round(t["recv_s"] - shift - t["end_ms"] / 1000 / (a.pace or 1.0), 3)
                lags.append(lag)
            f.write(json.dumps({"type": "token", **t, "lag_s": lag}, ensure_ascii=False) + "\n")
        done = {"type": "done", "text": s.text, "audio_s": s.audio_s, "connect_ms": s.connect_ms, "finalize_s": s.finalize_s,
                "lag_p50": pct(lags, 0.5), "lag_p90": pct(lags, 0.9), "languages": s.languages(), "terms": len(terms),
                "pace": a.pace, "region": a.region, "endpoint": a.endpoint, "ends": s.ends, "error": s.error}
        f.write(json.dumps(done, ensure_ascii=False) + "\n")
    (out / f"{name}.soniox.txt").write_text(s.text, encoding="utf-8")
    print(json.dumps({k: v for k, v in done.items() if k != "text"}, ensure_ascii=False))
    return 0 if not s.error else 1


if __name__ == "__main__":
    raise SystemExit(main())

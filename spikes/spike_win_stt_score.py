"""spike_win_stt.py の記録(spikes/logs/win_stt_*.jsonl)を採点する(Step 1・計測専用・標準ライブラリのみ)。

話し始め・話し終わりの正解は WAV の音量から取る: 20 ms ごとの RMS が -50 dBFS を超える所を声とみなし、
1.0 s 未満の間はつなげて 1 つの「発話」にする(合成音声は無音が -88 dBFS 前後で、声との差が大きい)。

  最初の partial  発話の話し始め(壁時計 = 供給の t0 + 音声の位置)から、その発話の音声を含む最初の結果が出るまで
  partial の間隔   発話の間に出た partial どうしの間(文字が変わったときだけ出るので、止まっている時間も含む)
  final           発話の話し終わりから、その終わりを含む final が出るまで(各エンジン自身の区切りで出る)
  CER             final を順につないだ文字列と正解(.txt)の編集距離 ÷ 正解の文字数。NFKC・小文字・漢数字→算用数字を
                  両方にかけ、句読点と空白を除いてから比べる
  認識 1 回        decode_ms(partial / final)。SAPI は内部で測れないので空欄
  CPU・GPU        計測中の 0.5 s ごとの標本。GPU は読み込み前 3 s の平均(他のアプリの分)を引いた増分

使い方: python spikes/spike_win_stt_score.py spikes/logs/win_stt_*.jsonl [--texts]
"""
from __future__ import annotations

import argparse
import array
import json
import math
import re
import statistics
import unicodedata
import wave
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "tests" / "fixtures" / "wav"
KANJI_DIGITS = str.maketrans("〇一二三四五六七八九", "0123456789")
PUNCT = re.compile(r"[\s、。,.!?「」『』()\[\]・:;\"'…‥〜~-]")
# 正解の文面は読み上げの音(カタカナ)で書いてある。英字の製品名で返すのは誤りでないので、補正後の CER ではそろえる
ALIASES = {"globalprotect": "グローバルプロテクト", "siem": "シーム"}


def utterances(wav_path: Path, thr_db: float = -50.0, frame_s: float = 0.02,
               join_gap_s: float = 1.0) -> list[tuple[float, float]]:
    with wave.open(str(wav_path)) as w:
        sr = w.getframerate()
        a = array.array("h", w.readframes(w.getnframes()))
    n = int(sr * frame_s)
    spans: list[list[float]] = []
    for i in range(0, len(a), n):
        seg = a[i:i + n]
        if not seg:
            break
        ms = sum(x * x for x in seg) / len(seg)
        db = 10 * math.log10(ms / 32768 ** 2) if ms > 0 else -120.0
        if db > thr_db:
            t0, t1 = i / sr, (i + len(seg)) / sr
            if spans and t0 - spans[-1][1] < join_gap_s:
                spans[-1][1] = t1
            else:
                spans.append([t0, t1])
    return [(s, e) for s, e in spans if e - s >= 0.1]


def norm(s: str, alias: bool = False) -> str:
    s = PUNCT.sub("", unicodedata.normalize("NFKC", s).lower().translate(KANJI_DIGITS))
    if alias:
        for k, v in ALIASES.items():
            s = s.replace(k, v)
    return s


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def score(path: Path) -> dict:
    evs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    run = next(e for e in evs if e.get("phase") == "run")
    tag = run.get("params", {}).get("tag", "")
    wav = run["wav"]
    pace = float(run.get("pace", 1.0))
    t0 = next(e["t0_ms"] for e in evs if e.get("phase") == "feed_start")
    parts = sorted((e for e in evs if e.get("type") == "partial" and e.get("text")), key=lambda e: e["t_ms"])
    finals = sorted((e for e in evs if e.get("type") == "final" and e.get("text")), key=lambda e: e["t_ms"])
    results = sorted(parts + finals, key=lambda e: e["t_ms"])

    def wall(audio_s: float) -> float:
        return t0 + audio_s * 1000 / pace

    utts = utterances(FIX / wav)
    first, gaps, fin, missed = [], [], [], 0
    for on, off in utts:
        hit = next((e for e in results if e["t_ms"] >= wall(on) and e["end_s"] > on + 0.05), None)
        if hit and hit["t_ms"] <= wall(off) + 5000:
            first.append(hit["t_ms"] - wall(on))
        ts = [e["t_ms"] for e in parts if wall(on) <= e["t_ms"] <= wall(off)]
        gaps += [b - a for a, b in zip(ts, ts[1:])]
        f = next((e for e in finals if e["end_s"] >= off - 0.3 and e["t_ms"] >= wall(on)), None)
        if f:
            fin.append(f["t_ms"] - wall(off))
        else:
            missed += 1

    ref = (FIX / wav).with_suffix(".txt").read_text(encoding="utf-8")
    hyp = "".join(e["text"] for e in finals)
    r, h = norm(ref), norm(hyp)
    ra, ha = norm(ref, alias=True), norm(hyp, alias=True)
    base = [e for e in evs if e.get("phase") == "res_base"]
    res = [e for e in evs if e.get("phase") == "res"]
    gpu0 = statistics.mean(e["gpu_util"] for e in base) if base else 0.0
    mem0 = statistics.mean(e["gpu_mem_mb"] for e in base) if base else 0.0
    feed_done = next((e for e in evs if e.get("phase") == "feed_done"), {})
    audio_s = float(feed_done.get("audio_s") or 0) or None
    dec_p = [e["decode_ms"] for e in parts if "decode_ms" in e]
    dec_f = [e["decode_ms"] for e in finals if "decode_ms" in e]
    load = next((e.get("ms") for e in evs if e.get("phase") == "load"), None)
    return {
        "engine": run["engine"] + (f"[{tag}]" if tag else ""), "wav": wav, "log": path.name,
        "utts": len(utts), "first": first, "gaps": gaps, "final": fin, "final_missed": missed,
        "partials": len(parts), "finals": len(finals),
        "edits": edit_distance(ra, ha), "ref_len": len(ra), "edits_raw": edit_distance(r, h), "ref_len_raw": len(r),
        "hyp": hyp, "ref": ref.replace("\n", ""),
        "dec_p": dec_p, "dec_f": dec_f,
        "busy": (sum(dec_p) + sum(dec_f)) / (audio_s * 1000) if audio_s and (dec_p or dec_f) else None,
        "max_final_s": max((e["end_s"] - e["start_s"] for e in finals), default=None),
        "cpu": [e["cpu"] for e in res], "rss": [e["rss_mb"] for e in res],
        "gpu": [e["gpu_util"] - gpu0 for e in res], "gmem": [e["gpu_mem_mb"] - mem0 for e in res],
        "load_ms": load,
    }


def f0(x, unit: str = "") -> str:
    return "—" if x is None else f"{x:,.0f}{unit}"


def row(name: str, rs: list[dict]) -> str:
    cat = lambda k: [x for r in rs for x in r[k]]   # noqa: E731
    first, gaps, fin = cat("first"), cat("gaps"), cat("final")
    edits, ref_len = sum(r["edits"] for r in rs), sum(r["ref_len"] for r in rs)
    raw = 100 * sum(r["edits_raw"] for r in rs) / max(1, sum(r["ref_len_raw"] for r in rs))
    missed = sum(r["final_missed"] for r in rs)
    utts = sum(r["utts"] for r in rs)
    cpu, gpu, gmem, rss = cat("cpu"), cat("gpu"), cat("gmem"), cat("rss")
    busy = [r["busy"] for r in rs if r["busy"] is not None]
    return " | ".join([
        f"`{name}`",
        f"{f0(pct(first, .5))} / {f0(pct(first, .9))}" + (f"(取れず {utts - len(first)})" if utts - len(first) else ""),
        f"{f0(pct(gaps, .5))} / {f0(pct(gaps, .9))} / {f0(max(gaps) if gaps else None)}",
        f"{f0(pct(fin, .5))} / {f0(pct(fin, .9))}" + (f"(出ず {missed})" if missed else ""),
        f"**{100 * edits / ref_len:.1f}%**({raw:.1f}%)" if ref_len else "—",
        f"{f0(pct(cat('dec_p'), .5))} / {f0(pct(cat('dec_f'), .5))}",
        f0(100 * statistics.mean(busy), "%") if busy else "—",
        f"{f0(statistics.mean(cpu) if cpu else None)} / {f0(max(cpu) if cpu else None)}",
        f"{f0(statistics.mean(gpu) if gpu else None)} / {f0(max(gmem) if gmem else None)}",
        f0(max(rss) if rss else None),
    ]).join(["| ", " |"])


HEAD = ("| エンジン | 最初の partial ms p50 / p90 | partial の間隔 ms p50 / p90 / 最大 | final(話し終わりから)ms p50 / p90 "
        "| CER 補正後(補正前) | 認識 1 回 ms(partial / final) | 計算の占有 | CPU % 平均 / 最大 | GPU 増分 % 平均 / メモリ MiB | RSS MiB |\n"
        "|---|---|---|---|---|---|---|---|---|---|")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--texts", action="store_true", help="final をつないだ文字列も出す")
    args = ap.parse_args()
    scored = [score(p) for p in args.logs]
    by_engine: dict[str, list[dict]] = defaultdict(list)
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in scored:
        by_engine[s["engine"]].append(s)
        by_pair[(s["engine"], s["wav"])].append(s)
    print("### 全素材の合計(発話を束ねた p50 / p90)\n")
    print(HEAD)
    for name, rs in by_engine.items():
        print(row(name, rs))
    print("\n### 素材ごと\n")
    print(HEAD.replace("| エンジン |", "| エンジン・素材 |"))
    for (name, wav), rs in sorted(by_pair.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        print(row(f"{name} {wav}", rs))
    print("\n### 補足\n")
    for s in scored:
        print(f"- `{s['engine']}` {s['wav']}: 発話 {s['utts']}・partial {s['partials']}・final {s['finals']}・"
              f"final 1 つの最長 {f0(s['max_final_s'])} s・読み込み {f0(s['load_ms'])} ms・誤り {s['edits']}/{s['ref_len']} 字"
              f"(`{s['log']}`)")
        if args.texts:
            print(f"  - final: {s['hyp']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

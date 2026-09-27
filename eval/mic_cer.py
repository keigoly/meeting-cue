"""自分の声(マイク)の文字起こし精度を、読み上げ原稿と比べて数える(2026-09-26 Step 1)。

    python eval/mic_cer.py --ref script.txt [--terms terms.txt] \
        --hyp live=~/.meeting-cue/sessions/<記録>/transcript.jsonl --hyp st=out_st.jsonl ...

--hyp の形式: `名前=パス`。パスは次のどれか。
  - 記録の transcript.jsonl(`channel` のある行)→ マイク(mic)の発言をつなぐ。`パス:system` で相手側
  - spikes/spike_stt_compare.swift の出力(`type: done` の行の text)
  - 素のテキスト
--terms: 1 行 = `正しい書き方|同じとみなす書き方…`。認識結果の書き方の揺れ(カタカナ・英字)をそろえてから比べ、
         正しく取れた語を数える。# で始まる行は注釈。
文字誤り率(CER)= 編集距離 / 原稿の文字数。比べる前に NFKC・小文字化・空白と記号の除去をする(依存なし・stdlib のみ)。
「以外」= 固有名詞以外の日本語の CER、「固有」= 固有名詞の範囲の CER(誤りを原稿の位置で振り分ける)。
"""
from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return "".join(ch for ch in s if unicodedata.category(ch)[0] not in ("P", "Z", "S", "C"))


def load_terms(path: str | None) -> list[list[str]]:
    if not path:
        return []
    rows = []
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            rows.append([w.strip() for w in line.split("|") if w.strip()])
    return rows


def unify(text: str, terms: list[list[str]]) -> str:
    """揺れを正しい書き方へ(比べる前の正規化の上で置き換える。長い書き方から)。"""
    t = normalize(text)
    pairs = sorted(((normalize(v), normalize(row[0])) for row in terms for v in row[1:]), key=lambda p: -len(p[0]))
    for v, canon in pairs:
        if v:
            t = t.replace(v, canon)
    return t


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def split_errors(ref: str, hyp: str, spans: list[tuple[int, int]]) -> tuple[int, int]:
    """編集距離の対応づけをたどり、誤りを「固有名詞の範囲」と「それ以外」に分けて数える。
    挿入は直前の原稿の文字の側に数える。固有名詞の揺れは区切り方と関係なく出るので、区切りの良し悪しは「それ以外」で見る。"""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    in_term = [False] * n
    for a, b in spans:
        for k in range(a, b):
            in_term[k] = True
    term_err = other_err = 0
    i, j = n, m
    while i > 0 or j > 0:
        pos = max(0, i - 1)
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            err = ref[i - 1] != hyp[j - 1]
            i, j = i - 1, j - 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            err = True
            i -= 1
        else:
            err = True
            j -= 1
        if err:
            if n and in_term[min(pos, n - 1)]:
                term_err += 1
            else:
                other_err += 1
    return term_err, other_err


def load_hyp(spec: str) -> str:
    path, channel = spec, "mic"
    if spec.rsplit(":", 1)[-1] in ("mic", "system"):
        path, channel = spec.rsplit(":", 1)
    p = Path(path).expanduser()
    if p.suffix != ".jsonl":
        return p.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = [r for r in rows if r.get("type") == "done"]
    if done:
        return done[-1]["text"]
    return "".join(r["text"] for r in rows if r.get("channel") == channel)


def score(ref: str, hyp: str, terms: list[list[str]]) -> dict:
    r, h = unify(ref, terms), unify(hyp, terms)
    canon = [normalize(row[0]) for row in terms]
    want = [c for c in canon if c in r]
    hit = [c for c in want if c in h]
    spans, start = [], 0
    for c in sorted(set(want), key=len, reverse=True):
        start = 0
        while (k := r.find(c, start)) >= 0:
            spans.append((k, k + len(c)))
            start = k + len(c)
    term_chars = len({k for a, b in spans for k in range(a, b)})
    term_err, other_err = split_errors(r, h, spans)
    return {"cer": edit_distance(r, h) / max(1, len(r)), "ref_chars": len(r), "hyp_chars": len(h),
            "cer_other": other_err / max(1, len(r) - term_chars), "cer_terms": term_err / max(1, term_chars),
            "terms": f"{len(hit)}/{len(want)}", "missed": [row[0] for row in terms if normalize(row[0]) in want
                                                           and normalize(row[0]) not in hit]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", required=True, help="読み上げ原稿(テキスト)")
    ap.add_argument("--terms", help="固有名詞と書き方の揺れ")
    ap.add_argument("--hyp", action="append", required=True, help="名前=パス(何度でも)")
    a = ap.parse_args()
    ref = Path(a.ref).expanduser().read_text(encoding="utf-8")
    terms = load_terms(a.terms)
    print(f"{'方式':<14} {'CER':>6} {'以外':>6} {'固有':>6} {'文字数':>7}  固有名詞  取りこぼした語")
    for spec in a.hyp:
        name, path = spec.split("=", 1)
        s = score(ref, load_hyp(path), terms)
        print(f"{name:<14} {s['cer']:>6.1%} {s['cer_other']:>6.1%} {s['cer_terms']:>6.1%} "
              f"{s['hyp_chars']:>4}/{s['ref_chars']:<4} {s['terms']:>6}  " + "・".join(s["missed"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

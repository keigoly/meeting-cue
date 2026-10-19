"""会議の文字起こしの方式を、同じ参照の文と比べる(REQUIREMENTS FR-2b の Step 1・2026-10-17)。

    python3 spikes/spike_soniox_score.py --ref <参照.txt> <名前>=<文のファイル> ...

- 文のファイルは .txt(そのまま)か .jsonl(meetcue の transcript.jsonl = 行の text を時刻順につなぐ /
  spike_soniox.py の出力 = done の text)。
- 比べる前に NFKC・小文字・記号と空白を除き、決めたフィラー(えー・あのー・まあ 等)を両方から除く。公式の書き起こし
  (記者会見の会見録)はフィラーを除いて整えてあるので、言ったとおりに書く方式だけが不利にならないようにする。
- CER = 編集距離 / 参照の字数。長い文(数千字)どうしでも速いよう、編集距離はビット並列(Myers / Hyyrö)で数える。
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path

FILLERS = ["えーっと", "えーと", "えっと", "ええと", "えー", "えぇ", "あのー", "あのう", "あの", "うーん", "うん", "あー",
           "んー", "そのー", "まあ", "まぁ", "なんか", "なんだろう"]
_FILLER_RE = re.compile("|".join(map(re.escape, sorted(FILLERS, key=len, reverse=True))))


def normalize(text: str, drop_fillers: bool = True) -> str:
    s = unicodedata.normalize("NFKC", text).lower()
    if drop_fillers:
        s = _FILLER_RE.sub("", s)
    return "".join(ch for ch in s if unicodedata.category(ch)[0] not in ("P", "Z", "S", "C"))


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距離(Hyyrö 2001 のビット並列。a の長さぶんのビットを 1 つの整数で持つ)。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    m = len(a)
    full, top = (1 << m) - 1, 1 << (m - 1)
    peq: dict[str, int] = {}
    for i, ch in enumerate(a):
        peq[ch] = peq.get(ch, 0) | (1 << i)
    pv, mv, score = full, 0, m
    for ch in b:
        eq = peq.get(ch, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & full
        mh = pv & xh
        if ph & top:
            score += 1
        elif mh & top:
            score -= 1
        ph = ((ph << 1) | 1) & full
        mh = (mh << 1) & full
        pv = (mh | ~(xv | ph)) & full
        mv = ph & xv
    return score


def read_text(path: Path) -> str:
    if path.suffix != ".jsonl":
        return path.read_text(encoding="utf-8")
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    done = [r for r in rows if r.get("type") == "done"]
    if done:
        return done[-1].get("text", "")
    lines = [r for r in rows if r.get("text") and "start_seconds" in r]
    lines.sort(key=lambda r: (r.get("start_seconds") or 0))
    return "".join(r["text"] for r in lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--keep-fillers", action="store_true", help="フィラーを除かずに比べる")
    ap.add_argument("hyps", nargs="+", help="名前=ファイル")
    a = ap.parse_args()
    ref = normalize(Path(a.ref).read_text(encoding="utf-8"), not a.keep_fillers)
    print(f"参照 {len(ref)} 字({a.ref})")
    print("| 方式 | CER | 字数 |\n|---|---|---|")
    for item in a.hyps:
        name, path = item.split("=", 1)
        hyp = normalize(read_text(Path(path)), not a.keep_fillers)
        print(f"| {name} | {edit_distance(ref, hyp) / max(1, len(ref)):.1%} | {len(hyp)} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

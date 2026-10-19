"""録音後の文字起こしし直し(修正 A)の物差し: 2 本(自分 / 相手)の文字起こしを時刻順に 1 本につなぎ、参照と比べる(2026-10-16)。

    python3 eval/redo_cer.py --ref ~/.meeting-cue/eval/<記録>/whisper.txt \
        --hyp live=~/.meeting-cue/sessions/<記録>/transcript.jsonl \
        --hyp redo=~/.meeting-cue/sessions/<記録>/transcript.redo.jsonl \
        --hyp seqw=<出力>/seqw_mic.jsonl,<出力>/seqw_system.jsonl

--ref: 参照の素のテキスト(.txt)か字幕(.srt)。人が直した正解でなければ、CER は「参照との食い違い」の概算になる
       (2026-10-16 実測: 同じ音声を faster-whisper でまとめて書き起こしても whisper.cpp の参照と 13.5〜16.6% 食い違う)。
--hyp: `名前=パス[,パス…]`。行は記録の transcript.jsonl と同じ形(channel / text / start_seconds)。同じフォルダに meta.json が
       あれば、チャネルの録音の頭のずれ(audio.<channel>.t0_ms)を足してから時刻順に並べる。
出すもの: CER(NFKC・小文字化・空白と記号を除いて比べる・eval/mic_cer.py と同じ)と、チャネルごとの行数・平均字数・
5 字以下の行・幻覚の句(Whisper が無音や短い区間で書く「ご視聴ありがとうございました」など)を含む行・暴走した繰り返し
(同じ句が 4 回以上続く)を含む行。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

try:
    from mic_cer import edit_distance, normalize
except ImportError:   # python -m eval.redo_cer
    from eval.mic_cer import edit_distance, normalize

HALLUCINATIONS = ("ご視聴ありがとうございました", "ご視聴", "さようなら", "また会いましょう", "see you guys", "バイバイ",
                  "チャンネル登録")
# 暴走した繰り返し(「はい、」× 20 など)。meetcue/redo.py の LOOP_RE と同じ(この道具は単独で動かすので写す)
LOOP_RE = re.compile(r"(.{2,15}?)(?:[、。,.!?！？\s]*\1){3,}")


def has_loop(text: str) -> bool:
    return any(any(c.isalpha() for c in m.group(1)) for m in LOOP_RE.finditer(text))


def read_ref(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".srt":   # 番号と時刻の行を除く
        text = "\n".join(ln for ln in text.splitlines()
                         if ln.strip() and not ln.strip().isdigit() and not re.match(r"\d+:\d+:\d+[,.]\d+ -->", ln))
    return text


def read_rows(paths: list[Path]) -> list[tuple[float, str, str]]:
    rows = []
    for p in paths:
        off: dict[str, float] = {}
        meta = p.parent / "meta.json"
        if meta.exists():
            audio = json.loads(meta.read_text(encoding="utf-8")).get("audio") or {}
            t0s = {ch: (a or {}).get("t0_ms") for ch, a in audio.items()}
            base = min((t for t in t0s.values() if t is not None), default=None)
            off = {ch: (t - base) / 1000 for ch, t in t0s.items() if t is not None and base is not None}
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            ch = r.get("channel", "system")
            rows.append(((r.get("start_seconds") or 0) + off.get(ch, 0.0), ch, r.get("text", "")))
    return sorted(rows, key=lambda x: x[0])


def channel_stats(rows: list[tuple[float, str, str]]) -> dict:
    out = {}
    for ch in sorted({c for _, c, _ in rows}):
        xs = [t for _, c, t in rows if c == ch]
        n = sum(map(len, xs))
        out[ch] = {"lines": len(xs), "chars": n, "avg_chars": round(n / len(xs), 1) if xs else 0,
                   "short_lines": sum(1 for t in xs if len(normalize(t)) <= 5),
                   "hallucination_lines": sum(1 for t in xs if any(h in t.lower() for h in HALLUCINATIONS)),
                   "loop_lines": sum(1 for t in xs if has_loop(t))}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="2 本の文字起こしを時刻順につなぎ、参照と比べる(CER と行の形)")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--hyp", action="append", required=True, help="名前=パス[,パス…]")
    a = ap.parse_args()
    ref = normalize(read_ref(Path(a.ref).expanduser()))
    for spec in a.hyp:
        name, paths = spec.split("=", 1)
        rows = read_rows([Path(p).expanduser() for p in paths.split(",")])
        hyp = normalize("".join(t for _, _, t in rows))
        print(json.dumps({"hyp": name, "cer": round(edit_distance(ref, hyp) / max(1, len(ref)) * 100, 1),
                          "ref_chars": len(ref), "hyp_chars": len(hyp), "channels": channel_stats(rows)},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

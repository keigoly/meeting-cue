"""YouTube の日本語字幕を、無音(3 s 以上)で区切ったかたまりで出す。正解表(eval/gt/*.json)の下書き用。

  python3 eval/captions.py <video_id>        # 字幕を取って表示(yt-dlp は uvx で一時的に使う)
  python3 eval/captions.py <file.ja.json3>   # 取得済みの字幕を表示

質問の直後に長い無音(回答時間)がある素材は、各かたまりの末尾が質問になりやすい。表示を見て、
質問の時刻と文面を eval/gt/<名前>.json の items に書く(kind: question / ambiguous)。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

GAP_MS = 3000


def mmss(ms: float) -> str:
    return f"{int(ms // 60000):02d}:{int(ms / 1000 % 60):02d}"


def fetch(video_id: str, out: Path) -> Path:
    subprocess.run(["uvx", "--quiet", "yt-dlp", "--skip-download", "--no-warnings", "--write-auto-subs",
                    "--write-subs", "--sub-langs", "ja", "--sub-format", "json3", "-o", str(out / "%(id)s.%(ext)s"),
                    f"https://www.youtube.com/watch?v={video_id}"], check=True, capture_output=True)
    return out / f"{video_id}.ja.json3"


def blocks(path: Path) -> list[tuple[int, str, int]]:
    ev = json.loads(path.read_text(encoding="utf-8"))["events"]
    rows = []
    for e in ev:
        t = "".join(s.get("utf8", "") for s in (e.get("segs") or [])).replace("\n", " ").strip()
        if t:
            rows.append((e["tStartMs"], e["tStartMs"] + e.get("dDurationMs", 0), t))
    out, cur, cur_t, last_end = [], [], None, 0
    for s, e, t in rows:
        if cur and s - last_end >= GAP_MS:
            out.append((cur_t, " ".join(cur), s - last_end))
            cur, cur_t = [], None
        if cur_t is None:
            cur_t = s
        cur.append(t)
        last_end = max(last_end, e)
    if cur:
        out.append((cur_t, " ".join(cur), 0))
    return out


def main(arg: str) -> None:
    p = Path(arg)
    if not p.exists():
        p = fetch(arg, Path(tempfile.mkdtemp(prefix="meetcue-cap-")))
    for t, text, gap in blocks(p):
        print(f"[{mmss(t)}] {text[:300]}" + (f"   <<無音 {gap / 1000:.0f}s>>" if gap >= GAP_MS else ""))


if __name__ == "__main__":
    main(sys.argv[1])

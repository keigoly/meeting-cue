"""一人語り(質問が視聴者宛てでない素材)の記録を集計する: 起動の一覧・区切りの遅れ・マイクへの回り込み。

  python3 eval/monologue.py <session_dir | session_id | latest>
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


def jl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def hms(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def pct(v: list[float]) -> str:
    if not v:
        return "n=0"
    v = sorted(v)
    p90 = v[min(len(v) - 1, int(round(0.9 * (len(v) - 1))))]
    return f"n={len(v)} p50={statistics.median(v):.0f} p90={p90:.0f} max={v[-1]:.0f}"


def resolve_session(arg: str) -> Path:
    """セッションのディレクトリ・ID(20260926_004431_9073e7aa)・"latest" のどれでも受ける。"""
    p = Path(arg).expanduser()
    if p.is_dir():
        return p
    root = Path(os.environ.get("MEETCUE_HOME") or (Path.home() / ".meeting-cue")) / "sessions"
    if arg == "latest":
        cands = sorted(d for d in root.iterdir() if (d / "meta.json").exists())
        return cands[-1]
    return root / arg


def main(d: Path) -> None:
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    tr, jd, mt = jl(d / "transcript.jsonl"), {r["rid"]: r for r in jl(d / "judgments.jsonl")}, jl(d / "metrics.jsonl")
    sysu = [r for r in tr if r["channel"] != "mic"]
    mic = [r for r in tr if r["channel"] == "mic"]
    dur = ((meta.get("ended_ms") or mt[-1]["ts_ms"]) - meta["started_ms"]) / 1000
    print(f"# 一人語りの集計 session={d.name} 長さ {dur / 60:.1f} 分 mode={meta['mode']} privacy={meta['privacy']}")
    print(f"system 発話 {len(sysu)} / mic 発話 {len(mic)} / 判定 {len(jd)} / 起動 {sum(1 for r in jd.values() if r.get('trigger'))}")
    print(f"source={dict(Counter(r.get('source') for r in jd.values()))} speech_act={dict(Counter(r['summary'].get('speech_act') for r in jd.values()))}")
    print()
    print("## 起動した発話(一人語りなので原則すべて誤起動の候補)")
    for u in sysu:
        r = jd.get(u["rid"])
        if r and r.get("trigger"):
            s = r["summary"]
            print(f"- [{hms(u['t_ms'])}] #{u['rid']} {u['text'][:70]}  act={s.get('speech_act')}({s.get('speech_act_p')}) to_me={s.get('to_me')} intent={s.get('intent')}")
    print()
    print("## 起動しなかったが質問・依頼と判定された発話(to_me で止まったもの)")
    for u in sysu:
        r = jd.get(u["rid"])
        if r and not r.get("trigger") and r["summary"].get("speech_act") in ("question", "request"):
            s = r["summary"]
            print(f"- [{hms(u['t_ms'])}] {u['text'][:60]}  act={s.get('speech_act')}({s.get('speech_act_p')}) to_me={s.get('to_me')}")
    print()
    print("## to_me の分布(system 発話・Jev)")
    tm = [r["summary"].get("to_me") or 0 for r in jd.values() if r.get("source") == "jev"]
    bins = Counter(min(9, int(x * 10)) for x in tm)
    print("  " + " ".join(f"{b / 10:.1f}:{bins.get(b, 0)}" for b in range(10)))
    print()
    print("## 区切りの遅れ(相手側)")
    # 音声の時計の起点: tap は device_start、file は asset_ready(どちらも helper の音声 0 秒にほぼ一致)
    dev = next((r["ts_ms"] for r in mt if r.get("phase") in ("helper_device_start",) and r.get("channel") == "system"), None) \
        or next((r["ts_ms"] for r in mt if r.get("phase") == "helper_asset_ready" and r.get("channel") == "system"), None)
    seg = [r for r in mt if r.get("phase") == "segment" and r.get("channel") == "system"]
    finals = {}
    for r in seg:
        if r.get("audio_end_s") is not None:
            finals[(r["audio_start_s"], r["audio_end_s"])] = r["ts_ms"]
    span = [(e - s) * 1000 for (s, e) in finals]
    print(f"- 1 回の final が抱えた音声の長さ: {pct(span)} ms(final {len(finals)} 回)")
    if dev:
        lag = [ts - (dev + e * 1000) for (s, e), ts in finals.items()]
        print(f"- 音声の終わり → 確定: {pct(lag)} ms(起点のずれで数百 ms の誤差あり)")
    ts = sorted({r["ts_ms"] for r in seg})
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    burst = Counter(r["ts_ms"] for r in seg)
    print(f"- 確定の間隔: {pct(gaps)} ms / 同時に出た発話の最大 {max(burst.values()) if burst else 0} 件")
    lf = [r for r in mt if r.get("phase") == "long_finalize" and r.get("channel") == "system"]
    print(f"- pause_finalize={sum(1 for r in mt if r.get('phase') == 'pause_finalize' and r.get('channel') == 'system')}"
          f" long_finalize={len(lf)} {dict(Counter(r.get('reason') for r in lf))}"
          f" safety={sum(1 for r in mt if r.get('phase') == 'safety_finalize')} dropped={sum(1 for r in seg if r.get('dropped'))}")
    ages = [r["age_ms"] for r in seg if isinstance(r.get("age_ms"), (int, float))]
    if ages:
        print(f"- 発話の最初の partial → 確定(age_ms): {pct(ages)} ms")
    ct = [r for r in mt if r.get("phase") == "carry_tail" and r.get("channel") == "system"]
    print(f"- 繰り越し {len(ct)} 件 {dict(Counter(r.get('reason', 'particle') for r in ct))}")
    lens = [len(u["text"]) for u in sysu]
    print(f"- 発話の長さ(字): {pct(lens)} / 10 字以下 {sum(1 for x in lens if x <= 10)} 件")
    print()
    print("## 所要")
    print(f"- 判定 {pct([r['ms'] for r in mt if r.get('phase') == 'judge'])} ms / jev 失敗 {sum(1 for r in mt if r.get('phase') == 'jev_failed')}")
    e2e = [r["ms"] for r in mt if r.get("phase") == "e2e_first_cue" and r.get("kind") == "cue"]
    print(f"- 最初のキュー(確定から) {pct(e2e)} ms / ナレッジだけ {pct([r['ms'] for r in mt if r.get('phase') == 'e2e_first_cue' and r.get('kind') == 'knowledge'])} ms")
    gm = [r for r in mt if r.get("phase") == "generate"]
    print(f"- 生成 {len(gm)} 本 ok={sum(1 for r in gm if r.get('ok'))} cancelled={sum(1 for r in gm if r.get('error') == 'cancelled')}"
          f" 費用 ${sum((r.get('cost_usd') or 0) for r in gm):.4f}")
    bad = [r for r in mt if r.get("phase", "").split("_", 1)[-1] in ("abort", "error", "results_error") or r.get("phase") == "handle_error"]
    print(f"- 異常 {len(bad)} 件 {[r.get('phase') for r in bad][:5]}")
    print()
    print("## マイクへの回り込み(スピーカー再生の影響)")
    def bg(s):
        s = "".join(ch for ch in s if not ch.isspace())
        return {s[i:i + 2] for i in range(len(s) - 1)}
    sysbg = set().union(*(bg(u["text"]) for u in sysu)) if sysu else set()
    echo = [m for m in mic if bg(m["text"]) and len(bg(m["text"]) & sysbg) / len(bg(m["text"])) >= 0.6]
    print(f"- mic 発話 {len(mic)} 件のうち、相手側と同じ文面(bigram 60% 以上一致)が {len(echo)} 件")


if __name__ == "__main__":
    main(resolve_session(sys.argv[1]))

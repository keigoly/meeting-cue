"""セッション記録を正解表(字幕由来)と突き合わせて採点する(Step 1 の見える化・回帰の物差し)。

  python3 eval/score.py <session_dir | session_id | latest> eval/gt/iroots_qp8xasjyQig.json

出力: 質問ごとの 検出/起動/最初のキュー ms/生成の成否、誤起動の一覧、区切りの健全性、閾値の掃引。
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

MATCH_MIN = 0.45


def norm(s: str) -> str:
    return re.sub(r"[\s、。？?！!・,.\"「」()（）]", "", s or "")


def bigrams(s: str) -> set[str]:
    s = norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def overlap(a: str, b: str) -> float:
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / max(1, min(len(A), len(B)))


def mmss(sec: float) -> str:
    sec = max(0, sec)
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def pct(v: list[float]) -> str:
    if not v:
        return "n=0"
    v = sorted(v)
    p90 = v[min(len(v) - 1, int(round(0.9 * (len(v) - 1))))]
    return f"n={len(v)} p50={statistics.median(v):.0f} p90={p90:.0f} max={v[-1]:.0f}"


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


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


def main(sd: Path, gtp: Path) -> None:
    gt = json.loads(gtp.read_text(encoding="utf-8"))
    items = gt["items"]
    for g in items:
        m, s = g["t"].split(":")
        g["sec"] = int(m) * 60 + int(s)
    tr = [r for r in load_jsonl(sd / "transcript.jsonl")]
    sysu = [r for r in tr if r["channel"] != "mic"]
    mic = [r for r in tr if r["channel"] == "mic"]
    jd = {r["rid"]: r for r in load_jsonl(sd / "judgments.jsonl")}
    mt = load_jsonl(sd / "metrics.jsonl")
    cues = load_jsonl(sd / "cues.jsonl")
    e2e = {r["rid"]: r["ms"] for r in mt if r.get("phase") == "e2e_first_cue" and r.get("kind") == "cue"}
    e2e_k = {r["rid"]: r["ms"] for r in mt if r.get("phase") == "e2e_first_cue" and r.get("kind") == "knowledge"}
    deep = {r["rid"] for r in mt if r.get("phase") == "deepdive"}
    gen = defaultdict(list)
    for r in mt:
        if r.get("phase") == "generate":
            gen[r["rid"]].append(r)
    paused = {r["rid"] for r in mt if r.get("phase") == "skipped_paused"}
    cue_n = defaultdict(lambda: [0, 0])
    for c in cues:
        if c.get("kind") == "cues":
            cue_n[c["rid"]][0] += len(c.get("answers") or [])
            cue_n[c["rid"]][1] += len(c.get("counters") or [])

    # 発話 → 正解の対応(bigram の重なり)
    assign: dict[str, dict] = {}
    for u in sysu:
        if len(norm(u["text"])) < 4:
            continue
        best = max(items, key=lambda g: overlap(u["text"], g["text"]))
        sc = overlap(u["text"], best["text"])
        if sc >= MATCH_MIN:
            assign[u["rid"]] = {"g": best, "score": sc}
    # 動画の時刻の推定(確定時刻 − 正解の開始時刻 の中央値)
    offs = [u["t_ms"] / 1000 - assign[u["rid"]]["g"]["sec"] for u in sysu if u["rid"] in assign]
    off = statistics.median(offs) if offs else sysu[0]["t_ms"] / 1000 if sysu else 0

    def vt(u):
        return mmss(u["t_ms"] / 1000 - off)

    trig = lambda rid: bool(jd.get(rid, {}).get("trigger"))
    print(f"# {gt['title']}  session={sd.name}")
    print(f"system 発話 {len(sysu)} / mic 発話 {len(mic)} / 判定 {len(jd)} / 起動 {sum(1 for r in jd.values() if r.get('trigger'))}"
          f" / 深掘り {len(deep)} / 一時停止中 {len(paused)}")
    print()
    print("## 質問ごと")
    print("| # | 字幕 | 質問 | 対応した発話 | 判定 act(p) / to_me | 起動 | 最初のキュー ms | 生成 | 回答/逆質問 |")
    print("|---|---|---|---|---|---|---|---|---|")
    hit = miss = 0
    first_ms = []
    for g in [g for g in items if g["kind"] == "question"]:
        us = [u for u in sysu if assign.get(u["rid"], {}).get("g") is g]
        fired = [u for u in us if trig(u["rid"]) or u["rid"] in deep]
        ok = bool(fired)
        hit += ok
        miss += not ok
        utxt = " / ".join(f"{u['text'][:40]}({assign[u['rid']]['score']:.2f})" for u in us) or "—(発話なし)"
        js = "; ".join(f"{jd[u['rid']]['summary'].get('speech_act')}({jd[u['rid']]['summary'].get('speech_act_p')})/"
                       f"{jd[u['rid']]['summary'].get('to_me')}" for u in us if u["rid"] in jd) or "—"
        fm = [e2e[u["rid"]] for u in fired if u["rid"] in e2e]
        if fm:
            first_ms.append(min(fm))
        gs = "; ".join(f"{r.get('part')}:{'ok' if r.get('ok') else r.get('error')}" for u in fired for r in gen.get(u["rid"], [])) or "—"
        cn = "; ".join(f"{cue_n[u['rid']][0]}/{cue_n[u['rid']][1]}" for u in fired if u["rid"] in cue_n) or "—"
        mark = ("○" if ok else "×") + ("(深掘り)" if any(u["rid"] in deep and not trig(u["rid"]) for u in fired) else "")
        print(f"| {g['n']} | {g['t']} | {g['text'][:28]} | {utxt} | {js} | {mark} | {min(fm) if fm else '—'} | {gs} | {cn} |")
    nq = hit + miss
    print()
    print("## 誤起動(正解の質問に対応しない起動)")
    false_t = []
    amb = []
    for u in sysu:
        if not trig(u["rid"]):
            continue
        a = assign.get(u["rid"])
        if a and a["g"]["kind"] == "question":
            continue
        (amb if a and a["g"]["kind"] == "ambiguous" else false_t).append(u)
    for u in false_t:
        s = jd[u["rid"]]["summary"]
        print(f"- [{vt(u)}] #{u['rid']} {u['text'][:60]}  act={s.get('speech_act')}({s.get('speech_act_p')}) to_me={s.get('to_me')}")
    if not false_t:
        print("- なし")
    if amb:
        print("- (保留: 反語的な問いかけ)" + " / ".join(f"[{vt(u)}] {u['text'][:30]}" for u in amb))
    trig_total = sum(1 for u in sysu if trig(u["rid"]))
    print()
    print("## 集計")
    print(f"- 取りこぼし: {miss}/{nq} = {miss / max(1, nq):.0%}(深掘りで拾ったものは検出に数える)")
    print(f"- 誤起動: {len(false_t)}/{trig_total} = {len(false_t) / max(1, trig_total):.0%}(起動のうち正解の質問でないもの。保留 {len(amb)} 件は除外)")
    print(f"- 最初のキュー(正解の質問・発話確定から): {pct(first_ms)} ms / 予算 p50 ≤ 2500")
    print(f"- ナレッジだけのカード: {pct(list(e2e_k.values()))} ms")
    jm = [r["ms"] for r in mt if r.get("phase") == "judge"]
    print(f"- 判定: {pct(jm)} ms / jev 失敗 {sum(1 for r in mt if r.get('phase') == 'jev_failed')} 件"
          f" / source={dict(Counter(r.get('source') for r in jd.values()))}")
    gm = [r for r in mt if r.get("phase") == "generate"]
    print(f"- 生成: {len(gm)} 本 ok={sum(1 for r in gm if r.get('ok'))} cancelled={sum(1 for r in gm if r.get('error') == 'cancelled')}"
          f" 失敗={sum(1 for r in gm if not r.get('ok') and r.get('error') != 'cancelled')}"
          f" / 初トークン {pct([r['ms_first_token'] for r in gm if r.get('ms_first_token')])} ms"
          f" / 費用 ${sum((r.get('cost_usd') or 0) for r in gm):.4f}")
    print()
    print("## 区切りの健全性")
    seg = [r for r in mt if r.get("phase") == "segment" and r.get("channel") != "mic"]
    print(f"- system segment {len(seg)} 件: dropped(相づち等)={sum(1 for r in seg if r.get('dropped'))}"
          f" forced={sum(1 for r in seg if r.get('forced'))} tail={dict(Counter(r.get('tail_kind') for r in seg))}")
    ph = Counter(r.get("phase") for r in mt)
    print(f"- safety_finalize={ph.get('safety_finalize', 0)} carry_flush={ph.get('carry_flush', 0)} carry_tail={ph.get('carry_tail', 0)}"
          f" handle_error={ph.get('handle_error', 0)} preempt={ph.get('generate_preempt', 0)}")
    bad = [r for r in mt if r.get("phase", "").startswith("helper_") and r["phase"].split("_", 1)[1] in ("abort", "error", "results_error")]
    print(f"- helper の異常: {len(bad)} 件" + ("".join(f"\n  - {json.dumps(r, ensure_ascii=False)[:200]}" for r in bad)))
    split = [g["n"] for g in items if g["kind"] == "question" and sum(1 for u in sysu if assign.get(u["rid"], {}).get("g") is g) > 1]
    print(f"- 1 問が複数の発話に割れた: {split or 'なし'}")
    print()
    print("## 閾値の掃引(Jev の判定だけ・深掘りは数えない)")
    print("| act_min \\ to_me_min | " + " | ".join(str(t) for t in (0.3, 0.4, 0.5, 0.6, 0.7)) + " |")
    print("|---|" + "---|" * 5)
    for a in (0.5, 0.6, 0.7):
        row = []
        for t in (0.3, 0.4, 0.5, 0.6, 0.7):
            def fires(rid):
                r = jd.get(rid)
                if not r or r.get("source") != "jev":
                    return False
                s = r["summary"]
                return s.get("speech_act") in ("question", "request") and (s.get("speech_act_p") or 0) >= a and (s.get("to_me") or 0) >= t
            det = sum(1 for g in items if g["kind"] == "question" and any(fires(u["rid"]) for u in sysu if assign.get(u["rid"], {}).get("g") is g))
            fp = sum(1 for u in sysu if fires(u["rid"]) and u["rid"] not in assign)
            row.append(f"検出 {det}/{nq} 誤 {fp}")
        print(f"| {a} | " + " | ".join(row) + " |")
    print()
    print("## 対応しなかった system 発話(先頭 25 件・区切りの確認用)")
    for u in [u for u in sysu if u["rid"] not in assign][:25]:
        s = jd.get(u["rid"], {}).get("summary", {})
        print(f"- [{vt(u)}] {u['text'][:50]}  {s.get('speech_act', '-')}/{s.get('to_me', '-')}{' 起動' if trig(u['rid']) else ''}")


if __name__ == "__main__":
    main(resolve_session(sys.argv[1]), Path(sys.argv[2]))

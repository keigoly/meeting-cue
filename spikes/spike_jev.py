"""Phase 0 (a): Jev の日本語判定 — 質問 / 非質問の発話を投げ、判定と所要 ms を記録する。

実行: uv run --python 3.12 --no-project python spikes/spike_jev.py
記録: spikes/logs/jev_<stamp>.jsonl(state 本文と答えを含む=較正用。公開 repo には入れない)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meetcue.judge import heuristic, jev  # noqa: E402
from meetcue.metrics import percentiles  # noqa: E402
from meetcue.secrets import openrouter_key  # noqa: E402

CASES = [
    # (mode, channel, prev, utterance, expected_act, expected_to_me)
    ("participant", "system", ["本日はご説明ありがとうございます。"],
     "この構成でGlobalProtectの分割トンネルはどう扱いますか。", "question", True),
    ("participant", "system", [], "導入にかかる費用はどのくらいでしょうか。", "question", True),
    ("presenter", "room", [], "その仕組みは既存のSIEMと競合するリスクはありませんか。", "question", True),
    ("presenter", "room", ["ご質問ありがとうございます。"], "事例があれば教えてください。", "request", True),
    ("audience", "system", [], "本日は当社のAIエージェント基盤についてお話しします。", "statement", False),
    ("participant", "system", ["費用は月額で考えています。"], "なるほど、承知しました。", "agreement", False),
    ("participant", "system", [], "今日の議題は三つあります。一つ目は来期の体制についてです。", "statement", False),
    ("participant", "system", [], "それでは次の議題に移りましょう。", "smalltalk", False),
    ("presenter", "room", [], "皆さんは普段どのツールで監視されていますか。", "question", False),
    ("participant", "system", ["こちらの案で進めたいと思います。"], "いつ頃までに回答をいただけますか。", "question", True),
]


def main() -> int:
    key = openrouter_key()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = Path(__file__).resolve().parent / "logs" / f"jev_{stamp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    ms_all: list[float] = []
    cost = 0.0
    ok_act = ok_trig = 0
    heur_ok_act = 0
    with out.open("w", encoding="utf-8") as fh:
        for i, (mode, ch, prev, utt, exp_act, exp_me) in enumerate(CASES, 1):
            state = jev.build_state(utt, prev, mode, ch)
            r = jev.decide(state, api_key=key)
            s = jev.summarize(r.answers) if r.ok else {}
            h = heuristic.judge(utt, mode=mode, channel=ch)
            trig = jev.should_trigger(s) if r.ok else None
            rec = {"i": i, "mode": mode, "channel": ch, "utterance": utt, "expected": {"act": exp_act, "to_me": exp_me},
                   "ok": r.ok, "error": r.error, "detail": r.detail, "ms": r.ms, "cost_usd": r.cost_usd,
                   "input_tokens": r.input_tokens, "summary": s, "trigger": trig, "heuristic": h}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if r.ok:
                ms_all.append(r.ms)
                cost += r.cost_usd or 0.0
                ok_act += int(s.get("speech_act") == exp_act)
                ok_trig += int(bool(trig) == exp_me)
            heur_ok_act += int(h.get("speech_act") == exp_act)
            mark = "OK " if (r.ok and s.get("speech_act") == exp_act) else "NG "
            print(f"{mark}#{i} {r.ms:7.1f}ms act={s.get('speech_act')}({s.get('speech_act_p')}) to_me={s.get('to_me')} "
                  f"intent={s.get('intent')} trig={trig} | exp={exp_act}/{exp_me} | heur={h.get('speech_act')} :: {utt}"
                  if r.ok else f"ERR #{i} {r.error} {r.detail[:120]} :: {utt}")
    n = len(CASES)
    print("---")
    print(f"jev: ok={len(ms_all)}/{n} act_correct={ok_act}/{len(ms_all)} trigger_correct={ok_trig}/{len(ms_all)} "
          f"latency={percentiles(ms_all)} cost=${cost:.5f}")
    print(f"heuristic: act_correct={heur_ok_act}/{n}")
    print(f"log: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

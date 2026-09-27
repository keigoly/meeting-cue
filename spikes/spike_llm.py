"""Phase 0 (c): OpenRouter 経由 Claude のキュー生成 — 初トークン ms・総 ms・費用・書式の通り具合。

実行: uv run --python 3.12 --no-project python spikes/spike_llm.py [model ...]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meetcue.cues import openrouter, prompts  # noqa: E402
from meetcue.knowledge.index import VaultIndex, query_terms  # noqa: E402
from meetcue.secrets import openrouter_key  # noqa: E402

DB = Path.home() / ".meeting-cue" / "index" / "vault.sqlite"
PROFILE = "IT 企業のエンジニア。ネットワーク機器の設計とサポートを担当。"   # 計測用の例(個人の情報は書かない)
UTT = "この構成でGlobalProtectの分割トンネルはどう扱いますか。YouTubeだけVPNを迂回させたいのですが。"
JUDG = {"speech_act": "question", "intent": "how", "answer_length": "medium", "urgency_label": "high"}


def main() -> int:
    key = openrouter_key()
    models = [a for a in sys.argv[1:] if not a.startswith("-")] or [openrouter.DEFAULT_MODEL, openrouter.FAST_MODEL]
    order = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--order=")]
    order = order[0].split(",") if order else None
    hits = VaultIndex(DB).search(UTT, k=5, terms=query_terms(UTT)) if DB.exists() else []
    msgs = prompts.build_messages(mode="participant", profile=PROFILE, context=["本日はご説明ありがとうございます。"],
                                  utterance=UTT, judgment=JUDG, hits=hits)
    print(f"hits={len(hits)} prompt_chars={sum(len(m['content']) for m in msgs)}")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = Path(__file__).resolve().parent / "logs" / f"llm_{stamp}.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for model in models:
            first_line_at: list[float] = []
            buf: list[str] = []
            t0 = time.perf_counter()

            def on_delta(d: str) -> None:
                buf.append(d)
                if "\n" in d and not first_line_at:
                    first_line_at.append(round((time.perf_counter() - t0) * 1000, 1))

            r = openrouter.stream_chat(msgs, api_key=key, model=model, on_delta=on_delta, provider_order=order)
            parsed = prompts.parse_cue_lines(r.text) if r.ok else {}
            rec = {"model": model, "order": order, "ok": r.ok, "error": r.error, "detail": r.detail, "provider": r.provider,
                   "ms_first_token": r.ms_first_token, "ms_first_line": first_line_at[0] if first_line_at else None,
                   "ms_total": r.ms_total, "in": r.input_tokens, "out": r.output_tokens, "cost_usd": r.cost_usd,
                   "answers": len(parsed.get("answers", [])), "counters": len(parsed.get("counters", [])),
                   "text": r.text}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"\n=== {model} ok={r.ok} err={r.error} first_token={r.ms_first_token}ms first_line={rec['ms_first_line']}ms "
                  f"total={r.ms_total}ms in={r.input_tokens} out={r.output_tokens} cost=${r.cost_usd} provider={r.provider}")
            print(r.text if r.ok else r.detail)
            print(f"parsed: intent={parsed.get('intent','')!r} answers={rec['answers']} counters={rec['counters']}")
    print(f"\nlog: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

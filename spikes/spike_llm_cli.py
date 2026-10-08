"""2026-10-08: Claude サブスク(Claude Code 経由)の回答生成 — 温めたプロセスで初トークン・最初の行・総 ms を比べる。

Haiku 5.5 は Claude Code から思考(adaptive)を切れないため、--effort と依頼文の一文(--hint)の効き方を見る。
実行: uv run --python 3.12 --no-project python spikes/spike_llm_cli.py claude-sonnet-5 claude-haiku-5-5 [--effort=low] [--hint]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meetcue.cues import claude_cli, llm, prompts  # noqa: E402
from meetcue.knowledge.index import VaultIndex, query_terms  # noqa: E402

DB = Path.home() / ".meeting-cue" / "index" / "vault.sqlite"
PROFILE = "IT 企業のエンジニア。ネットワーク機器の設計とサポートを担当。"   # 計測用の例(個人の情報は書かない)
UTT = "この構成でGlobalProtectの分割トンネルはどう扱いますか。YouTubeだけVPNを迂回させたいのですが。"
JUDG = {"speech_act": "question", "intent": "how", "answer_length": "medium", "urgency_label": "high"}


def main() -> int:
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    effort = next((a.split("=", 1)[1] for a in flags if a.startswith("--effort=")), None)
    models = [a for a in sys.argv[1:] if not a.startswith("--")] or ["claude-sonnet-5", "claude-haiku-5-5"]
    hits = VaultIndex(DB).search(UTT, k=5, terms=query_terms(UTT)) if DB.exists() else []
    msgs = prompts.build_messages(mode="participant", profile=PROFILE, context=["本日はご説明ありがとうございます。"],
                                  utterance=UTT, judgment=JUDG, hits=hits)
    if "--hint" in flags:
        msgs = llm.with_fast_hint(msgs)
    cli = claude_cli.find_cli()
    if not cli:
        print("claude が見つかりません", file=sys.stderr)
        return 1
    fast = "--hint" in flags   # 一文を依頼文と起動時の system の両方に入れる(cues/llm.py と同じ)
    pool = claude_cli.Pool(cli, Path.home() / ".meeting-cue" / "claude-cli-cwd", {(m, effort, fast): 1 for m in models})
    pool.ensure()
    time.sleep(12)   # 起動を済ませる(温めた状態で測る)
    for rnd in range(3):
        for m in models:
            first_line: list[int] = []
            t0 = time.perf_counter()

            def on_delta(d: str, t0=t0, fl=first_line) -> None:
                if "\n" in d and not fl:
                    fl.append(round((time.perf_counter() - t0) * 1000))

            r = claude_cli.stream_chat(msgs, pool=pool, model=m, timeout=60, effort=effort, fast=fast, on_delta=on_delta)
            p = prompts.parse_cue_lines(r.text) if r.ok else {}
            print(f"{rnd} {m} effort={effort} hint={'--hint' in flags}: ok={r.ok} err={r.error} warm={r.extra.get('warm')} "
                  f"first_token={r.ms_first_token} first_line={first_line[:1]} total={r.ms_total} "
                  f"answers={len(p.get('answers', []))} counters={len(p.get('counters', []))}", flush=True)
        time.sleep(12)   # 補充を待つ
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

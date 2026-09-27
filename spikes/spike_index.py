"""Phase 0 (b): Vault の FTS5 索引 — 構築時間・サイズ・検索 ms を測る。

実行: uv run --python 3.12 --no-project python spikes/spike_index.py [--full] [--with-youtube]
索引は ~/.meeting-cue/index/vault.sqlite(Vault 本文の複製・repo に入れない)。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from meetcue.knowledge.index import DEFAULT_EXCLUDE, VaultIndex, query_terms  # noqa: E402
from meetcue.metrics import percentiles  # noqa: E402

VAULT = Path(os.environ.get("MEETCUE_VAULT", str(Path.home() / "ObsidianVault")))   # 計測する Vault
DB = Path.home() / ".meeting-cue" / "index" / "vault.sqlite"

QUERIES = [
    "この構成でGlobalProtectの分割トンネルはどう扱いますか",
    "導入にかかる費用はどのくらいでしょうか",
    "既存のSIEMと競合するリスクはありませんか",
    "Jevを判定層に使う狙いは何ですか",
    "Syncthingの同期が止まる原因は",
    "登壇の機会はどこで探していますか",
    "アイカツの同時接続数の監視",
    "プロジェクトの進め方の決まりは",
]


def main() -> int:
    full = "--full" in sys.argv
    exclude = list(DEFAULT_EXCLUDE)
    if "--with-youtube" not in sys.argv:
        exclude.append("03_Resources/YouTube")
    idx = VaultIndex(DB)
    t0 = time.perf_counter()
    stats = idx.build(VAULT, exclude=tuple(exclude), full=full)
    print("build:", json.dumps(stats, ensure_ascii=False), "stats:", idx.stats())
    ms: list[float] = []
    for q in QUERIES:
        terms = query_terms(q)
        t1 = time.perf_counter()
        hits = idx.search(q, k=5, terms=terms)
        dt = round((time.perf_counter() - t1) * 1000, 1)
        ms.append(dt)
        print(f"\n[{dt}ms] {q}\n  terms={terms}")
        for h in hits[:3]:
            print(f"  - {h.path}#{h.heading[:40]} (bm25={h.rank:.2f}) :: {h.snippet(terms, 100)}")
    print("\nsearch latency:", percentiles(ms), "total_build_ms:", round((time.perf_counter() - t0) * 1000))
    return 0


if __name__ == "__main__":
    sys.exit(main())

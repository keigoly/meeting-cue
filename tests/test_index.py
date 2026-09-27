from pathlib import Path

from meetcue.knowledge.index import VaultIndex, chunk_markdown, ngram_text, query_terms


def test_ngram_and_terms():
    assert ngram_text("費用GlobalProtect") == "費用 globalprotect"
    assert ngram_text("分割トンネル") == "分割 割ト トン ンネ ネル"
    t = query_terms("この構成でGlobalProtectの分割トンネルはどう扱いますか")
    assert "分割トンネル" in t and "globalprotect" in t and "構成" in t
    assert all(len(x) >= 2 for x in t)
    assert "どこ" not in query_terms("登壇の機会はどこで探していますか")


def test_chunk_markdown_headings_and_size():
    md = "---\ntitle: x\n---\n# 見出し1\n本文A\n\n## 見出し2\n" + ("段落。" * 300) + "\n\n次の段落。"
    chunks = chunk_markdown(md, chunk_chars=200)
    assert chunks[0] == ("見出し1", "本文A")
    assert all(len(b) <= 200 for _, b in chunks[1:])
    assert all(h == "見出し2" for h, _ in chunks[1:])


def test_index_build_search_incremental(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "01_Projects").mkdir(parents=True)
    (vault / "04_Context" / "Session_Log").mkdir(parents=True)
    (vault / "01_Projects" / "gp.md").write_text("# GlobalProtect\n分割トンネルで YouTube だけ VPN を迂回させる設定。\n", encoding="utf-8")
    (vault / "01_Projects" / "cost.md").write_text("# 見積\n導入の費用は月額で考える。\n", encoding="utf-8")
    (vault / "04_Context" / "Session_Log" / "x.md").write_text("# log\n分割トンネル 分割トンネル\n", encoding="utf-8")
    idx = VaultIndex(tmp_path / "i.sqlite")
    st = idx.build(vault)
    assert st["added"] == 2 and st["files"] == 2  # Session_Log は除外
    hits = idx.search("この構成で分割トンネルはどう扱いますか")
    assert hits and hits[0].path == "01_Projects/gp.md"
    assert idx.search("費用はどのくらいでしょうか")[0].path == "01_Projects/cost.md"
    # 増分: 変更なしなら何も書かない
    st2 = idx.build(vault)
    assert st2["added"] == 0 and st2["updated"] == 0
    # 削除が反映される
    (vault / "01_Projects" / "cost.md").unlink()
    st3 = idx.build(vault)
    assert st3["removed"] == 1 and not idx.search("費用はどのくらいでしょうか")

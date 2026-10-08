"""用語タブ(FR-6e・2026-10-08): 会話に出た専門用語に、短い説明と自分の Obsidian のノートを付けて次々に出す。

生成(openrouter.stream_chat)を差し替え、自動の起動条件(字数・間隔・回答が先)・候補語で索引を先に引いて渡すこと・
語ごとのノート・同じ語は 1 度だけ・索引に無い語は MCP で補うこと・生成なしの縮退・記録と録音後の画面を確かめる。
"""
import asyncio
import json
import time

import meetcue.pipeline as pl
from meetcue import library
from meetcue.config import Config
from meetcue.cues import openrouter, prompts
from meetcue.knowledge.index import Hit, VaultIndex, title_rank
from meetcue.segmenter import Utterance
from meetcue.session import Session

GLOSS = ("用語1: SASE | サシー / Secure Access Service Edge | セキュリティ | ネットワークとセキュリティをクラウドで束ねる考え方。 | 根拠: 03_Resources/SASE.md#SASE\n"
         "用語2: ZTNA | ゼットティーエヌエー | セキュリティ | 利用者と端末を毎回確かめて社内の資源へつなぐ仕組み。 | 根拠: なし\n")


class FakeUI:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.events.append((name, a))


def test_glossary_prompt_parse_and_lookup(tmp_path):
    h = Hit("03_Resources/SASE.md", "SASE", "ネットワークとセキュリティ", -1.0)
    m = prompts.build_glossary_messages(text="相手: SASE を入れます", vault={"SASE": h}, known=["VPN"], n=3, level="大学の語")
    assert "大学の語" in m[0]["content"] and "- SASE → [03_Resources/SASE.md#SASE]" in m[1]["content"]
    assert "VPN" in m[1]["content"] and "最大 3 語" in m[1]["content"]
    it = prompts.parse_glossary_line(GLOSS.splitlines()[0])
    assert it == {"term": "SASE", "reading": "サシー / Secure Access Service Edge", "field": "セキュリティ",
                  "desc": "ネットワークとセキュリティをクラウドで束ねる考え方。", "source": "03_Resources/SASE.md#SASE"}
    assert prompts.parse_glossary_line(GLOSS.splitlines()[1])["source"] == ""          # なし → 空
    assert prompts.parse_glossary_line("回答1: x") is None and prompts.parse_glossary_line("用語1:  | x") is None
    root = tmp_path / "v"
    (root / "03_Resources").mkdir(parents=True)
    (root / "03_Resources" / "SASE.md").write_text("# SASE\nネットワークとセキュリティをクラウドで束ねる。\n", encoding="utf-8")
    (root / "03_Resources" / "雑記.md").write_text("# メモ\n今日は SASE の話を聞いた。\n", encoding="utf-8")
    idx = VaultIndex(tmp_path / "i.sqlite")
    idx.build(root, include=("03_Resources",))
    hits = idx.lookup("SASE", k=2)
    assert [h.path for h in hits] == ["03_Resources/SASE.md", "03_Resources/雑記.md"]     # 題名が語そのもののノートが先
    assert title_rank(hits[0], "sase") == 0 and title_rank(hits[1], "SASE") == 2
    assert idx.lookup("存在しない語") == [] and idx.lookup("") == []
    (root / "03_Resources" / "英語.md").write_text("# memo\nSASEX と CASBS は別の語。\n", encoding="utf-8")
    idx.build(root, include=("03_Resources",))
    assert "03_Resources/英語.md" not in [h.path for h in idx.lookup("SASE", k=5)]           # 英字は単語の区切りで
    assert idx.lookup("CASB") == []


def _fake_stream(calls, text=GLOSS):
    def stream_chat(messages, *, api_key, model, timeout, max_tokens, on_delta=None, should_stop=None, **kw):
        kind = "glossary" if "専門用語" in messages[0]["content"] else "answer"
        calls.append((kind, model, messages[1]["content"]))
        out = text if kind == "glossary" else "意図: x\n回答1: a | b | 根拠: なし\n"
        on_delta and on_delta(out)
        return openrouter.StreamResult(ok=True, text=out, model=model, ms_total=30, ms_first_token=10, cost_usd=0.0004)
    return stream_chat


def _pipeline(tmp_path, privacy="private"):
    cfg = Config(app_dir=tmp_path, privacy=privacy)
    cfg.selector.enabled = False
    root = tmp_path / "vault"
    (root / "03_Resources").mkdir(parents=True)
    (root / "03_Resources" / "SASE.md").write_text("# SASE\nネットワークとセキュリティをクラウドで束ねる考え方。\n", encoding="utf-8")
    VaultIndex(cfg.index_db).build(root, include=("03_Resources",))
    session = Session(tmp_path / "sessions", mode="participant", privacy=privacy, sources=[], model="m")
    return pl.Pipeline(cfg, session, FakeUI(), [], api_key="k", jev_api_key=None), session


def _say(p, text, ch="system"):
    p._gloss_buf.append((ch, text))
    p._convo.append((ch, text))


def test_auto_conditions_and_items_with_notes_and_mcp(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(openrouter, "stream_chat", _fake_stream(calls))
    p, session = _pipeline(tmp_path)
    asked = []

    def mcp_search(terms, k):
        asked.append(terms)
        return [Hit("02_Ideas/ゼロトラスト.md", "", "ZTNA の比較", 0.0, "mcp")]
    p.mcp_search = mcp_search

    async def run():
        _say(p, "短い")
        p._maybe_glossary()
        assert p._gloss_task is None                                      # 字数が足りない
        _say(p, "来期は SASE と ZTNA を入れて、拠点の VPN をやめる方向で検討しています。", "mic")
        p._answer_active = True
        p._maybe_glossary()
        assert p._gloss_task is None                                      # 回答の生成中は始めない
        p._answer_active = False
        p._maybe_glossary()
        await p._gloss_task
        _say(p, "SASE の費用感と、ZTNA の導入の順番について、来週の定例までに資料で教えてください。")
        p._gloss_last = -1e9
        p._maybe_glossary()
        await p._gloss_task
        p._maybe_glossary()
        assert p._gloss_task.done()                                       # 間隔が空いていない
    asyncio.run(run())
    g = [c for c in calls if c[0] == "glossary"]
    assert len(g) == 2 and g[0][1] == "anthropic/claude-haiku-5.5"        # Haiku 5.5
    assert "自分: 来期は SASE" in g[0][2] and "- SASE → [03_Resources/SASE.md#SASE]" in g[0][2]   # 候補語で索引を先に引く
    assert "SASE" not in g[0][2].split("# 既に出した語")[1].split("#")[0]
    items = [a[1] for n, a in p.ui.events if n == "glossary_item"]
    assert [it["term"] for it in items] == ["SASE", "ZTNA"]               # 2 回目は同じ語を出さない
    assert items[0]["notes"][0]["path"] == "03_Resources/SASE.md" and items[1]["notes"] == []
    notes = [a for n, a in p.ui.events if n == "glossary_notes"]
    assert notes and notes[0][1] == "ZTNA" and notes[0][2][0]["source"] == "mcp" and asked == [["ZTNA"]]   # 索引に無ければ MCP
    ph = [json.loads(x) for x in (session.dir / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    done = [x for x in ph if x["phase"] == "glossary"]
    assert done[0]["items"] == 2 and done[0]["with_notes"] == 2 and done[0]["mcp"] == 1 and done[1]["items"] == 0
    session.close()
    d = library.session_detail(tmp_path / "sessions", session.dir.name)
    assert [g["term"] for g in d["glossary"]] == ["SASE", "ZTNA"] and d["glossary"][1]["notes"][0]["path"] == "02_Ideas/ゼロトラスト.md"


def test_manual_waits_for_answer_and_local_falls_back_to_note_titles(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(openrouter, "stream_chat", _fake_stream(calls))
    p, session = _pipeline(tmp_path)
    u = Utterance(rid="q1", channel="system", text="SASE とは何ですか", start_s=0, end_s=1, t_final_ms=int(time.time() * 1000))

    async def run():
        _say(p, "SASE とは何ですか")
        await p._start_generation(u, [], {"speech_act": "question", "intent": "what"}, [])
        p.action("glossary")                                              # 回答中に押す → 回答の後で
        assert p._gloss_pending and p._gloss_task is None
        await asyncio.gather(*p._gen_tasks)
        await asyncio.sleep(0.05)
        assert p._gloss_task is not None
        await p._gloss_task
    asyncio.run(run())
    assert [c[0] for c in calls] == ["answer", "answer", "glossary"]   # 回答・逆質問の 2 本の後で

    local, s2 = _pipeline(tmp_path / "l", privacy="local")              # LOCAL: 生成しない → ノートの題名と一致する語だけ
    local.ui = FakeUI()

    async def run2():
        _say(local, "SASE を入れるかどうか、来月までに決めたいと考えています。よろしくお願いします。")
        local._maybe_glossary()
        await local._gloss_task
    asyncio.run(run2())
    items = [a[1] for n, a in local.ui.events if n == "glossary_item"]
    assert [it["term"] for it in items] == ["SASE"] and items[0]["llm"] is False and items[0]["notes"]
    assert len(calls) == 3                                                # LOCAL は外へ送っていない

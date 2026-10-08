"""深掘りの段(FR-6d・2026-10-08): 回答タブの質問を「少し深く(Sonnet)」「深く(Opus)」で掘り下げる。

生成(openrouter.stream_chat)を差し替え、Vault(索引 + MCP)から多めに引いて渡すこと・行ごとに画面へ出すこと・
(rid, 段)で冪等なこと・次の質問で打ち切られないこと・失敗したら押し直せること・記録と録音後の画面を確かめる。
"""
import asyncio
import json
import time

import meetcue.pipeline as pl
from meetcue import library
from meetcue.config import Config
from meetcue.cues import openrouter, prompts
from meetcue.knowledge.index import Hit, VaultIndex
from meetcue.segmenter import Utterance
from meetcue.session import Session

DIG_TEXT = ("結論: 分割トンネルは除外リストで扱えます。\n"
            "根拠1: 2026-09 の検証で YouTube の除外が通った | 01_Projects/VPN/検証.md#結果\n"
            "回答1: 除外で扱う | ドメインで除外します。 | 根拠: 01_Projects/VPN/検証.md#結果\n"
            "注意: IP は変わりやすい\n"
            "逆質問1: 対象は全員ですか | 範囲\n")


class FakeUI:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.events.append((name, a))


def test_dig_prompt_and_parse():
    hits = [Hit("01_Projects/VPN/検証.md", "結果", "本文" * 800, -1.0)]
    m = prompts.build_dig_messages(level="deep", mode="participant", profile="SE", transcript=["相手: 分割は?"],
                                   utterance="分割トンネルはどう扱いますか", intent="how", hits=hits, body_chars=1200)
    assert "深く" in m[0]["content"] and "横断" in m[0]["content"] and "少し深く" not in m[0]["content"]
    assert "[1] 01_Projects/VPN/検証.md#結果" in m[1]["content"] and "本文" * 600 in m[1]["content"]   # 本文は長めに
    assert "回答3: <見出し>" in m[1]["content"]
    assert "少し深く" in prompts.build_dig_messages(level="mid", mode="presenter", profile="", transcript=[], utterance="u",
                                                  intent=None, hits=[])[0]["content"]
    p = prompts.parse_dig_lines("前置き\n" + DIG_TEXT)
    assert p["conclusion"] == "分割トンネルは除外リストで扱えます。" and p["cautions"] == ["IP は変わりやすい"]
    assert p["evidence"] == [{"index": 1, "text": "2026-09 の検証で YouTube の除外が通った", "source": "01_Projects/VPN/検証.md#結果"}]
    assert p["answers"][0] == {"index": 1, "title": "除外で扱う", "body": "ドメインで除外します。", "source": "01_Projects/VPN/検証.md#結果"}
    assert p["counters"] == [{"index": 1, "question": "対象は全員ですか", "aim": "範囲"}]
    assert prompts.parse_dig_line("意図: x") is None


def _fake_stream(calls, *, fail_first=False, slow=0.0):
    state = {"n": 0}

    def stream_chat(messages, *, api_key, model, timeout, max_tokens, on_delta=None, should_stop=None, **kw):
        sysm = messages[0]["content"]
        kind = "dig" if "掘り下げ" in sysm else "answer"
        calls.append((kind, model, kw.get("reasoning"), max_tokens, messages[1]["content"]))
        if kind == "dig":
            state["n"] += 1
            if fail_first and state["n"] == 1:
                return openrouter.StreamResult(ok=False, error="http_error", rc=500, model=model, ms_total=5)
            t0 = time.monotonic()
            while time.monotonic() - t0 < slow:   # 押してから考えている間に次の質問が来る
                if should_stop and should_stop():
                    return openrouter.StreamResult(ok=False, error="cancelled", model=model, ms_total=5)
                time.sleep(0.01)
            on_delta and on_delta(DIG_TEXT)
            return openrouter.StreamResult(ok=True, text=DIG_TEXT, model=model, ms_total=80, ms_first_token=30)
        text = "意図: 方法\n回答1: 除外 | ドメインで | 根拠: なし\n逆質問1: 範囲は? | 確認\n"
        on_delta and on_delta(text)
        return openrouter.StreamResult(ok=True, text=text, model=model, ms_total=40, ms_first_token=10)
    return stream_chat


def _pipeline(tmp_path, vault=True):
    cfg = Config(app_dir=tmp_path)
    cfg.selector.enabled = False
    if vault:   # 小さな Vault の索引(深掘りは多めに引く)
        root = tmp_path / "vault"
        (root / "01_Projects" / "VPN").mkdir(parents=True)
        (root / "01_Projects" / "VPN" / "検証.md").write_text("# 結果\nGlobalProtect の分割トンネルで YouTube を除外した。\n",
                                                             encoding="utf-8")
        VaultIndex(cfg.index_db).build(root)
    session = Session(tmp_path / "sessions", mode="participant", privacy="private", sources=[], model="m")
    p = pl.Pipeline(cfg, session, FakeUI(), [], api_key="k", jev_api_key=None)
    return p, session


def _question(p, rid="q1", text="GlobalProtect の分割トンネルはどう扱いますか"):
    u = Utterance(rid=rid, channel="system", text=text, start_s=0, end_s=1, t_final_ms=int(time.time() * 1000))
    p._remember(u, ["[system] YouTube だけ VPN を迂回させたい"], {"speech_act": "question", "intent": "how"}, [])
    return u


def _phases(session):
    return [json.loads(line) for line in (session.dir / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]


def test_dig_mid_streams_lines_with_vault_and_mcp_and_records(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(openrouter, "stream_chat", _fake_stream(calls))
    p, session = _pipeline(tmp_path)
    asked = []
    p.mcp_search = lambda terms, k: asked.append((terms, k)) or [
        Hit("02_Ideas/VPN 案.md", "", "MCP で見つけた案", 0.0, "mcp"), Hit("01_Projects/VPN/検証.md", "", "重複", 0.0, "mcp")]
    _question(p)

    async def run():
        p.action("dig", {"rid": "q1", "level": "mid"})
        await asyncio.gather(*p._digs.values())
        p.action("dig", {"rid": "q1", "level": "mid"})   # 冪等: 済んだ後に押しても重ねない
    asyncio.run(run())
    dig = [c for c in calls if c[0] == "dig"]
    assert len(dig) == 1 and dig[0][1] == "anthropic/claude-sonnet-5.5" and dig[0][2] == {"effort": "low"} and dig[0][3] == 4000
    assert "01_Projects/VPN/検証.md#結果" in dig[0][4] and "02_Ideas/VPN 案.md" in dig[0][4]   # 索引 + MCP(重複は除く)
    assert asked and asked[0][1] == p.cfg.dig.top_k_mid and "globalprotect" in asked[0][0]
    names = [e[0] for e in p.ui.events]
    assert names.count("dig_line") == 5 and "dig_start" in names and "dig_done" in names
    start = next(a for n, a in p.ui.events if n == "dig_start")
    assert start[1] == "mid" and [n["source"] for n in start[3]] == ["index", "mcp"]
    assert any("掘り下げ済み" in str(a) for n, a in p.ui.events if n == "status")
    ph = _phases(session)
    r = next(x for x in ph if x["phase"] == "dig_retrieve")
    assert r["local"] == 1 and r["mcp"] == 2 and r["mcp_used"] == 1 and r["mcp_error"] is None
    d = next(x for x in ph if x["phase"] == "dig")
    assert d["ok"] and d["level"] == "mid" and d["answers"] == 1 and d["evidence"] == 1
    cue = [json.loads(x) for x in (session.dir / "cues.jsonl").read_text(encoding="utf-8").splitlines()][-1]
    assert cue["kind"] == "dig" and cue["rid"] == "q1" and cue["conclusion"].startswith("分割トンネル")
    session.close()
    detail = library.session_detail(tmp_path / "sessions", session.dir.name)
    g = detail["cues"]["q1"]["digs"][0]
    assert g["level"] == "mid" and g["ok"] and g["evidence"][0]["source"] == "01_Projects/VPN/検証.md#結果"
    assert [n["source"] for n in g["notes"]] == ["index", "mcp"]


def test_dig_is_not_cancelled_by_next_question_and_mcp_timeout_falls_back(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(openrouter, "stream_chat", _fake_stream(calls, slow=0.4))
    p, session = _pipeline(tmp_path)
    p.cfg.dig.mcp_timeout_s = 0.1

    def slow_mcp(terms, k):
        time.sleep(0.5)
        return []
    p.mcp_search = slow_mcp
    u1 = _question(p)
    u2 = Utterance(rid="q2", channel="system", text="費用はいくらですか", start_s=0, end_s=1, t_final_ms=int(time.time() * 1000))

    async def run():
        p.action("dig", {"rid": u1.rid, "level": "deep"})
        await asyncio.sleep(0.25)
        await p._start_generation(u2, [], {"speech_act": "question", "intent": "cost"}, [])   # 次の質問(回答は打ち切り対象)
        await asyncio.gather(*p._gen_tasks, *p._digs.values())
    asyncio.run(run())
    ph = _phases(session)
    d = next(x for x in ph if x["phase"] == "dig")
    assert d["ok"] and d["level"] == "deep" and d["model"] == "anthropic/claude-opus-5.5"   # 次の質問で打ち切られない
    assert next(x for x in ph if x["phase"] == "dig_retrieve")["mcp_error"] == "timeout"   # MCP が遅ければ索引だけで
    assert next(c for c in calls if c[0] == "dig")[2] == {"effort": "medium"}


def test_dig_failure_can_be_retried_and_guards(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(openrouter, "stream_chat", _fake_stream(calls, fail_first=True))
    p, session = _pipeline(tmp_path, vault=False)
    _question(p)

    async def run():
        p.action("dig", {"rid": "q1", "level": "mid"})
        await asyncio.gather(*p._digs.values())
        assert ("q1", "mid") not in p._digs                # 失敗したら押し直せる
        p.action("dig", {"rid": "q1", "level": "mid"})
        await asyncio.gather(*p._digs.values())
        p.action("dig", {"rid": "nope", "level": "mid"})   # 知らない質問
        p.action("dig", {"rid": "q1", "level": "max"})     # 知らない段
    asyncio.run(run())
    assert [x["ok"] for x in _phases(session) if x["phase"] == "dig"] == [False, True]
    assert sum(1 for n, a in p.ui.events if n == "status" and "見つかりません" in str(a)) == 2
    off = pl.Pipeline(Config(app_dir=tmp_path, privacy="local"), session, FakeUI(), [], api_key="k")   # LOCAL は生成しない
    _question(off)
    off.action("dig", {"rid": "q1", "level": "mid"})
    assert not off._digs and any("生成が止まっている" in str(a) for n, a in off.ui.events if n == "status")

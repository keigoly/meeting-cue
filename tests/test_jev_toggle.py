"""設定で「質問を判定する(Jev)」をオフにすると、判定も生成も起きない(文字起こしと記録だけ・2026-09-26)。"""
import asyncio
import json
import time
from types import SimpleNamespace

import meetcue.pipeline as pl
from meetcue.config import Config
from meetcue.judge import jev
from meetcue.segmenter import Utterance
from meetcue.session import Session


class FakeUI:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.events.append((name, a))


def _run(tmp_path, monkeypatch, *, jev_enabled):
    called = []
    fail = SimpleNamespace(ok=False, error="offline", rc=1, detail="", ms=1.0, cost_usd=None)   # 失敗 → ヒューリスティックへ
    monkeypatch.setattr(jev, "decide", lambda *a, **kw: called.append("jev") or fail)
    cfg = Config(app_dir=tmp_path)
    session = Session(tmp_path / "sessions", mode="participant", privacy="private", sources=[], model="m")
    ui = FakeUI()
    p = pl.Pipeline(cfg, session, ui, [], api_key="k", llm_enabled=False, jev_enabled=jev_enabled)
    u = Utterance(rid="q1", channel="system", text="導入の費用はどのくらいですか", start_s=0, end_s=1,
                  t_final_ms=int(time.time() * 1000))
    asyncio.run(p._handle(u))
    rows = [json.loads(x) for x in (session.dir / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    return p, ui, called, [r["phase"] for r in rows]


def test_jev_off_skips_detection(tmp_path, monkeypatch):
    p, ui, called, phases = _run(tmp_path, monkeypatch, jev_enabled=False)
    assert p.jev_key is None and called == []
    assert "judge_skipped" in phases and "judge" not in phases
    names = [e[0] for e in ui.events]
    assert "utterance" in names and "judgment" not in names        # 文字起こしは出す・判定は出さない
    assert (p.session.dir / "transcript.jsonl").exists()


def test_jev_on_still_judges(tmp_path, monkeypatch):
    p, ui, called, phases = _run(tmp_path, monkeypatch, jev_enabled=True)
    assert p.jev_key == "k" and called == ["jev"]                   # Jev を呼ぶ(ここでは失敗させてヒューリスティックへ)
    assert "judge" in phases and "judge_skipped" not in phases


def test_generation_target_and_jev_key_are_separate(tmp_path):
    from meetcue.cues import llm
    cfg = Config(app_dir=tmp_path)
    session = Session(tmp_path / "sessions", mode="participant", privacy="private", sources=[], model="m")
    t = llm.target_for(cfg, "anthropic", "gen-key", {})
    p = pl.Pipeline(cfg, session, FakeUI(), [], api_key="gen-key", target=t, jev_api_key="openrouter-key")
    assert p.target.provider == "anthropic" and p.target.api_key == "gen-key" and p.jev_key == "openrouter-key"
    assert p.model == "claude-sonnet-5" and p.llm_enabled
    local = Config(app_dir=tmp_path, privacy="local")                     # LOCAL はどちらのキーも持たない
    s2 = Session(tmp_path / "s2", mode="participant", privacy="local", sources=[], model="m")
    p2 = pl.Pipeline(local, s2, FakeUI(), [], api_key="gen-key", target=llm.target_for(local, "anthropic", "gen-key", {}),
                     jev_api_key="openrouter-key")
    assert p2.target.api_key is None and p2.jev_key is None and not p2.llm_enabled

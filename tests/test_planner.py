"""質問タブ(FR-6c・U3)と優先制御(U4): 相手からの質問への回答が常に先。

生成(openrouter.stream_chat)を差し替え、質問タブが考えている最中に回答の生成が始まったら打ち切られること、
回答の後の冷却、回答中に押された手動の依頼が回答の後に実行されることを確かめる。
"""
import asyncio
import json
import time

import meetcue.pipeline as pl
from meetcue.config import Config
from meetcue.cues import openrouter, prompts
from meetcue.segmenter import Utterance
from meetcue.session import Session


def test_plan_prompt_and_parse():
    msgs = prompts.build_plan_messages(mode="participant", profile="SE", transcript=["相手: 費用の話です", "自分: はい"],
                                       hits=[], n=3)
    assert "先回り" in msgs[0]["content"] and "深く" not in msgs[0]["content"]
    assert "相手: 費用の話です" in msgs[1]["content"] and "質問3: <質問> | <ねらい> | <頃合い>" in msgs[1]["content"]
    assert "見落とされている前提" in prompts.build_plan_messages(mode="audience", profile="", transcript=[], hits=[],
                                                              deep=True)[0]["content"]
    items = prompts.parse_plan_lines("前置き\n質問1: 期限はいつですか | 次の一手 | 今すぐ\n質問2: 対象は? | 範囲")
    assert items == [{"index": 1, "question": "期限はいつですか", "aim": "次の一手", "timing": "今すぐ"},
                     {"index": 2, "question": "対象は?", "aim": "範囲", "timing": ""}]


class FakeUI:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.events.append((name, a))


def _fake_stream(calls):
    def stream_chat(messages, *, api_key, model, timeout, max_tokens, on_delta=None, should_stop=None,
                    provider_order=None, **kw):
        is_plan = "先回り" in messages[0]["content"]
        calls.append(("plan" if is_plan else "answer", model))
        if is_plan:   # 考え始めて 1 行出したら、打ち切られるまで(最大 3 s)粘る
            text = "質問1: 期限はいつですか | 次の一手 | 今\n"
            on_delta and on_delta(text)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 3:
                if should_stop and should_stop():
                    return openrouter.StreamResult(ok=False, text=text, error="cancelled", model=model, ms_total=10)
                time.sleep(0.01)
            return openrouter.StreamResult(ok=True, text=text, model=model, ms_total=3000)
        text = "意図: 費用\n回答1: 見積り | 初期は約 100 万円です | 根拠: なし\n逆質問1: 対象は何名ですか | 範囲\n"
        on_delta and on_delta(text)
        return openrouter.StreamResult(ok=True, text=text, model=model, ms_total=50, ms_first_token=20)
    return stream_chat


def _pipeline(tmp_path):
    cfg = Config(app_dir=tmp_path)
    cfg.selector.enabled = False             # 採点(Jev)は呼ばない
    cfg.planner.cooldown_s = 0.3
    session = Session(tmp_path / "sessions", mode="participant", privacy="private", sources=[], model="m")
    p = pl.Pipeline(cfg, session, FakeUI(), [], api_key="k")
    p._convo.extend([("system", "今日は来期の体制とセキュリティ監視の見直しについてご相談したいと思っています。" * 3),
                     ("mic", "承知しました。現状の構成から伺えますか。")])
    p._plan_chars = 200
    return p, session


def _phases(session):
    return [json.loads(line) for line in (session.dir / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]


def test_question_preempts_planner_and_cooldown(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(openrouter, "stream_chat", _fake_stream(calls))
    p, session = _pipeline(tmp_path)
    u = Utterance(rid="q1", channel="system", text="導入の費用はどのくらいですか", start_s=0, end_s=1,
                  t_final_ms=int(time.time() * 1000))

    async def run():
        p._maybe_plan()                                  # 自動で考え始める
        assert p._plan_task and not p._plan_task.done()
        await asyncio.sleep(0.1)
        await p._start_generation(u, [], {"speech_act": "question", "intent": "cost"}, [])   # 相手の質問 → 最優先
        await asyncio.wait_for(p._plan_task, 1)          # 質問タブはすぐ打ち切られる
        await asyncio.gather(*p._gen_tasks)
        await asyncio.sleep(0.05)
        assert not p._answer_active and p._plan_block_until > time.monotonic()
        p._plan_chars, p._plan_last = 500, -1e9
        p._maybe_plan()                                  # 冷却中は自動で再開しない
        assert p._plan_task.done()
        await asyncio.sleep(0.35)
        p._maybe_plan()                                  # 冷却が明けたら再開
        assert not p._plan_task.done()
        p._plan_stop.set()
        await p._plan_task
    asyncio.run(run())
    ph = _phases(session)
    assert [r["phase"] for r in ph if r["phase"] in ("plan_preempt",)] == ["plan_preempt"]
    plans = [r for r in ph if r["phase"] == "plan"]
    assert plans[0]["error"] == "cancelled" and plans[0]["trigger"] == "auto"
    assert sum(1 for r in ph if r["phase"] == "generate" and r["ok"]) == 2   # 回答・逆質問の 2 本は最後まで
    cues = [json.loads(line) for line in (session.dir / "cues.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(c["kind"] == "plan" and c["error"] == "cancelled" and c["items"][0]["question"] == "期限はいつですか"
               for c in cues)
    assert [c for c in calls if c[0] == "plan"][0][1] == "anthropic/claude-sonnet-5"   # 自動は Sonnet


def test_manual_request_waits_for_answers_then_runs_deep_on_opus(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(openrouter, "stream_chat", _fake_stream(calls))
    p, session = _pipeline(tmp_path)
    u = Utterance(rid="q2", channel="system", text="リスクはありませんか", start_s=0, end_s=1,
                  t_final_ms=int(time.time() * 1000))

    async def run():
        await p._start_generation(u, [], {"speech_act": "question", "intent": "risk"}, [])
        p.action("plan_deep")                            # 回答中に「深く考える」→ 回答の後で
        assert p._plan_pending == "deep" and p._plan_task is None
        await asyncio.gather(*p._gen_tasks)
        await asyncio.sleep(0.05)
        assert p._plan_task and not p._plan_task.done() and p._plan_pending is None
        p._plan_stop.set()
        await p._plan_task
        p.paused = True
        p._plan_chars, p._plan_last = 500, -1e9
        p._maybe_plan()                                  # 一時停止中は自動で考えない
        assert p._plan_task.done()
        p.action("plan")                                 # 手動は一時停止中でも効く
        assert not p._plan_task.done()
        p._plan_stop.set()
        await p._plan_task
    asyncio.run(run())
    models = [m for kind, m in calls if kind == "plan"]
    assert models == ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5"]
    assert any("回答を優先しています" in str(a) for name, a in p.ui.events if name == "status")


def test_openrouter_body_reasoning(monkeypatch):
    """既定は思考なし + temperature。Opus 5.5 向けに reasoning を渡すと temperature を送らない(思考は切れない)。"""
    import urllib.request
    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data))
        raise OSError("stop")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    openrouter.stream_chat([{"role": "user", "content": "x"}], api_key="k", model="anthropic/claude-sonnet-5")
    openrouter.stream_chat([{"role": "user", "content": "x"}], api_key="k", model="anthropic/claude-opus-5.5",
                           reasoning={"effort": "medium"})
    assert bodies[0]["reasoning"] == {"enabled": False} and "temperature" in bodies[0]
    assert bodies[1]["reasoning"] == {"effort": "medium"} and "temperature" not in bodies[1]

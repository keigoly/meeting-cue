"""停止(2026-09-26 keigoly様 選択): 停止を押したらすぐ「録音中」を外し、作りかけの回答は裏で最後まで仕上げる。

STT ヘルパーと区切り処理を差し替え、生成に時間がかかっている最中に停止したとき、
(1) 生成が終わる前に recording=False / saving=True が画面へ出ること、(2) 生成は打ち切らずに最後まで終わること、
(3) 停止の内訳(metrics の stop_requested / stop)が残ることを確かめる。
"""
import asyncio
import json
import sys
import time

import meetcue.pipeline as pl
from meetcue.config import Config
from meetcue.cues import openrouter
from meetcue.segmenter import Utterance
from meetcue.session import Session


class FakeUI:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.events.append((name, kw, time.monotonic()))


class FakeHelper:
    def __init__(self, argv, channel, on_diag=None):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass


class FakeSegmenter:
    def __init__(self, *a, **kw):
        pass

    async def run(self, out):
        await asyncio.Event().wait()   # 停止で cancel されるまで


class FakeSource:   # Source と同じ形(argv[0] は存在するパス)
    kind, channel, arg = "file", "system", ""

    def argv(self, cfg):
        return [sys.executable]


def test_stop_releases_recording_before_answers_finish(tmp_path, monkeypatch):
    ends = []

    def stream_chat(messages, *, api_key, model, timeout, max_tokens, on_delta=None, should_stop=None, **kw):
        time.sleep(0.5)   # 生成に時間がかかる(停止で打ち切らないこと)
        text = "意図: 費用\n回答1: 見積り | 初期は約 100 万円です | 根拠: なし\n逆質問1: 対象は何名ですか | 範囲\n"
        on_delta and on_delta(text)
        ends.append(time.monotonic())
        return openrouter.StreamResult(ok=True, text=text, model=model, ms_total=500, ms_first_token=20)

    monkeypatch.setattr(openrouter, "stream_chat", stream_chat)
    monkeypatch.setattr(pl, "STTHelper", FakeHelper)
    monkeypatch.setattr(pl, "Segmenter", FakeSegmenter)
    cfg = Config(app_dir=tmp_path)
    cfg.selector.enabled = False   # 採点(Jev)は呼ばない
    cfg.planner.enabled = False
    session = Session(tmp_path / "sessions", mode="participant", privacy="private", sources=[], model="m")
    p = pl.Pipeline(cfg, session, FakeUI(), [FakeSource()], api_key="k")
    p.hotkeys = False
    u = Utterance(rid="q1", channel="system", text="導入の費用はどのくらいですか", start_s=0, end_s=1,
                  t_final_ms=int(time.time() * 1000))

    async def run():
        task = asyncio.create_task(p.run(install_signals=False))
        await asyncio.sleep(0.1)
        await p._start_generation(u, [], {"speech_act": "question", "intent": "cost"}, [])
        await asyncio.sleep(0.05)
        p.request_stop()
        await asyncio.wait_for(task, 5)
    asyncio.run(run())

    saving = [t for name, kw, t in p.ui.events if name == "set_state" and kw.get("recording") is False
              and kw.get("saving") is True]
    assert saving and len(ends) == 2 and saving[0] < min(ends)   # 生成が終わる前に「録音中」を外した
    ph = [json.loads(line) for line in (session.dir / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for r in ph if r["phase"] == "generate" and r["ok"]) == 2   # 回答・逆質問は最後まで
    assert [r["phase"] for r in ph if r["phase"] in ("stop_requested", "stop")] == ["stop_requested", "stop"]
    stop = next(r for r in ph if r["phase"] == "stop")
    assert stop["pending"] == 2 and stop["timed_out"] is False and stop["gen_after_stop"] == 0
    assert stop["gen_wait_ms"] >= 200 and stop["ms"] >= stop["gen_wait_ms"]

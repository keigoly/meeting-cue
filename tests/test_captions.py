"""ライブ字幕(2026-09-27): 録音していない間の字幕だけの動き・画面の借用(合図が途絶えたら止める)・録音への引き継ぎ・翻訳。

STT ヘルパーと生成 AI は差し替える(本物のマイク・タップ・ネットワークには触れない)。
"""
import asyncio
import sys
import time

import meetcue.captions as cap
from meetcue import app as app_mod
from meetcue.app import App
from meetcue.config import Config
from meetcue.cues import llm, openrouter
from meetcue.metrics import Metrics
from meetcue.segmenter import Utterance


class FakeWeb:
    def __init__(self):
        self.events, self.state = [], {}

    def push(self, ev, keep=True):
        self.events.append(ev)

    def set_state(self, **kv):
        self.state.update(kv)


class FakeHelper:
    started = []

    def __init__(self, argv, channel, on_diag=None):
        self.argv, self.channel = argv, channel

    async def start(self):
        FakeHelper.started.append((self.channel, self.argv))

    async def stop(self):
        pass


class FakeSegmenter:
    def __init__(self, helper, channel, cfg, m, on_partial=None):
        self.channel, self.on_partial = channel, on_partial

    async def run(self, out):
        self.on_partial(self.channel, "こんに")
        await out.put(Utterance(rid="r1", channel=self.channel, text="こんにちは。", start_s=0, end_s=1,
                                t_final_ms=int(time.time() * 1000)))
        await asyncio.Event().wait()


class FakeSource:
    def __init__(self, kind, channel):
        self.kind, self.channel, self.arg = kind, channel, ""

    def argv(self, cfg):
        return [sys.executable, "--locale", cfg.locale, "--channel", self.channel]


def test_runner_streams_captions_without_saving(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "STTHelper", FakeHelper)
    monkeypatch.setattr(cap, "Segmenter", FakeSegmenter)
    FakeHelper.started = []
    web, finals = FakeWeb(), []
    cfg = Config(app_dir=tmp_path)
    r = cap.CaptionRunner(cfg, web, [FakeSource("mic", "mic"), FakeSource("tap-all", "system")],
                          on_final=lambda *a: finals.append(a), metrics=Metrics(tmp_path / "m.jsonl"))

    async def run():
        await r.start({"system"}, "en-US")          # 選んだ音源だけ・選んだ言語で
        await asyncio.sleep(0.05)
        assert r.running and r.key == (("system",), "en-US")
        await r.stop()
    asyncio.run(run())
    assert [c for c, _ in FakeHelper.started] == ["system"] and "en-US" in FakeHelper.started[0][1]
    assert {"type": "caption", "phase": "partial", "channel": "system", "text": "こんに"} in web.events
    assert any(e.get("phase") == "final" and e["text"] == "こんにちは。" for e in web.events)
    assert finals == [("r1", "system", "こんにちは。")]
    assert web.state["caption"] is False and not r.running
    assert not (tmp_path / "sessions").exists()     # 保存しない


class FakeRunner:
    def __init__(self):
        self.key, self.calls = None, []

    @property
    def running(self):
        return self.key is not None

    async def start(self, channels, locale):
        self.key = (tuple(sorted(channels)), locale)
        self.calls.append(("start", self.key))

    async def stop(self):
        if self.key:
            self.calls.append(("stop", self.key))
        self.key = None


def test_lease_starts_and_expires_and_recording_takes_over(tmp_path):
    a = App(Config(app_dir=tmp_path), sources=[], window=False)
    a._caption = FakeRunner()

    async def run():
        a._caption_request({"on": True, "channels": ["system", "bogus"], "locale": "xx", "translate": "en"})
        await asyncio.sleep(0.01)
        assert a._caption.key == (("system",), "ja-JP") and a._translator.lang == "en"   # 知らない値は既定へ
        a._task = asyncio.get_running_loop().create_future()                               # 録音中: 録音側に任せる
        await a._caption_sync()
        assert a._caption.key is None
        a._task.set_result(None)
        await a._caption_sync()                                                            # 録音が終わったら戻す
        assert a._caption.key == (("system",), "ja-JP")
        a._caption_want["until"] = time.monotonic() - 1                                    # 合図が途絶えた = 閉じた
        await a._caption_sync()
        assert a._caption.key is None and a._translator.lang == ""
    asyncio.run(run())


def test_translator_uses_target_and_respects_local(tmp_path, monkeypatch):
    calls = []

    def fake_stream(target, msgs, **kw):
        calls.append(msgs)
        return openrouter.StreamResult(ok=True, text="Hello.", model="m", ms_total=12, ms_first_token=5)
    monkeypatch.setattr(llm, "stream", fake_stream)
    web = FakeWeb()
    class Target:
        provider = "claude-cli"
    state = {"target": Target(), "why": ""}
    t = cap.Translator(web, lambda: (state["target"], state["why"]), metrics=Metrics(tmp_path / "m.jsonl"))

    async def run():
        t.submit("r0", "system", "前の行")          # 訳さない設定: 文脈にだけ積む
        t.lang = "en"
        t.submit("r1", "system", "こんにちは。")
        await asyncio.sleep(0.1)
        state["target"], state["why"] = None, "LOCAL の録音中は翻訳しません(外へ送りません)"
        t.submit("r2", "system", "機密です。")
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert len(calls) == 1 and "LAST line:\nこんにちは。" in calls[0][1]["content"] and "- 前の行" in calls[0][1]["content"]
    assert "English" in calls[0][0]["content"]
    tr = [e for e in web.events if e["type"] == "caption_tr"]
    assert tr[0]["rid"] == "r1" and tr[0]["text"] == "Hello."
    assert tr[1] == {"type": "caption_tr", "rid": "r2", "text": "", "error": "LOCAL の録音中は翻訳しません(外へ送りません)"}


def test_translator_reports_exceptions(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("down")
    monkeypatch.setattr(llm, "stream", boom)
    web = FakeWeb()

    class Target:
        provider = "openrouter"
    t = cap.Translator(web, lambda: (Target(), ""), metrics=Metrics(tmp_path / "m.jsonl"))
    t.lang = "ja"

    async def run():
        t.submit("r1", "system", "Hello.")
        await asyncio.sleep(0.1)
        t.submit("r2", "system", "Again.")              # 失敗の後も詰まらない(同時の数が戻る)
        await asyncio.sleep(0.1)
    asyncio.run(run())
    errs = [e for e in web.events if e["type"] == "caption_tr"]
    assert [e["rid"] for e in errs] == ["r1", "r2"] and all("RuntimeError" in e["error"] for e in errs)


def test_translate_target_refuses_local_and_ai_off(tmp_path, monkeypatch):
    a = App(Config(app_dir=tmp_path), sources=[], window=False)

    class S:
        meta = {"privacy": "local"}
    a.session = S()
    assert a._translate_target()[0] is None and "LOCAL" in a._translate_target()[1]
    a.session = None
    a._ui_path.write_text('{"generate": false}', encoding="utf-8")
    assert a._translate_target() == (None, "AI の生成をオフにしているため翻訳しません(⚙)")


def test_version_reports_commits(tmp_path, monkeypatch):
    answers = {("rev-parse", "HEAD"): "b" * 40, ("cat-file", "-t", "a" * 40): "commit",
               ("log", "--format=%h %s", "a" * 40 + ".." + "b" * 40): "bbbbbbb 新しい機能",
               ("diff", "--name-only", "a" * 40 + ".." + "b" * 40): "helpers/macos/overlay_helper/main.swift\nmeetcue/app.py",
               ("log", "--format=%h%x1f%s%x1f%b%x1e", "a" * 40 + ".." + "b" * 40):
                   "bbbbbbb\x1f新しい機能\x1f- 説明の 1 行目\n- 2 行目\n\nCo-Authored-By: x\n\x1e\n"}
    monkeypatch.setattr(app_mod, "_git", lambda *args: answers.get(args, ""))
    a = App(Config(app_dir=tmp_path), sources=[], window=False)
    a._boot_commit = "a" * 40
    v = a._version("a" * 40)
    assert v["behind"] == 1 and v["subjects"] == ["bbbbbbb 新しい機能"] and v["rebuild"] is True and v["head"] == "bbbbbbb"
    assert v["notes"] == [{"hash": "bbbbbbb", "subject": "新しい機能", "detail": "説明の 1 行目"}]


def test_real_webui_accepts_caption_events_without_history():
    """2026-09-27 実機で見つけた不具合: WebUI.push が keep を受けず、最初の partial で区切りの処理が止まっていた。"""
    from meetcue.ui.web import WebUI
    w = WebUI(port=0)
    w.push({"type": "caption", "phase": "partial", "channel": "system", "text": "こ"}, keep=False)
    w.push({"type": "session_started", "id": "x"})
    assert [e["type"] for e in w._history] == ["session_started"]


def test_runner_reports_a_crashed_task(tmp_path, monkeypatch):
    class BadSegmenter(FakeSegmenter):
        async def run(self, out):
            raise TypeError("boom")
    monkeypatch.setattr(cap, "STTHelper", FakeHelper)
    monkeypatch.setattr(cap, "Segmenter", BadSegmenter)
    web = FakeWeb()
    r = cap.CaptionRunner(Config(app_dir=tmp_path), web, [FakeSource("tap-all", "system")],
                          metrics=Metrics(tmp_path / "m.jsonl"))

    async def run():
        await r.start({"system"}, "ja-JP")
        await asyncio.sleep(0.05)
        await r.stop()
    asyncio.run(run())
    assert "字幕の処理が止まりました: TypeError: boom" in web.state["caption_error"]

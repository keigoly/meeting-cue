"""会議の文字起こしを Soniox で(FR-2b・2026-10-18): STT ヘルパーの契約の顔をした包み(meetcue.soniox_stt)。

ヘルパーと Soniox の接続をどちらも手元の偽物に差し替え、(1) 語が契約の partial / final になること(時刻・finalize との対応)、
(2) Soniox が動いている間は端末内の認識を見せないこと、(3) 終わり(bye)で最後の確定を待つこと、(4) 切れたら出し終えた
final の終わりから送り直すこと、(5) 切れている間の finalize で届いていた語を final にすること、(6) 直らない誤り・鍵なしで
端末内に切り替え、出せていない区間を端末内の final で補うこと、(7) 詰まりを見つけてつなぎ直すこと、
(8) パイプラインが選んだときだけ --pcm-out を付けて包みを使うこと、を確かめる。
"""
import asyncio
import base64
import sys
import threading
import time

import meetcue.soniox_stt as sx
from meetcue.soniox_stt import SonioxSTT, tidy

SR = 16000


class FakeInner:
    """STTHelper の代わり。テストが q にイベントを積む。"""

    def __init__(self, argv, channel, on_diag=None):
        self.argv, self.channel = argv, channel
        self.q: asyncio.Queue = asyncio.Queue()
        self.cmds: list[str] = []
        self.ready = None
        self.exited = False
        self.recording = False

    async def start(self):
        pass

    async def events(self):
        while True:
            ev = await self.q.get()
            if ev is None:
                return
            yield ev

    async def finalize(self):
        self.cmds.append("finalize")

    async def send(self, cmd):
        self.cmds.append(cmd)

    async def stop(self):
        self.cmds.append("quit")
        await self.q.put(None)

    async def wait(self, timeout):
        return 0


class FakeSession:
    """soniox.Session の代わり。start の結果を決められ、テストが語と終わりを送り込む。"""

    def __init__(self, ok=True, code=None, **kw):
        self.kw = kw
        self.ok, self.code = ok, code
        self.final_tokens, self.partial_tokens, self.marks = [], [], []
        self.proc_ms = 0
        self.t_open = self.t_last_msg = None
        self.error = None
        self.error_code = None
        self.lock = threading.Lock()
        self.audio = bytearray()
        self.finalized = 0
        self.keepalives = 0
        self.closed = False

    def start(self):
        if not self.ok:
            self.error, self.error_code = f"connect: HTTP {self.code}", self.code
            return False
        self.t_open = self.t_last_msg = time.monotonic()
        return True

    def send_audio(self, pcm):
        self.audio += pcm
        return True

    def finalize(self):
        self.finalized += 1

    def keepalive(self):
        self.keepalives += 1

    def close(self, timeout=5.0):
        self.closed = True

    # テストから
    def push(self, final=(), partial=(), mark=None):
        with self.lock:
            for text, s, e in final:
                self.final_tokens.append({"text": text, "start_ms": s, "end_ms": e})
            if mark:
                self.marks.append((mark, len(self.final_tokens)))
            self.partial_tokens = [{"text": t, "start_ms": s, "end_ms": e} for t, s, e in partial]
        self.t_last_msg = time.monotonic()
        self.kw["on_update"](self)

    def die(self, code=None, error="closed"):
        self.error, self.error_code = error, code
        self.kw["on_done"](self)


class Factory:
    def __init__(self, *results):
        self.results = list(results)   # (ok, code) を順に。尽きたら成功
        self.made: list[FakeSession] = []

    def __call__(self, **kw):
        ok, code = self.results.pop(0) if self.results else (True, None)
        s = FakeSession(ok, code, **kw)
        self.made.append(s)
        return s


class Metrics:
    def __init__(self):
        self.rows = []

    def emit(self, rid, phase, **kw):
        self.rows.append({"rid": rid, "phase": phase, **kw})

    def phases(self):
        return [r["phase"] for r in self.rows]


class UI:
    def __init__(self):
        self.msgs = []

    def status(self, msg, code=None):
        self.msgs.append(("status", msg))

    def error(self, msg, code=None):
        self.msgs.append(("error", msg))


def audio(seconds: float) -> dict:
    return {"type": "audio", "pcm": base64.b64encode(b"\x00\x00" * int(SR * seconds)).decode()}


class Run:
    """本番のセグメンターと同じく、events() を裏で回し続けて届いたイベントをためる。"""

    def __init__(self, w):
        self.got: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self._consume(w))

    async def _consume(self, w):
        async for ev in w.events():
            await self.got.put(ev)


async def pull(it, n: int, timeout: float = 2.0) -> list[dict]:
    return [await asyncio.wait_for(it.got.get(), timeout) for _ in range(n)]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


def make(monkeypatch, factory, key="k", **kw):
    monkeypatch.setattr(sx, "STTHelper", FakeInner)
    engines = []
    w = SonioxSTT(["helper", "--pcm-out"], channel="system", api_key=key, metrics=Metrics(), ui=UI(),
                  session_factory=factory, on_engine=lambda *a: engines.append(a), **kw)
    return w, engines


def test_tidy_removes_spaces_around_japanese_punctuation_only():
    assert tidy("確認します。 よろしくお願いします。") == "確認します。よろしくお願いします。"
    assert tidy("Claude Code を 使う 、 はい") == "Claude Code を 使う、はい"


def test_tokens_become_partial_and_final_with_times(monkeypatch):
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR, "engine": "faster-whisper"})
        (ready,) = await pull(it, 1)
        assert ready["engine"] == "soniox" and ready["local_engine"] == "faster-whisper"
        await settle()
        sess = f.made[0]
        assert sess.kw["sample_rate"] == SR and sess.kw["language_hints"] == ["ja", "en"]
        await w.inner.q.put(audio(1.0))
        await settle()
        assert len(sess.audio) == SR * 2
        sess.push(final=[("本日は", 100, 600)], partial=[("お時間", 700, 900)])
        (p,) = await pull(it, 1)
        assert p["type"] == "partial" and p["text"] == "本日はお時間" and (p["start_s"], p["end_s"]) == (0.1, 0.9)
        await w.finalize()
        assert sess.finalized == 1 and w.inner.cmds == ["finalize"]   # 端末内の認識にも区切りを送る
        sess.push(final=[("お時間を", 700, 1000), ("いただき。", 1000, 1400)], mark="fin")
        (fin,) = await pull(it, 1)
        assert fin["type"] == "final" and fin["text"] == "本日はお時間をいただき。"
        assert (fin["start_s"], fin["end_s"]) == (0.1, 1.4) and fin["how"] == "fin"
        assert "soniox_finalize" in w.m.phases()
        await w.finalize()
        sess.push(mark="fin")   # 待たれている確定は、何も残っていなくても空の final を返す(セグメンターの待ちを終わらせる)
        (empty,) = await pull(it, 1)
        assert empty["type"] == "final" and empty["text"] == ""
        await w.stop()
        done = [r for r in w.m.rows if r["phase"] == "soniox_done"][0]
        assert done["engine"] == "soniox" and done["sent_s"] == 1.0 and done["sessions"] == 1 and "usd" in done
    asyncio.run(main())


def test_local_events_hidden_while_soniox_is_live(monkeypatch):
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        await w.inner.q.put({"type": "partial", "text": "端末内", "start_s": 0, "end_s": 1})
        await w.inner.q.put({"type": "final", "text": "端末内", "start_s": 0, "end_s": 1})
        f.made[0].push(partial=[("Soniox", 0, 500)])
        (ev,) = await pull(it, 1)
        assert ev["text"] == "Soniox" and ev["engine"] == "soniox"
        await w.stop()
    asyncio.run(main())


def test_bye_waits_for_the_last_fin(monkeypatch):
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        sess = f.made[0]
        sess.push(partial=[("最後の", 0, 400)])
        await pull(it, 1)
        await w.inner.q.put({"type": "bye"})
        await settle()
        assert sess.finalized == 1                       # 終わる前に最後の語を確定させる
        sess.push(final=[("最後の文。", 0, 800)], mark="fin")
        fin, bye = await pull(it, 2)
        assert fin["text"] == "最後の文。" and bye["type"] == "bye"
        await w.stop()
    asyncio.run(main())


def test_reconnect_resends_from_the_end_of_the_last_final(monkeypatch):
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        first = f.made[0]
        await w.inner.q.put(audio(1.0))
        await settle()
        await w.finalize()
        first.push(final=[("一文目。", 0, 1000)], mark="fin")
        await pull(it, 1)
        await w.inner.q.put(audio(2.0))
        await settle()
        first.push(partial=[("二文目", 1100, 2500)])
        await pull(it, 1)
        w.HEALTHY_S, first.proc_ms = 0.0, 3000           # しばらく働いていた接続
        first.die(code=None, error="Connection reset")   # 相手から切られた → すぐつなぎ直す
        await settle()
        assert len(f.made) == 2
        second = f.made[1]
        assert len(second.audio) == SR * 2 * 2           # 出し終えた final の終わり(1.0 s)から今まで = 2 s
        second.push(partial=[("二文目です", 100, 1500)])
        (p,) = await pull(it, 1)
        assert p["text"] == "二文目です" and (p["start_s"], p["end_s"]) == (1.1, 2.5)   # 送り直した位置を足す
        assert "soniox_reconnect" in w.m.phases() and w.stats["reconnects"] == 1
        await w.stop()
    asyncio.run(main())


def test_finalize_during_outage_turns_received_words_into_a_final(monkeypatch):
    async def main():
        f = Factory((True, None), (False, 503), (False, 503))
        w, _ = make(monkeypatch, f)
        w.RETRY_S = (0.0, 5.0)   # 2 回目の試行の前に finalize を送る
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        first = f.made[0]
        await w.inner.q.put(audio(2.0))
        first.push(final=[("届いて", 0, 600)], partial=[("いた語", 600, 1200)])
        await pull(it, 1)
        first.die(code=None)
        await settle()
        await w.finalize()
        (ev,) = await pull(it, 1)
        assert ev["type"] == "final" and ev["text"] == "届いていた語" and ev["how"] == "outage"
        assert w._emitted_end == int(1.2 * SR)            # つなぎ直したら、この後ろから送り直す
        await w.stop()
    asyncio.run(main())


def test_no_retry_error_switches_to_local_and_fills_the_gap(monkeypatch):
    async def main():
        f = Factory((False, 402))
        w, engines = make(monkeypatch, f)
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "final", "text": "端末内の一文目。", "start_s": 0.0, "end_s": 1.5})   # ready の前の分も
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        evs = await pull(it, 2)
        assert evs[0]["type"] == "ready"
        assert evs[1]["type"] == "final" and evs[1]["text"] == "端末内の一文目。"   # 出せていない区間を補う
        assert engines == [("system", "local", "残高がありません(HTTP 402)")]
        assert ("error", "[system] Soniox が使えないため、このパソコンの文字起こしに切り替えました(残高がありません(HTTP 402))") in w.ui.msgs
        await w.inner.q.put({"type": "partial", "text": "この後は端末内", "start_s": 2, "end_s": 3})
        (p,) = await pull(it, 1)
        assert p["text"] == "この後は端末内" and w.engine == "local" and len(f.made) == 1   # 402 はつなぎ直さない
        assert [r for r in w.m.rows if r["phase"] == "soniox_fallback"][0]["filled"] == 1
        await w.stop()
    asyncio.run(main())


def test_no_key_goes_local_without_connecting(monkeypatch):
    async def main():
        f = Factory()
        w, engines = make(monkeypatch, f, key=None)
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        assert f.made == [] and w.engine == "local" and engines[0][2] == "キーが登録されていません"
        await w.stop()
    asyncio.run(main())


def test_stall_is_detected_and_reconnected(monkeypatch):
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        w.STALL_S, w.TICK_S, w.RETRY_S = 0.05, 0.02, (0.0,)
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        f.made[0].t_last_msg = time.monotonic() - 1   # 応答が止まり、処理済み(proc_ms = 0)も進まない
        await w.inner.q.put(audio(1.0))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(f.made) == 2:
                break
        assert "soniox_stall" in w.m.phases() and len(f.made) == 2 and f.made[0].closed
        await w.stop()
    asyncio.run(main())


def test_server_that_accepts_then_drops_still_falls_back(monkeypatch):
    """2026-10-18 の読み直し: つながってもすぐ 503 で切られるのを繰り返すと、つなぐたびに時計を数え直して切り替えに届かな
    かった。途切れの時計は接続をまたいで持ち、GIVE_UP_S で端末内へ切り替える(試す間隔も延びる)。"""
    async def main():
        f = Factory()
        w, engines = make(monkeypatch, f)
        w.RETRY_S, w.GIVE_UP_S = (0.0, 0.05), 0.3
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        killed = 0
        t0 = time.monotonic()
        while w.engine == "soniox" and time.monotonic() - t0 < 3:
            await asyncio.sleep(0.01)
            if len(f.made) > killed and f.made[killed].t_open is not None:
                f.made[killed].die(code=503, error="Soniox 503: unavailable")
                killed += 1
        assert w.engine == "local" and engines[0][1] == "local"
        assert 3 <= len(f.made) <= 10                       # 数え直していたときは 3 s で 60 本を超えた
        await w.stop()
    asyncio.run(main())


def test_healthy_connection_resets_the_outage_clock(monkeypatch):
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        w.HEALTHY_S, w.TICK_S = 0.3, 0.02
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        assert w._outage_t0 is not None                     # 最初の接続も、しばらく保てるまでは途切れの続き
        await asyncio.sleep(0.45)
        assert w._outage_t0 is None and w._attempts == 0
        await w.stop()
    asyncio.run(main())


def test_keepalive_while_no_audio_arrives(monkeypatch):
    """Windows のループバックは何も鳴っていないと音を渡さない。音が来ない間は keepalive を送って接続を保つ。"""
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        w.KEEPALIVE_S, w.TICK_S = 0.1, 0.02
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await asyncio.sleep(0.35)
        assert f.made[0].keepalives >= 2
        before = f.made[0].keepalives
        for _ in range(10):                                  # 音が流れている間は送らない
            await w.inner.q.put(audio(0.02))
            await asyncio.sleep(0.03)
        assert f.made[0].keepalives == before
        await w.stop()
    asyncio.run(main())


def test_bye_then_eof_still_waits_for_the_last_fin(monkeypatch):
    """本物のヘルパーは bye の直後に終わる(stdout の EOF)。最後の確定を待っている間に EOF が来ても、確定を出してから終える。"""
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        sess = f.made[0]
        sess.push(partial=[("最後の", 0, 400)])
        await pull(it, 1)
        await w.inner.q.put({"type": "bye"})
        await w.inner.q.put(None)                           # すぐ EOF
        await settle()
        assert sess.finalized == 1
        sess.push(final=[("最後の文です。", 0, 900)], mark="fin")
        fin, bye = await pull(it, 2)
        assert fin["text"] == "最後の文です。" and bye["type"] == "bye"
        await w.stop()
    asyncio.run(main())


def test_late_fin_after_the_deadline_is_not_repeated(monkeypatch):
    """finalize から FIN_DEADLINE_S たっても "<fin>" が来なければ、届いている語で final にする。後から同じ語が確定しても出さない。"""
    async def main():
        f = Factory()
        w, _ = make(monkeypatch, f)
        w.FIN_DEADLINE_S, w.TICK_S = 0.1, 0.02
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        sess = f.made[0]
        sess.push(final=[("本日は", 0, 400)], partial=[("晴れです", 400, 900)])
        await pull(it, 1)
        await w.finalize()
        (ev,) = await pull(it, 1, timeout=1.0)
        assert ev["type"] == "final" and ev["text"] == "本日は晴れです" and ev["how"] == "fin_timeout"
        assert "soniox_fin_timeout" in w.m.phases()
        sess.push(final=[("晴れです。", 400, 950)], mark="fin")            # 遅れて届いた確定(同じ語)
        sess.push(partial=[("次の文", 1000, 1300)])
        (nxt,) = await pull(it, 1)
        assert nxt["type"] == "partial" and nxt["text"] == "次の文"        # 同じ文の 2 回目も、空の final も出ない
        await w.stop()
    asyncio.run(main())


def test_stop_does_not_wait_for_soniox_to_close(monkeypatch):
    class SlowClose(FakeSession):
        def close(self, timeout=5.0):
            time.sleep(1.5)
            self.closed = True

    async def main():
        made = []
        w, _ = make(monkeypatch, lambda **kw: made.append(SlowClose(**kw)) or made[-1])
        await w.start()
        it = Run(w)
        await w.inner.q.put({"type": "ready", "dst_hz": SR})
        await pull(it, 1)
        await settle()
        t0 = time.monotonic()
        await w.stop()
        assert time.monotonic() - t0 < 0.5                 # 終わりの返事を待たない(ヘルパーの停止と並べて裏で閉じる)
        assert "soniox_done" in w.m.phases()
    asyncio.run(main())


def test_pipeline_wraps_helpers_only_when_soniox_is_chosen(tmp_path, monkeypatch):
    import meetcue.pipeline as pl
    from meetcue.config import Config
    from meetcue.session import Session

    made = []

    class Wrap:
        def __init__(self, argv, **kw):
            made.append(("soniox", argv, kw))

        async def start(self):
            pass

        async def stop(self):
            pass

    class Plain(Wrap):
        def __init__(self, argv, channel, on_diag=None):
            made.append(("local", argv, {}))

    class Seg:
        def __init__(self, *a, **kw):
            pass

        async def run(self, out):
            await asyncio.Event().wait()

    class Src:
        kind, channel, arg = "file", "system", ""

        def argv(self, cfg):
            return [sys.executable]

    class NullUI:
        def __getattr__(self, name):
            return lambda *a, **kw: None

    monkeypatch.setattr(sx, "SonioxSTT", Wrap)
    monkeypatch.setattr(pl, "STTHelper", Plain)
    monkeypatch.setattr(pl, "Segmenter", Seg)
    for privacy, want in (("private", "soniox"), ("local", "local")):
        made.clear()
        cfg = Config(app_dir=tmp_path, record_audio=False, privacy=privacy)
        cfg.soniox.terms_file = tmp_path / "none.txt"
        session = Session(tmp_path / privacy, mode="participant", privacy=privacy, sources=["file:system"], model="m")
        p = pl.Pipeline(cfg, session, NullUI(), [Src()], api_key=None, llm_enabled=False, stt="soniox", soniox_key="sk")
        p.hotkeys = False
        asyncio.run(p.run(seconds=0.05))
        session.close()
        kind, argv, kw = made[0]
        assert kind == want and p.stt == want
        if want == "soniox":
            assert argv[-1] == "--pcm-out" and kw["api_key"] == "sk" and session.meta["stt"]["engine"] == "soniox"
        else:
            assert "--pcm-out" not in argv and "stt" not in session.meta   # LOCAL では音を送らない

"""会議の文字起こしを Soniox で(REQUIREMENTS FR-2b・2026-10-18)— STT ヘルパーの契約(FR-2)の顔をした包み。

パイプラインとセグメンターから見ると STTHelper と同じ(start / events / finalize / stop)。中では:
- 音の取り込みと録音は、今の STT ヘルパーがそのまま行う。`--pcm-out` を付けて起こすと、認識に渡すのと同じ音
  (モノラル s16le・標本の速さは ready の dst_hz)を `{"type":"audio","seq":n,"pcm":<base64>}` で stdout にも出す
  (音声入力の dictation_helper と同じ形)。本体はそれを Soniox(stt-rt-v5)へ流す。
- 語を契約の partial / final に直す。いつ確定させるかは今までどおりセグメンターが決める: finalize → Soniox に
  finalize → "<fin>" までの語を 1 つの final にする(確定まで 0.2〜0.5 s・spikes/SONIOX_STT.md の 4)。partial は
  「まだ final にしていない確定した語 + まだ変わりうる語」。時刻は音の頭からの秒(ヘルパーと同じ物差し)。
- ヘルパーは端末内の認識も続ける(画面には出さない)。Soniox に届かない(鍵なし・400/401/402/403・つなぎ直しが
  GIVE_UP_S 続けて失敗)と、このチャネルは端末内の認識に切り替える。そのとき、Soniox でまだ出せていない区間の
  端末内の final を足してから、先は端末内の partial / final をそのまま流す(会議の終わりまで Soniox へは戻さない)。
- 接続が切れたら(相手から切られた・送信の詰まり・処理の遅れ STALL_S)、出し終えた final の終わりから先の音
  (最大 BUFFER_S 秒)を送り直してつなぎ直す。切れている間に finalize が来たら、それまでに届いていた語を final にする
  (セグメンターが待ちきれずに partial を出し、つなぎ直した後の文と重なるのを防ぐ)。
- 途切れの時計は接続をまたいで持つ(2026-10-18 の読み直しで直した): つながってもすぐ切られる(受け付けてから 503 で
  切る等)のを繰り返すと、つなぐたびに数え直して切り替えに届かなかった。HEALTHY_S 切られずに保った接続だけが時計を戻す。
- 音を KEEPALIVE_S 送っていなければ keepalive を送る(音も keepalive も無いと Soniox は約 21 s で 408 を返して閉じる。
  Windows のループバックは何も鳴っていない間は音を渡さないので、
  会議の始まりの待ち時間に Soniox が接続を閉じないように・2026-10-18 Windows 実機で確認)。
- finalize から FIN_DEADLINE_S たっても "<fin>" が来なければ、届いている語をその場で final にし、後から確定した同じ語は
  捨てる(セグメンターの safety_ms の 2.5〜3 s より先に出す。後から来た final が同じ文の 2 回目になるのを防ぐ)。
- 計測(metrics・rid = "STT"): soniox_connect / soniox_finalize / soniox_fin_timeout / soniox_down / soniox_stall /
  soniox_reconnect / soniox_fallback / soniox_done(流した音の長さと費用の目安)。
"""
from __future__ import annotations

import asyncio
import base64
import re
import threading
import time
from collections import deque
from typing import AsyncIterator, Callable, Optional

from . import soniox
from .stt_reader import STTHelper

NO_RETRY = {400, 401, 402, 403}   # 設定・鍵・残高・権限の誤り。つなぎ直しても直らない
USD_PER_HOUR = 0.12               # リアルタイムの音声 1 時間の値段(REQUIREMENTS §5.2)
REASONS = {400: "設定が受け付けられませんでした", 401: "キーが正しくありません", 402: "残高がありません",
           403: "キーにリアルタイムの権限がありません"}


def tidy(text: str) -> str:
    """Soniox は「。」の後などに空白を入れる。句読点の前後の空白だけを除く(語の間の空白・英語の空白は残す)。"""
    s = re.sub(r"[ 　]+(?=[、。，．,.!?！？」』）])", "", text or "")
    s = re.sub(r"(?<=[、。，．「『（！？])[ 　]+", "", s)
    return s.strip()


def reason_text(code: int | None) -> str:
    return f"{REASONS[code]}(HTTP {code})" if code in REASONS else (f"接続できません(HTTP {code})" if code else "接続できません")


class SonioxSTT:
    BUFFER_S = 90.0      # 送り直しのためにためておく音の上限(秒)
    RETRY_S = (0.0, 1.0, 2.0, 4.0, 8.0)
    GIVE_UP_S = 45.0     # 途切れ(つながらない・つないでもすぐ切られる)がこれだけ続いたら端末内に切り替える
    HEALTHY_S = 10.0     # この長さ切られずに保った接続だけが、途切れの時計と試した回数を戻す
    KEEPALIVE_S = 8.0    # 音をこれだけ送っていなければ keepalive を送る(Soniox は音も keepalive も無いと約 21 s で 408 を
                         # 返して閉じる・keepalive を送れば保てる。2026-10-18 実測)
    STALL_S = 15.0       # 送った音より処理がこれだけ遅れ、応答も止まったら詰まったとみなす(無音でも約 1 s ごとに応答が来る)
    FIN_DEADLINE_S = 2.0  # finalize からこれだけ "<fin>" が来なければ、届いている語で final にする(実測 p90 0.5 s)。
                          # パイプラインはセグメンターの safety_ms − 0.5 s を渡す(相手側・自分 3.0 s → 2.5 s)
    FIN_WAIT_S = 3.0     # 終わり(bye)で最後の finalize の確定を待つ
    TICK_S = 0.5

    def __init__(self, argv: list[str], *, channel: str, api_key: str | None,
                 on_diag: Optional[Callable[[dict], None]] = None, metrics=None, ui=None,
                 terms: list[str] | tuple[str, ...] = (), language_hints: list[str] | tuple[str, ...] = ("ja", "en"),
                 region: str = "us", on_engine: Optional[Callable[[str, str, str | None], None]] = None,
                 session_factory: Optional[Callable[..., "soniox.Session"]] = None, fin_deadline_s: float | None = None):
        self.inner = STTHelper(argv, channel=channel, on_diag=on_diag)
        if fin_deadline_s:   # パイプラインがセグメンターの safety_ms より短く渡す(先に final を出すため)
            self.FIN_DEADLINE_S = fin_deadline_s
        self.channel = channel
        self.m = metrics
        self.ui = ui
        self.on_engine = on_engine   # (channel, engine, 理由) 端末内に切り替えたとき(画面の印と記録)
        self._key = api_key
        self._opts = {"terms": list(terms), "language_hints": list(language_hints), "region": region}
        self._factory = session_factory or (lambda **kw: soniox.Session(api_key or "", **kw))
        self.engine = "soniox"       # soniox | local(切り替えた後)
        self.reason: str | None = None
        self._q: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self._hz = soniox.SAMPLE_RATE
        # 送り直しのための音
        self._pcm = bytearray()
        self._pcm_start = 0          # _pcm[0] の位置(標本・音の頭から)
        self._pos = 0                # 受け取った音の終わり(標本)
        # 今の接続
        self._sess: soniox.Session | None = None
        self._dead: soniox.Session | None = None   # 切れた接続(つなぎ直すまで、届いていた語を final にできる)
        self._state = "wait"         # wait(ready の前)| connecting | live | retrying | local
        self._base = 0               # 今の接続で流し始めた位置(標本)
        self._emitted = 0            # 今の接続の final_tokens のうち final として出し終えた数
        self._marks_seen = 0
        self._emitted_end = 0        # 出し終えた final の終わり(標本)。送り直しはここから
        self._last_partial = ""
        self._fin_t: float | None = None    # finalize を送った時刻(確定を待っている間)
        self._skip_end_ms = -1.0            # 期限で出した語の終わり(今の接続の ms)。これより前に終わる語は出し済み
        self._down_t: float | None = None   # 切れた時刻(つなぎ直しの間)
        self._outage_t0: float | None = None   # 途切れが始まった時刻(接続をまたいで持つ・健全な接続で戻す)
        self._attempts = 0                  # 途切れの間に試した回数(待ちの長さを決める)
        self._live_t = 0.0                  # 今の接続がつながった時刻
        self._sent_t = 0.0                  # 最後に音か keepalive を送った時刻
        self._bye: dict | None = None
        self._bye_t = 0.0
        self._eof = False
        self._local_finals: deque = deque(maxlen=400)   # 画面に出していない端末内の final(切り替えたときに足す)
        self._last_code: int | None = None
        self.stats = {"sessions": 0, "reconnects": 0, "finals": 0, "sent_s": 0.0, "keepalives": 0}

    # ---- STTHelper と同じ顔 ---------------------------------------------------------------
    @property
    def ready(self) -> dict | None:
        return self.inner.ready

    @property
    def exited(self) -> bool:
        return self.inner.exited

    @property
    def recording(self) -> bool:
        return self.inner.recording

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.inner.start()
        self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._tick())]

    async def finalize(self) -> None:
        if self._state == "live" and self._sess is not None:
            if self._fin_t is None:
                self._fin_t = time.perf_counter()
            self._sess.finalize()
        elif self._state in ("connecting", "retrying"):
            for ev in self._flush("outage"):
                await self._q.put(("emit", ev))
        await self.inner.finalize()   # 端末内の認識(見せていない間も、切り替えたときに区切りがそろうように)

    async def send(self, cmd: str) -> None:
        await self.inner.send(cmd)

    async def wait(self, timeout: float) -> int | None:
        return await self.inner.wait(timeout)

    async def stop(self) -> None:
        """ヘルパーを止め(録音を閉じる)、Soniox に終わりを伝えて閉じる。流した音の長さと費用の目安を残す。"""
        self._closing = True
        sess, self._sess = self._sess, None
        if sess is not None:   # 停止を遅くしない: Soniox を閉じるのは裏で(ヘルパーの停止と並べ、終わりの返事は待たない)
            threading.Thread(target=sess.close, args=(1.0,), name="soniox-close", daemon=True).start()
        await self.inner.stop()
        for t in self._tasks:
            t.cancel()
        sent = round(self.stats["sent_s"], 1)
        self._metric("soniox_done", engine=self.engine, reason=self.reason, sent_s=sent,
                     usd=round(sent / 3600 * USD_PER_HOUR, 4), sessions=self.stats["sessions"],
                     reconnects=self.stats["reconnects"], finals=self.stats["finals"], keepalives=self.stats["keepalives"])

    async def events(self) -> AsyncIterator[dict]:
        while True:
            kind, item = await self._q.get()
            if kind == "eof":   # ヘルパーは bye の直後に終わる。最後の確定を待っている(_bye)なら、それが済むまで続ける
                if self._bye is None:
                    return
                self._eof = True
            out: list[dict] = []
            if kind == "helper":
                out += self._on_helper(item)
            elif kind == "soniox":
                out += self._soniox_events(item)
            elif kind == "done":
                out += self._on_done(item)
            elif kind == "fallback":
                out += self._to_local(item)
            elif kind == "emit":
                out.append(item)
            elif kind == "tick":
                out += self._on_tick()
            if self._bye is not None and (self._fin_t is None or self._state != "live"
                                          or time.monotonic() - self._bye_t > self.FIN_WAIT_S):
                out += self._flush("bye") + [self._bye]
                self._bye = None
            for ev in out:
                yield ev
                if ev.get("type") == "bye":
                    return

    # ---- ヘルパーのイベント -----------------------------------------------------------------
    def _on_helper(self, ev: dict) -> list[dict]:
        t = ev.get("type")
        if t == "audio":
            self._on_audio(ev)
            return []
        if t == "ready":
            self._hz = int(ev.get("dst_hz") or soniox.SAMPLE_RATE)
            out = [{**ev, "engine": self.engine, "local_engine": ev.get("engine")}]
            if self._state == "wait":
                if self._key:
                    self._spawn(self._connect_loop("start"))
                else:
                    out += self._to_local("キーが登録されていません")
            return out
        if t in ("partial", "final"):
            if self._state == "local":
                return [] if (t == "final" and self._covered(ev)) else [ev]
            if t == "final":
                self._local_finals.append(ev)
            return []
        if t == "bye":
            if self._state == "live" and self._sess is not None and self._pending()[0]:
                self._sess.finalize()   # 最後の語を確定させてから終える(FIN_WAIT_S まで)
                self._fin_t = self._fin_t or time.perf_counter()
                self._bye, self._bye_t = ev, time.monotonic()
                return []
            out = self._flush("bye")
            if self._state in ("connecting", "retrying"):   # つながらないまま終わった: 端末内の文で埋める
                out += self._to_local("終わるまでに Soniox へつながりませんでした")
            return out + [ev]
        return [ev]

    def _on_audio(self, ev: dict) -> None:
        if self._state == "local":
            return
        try:
            pcm = base64.b64decode(ev.get("pcm") or "")
        except (ValueError, TypeError):
            return
        if not pcm:
            return
        self._pcm += pcm
        self._pos += len(pcm) // 2
        if self._state == "live" and self._sess is not None:
            self._sess.send_audio(pcm)
            self.stats["sent_s"] += len(pcm) / 2 / self._hz
            self._sent_t = time.monotonic()
        self._trim()

    def _trim(self) -> None:
        keep = max(self._emitted_end, self._pos - int(self.BUFFER_S * self._hz))
        if keep > self._pcm_start:
            del self._pcm[: (keep - self._pcm_start) * 2]
            self._pcm_start = keep

    # ---- 接続 ------------------------------------------------------------------------------
    def _spawn(self, coro) -> None:
        self._tasks.append(asyncio.create_task(coro))

    def _from_thread(self, kind: str) -> Callable[["soniox.Session"], None]:
        def put(sess: "soniox.Session") -> None:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._q.put_nowait, (kind, sess))
        return put

    async def _pump(self) -> None:
        try:
            async for ev in self.inner.events():
                await self._q.put(("helper", ev))
        finally:
            await self._q.put(("eof", None))

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self.TICK_S)
            await self._q.put(("tick", None))

    async def _connect(self) -> dict | None:
        """1 回つなぐ。つながったら、出し終えた final の終わりから今までの音をまとめて送る。送り直した長さを返す。"""
        sess = self._factory(**self._opts, sample_rate=self._hz, on_update=self._from_thread("soniox"),
                             on_done=self._from_thread("done"))
        t0 = time.perf_counter()
        ok = await asyncio.to_thread(sess.start)
        self._metric("soniox_connect", ms=round((time.perf_counter() - t0) * 1000), ok=ok, error=sess.error,
                     error_code=sess.error_code, state=self._state)
        self._last_code = sess.error_code
        if not ok or self._closing or self._state == "local":
            if ok:
                await asyncio.to_thread(sess.close, 1.0)
            return None
        base = max(self._emitted_end, self._pcm_start)
        lost_s = round((base - self._emitted_end) / self._hz, 2)
        self._sess, self._dead, self._base = sess, None, base
        self._emitted = self._marks_seen = 0
        self._last_partial = ""
        self._fin_t = None
        self._skip_end_ms = -1.0
        self._live_t = self._sent_t = time.monotonic()
        backlog = bytes(self._pcm[(base - self._pcm_start) * 2:])
        step = self._hz // 10 * 2
        for i in range(0, len(backlog), step):
            sess.send_audio(backlog[i:i + step])
        resent_s = round(len(backlog) / 2 / self._hz, 2)
        self.stats["sent_s"] += resent_s
        self.stats["sessions"] += 1
        self._state = "live"
        return {"resent_s": resent_s, "lost_s": lost_s}

    async def _connect_loop(self, why: str) -> None:
        """最初の接続と、切れた後のつなぎ直し。直らない誤りか、途切れが GIVE_UP_S 続いたら端末内へ切り替える。
        途切れの時計(_outage_t0)と試した回数(_attempts)は接続をまたいで持ち、健全な接続(_on_tick)だけが戻す。"""
        self._state = "connecting" if why == "start" else "retrying"
        if self._outage_t0 is None:
            self._outage_t0 = time.monotonic()
        while not self._closing and self._state != "local":
            delay = self.RETRY_S[min(self._attempts, len(self.RETRY_S) - 1)]
            if delay:
                await asyncio.sleep(delay)
            got = await self._connect()
            if got is not None:
                if why != "start":
                    self.stats["reconnects"] += 1
                    gap = round(time.monotonic() - (self._down_t or self._outage_t0), 2)
                    self._metric("soniox_reconnect", ok=True, gap_s=gap, attempts=self._attempts, reason=why, **got)
                    if self.ui is not None:
                        self.ui.status(f"[{self.channel}] Soniox につなぎ直しました({gap} 秒)")
                return
            self._attempts += 1
            if self._last_code in NO_RETRY or time.monotonic() - self._outage_t0 >= self.GIVE_UP_S:
                break
        if not self._closing and self._state != "local":
            await self._q.put(("fallback", reason_text(self._last_code)))

    def _on_done(self, sess: "soniox.Session") -> list[dict]:
        """接続が終わった(相手から切られた・誤り・送信の詰まり)。直らない誤りなら端末内へ、ほかはつなぎ直す。"""
        if sess is not self._sess or self._closing or self._state != "live":
            return []
        self._sess, self._dead = None, sess
        code = sess.error_code
        self._metric("soniox_down", error=sess.error, error_code=code, close_code=getattr(sess, "close_code", None),
                     close_reason=getattr(sess, "close_reason", None), at_s=round(self._pos / self._hz, 1),
                     lived_s=round(time.monotonic() - self._live_t, 1))
        if code in NO_RETRY:
            return self._to_local(reason_text(code))
        now = time.monotonic()
        if now - self._live_t >= self.HEALTHY_S:   # しばらく保った接続: 新しい途切れとして数え直す
            self._outage_t0, self._attempts = None, 0
        else:                                                         # つないでもすぐ切れた: 同じ途切れの続き・間隔を延ばす
            self._attempts += 1
        if self._outage_t0 is None:
            self._outage_t0 = now
        if now - self._outage_t0 >= self.GIVE_UP_S:   # つないでは切られるのが続いた
            return self._to_local(reason_text(code))
        self._state, self._down_t = "retrying", now
        if self.ui is not None:
            self.ui.status(f"[{self.channel}] Soniox との接続が切れました。つなぎ直しています")
        self._spawn(self._connect_loop("413" if code == 413 else "down"))   # 413 = 1 本の上限(300 分)
        return []

    def _on_tick(self) -> list[dict]:
        sess = self._sess
        if self._state != "live" or sess is None or self._closing:
            return []
        now = time.monotonic()
        if self._outage_t0 is not None and now - self._live_t >= self.HEALTHY_S:
            self._outage_t0, self._attempts = None, 0   # 切られずに保てた: 途切れは終わった
        if now - self._sent_t >= self.KEEPALIVE_S:   # 音が来ない(Windows のループバックは無音だと音を渡さない)
            sess.keepalive()
            self._sent_t = now
            self.stats["keepalives"] += 1
        lag_ms = (self._pos - self._base) * 1000 / self._hz - sess.proc_ms
        quiet_s = now - (sess.t_last_msg or sess.t_open or now)
        if lag_ms > self.STALL_S * 1000 and quiet_s > self.STALL_S:
            self._metric("soniox_stall", lag_ms=round(lag_ms), quiet_s=round(quiet_s, 1))
            sess.error = sess.error or f"stall: {round(lag_ms)} ms"
            self._spawn(asyncio.to_thread(sess.close, 0.5))
            return self._on_done(sess)
        if self._fin_t is not None and time.perf_counter() - self._fin_t > self.FIN_DEADLINE_S:
            self._metric("soniox_fin_timeout", ms=round((time.perf_counter() - self._fin_t) * 1000))
            self._fin_t = None
            return self._flush("fin_timeout")
        return []

    # ---- 語 → 契約のイベント -----------------------------------------------------------------
    def _metric(self, phase: str, **kw) -> None:
        if self.m is not None:
            self.m.emit("STT", phase, channel=self.channel, **kw)

    def _sec(self, sess_ms) -> float | None:
        return round(self._base / self._hz + sess_ms / 1000, 2) if isinstance(sess_ms, (int, float)) else None

    def _ev(self, typ: str, toks: list[dict], **kw) -> dict:
        starts = [t["start_ms"] for t in toks if isinstance(t.get("start_ms"), (int, float))]
        ends = [t["end_ms"] for t in toks if isinstance(t.get("end_ms"), (int, float))]
        return {"type": typ, "channel": self.channel, "text": tidy("".join(t["text"] for t in toks)),
                "start_s": self._sec(starts[0]) if starts else None, "end_s": self._sec(ends[-1]) if ends else None,
                "fed_s": round(self._pos / self._hz, 2), "engine": "soniox", **kw}

    def _advance(self, toks: list[dict]) -> None:
        """出し終えた語の終わりまで、送り直しの起点を進める。"""
        ends = [t["end_ms"] for t in toks if isinstance(t.get("end_ms"), (int, float))]
        if ends:
            self._emitted_end = max(self._emitted_end, self._base + int(ends[-1] * self._hz / 1000))
        self._trim()

    def _fresh(self, toks: list[dict]) -> list[dict]:
        """期限で出し済みの区間で始まる語(始まりが _skip_end_ms より前)を除く。後から届いた "<fin>" で同じ語が確定しても
        2 回出さない(確定すると語の切れ目と終わりの時刻が少しずれるので、終わりではなく始まりで見る)。"""
        if self._skip_end_ms < 0:
            return toks
        return [t for t in toks if not (isinstance(t.get("start_ms"), (int, float)) and t["start_ms"] < self._skip_end_ms)]

    def _pending(self) -> tuple[str, list[dict]]:
        sess = self._sess or self._dead
        if sess is None:
            return "", []
        with sess.lock:
            toks = self._fresh(sess.final_tokens[self._emitted:] + list(sess.partial_tokens))
        return tidy("".join(t["text"] for t in toks)), toks

    def _soniox_events(self, sess: "soniox.Session") -> list[dict]:
        if sess is not self._sess or self._state != "live":
            return []
        out = []
        with sess.lock:
            marks = sess.marks[self._marks_seen:]
            self._marks_seen += len(marks)
            groups = []
            for kind, idx in marks:
                groups.append((kind, self._fresh(sess.final_tokens[self._emitted:idx])))
                self._emitted = max(self._emitted, idx)
        for kind, toks in groups:
            waiting = kind == "fin" and self._fin_t is not None   # セグメンターが確定を待っている
            if waiting:
                self._metric("soniox_finalize", ms=round((time.perf_counter() - self._fin_t) * 1000, 1))
                self._fin_t = None
            self._last_partial = ""
            ev = self._ev("final", toks, how=kind)
            self._advance(toks)
            if ev["text"]:
                self.stats["finals"] += 1
            if ev["text"] or waiting:   # 待たれている "<fin>" は空でも返す(セグメンターの確定待ちを終わらせる)
                out.append(ev)
        text, toks = self._pending()
        if text != self._last_partial:
            self._last_partial = text
            if text:
                out.append(self._ev("partial", toks))
        return out

    def _flush(self, how: str) -> list[dict]:
        """届いている語(確定した語 + まだ変わりうる語)を、その場で 1 つの final にする。"""
        sess = self._sess or self._dead
        self._last_partial = ""
        if sess is None:
            return []
        with sess.lock:
            toks = self._fresh(sess.final_tokens[self._emitted:] + list(sess.partial_tokens))
            self._emitted = len(sess.final_tokens)
            if sess is self._dead:
                sess.partial_tokens = []
        ends = [t["end_ms"] for t in toks if isinstance(t.get("end_ms"), (int, float))]
        if ends:   # まだ変わりうる語も出したので、後からそれが確定して届いても出さない
            self._skip_end_ms = max(self._skip_end_ms, ends[-1])
        ev = self._ev("final", toks, how=how)
        if not ev["text"]:
            return []
        self._advance(toks)
        self.stats["finals"] += 1
        return [ev]

    def _covered(self, ev: dict) -> bool:
        """端末内の final が、Soniox で出し終えた区間の中に収まっているか(切り替えた直後の重なりを捨てる)。"""
        end = ev.get("end_s")
        return isinstance(end, (int, float)) and end <= self._emitted_end / self._hz + 0.3

    def _to_local(self, reason: str) -> list[dict]:
        """このチャネルを端末内の認識に切り替える(会議の終わりまで)。Soniox で出せていない区間の端末内の final を足す。"""
        if self._state == "local":
            return []
        prev = self._state
        self._state, self.engine, self.reason = "local", "local", reason
        sess, self._sess, self._dead = self._sess, None, None
        if sess is not None:
            self._spawn(asyncio.to_thread(sess.close, 1.0))
        fill = [ev for ev in self._local_finals if not self._covered(ev)]
        self._local_finals.clear()
        self._pcm.clear()
        self._metric("soniox_fallback", reason=reason, prev=prev, filled=len(fill),
                     from_s=round(self._emitted_end / self._hz, 2))
        if self.ui is not None:
            self.ui.error(f"[{self.channel}] Soniox が使えないため、このパソコンの文字起こしに切り替えました({reason})",
                          code="stt_fallback")
        if self.on_engine is not None:
            self.on_engine(self.channel, "local", reason)
        return fill

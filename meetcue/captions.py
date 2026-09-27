"""ライブ字幕(2026-09-27 keigoly様: 録音していなくても字幕を出す・翻訳もする)。

- 録音中は Pipeline が partial / utterance を配るので、字幕の画面(caption.html)はそれを使う。
- 録音していない間は CaptionRunner が同じ STT ヘルパー(スピーカー = tap-all・マイク = stt-helper)を**保存なし**で動かし、
  `caption` イベント(phase = partial / final)を配る。記録・判定・生成はしない。画面が開いている間だけ動かす(借用)。
- Translator: 確定した字幕を 1 行ずつ生成 AI で訳し、`caption_tr` を配る(Claude サブスクなら Sonnet)。
  LOCAL の録音中・AI の生成をオフにしているときは訳さない(外へ送らない約束を守る)。

記録は ~/.meeting-cue/logs/captions-<日付>.jsonl(phase = caption_start / caption_stop / caption_error / caption_tr /
caption_level = 5 s ごとの音量と partial・final の数)。
"""
from __future__ import annotations

import asyncio
import dataclasses
import math
import time
from collections import deque
from pathlib import Path

from .config import Config
from .cues import llm
from .metrics import Metrics, new_rid
from .pipeline import Source, helper_hint, segmenter_config
from .replacements import Replacer
from .segmenter import Segmenter
from .stt_reader import STTHelper

LOCALES = {"ja-JP": "日本語", "en-US": "English"}
TRANSLATE = {"ja": "Japanese", "en": "English"}


def caption_metrics(cfg: Config) -> Metrics:
    return Metrics(cfg.app_dir / "logs" / f"captions-{time.strftime('%Y%m%d')}.jsonl")


class CaptionRunner:
    """録音していない間の字幕だけの動き。start / stop は App の 1 つのロックの下で呼ぶ。"""

    def __init__(self, cfg: Config, web, sources: list[Source], *, on_final=None, metrics: Metrics | None = None):
        self.cfg = cfg
        self.web = web
        self.sources = sources            # アプリの音源(mic:mic / tap-all:system)。字幕はこの中から選んだ channel だけ動かす
        self.on_final = on_final          # (rid, channel, text) → 翻訳へ
        self.m = metrics or caption_metrics(cfg)
        self.replacer = Replacer(cfg.app_dir / "replacements.txt")   # 置き換え辞書(録音と同じもの)
        self.key: tuple | None = None     # (channels, locale) 動いている組み合わせ
        self._helpers: list[STTHelper] = []
        self._tasks: list[asyncio.Task] = []
        # 見える化(2026-09-27 Step 1: 実機で字幕がほとんど出なかった): 5 s ごとに音量と partial / final の数を残す
        self._win: dict[str, dict] = {}

    @property
    def running(self) -> bool:
        return self.key is not None

    async def start(self, channels: set[str], locale: str) -> None:
        cfg = dataclasses.replace(self.cfg, locale=locale)
        t0 = time.perf_counter()
        queue: asyncio.Queue = asyncio.Queue()
        started = []
        for src in self.sources:
            if src.channel not in channels:
                continue
            argv = src.argv(cfg)
            if not Path(argv[0]).exists():
                self._error(f"helper が無い: {argv[0]}({helper_hint()})")
                continue
            helper = STTHelper(argv, channel=src.channel, on_diag=lambda d, c=src.channel: self._diag(c, d))
            await helper.start()
            self._helpers.append(helper)
            seg = Segmenter(helper, src.channel, segmenter_config(cfg, src), self.m, on_partial=self._partial)
            self._tasks.append(asyncio.create_task(seg.run(queue)))
            self._tasks[-1].add_done_callback(self._task_done)
            started.append(src.channel)
        if not started:
            self.m.emit("CAP", "caption_start", ok=False, channels=sorted(channels), locale=locale)
            return
        self._tasks.append(asyncio.create_task(self._consume(queue)))
        self._tasks[-1].add_done_callback(self._task_done)
        self.key = (tuple(sorted(channels)), locale)
        self.m.emit("CAP", "caption_start", ok=True, channels=started, locale=locale,
                    ms=round((time.perf_counter() - t0) * 1000, 1))
        self.web.set_state(caption=True, caption_channels=started, caption_locale=locale, caption_error="")

    async def stop(self) -> None:
        if not self.running and not self._helpers:
            return
        t0 = time.perf_counter()
        for t in self._tasks:
            t.cancel()
        for h in self._helpers:
            await h.stop()
        self._tasks, self._helpers = [], []
        self.key = None
        self.m.emit("CAP", "caption_stop", ms=round((time.perf_counter() - t0) * 1000, 1))
        self.web.set_state(caption=False)

    def _task_done(self, t: asyncio.Task) -> None:
        """区切り・配信の処理が例外で終わったら、黙って止まらず画面の下に出す(稼働中のまま字幕が出ない状態にしない)。"""
        if not t.cancelled() and t.exception() is not None:
            e = t.exception()
            self._error(f"字幕の処理が止まりました: {type(e).__name__}: {e}")

    LEVEL_WINDOW_S = 5.0

    def _tally(self, channel: str, key: str, value: float = 1.0) -> None:
        w = self._win.setdefault(channel, {"t0": time.monotonic(), "n": 0, "pow": 0.0, "max": -90.0, "partials": 0,
                                           "finals": 0})
        if key == "db":
            w["n"] += 1
            w["pow"] += 10 ** (value / 10)
            w["max"] = max(w["max"], value)
        else:
            w[key] += 1
        if time.monotonic() - w["t0"] >= self.LEVEL_WINDOW_S:
            mean = 10 * math.log10(w["pow"] / w["n"]) if w["n"] and w["pow"] > 0 else None
            self.m.emit("CAP", "caption_level", channel=channel, mean_db=round(mean, 1) if mean is not None else None,
                        max_db=round(w["max"], 1), partials=w["partials"], finals=w["finals"])
            self._win[channel] = {"t0": time.monotonic(), "n": 0, "pow": 0.0, "max": -90.0, "partials": 0, "finals": 0}

    def _partial(self, channel: str, text: str) -> None:
        self._tally(channel, "partials")
        self.web.push({"type": "caption", "phase": "partial", "channel": channel, "text": self.replacer.apply(text)},
                      keep=False)

    async def _consume(self, queue: asyncio.Queue) -> None:
        while True:
            u = await queue.get()
            self._tally(u.channel, "finals")
            text = self.replacer.apply(u.text)   # 置き換え辞書(翻訳にも正しい語で渡す)
            self.web.push({"type": "caption", "phase": "final", "channel": u.channel, "text": text, "rid": u.rid},
                          keep=False)
            if self.on_final:
                self.on_final(u.rid, u.channel, text)

    def _diag(self, channel: str, d: dict) -> None:
        if d.get("phase") == "level":
            self._tally(channel, "db", float(d.get("db", -90)))
            return
        if d.get("phase") in ("abort", "error", "results_error", "record_error"):
            self._error(f"{'マイク' if channel == 'mic' else 'スピーカー'}の文字起こしが止まりました: "
                        f"{d.get('reason') or d.get('error') or d.get('phase')}")

    def _error(self, text: str) -> None:
        self.m.emit("CAP", "caption_error", error=text)
        self.web.set_state(caption_error=text)


class Translator:
    """確定した字幕を訳す。target() は呼ぶたびに (Target, 理由) を返す(設定・録音の状態で変わる)。"""

    CONTEXT = 3        # 前の字幕を何行添えるか(言い回しをつなげる)
    MAX_INFLIGHT = 2   # 同時に訳す数(溜まったら古いものは捨てる = 字幕に追いつくことを優先)

    def __init__(self, web, target_fn, *, metrics: Metrics):
        self.web = web
        self.target_fn = target_fn
        self.m = metrics
        self.lang = ""                    # "" = 訳さない / "ja" / "en"
        self._recent: deque = deque(maxlen=self.CONTEXT)
        self._inflight = 0

    def submit(self, rid: str, channel: str, text: str) -> None:
        """イベントループの上から呼ぶ(runner の consume / Pipeline の utterance)。"""
        context = list(self._recent)
        self._recent.append(text)
        if not self.lang or not text.strip():
            return
        target, why = self.target_fn()
        if target is None:
            self.web.push({"type": "caption_tr", "rid": rid, "text": "", "error": why}, keep=False)
            return
        if self._inflight >= self.MAX_INFLIGHT:
            self.m.emit(rid, "caption_tr", ok=False, error="skipped_busy")
            return
        self._inflight += 1
        asyncio.ensure_future(self._run(rid, channel, text, context, target, self.lang))

    async def _run(self, rid: str, channel: str, text: str, context: list[str], target, lang: str) -> None:
        try:
            msgs = build_messages(text, context, lang)
            try:
                r = await asyncio.to_thread(llm.stream, target, msgs, role="main", timeout=30, max_tokens=400)
            except Exception as e:  # noqa: BLE001 — 翻訳の失敗は字幕を止めない。記録して画面に出す
                self.m.emit(rid, "caption_tr", ok=False, error=f"{type(e).__name__}: {e}", lang=lang)
                self.web.push({"type": "caption_tr", "rid": rid, "channel": channel, "text": "",
                               "error": f"翻訳に失敗しました: {type(e).__name__}"}, keep=False)
                return
            out = r.text.strip() if r.ok else ""
            self.m.emit(rid, "caption_tr", ok=r.ok, error=r.error, ms=round(r.ms_total or 0, 1),
                        ms_first_token=r.ms_first_token, provider=target.provider, model=r.model, lang=lang,
                        chars=len(text), warm=r.extra.get("warm") if r.extra else None)
            self.web.push({"type": "caption_tr", "rid": rid, "channel": channel, "text": out,
                           "error": "" if r.ok else (r.error or "failed")}, keep=False)
        finally:
            self._inflight -= 1


def build_messages(text: str, context: list[str], lang: str) -> list[dict]:
    to = TRANSLATE.get(lang, "Japanese")
    sys = (f"You translate live meeting captions into {to}. Output only the {to} translation of the LAST line, "
           "with no quotes, labels, notes or explanations. Keep names and product names as they are. "
           f"If the line is already in {to}, output it unchanged. Speech recognition may contain small errors; "
           "translate the most likely intended meaning.")
    ctx = "\n".join(f"- {c}" for c in context)
    user = (f"Previous lines (context only, do not translate):\n{ctx}\n\n" if ctx else "") + f"LAST line:\n{text}"
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def new_caption_rid() -> str:
    return new_rid()

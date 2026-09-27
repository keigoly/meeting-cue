"""セグメンター — STT の partial / final ストリームを「発話」に切る。

方式(前例のリアルタイム字幕の実測の知見を初版から取り込む):
- partial が更新されるたびに last_change を更新。pending の発話があり、更新が止まって
  しきい値を超えたら helper に finalize を送る(日本語は自動 final が出ない)。
- しきい値は末尾の形で変える: 文末記号なら速く(pause_sentence_ms)、助詞・語中なら待つ
  (pause_incomplete_ms)、それ以外は pause_ms。
- final を文末記号で分割し、相づち・極短は捨てる。助詞で終わる切れ端は次へ繰り越すが、
  flush_carry_ms 待っても続きが来なければそのまま出す(質問の取りこぼし防止)。
- finalize を送っても final が来ないときは safety_ms 後に現在の partial を強制的に発話にする。
- 間が来ないまま話し続ける音声は、発話が long_utterance_ms を超えて partial に文末があれば(または
  long_utterance_hard_ms を超えたら)間を待たずに finalize する。文末より後ろの言いかけは次の発話へ繰り越す。
全判定を metrics に記録する(rid 付き)。
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from .metrics import Metrics, new_rid
from .stt_reader import STTHelper

_SENT_END = "。．.!?！？"
_TRIM = " \t。．.!?！？、,・…\"'’”)）]】"
_JA_PARTICLE = ("から", "まで", "より", "ので", "けど", "たり", "ながら",
                "は", "が", "を", "に", "へ", "と", "で", "も", "の", "や", "て", "し")
_JA_END_RE = re.compile(
    r"(ございます|でしょうか|でしょう|ください|ませんか|ません|ますか|でしたか|でした|ました|ですか|です|ます|"
    r"である|した|する|ある|いる|なる|できる|思う|だ|ね|よ|か|かな)\Z")
_ASCII_RE = re.compile(r"[\x00-\x7f]+\Z")
_JA_CHAR_RE = re.compile(r"[ぁ-んァ-ヶ一-龥ー]\Z")
_EN_FUNC = {"and", "or", "but", "to", "of", "in", "on", "at", "for", "with", "the", "a", "an",
            "that", "is", "are", "was", "were", "by", "from", "as", "so", "if", "when", "will",
            "would", "can", "could", "about", "into", "than", "then", "we", "i", "it", "he",
            "she", "they", "you", "this", "these", "let", "me"}
_SENT_END_RE = re.compile(r"[。．!?！？]+|(?<!\d)\.+(?!\d)")
INCOMPLETE_TAILS = ("particle", "mid_word", "func_word", "empty")


def tail_punct(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and t[-1] in _SENT_END


def tail_kind(text: str) -> str:
    """末尾の形: ja_end / particle / mid_word / func_word / en_other / other / empty。"""
    core = (text or "").strip().strip(_TRIM)
    if not core:
        return "empty"
    if _ASCII_RE.match(core):
        words = re.sub(r"[^\w\s\-]", "", core).strip().lower().split()
        return "func_word" if words and words[-1] in _EN_FUNC else "en_other"
    if _JA_END_RE.search(core):
        return "ja_end"
    for p in _JA_PARTICLE:
        if core.endswith(p):
            return "particle"
    if _JA_CHAR_RE.search(core):
        return "mid_word"
    return "other"


def split_sentences(text: str) -> list[str]:
    out, start = [], 0
    for m in _SENT_END_RE.finditer(text):
        piece = text[start:m.end()].strip()
        if piece:
            out.append(piece)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


@dataclass
class SegmenterConfig:
    """マイク用の既定。file / tap 音源は partial が約 1 秒周期のバーストで届く(2026-09-25 実測:
    ギャップ p50 976 ms / max 1000 ms)ため、しきい値は 1 秒を超える値にする(config の [segmenter_tap])。"""
    pause_ms: int = 500
    pause_incomplete_ms: int = 900
    pause_sentence_ms: int = 300
    poll_ms: int = 100
    safety_ms: int = 2500          # finalize 後に final が来ないときの強制確定
    flush_carry_ms: int = 1500     # 繰り越した切れ端を単独で出すまでの待ち
    max_chars: int = 120
    min_chars: int = 2
    carry_tail: bool = True
    # 話し続ける音声の打ち切り(2026-09-26 実測: 一人語りで partial が約 1 秒ごとに更新され続け、停止検出が
    # 4 分で 2 回しか発火せず、確定が最大 59 s 遅れた)。0 で無効(従来の動き)。
    long_utterance_ms: int = 8000        # 発話がこれより長く続き、partial に文末があれば間を待たずに finalize
    long_utterance_hard_ms: int = 20000  # これを超えたら文末がなくても finalize
    backchannel: tuple[str, ...] = ("はい", "ええ", "うん", "あー", "えー", "なるほど", "そうですね", "はいはい")


@dataclass
class Utterance:
    rid: str
    channel: str
    text: str
    start_s: float | None
    end_s: float | None
    t_final_ms: int            # 発話が確定した壁時計(ms)。E2E 計測の起点
    forced: bool = False       # 強制 finalize 由来か
    tail: str = ""
    extra: dict = field(default_factory=dict)


class Segmenter:
    def __init__(self, helper: STTHelper, channel: str, cfg: SegmenterConfig, m: Metrics,
                 on_partial=None):
        self.helper = helper
        self.channel = channel
        self.cfg = cfg
        self.m = m
        self.on_partial = on_partial
        self._current = ""
        self._last_change = time.perf_counter()
        self._pending = False
        self._finalize_sent = False
        self._finalize_t: float | None = None
        self._carry = ""
        self._carry_t: float | None = None
        self._last_ev: dict = {}
        # 繰り越した切れ端の時刻(2026-09-27): 切り出した final の範囲と位置から見積もる。以前は単独で出すときに
        # 最後の partial の範囲を使い、partial の範囲は前の確定の終わりから始まるため、長い無音の後で最大 60 s 早くなっていた
        self._carry_span: tuple[float | None, float | None] = (None, None)
        self._gaps: list[float] = []      # 現在の発話の partial 更新間隔(ms・≥ 50ms のチャンク間ギャップ)
        self._partials_n = 0
        self._reset_after = False         # final を出した後、次の partial で統計を消す
        self._utt_t0 = time.perf_counter()  # 現在の発話の最初の partial の時刻(長さの打ち切りと age_ms に使う)
        self._long_cut = False            # 長さで finalize を送った(final が来るまで再送しない・切れ端を繰り越す)
        self._long_cont_until = 0.0       # 長さで切った final の直後に届く続きの final(同じ finalize が 2 つに割れる)の受付期限
        self._age_ms: int | None = None
        self.stats = {"partials": 0, "finals": 0, "forced": 0, "safety": 0, "dropped": 0, "utterances": 0, "long": 0}

    async def run(self, out: asyncio.Queue) -> None:
        watcher = asyncio.create_task(self._watch(out))
        try:
            await self._consume(out)
        finally:
            watcher.cancel()
            if self._current.strip():
                await self._emit(self._current, self._last_ev, out, forced=True)
            if self._carry:
                await self._emit_piece(self._carry, self._carry_ev(), out, forced=True)
                self._carry = ""

    async def _consume(self, out: asyncio.Queue) -> None:
        async for ev in self.helper.events():
            t = ev.get("type")
            if t == "partial":
                text = (ev.get("text") or "").strip()
                self.stats["partials"] += 1
                self._last_ev = ev
                if text and text != self._current:
                    now = time.perf_counter()
                    if self._reset_after:
                        self._gaps, self._partials_n, self._reset_after = [], 0, False
                    if self._pending:
                        d = (now - self._last_change) * 1000
                        if d >= 50:
                            self._gaps.append(d)
                    else:
                        self._utt_t0 = now
                    self._partials_n += 1
                    self._current = text
                    self._last_change = now
                    self._pending = True
                    self._finalize_sent = False
                    if self.on_partial:
                        self.on_partial(self.channel, text)
            elif t == "final":
                text = (ev.get("text") or "").strip()
                self.stats["finals"] += 1
                self._reset_after = True
                forced = self._finalize_sent
                if forced:
                    self.stats["forced"] += 1
                self._pending = False
                self._current = ""
                self._finalize_sent = False
                self._finalize_t = None
                if text:
                    await self._emit(text, ev, out, forced=forced)
            elif t == "bye":
                break

    async def _watch(self, out: asyncio.Queue) -> None:
        cfg = self.cfg
        while True:
            await asyncio.sleep(cfg.poll_ms / 1000)
            now = time.perf_counter()
            # 繰り越しの flush
            if self._carry and self._carry_t and (now - self._carry_t) * 1000 >= cfg.flush_carry_ms and not self._pending:
                piece, self._carry, self._carry_t = self._carry, "", None
                self.m.emit("SEG", "carry_flush", channel=self.channel, text=piece)
                await self._emit_piece(piece, self._carry_ev(), out, forced=True)
            if not self._pending:
                continue
            idle_ms = (now - self._last_change) * 1000
            if not self._finalize_sent:
                tk = tail_kind(self._current)
                # 文末記号だけでは速く切らない: STT は節の途中にも句点を打つ(実測 '…の文化。' '…シームと。')。
                # 「用言・丁寧形で終わり、かつ句点付き」のときだけ速い経路にする。
                if tail_punct(self._current) and tk == "ja_end":
                    threshold = cfg.pause_sentence_ms
                elif tk in INCOMPLETE_TAILS:
                    threshold = cfg.pause_incomplete_ms
                else:
                    threshold = cfg.pause_ms
                # 話し続けていて間が来ない: 文末を含むなら長さで切る(切れ端は _emit で次へ繰り越す)
                age_ms = (now - self._utt_t0) * 1000
                long_reason = ""
                if idle_ms < threshold and not self._long_cut:
                    if cfg.long_utterance_ms and age_ms >= cfg.long_utterance_ms and _SENT_END_RE.search(self._current):
                        long_reason = "sentence"
                    elif cfg.long_utterance_hard_ms and age_ms >= cfg.long_utterance_hard_ms:
                        long_reason = "hard"
                if idle_ms >= threshold or long_reason:
                    self._finalize_sent = True
                    self._finalize_t = now
                    if long_reason:
                        self._long_cut = True
                        self.stats["long"] += 1
                        self.m.emit("SEG", "long_finalize", channel=self.channel, reason=long_reason,
                                    age_ms=round(age_ms), idle_ms=round(idle_ms, 1), chars=len(self._current))
                    else:
                        self.m.emit("SEG", "pause_finalize", channel=self.channel, idle_ms=round(idle_ms, 1),
                                    threshold_ms=threshold, tail_kind=tk, chars=len(self._current))
                    await self.helper.finalize()
            elif self._finalize_t and (now - self._finalize_t) * 1000 >= cfg.safety_ms:
                # final が来ない → 現在の partial を強制確定
                text = self._current
                self.stats["safety"] += 1
                self._pending = False
                self._current = ""
                self._finalize_sent = False
                self._finalize_t = None
                self.m.emit("SEG", "safety_finalize", channel=self.channel, chars=len(text))
                await self._emit(text, self._last_ev, out, forced=True)

    def _carry_ev(self) -> dict:
        """繰り越した切れ端を単独で出すときの範囲(見積もりが無ければ従来どおり最後の partial)。"""
        s, e = self._carry_span
        self._carry_span = (None, None)
        return {"start_s": s, "end_s": e} if s is not None else self._last_ev

    async def _emit(self, text: str, ev: dict, out: asyncio.Queue, *, forced: bool) -> None:
        if self._carry and self._carry_span[0] is not None:   # 繰り越しを頭に付ける: 始まりは切れ端の始まり
            cs = self._carry_span[0]
            if ev.get("start_s") is None or cs < ev["start_s"]:
                ev = {**ev, "start_s": cs}
        self._carry_span = (None, None)
        text = (self._carry + text).strip()
        gaps_snapshot, self._gaps, self._partials_n = self._gaps, [], 0
        self._gaps = gaps_snapshot  # _emit_piece が同じ発話の統計を使う。次の partial で下でリセット
        self._carry, self._carry_t = "", None
        long_cut, self._long_cut = self._long_cut, False
        now = time.perf_counter()
        # 2026-09-26 実測: 長さで送った finalize 1 回に final が 2 つ返ることがある(27 回中 2 回・'ほぼ業界' | '初の AI…')
        cont = not long_cut and now < self._long_cont_until   # 長さで切った final の直後の 1 回だけ
        self._long_cont_until = now + 1.5 if long_cut else 0.0
        self._age_ms = round((now - self._utt_t0) * 1000)
        pieces = split_sentences(text)
        # 「まず」「次に」「最後に」のような談話標識だけの短い切れ端(≤ 3 字・文として閉じていない)は
        # 次の発話の頭として繰り越す(2026-09-25 実測: 'まず' が単独の発話になり smalltalk と判定された)。
        if self.cfg.carry_tail and pieces and len(pieces[-1].strip(_TRIM)) <= 3 and tail_kind(pieces[-1]) != "ja_end":
            self._carry = pieces.pop()
            self._carry_t = time.perf_counter()
            self.m.emit("SEG", "carry_tail", channel=self.channel, text=self._carry, chars=len(self._carry), reason="short")
        elif self.cfg.carry_tail and pieces and tail_kind(pieces[-1]) == "particle" and len(pieces[-1]) <= self.cfg.max_chars:
            self._carry = pieces.pop()
            self._carry_t = time.perf_counter()
            self.m.emit("SEG", "carry_tail", channel=self.channel, text=self._carry, chars=len(self._carry))
        elif (self.cfg.carry_tail and pieces and not tail_punct(pieces[-1]) and len(pieces[-1]) <= self.cfg.max_chars
              and ((long_cut and len(pieces) > 1) or cont)):
            # 長さで切った final の文末より後ろ(と、直後に届く続きの final)は言いかけ。単独の発話にせず次の発話の頭へ。
            # 文末のない 1 片だけの final(hard で切った)は繰り越さない(上限の意味がなくなるため)
            self._carry = pieces.pop()
            self._carry_t = time.perf_counter()
            self.m.emit("SEG", "carry_tail", channel=self.channel, text=self._carry, chars=len(self._carry),
                        reason="long_cut_cont" if cont else "long_cut")
        if self._carry:   # 切れ端の始まり = final の範囲を文字数で按分した位置(画面の estimateStarts と同じ考え方)
            s0, e0 = ev.get("start_s"), ev.get("end_s")
            if isinstance(s0, (int, float)) and isinstance(e0, (int, float)) and e0 > s0 and text:
                frac = max(0.0, (len(text) - len(self._carry)) / len(text))
                self._carry_span = (round(s0 + (e0 - s0) * frac, 2), e0)
            else:
                self._carry_span = (s0, e0)
        for piece in pieces:
            await self._emit_piece(piece, ev, out, forced=forced)
        self._age_ms = None

    async def _emit_piece(self, text: str, ev: dict, out: asyncio.Queue, *, forced: bool) -> None:
        for chunk in self._split_long(text.strip()):
            rid = new_rid()
            core = chunk.strip(_TRIM)
            drop = (len(core) < self.cfg.min_chars) or (core in self.cfg.backchannel)
            tk = tail_kind(chunk)
            gaps = sorted(self._gaps)
            self.m.emit(rid, "segment", channel=self.channel, text=chunk, chars=len(chunk), dropped=drop,
                        forced=forced, tail_kind=tk, tail_punct=tail_punct(chunk),
                        audio_start_s=ev.get("start_s"), audio_end_s=ev.get("end_s"),
                        partials=self._partials_n, age_ms=self._age_ms,
                        gap_p50_ms=round(gaps[len(gaps) // 2], 0) if gaps else None,
                        gap_max_ms=round(gaps[-1], 0) if gaps else None)
            if drop:
                self.stats["dropped"] += 1
                continue
            self.stats["utterances"] += 1
            await out.put(Utterance(rid=rid, channel=self.channel, text=chunk,
                                    start_s=ev.get("start_s"), end_s=ev.get("end_s"),
                                    t_final_ms=int(time.time() * 1000), forced=forced, tail=tk))

    def _split_long(self, text: str) -> list[str]:
        if len(text) <= self.cfg.max_chars:
            return [text]
        parts, buf = [], ""
        for ch in text:
            buf += ch
            if ch in "、," + _SENT_END or len(buf) >= self.cfg.max_chars:
                parts.append(buf)
                buf = ""
        if buf:
            parts.append(buf)
        return [p.strip() for p in parts if p.strip()]

"""ターミナル表示(Phase 1 の見える化)。文字起こしとキューを時系列に流す。"""
from __future__ import annotations

import sys

_COLORS = {"mic": "36", "system": "33", "room": "35", "cue": "32", "judge": "90", "know": "34", "err": "31"}


class TerminalUI:
    def __init__(self, *, show_partials: bool = False, color: bool | None = None):
        self.show_partials = show_partials
        self.color = sys.stdout.isatty() if color is None else color
        self._partial_open = False

    def _c(self, key: str, s: str) -> str:
        return f"\033[{_COLORS.get(key, '0')}m{s}\033[0m" if self.color else s

    def _endpartial(self) -> None:
        if self._partial_open:
            sys.stdout.write("\r\033[K")
            self._partial_open = False

    def partial(self, channel: str, text: str) -> None:
        if not self.show_partials or not self.color:
            return
        sys.stdout.write("\r\033[K" + self._c(channel, f"  … [{channel}] {text[-80:]}"))
        sys.stdout.flush()
        self._partial_open = True

    def status(self, msg: str, code: str | None = None) -> None:   # code は Web 画面用(ここでは使わない)
        self._endpartial()
        print(self._c("judge", f"· {msg}"), flush=True)

    def error(self, msg: str, code: str | None = None) -> None:
        self._endpartial()
        print(self._c("err", f"! {msg}"), flush=True)

    def utterance(self, channel: str, text: str, rid: str) -> None:
        self._endpartial()
        print(self._c(channel, f"[{channel}] {text}") + self._c("judge", f"  #{rid}"), flush=True)

    def judgment(self, rid: str, s: dict, trigger: bool, ms: float, source: str) -> None:
        line = (f"    judge({source} {ms:.0f}ms) act={s.get('speech_act')}({s.get('speech_act_p')}) "
                f"to_me={s.get('to_me')} intent={s.get('intent')} len={s.get('answer_length')} → "
                f"{'TRIGGER' if trigger else '-'}")
        print(self._c("judge", line), flush=True)

    def knowledge(self, rid: str, hits: list, terms: list[str], ms: float) -> None:
        if not hits:
            print(self._c("know", f"    knowledge({ms:.0f}ms) terms={terms} → なし"), flush=True)
            return
        print(self._c("know", f"    knowledge({ms:.0f}ms) terms={terms}"), flush=True)
        for h in hits[:3]:
            print(self._c("know", f"      - {h.path}#{h.heading[:30]} :: {h.snippet(terms, 90)}"), flush=True)

    # 質問タブ(U3)
    def plan_start(self, pid: str, trigger: str, model: str) -> None:
        self._endpartial()
        label = {"auto": "自動", "manual": "手動", "deep": "深く考える"}.get(trigger, trigger)
        print(self._c("know", f"  ? 質問タブ #{pid}({label}・{model})"), flush=True)

    def plan_line(self, pid: str, line: str) -> None:
        self._endpartial()
        print(self._c("know", f"    ? {line}"), flush=True)

    def plan_done(self, pid: str, ok: bool, error: str | None, ms_first: float | None, ms_total: float,
                  cost: float | None) -> None:
        self._endpartial()
        msg = f"完了 {ms_total:.0f}ms" if ok else ("回答を優先して中断" if error == "cancelled" else f"失敗 {error}")
        print(self._c("know", f"    ? #{pid} {msg}"), flush=True)

    def cue_line(self, rid: str, line: str) -> None:
        self._endpartial()
        print(self._c("cue", f"    ▶ {line}"), flush=True)

    def ranking(self, rid: str, kind_ja: str, ranked: list, display: int, ms: float, partial: bool = False) -> None:
        """Jev の採点で並べ替えた候補。上位 display 件に ★、点数の内訳を添える。"""
        self._endpartial()
        parts = []
        for i, c in enumerate(ranked):
            mark = "★" if i < display and c.total is not None else "・"
            tot = f"{c.total:.2f}" if c.total is not None else (c.error or "?")
            parts.append(f"{mark}{kind_ja}{c.index}({tot})")
        note = "・途中まで(次の質問で打ち切り)" if partial else ""
        print(self._c("cue", f"    ◆ Jev の選択({ms:.0f}ms{note}): " + "  ".join(parts)), flush=True)
        top = ranked[0] if ranked and ranked[0].total is not None else None
        if top:
            detail = " ".join(f"{k}={v:.2f}" for k, v in top.scores.items())
            print(self._c("judge", f"      一押し {kind_ja}{top.index}: {top.text[:80]}  [{detail}]"), flush=True)

    def cue_done(self, rid: str, ok: bool, ms_first: float | None, ms_total: float, cost: float | None,
                 error: str | None = None) -> None:
        if ok:
            print(self._c("judge", f"    cue done first={ms_first}ms total={ms_total}ms cost=${cost}"), flush=True)
        else:
            print(self._c("err", f"    cue failed: {error} ({ms_total}ms)"), flush=True)

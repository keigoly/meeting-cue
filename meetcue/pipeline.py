"""オーケストレーター — 音源 → セグメンター → 判定 → 検索 → 生成 → 表示・記録(Phase 1)。

1 発話の流れ(docs/REQUIREMENTS.md §6):
  final → Utterance → (mic は記録のみ) → judge(Jev / heuristic) → retrieve(FTS5)
       → trigger なら generate(ストリーミング・新しい質問が来たら古い生成を打ち切る)
全段 rid 付きで metrics に残す。E2E は「発話確定 → 最初のキュー行」を e2e_first_cue で計測する。
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .cues import llm, prompts
from .judge import heuristic, jev, selector
from .knowledge.index import VaultIndex, query_terms, title_rank
from .ledger import Ledger
from .metrics import new_rid
from .replacements import Replacer
from .segmenter import Segmenter, Utterance
from .session import Session
from .stt_reader import STTHelper


@dataclass
class Source:
    kind: str          # mic | file | tap
    channel: str       # mic | system | room
    arg: str = ""      # file path / tap name / pid

    @classmethod
    def parse(cls, spec: str) -> "Source":
        """mic | room | file:PATH[:channel] | tap:NAME | tap-pid:N | tap-all"""
        if spec in ("mic", "room"):
            return cls("mic", spec)
        if spec.startswith("file:"):
            rest = spec[5:]
            ch = "system"
            if rest.count(":") >= 1 and rest.rsplit(":", 1)[1] in ("mic", "system", "room"):
                rest, ch = rest.rsplit(":", 1)
            return cls("file", ch, rest)
        if spec.startswith("tap:"):
            return cls("tap", "system", spec[4:])
        if spec.startswith("tap-pid:"):
            return cls("tap-pid", "system", spec[8:])
        if spec == "tap-all":
            return cls("tap-all", "system", "")
        raise ValueError(f"unknown source: {spec}")

    def argv(self, cfg: Config, platform: str | None = None) -> list[str]:
        if (platform or sys.platform) == "win32":
            return self._argv_windows(cfg)
        stt = str(cfg.helpers_dir / "stt_helper" / "stt-helper")
        tap = str(cfg.helpers_dir / "tap_helper" / "tap-helper")
        if self.kind == "mic":
            return [stt, "--locale", cfg.locale, "--channel", self.channel]
        if self.kind == "file":
            return [stt, "--locale", cfg.locale, "--channel", self.channel, "--file", self.arg,
                    "--pace", str(cfg.file_pace)]
        if self.kind == "tap":
            return [tap, "--locale", cfg.locale, "--channel", self.channel, "--name", self.arg]
        if self.kind == "tap-pid":
            return [tap, "--locale", cfg.locale, "--channel", self.channel, "--pid", self.arg]
        if self.kind == "tap-all":
            return [tap, "--locale", cfg.locale, "--channel", self.channel, "--exclude-pid", str(os.getpid())]
        raise ValueError(self.kind)

    def _argv_windows(self, cfg: Config) -> list[str]:
        """Windows: 専用の仮想環境の python で helpers/windows/stt_helper/stt_helper.py を動かす(FR-12・契約は FR-2 と同じ)。
        取り込みは WASAPI(PyAudioWPatch)。tap 系はどれも出力機器のループバック(機器単位・会議アプリだけは選べない)。"""
        base = [str(cfg.stt_python), str(cfg.helpers_dir / "stt_helper" / "stt_helper.py"),
                "--locale", cfg.locale, "--channel", self.channel]
        lb = ["--loopback-device", cfg.loopback_device] if cfg.loopback_device else []
        if self.kind == "mic":
            return base + (["--mic-device", cfg.mic_device] if cfg.mic_device else [])
        if self.kind == "file":
            return base + ["--file", self.arg, "--pace", str(cfg.file_pace)]
        if self.kind == "tap":
            return base + ["--loopback", f"name:{self.arg}"] + lb
        if self.kind == "tap-pid":
            return base + ["--loopback", f"pid:{self.arg}"] + lb
        if self.kind == "tap-all":
            return base + ["--loopback", "all", "--exclude-pid", str(os.getpid())] + lb
        raise ValueError(self.kind)


def helper_hint(platform: str | None = None) -> str:
    """ヘルパーが無いときの案内(OS ごと)。"""
    if (platform or sys.platform) == "win32":
        return r"packaging\windows\setup.ps1 を実行"
    return "helpers/macos で make"


def segmenter_config(cfg: Config, src: Source):
    """区切りの設定。自分の声(mic)は長めに区切って文脈を保つ(2026-09-27)。対面の相手(room)はマイク音源の短い値で
    判定を急ぐ。会議アプリの出力(tap)と相手側のファイルは partial が約 1 秒周期なので 1 秒超。ライブ字幕も同じ値を使う。"""
    if src.channel == "mic":
        return cfg.segmenter_self
    return cfg.segmenter if src.kind == "mic" else cfg.segmenter_tap


_UNSET = object()


class Pipeline:
    def __init__(self, cfg: Config, session: Session, ui, sources: list[Source], *,
                 api_key: str | None, llm_enabled: bool = True, jev_enabled: bool = True, model: str | None = None,
                 target: llm.Target | None = None, jev_api_key=_UNSET, stt: str = "local", soniox_key: str | None = None):
        self.cfg = cfg
        self.session = session
        self.ui = ui
        self.sources = sources
        # 会議の文字起こし(FR-2b・2026-10-18): soniox = 音を Soniox へ送る(会議ごとに選ぶ・LOCAL では使わない)。
        # 届かないチャネルは端末内の認識に切り替わる(meetcue.soniox_stt)
        self.stt = "soniox" if stt == "soniox" and cfg.privacy != "local" else "local"
        self.soniox_key = soniox_key if self.stt == "soniox" else None
        self._stt_fallback: dict[str, str] = {}   # 端末内に切り替わったチャネル → 理由
        self.api_key = api_key if cfg.privacy != "local" else None
        self.llm_enabled = llm_enabled and self.api_key is not None
        # 質問の判定(2026-09-26 設定画面): オフなら判定そのものをしない(回答タブは動かず、選ぶ係の ★ も付けない)。
        # オンでも LOCAL / 鍵なしなら従来どおりヒューリスティック。jev_key = Jev に渡すキー(オフなら None)
        # 生成の接続先(2026-09-26 第 2 段): OpenRouter / Anthropic / OpenAI。Jev は接続先によらず OpenRouter のキー(jev_api_key)。
        # jev_api_key を渡さない呼び出し(meetcue run・試験)は従来どおり api_key(OpenRouter)を Jev にも使う
        self.jev_enabled = jev_enabled
        jk = self.api_key if jev_api_key is _UNSET else jev_api_key
        self.jev_key = jk if (jev_enabled and cfg.privacy != "local") else None
        self.model = target.model if target else (model or cfg.llm.model)
        self.target = target or llm.Target("openrouter", self.api_key, self.model, cfg.llm.fast_model,
                                           cfg.planner.deep_model, {}, cfg.llm.provider_order or None,
                                           mid_model=cfg.planner.mid_model)
        self.target.api_key = self.api_key   # LOCAL ではキーを持たせない
        self.m = session.metrics
        self.replacer = Replacer(cfg.app_dir / "replacements.txt")   # 置き換え辞書(⚙ で編集・更新時刻で読み直す)
        self.index = VaultIndex(cfg.index_db) if cfg.index_db.exists() else None
        self.ledger = Ledger(cfg.ledger_path, cfg.llm.monthly_budget_usd)
        self.context: list[str] = []
        self._helpers: list[STTHelper] = []
        self._gen_tasks: list[asyncio.Task] = []
        self._gen_stop = threading.Event()
        self._first_cue: dict[str, int] = {}   # rid → 最初のキュー行までの ms(2 ストリームで 1 回だけ)
        self.paused = False
        self._last_other: tuple | None = None   # (Utterance, prev, summary, hits) 直近の相手の発話(deepdive 用)
        self._recent: deque = deque(maxlen=6)     # 直近の相手の発話(deepdive は質問らしいものを優先)
        self._aux: list[asyncio.subprocess.Process] = []   # hotkey / overlay helper
        self.hotkeys = True
        self.hotkey_emit = ""        # テスト用: "pause@2,deepdive@4"
        self.overlay = False
        self.overlay_url = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_ev: asyncio.Event | None = None
        self._stop_t0: float | None = None   # 「録音を停止」を受けた時刻(perf_counter)。停止の内訳(phase=stop)に使う
        self._gen_after_stop = 0             # 停止を受けた後に始まった回答の生成の数
        self._queue: asyncio.Queue = asyncio.Queue()
        self.counts = {"utterances": 0, "judged": 0, "triggered": 0, "generated": 0, "failed": 0}
        self._levels: dict[str, dict] = {}   # channel → 5 s 集計と無音の追跡(_level)
        self._level_series: dict[str, list[int]] = {}   # channel → 0.1 s ごとの dB(録音後の波形・levels.json)
        self._rec_paths: set[str] = set()
        # 質問タブ(FR-6c・U3)と優先制御(U4)
        self._convo: deque = deque(maxlen=400)          # (channel, text) 会話の全体(質問タブに渡す)
        self._plan_task: asyncio.Task | None = None
        self._plan_stop = threading.Event()
        self._plan_pid = ""
        self._plan_last = -1e9                          # 最後に自動で考えた時刻(monotonic)
        self._plan_chars = 0                            # 前回から進んだ会話の字数
        self._plan_seq = 0
        self._plan_pending: str | None = None           # 回答中に押された手動の依頼(回答が終わったら実行)
        self._answer_active = False                     # 相手の質問への回答を生成中
        self._plan_block_until = 0.0                    # 回答の後の冷却(monotonic)
        self.plan_auto = cfg.planner.enabled and cfg.planner.auto
        # 深掘り(FR-6d・2026-10-08): 相手の発言(rid)→ 掘り下げに要るもの。押されたら mid(Sonnet)/ deep(Opus)を別タスクで。
        # 手動の依頼なので次の質問では打ち切らない(止め札は停止のときだけ立てる)。(rid, 段) で冪等
        self._utts: dict[str, tuple] = {}
        self._digs: dict[tuple, asyncio.Task] = {}
        self._dig_stop = threading.Event()
        # Obsidian の MCP 検索(FR-5b)。App が渡す: (語のリスト, 件数) → [Hit]。None なら索引だけ
        self.mcp_search = None
        # 用語タブ(FR-6e・2026-10-08): 確定した発言(相手・自分)を貯め、間隔と字数を満たしたら Haiku で専門用語を拾う
        self.gloss_auto = cfg.glossary.enabled and cfg.glossary.auto
        self._gloss_buf: list[tuple[str, str]] = []
        self._gloss_seen: set[str] = set()      # 出した語(正規化)。1 回の録音で 1 度だけ
        self._gloss_task: asyncio.Task | None = None
        self._gloss_last = -1e9
        self._gloss_seq = 0
        self._gloss_pending = False             # 回答中に押された手動の依頼

    MODES = ("participant", "presenter", "audience")

    # ---- 操作(ホットキー / Web UI のボタン) ------------------------------------------
    def action_threadsafe(self, name: str, body: dict | None = None) -> None:
        """別スレッド(HTTP サーバ)から呼ぶ。"""
        if self._loop:
            self._loop.call_soon_threadsafe(self.action, name, body)

    def action(self, name: str, body: dict | None = None) -> None:
        if name in ("toggle_pause", "pause", "resume"):
            new = (not self.paused) if name == "toggle_pause" else (name == "pause")
            if new != self.paused:
                self.paused = new
                self.m.emit("CTL", "pause" if new else "resume")
                self.ui.status("一時停止(文字起こしは続く・判定と生成を止める)" if new else "再開")
                self._set_state(paused=new)
        elif name == "mode":
            i = self.MODES.index(self.cfg.mode) if self.cfg.mode in self.MODES else 0
            self.cfg.mode = self.MODES[(i + 1) % len(self.MODES)]
            self.m.emit("CTL", "mode", mode=self.cfg.mode)
            self.ui.status(f"mode → {self.cfg.mode}")
            self._set_state(mode=self.cfg.mode)
        elif name == "deepdive":
            if not self._recent:
                self.ui.status("深掘りする発話がまだありません")
                return
            # 直近 6 発話のうち質問・依頼らしいものを優先(相づちで深掘りしない)。無ければ直近。
            pick = None
            for item in reversed(self._recent):
                s = item[2] or heuristic.judge(item[0].text, mode=self.cfg.mode, channel=item[0].channel)
                if s.get("speech_act") in ("question", "request"):
                    pick = (item[0], item[1], s, item[3])
                    break
            if pick is None:
                u0, prev0, s0, h0 = self._recent[-1]
                pick = (u0, prev0, s0 or heuristic.judge(u0.text, mode=self.cfg.mode, channel=u0.channel), h0)
            u, prev, summary, hits = pick
            self.m.emit(u.rid, "deepdive")
            self.ui.status(f"深掘り: #{u.rid} {u.text[:40]}")
            asyncio.ensure_future(self._start_generation(u, prev, summary, hits, force=True))
        elif name == "dig":
            b = body or {}
            self._request_dig(str(b.get("rid") or ""), str(b.get("level") or ""))
        elif name == "glossary":
            self._request_glossary()
        elif name == "glossary_auto":
            self.gloss_auto = not self.gloss_auto
            self.m.emit("CTL", "glossary_auto", on=self.gloss_auto)
            self.ui.status("用語タブ: 自動で拾う " + ("オン" if self.gloss_auto else "オフ"))
            self._set_state(glossary_auto=self.gloss_auto)
        elif name in ("plan", "plan_mid", "plan_deep"):
            self._request_plan({"plan_deep": "deep", "plan_mid": "mid"}.get(name, "manual"))
        elif name == "plan_auto":
            self.plan_auto = not self.plan_auto
            self.m.emit("CTL", "plan_auto", on=self.plan_auto)
            self.ui.status("質問タブ: 自動で考える " + ("オン" if self.plan_auto else "オフ"))
            self._set_state(plan_auto=self.plan_auto)
        elif name in ("hide", "clear"):
            pass   # overlay 側 / 画面側で処理
        else:
            self.ui.error(f"unknown action: {name}")

    def _set_state(self, **kv) -> None:
        fn = getattr(self.ui, "set_state", None)
        if fn:
            fn(**kv)

    async def _spawn_aux(self) -> None:
        hk = self.cfg.helpers_dir / "hotkey_helper" / "hotkey-helper"
        if self.hotkeys and hk.exists():
            argv = [str(hk)] + (["--emit", self.hotkey_emit] if self.hotkey_emit else [])
            p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self._aux.append(p)
            asyncio.create_task(self._read_hotkeys(p))
        ov = self.cfg.helpers_dir / "overlay_helper" / "overlay-helper"
        if self.overlay and ov.exists() and self.overlay_url:
            p = await asyncio.create_subprocess_exec(str(ov), "--url", self.overlay_url, stdin=asyncio.subprocess.PIPE,
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            self._aux.append(p)
            asyncio.create_task(self._read_diag(p, "overlay"))
        elif self.overlay:
            self.ui.error(f"overlay helper が無い: {ov}(helpers/macos で make)")

    async def _read_hotkeys(self, p) -> None:
        import json as _json
        assert p.stdout
        async for raw in p.stdout:
            try:
                ev = _json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if ev.get("type") == "ready":
                self.m.emit("CTL", "hotkeys_ready", status=ev.get("status"))
                self.ui.status("hotkeys: ⌃⌥P 一時停止 / ⌃⌥D 深掘り / ⌃⌥M モード" + (" / ⌃⌥L 固定 / ⌃⌥H 表示" if self.overlay else ""))
            elif ev.get("type") == "hotkey":
                self.m.emit("CTL", "hotkey", name=ev.get("name"), test=ev.get("test", False))
                name = str(ev.get("name"))
                self.action("toggle_pause" if name == "pause" else name)

    async def _read_diag(self, p, label: str) -> None:
        import json as _json
        assert p.stderr
        async for raw in p.stderr:
            try:
                d = _json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            self.m.emit("CTL", f"{label}_{d.get('phase', 'diag')}", **{k: v for k, v in d.items() if k not in ("phase", "t_ms")})

    async def _stop_aux(self) -> None:
        for p in self._aux:
            if p.returncode is None:
                try:
                    if p.stdin:
                        p.stdin.write(b"quit\n")
                        await p.stdin.drain()
                except (ConnectionError, RuntimeError):
                    pass
                try:
                    await asyncio.wait_for(p.wait(), timeout=2)
                except asyncio.TimeoutError:
                    p.terminate()

    # ---- 起動・停止 ----------------------------------------------------------
    def _segmenter_config(self, src: Source):
        return segmenter_config(self.cfg, src)

    def request_stop(self) -> None:
        """別スレッド・別タスクから止める(meetcue app の「録音を停止」)。Ctrl-C と同じ後始末を通る。"""
        if self._loop and self._stop_ev:
            if self._stop_t0 is None:
                self._stop_t0 = time.perf_counter()
                self.m.emit("STOP", "stop_requested")
            self._loop.call_soon_threadsafe(self._stop_ev.set)

    async def run(self, *, seconds: float | None = None, install_signals: bool = True) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._stop_ev = asyncio.Event()
        self._set_state(plan_auto=self.plan_auto, glossary_auto=self.gloss_auto)
        self._set_state(mode=self.cfg.mode, privacy=self.cfg.privacy, paused=False,
                        session=self.session.dir.name, llm=self.model if self.llm_enabled else "off",
                        stt=self.stt, stt_fallback={})
        await self._spawn_aux()
        seg_tasks = []
        rec_dir = getattr(self.session, "dir", None)
        terms: list[str] = []
        if self.stt == "soniox":
            from .dictation import load_terms
            terms = load_terms(self.cfg.soniox.terms_file)
            if hasattr(self.session, "meta"):
                from . import soniox
                self.session.meta["stt"] = {"engine": "soniox", "model": soniox.MODEL, "region": self.cfg.soniox.region,
                                            "terms": len(terms), "fallback": {}}
                self.session.write_meta()
        for src in self.sources:
            argv = src.argv(self.cfg)
            if self.cfg.record_audio and rec_dir is not None:   # 録音(U2): 文字起こしと同じ音声を m4a に
                out = Path(rec_dir) / "audio" / f"{src.channel}.m4a"
                n = 2
                while out.exists() or str(out) in self._rec_paths:
                    out = Path(rec_dir) / "audio" / f"{src.channel}-{n}.m4a"
                    n += 1
                self._rec_paths.add(str(out))
                argv += ["--record", str(out)]
            if not Path(argv[0]).exists():
                self.ui.error(f"helper が無い: {argv[0]}({helper_hint()})")
                continue
            seg_cfg = self._segmenter_config(src)
            if self.stt == "soniox":   # 取り込みと録音はヘルパーのまま。同じ音を受け取って Soniox へ流す
                from .soniox_stt import SonioxSTT
                helper = SonioxSTT(argv + ["--pcm-out"], channel=src.channel, api_key=self.soniox_key,
                                   on_diag=lambda d, c=src.channel: self._diag(c, d), metrics=self.m, ui=self.ui,
                                   terms=terms, language_hints=self.cfg.soniox.language_hints,
                                   region=self.cfg.soniox.region, on_engine=self._stt_engine,
                                   fin_deadline_s=max(0.3, seg_cfg.safety_ms / 1000 - 0.5))   # セグメンターより先に final
            else:
                helper = STTHelper(argv, channel=src.channel, on_diag=lambda d, c=src.channel: self._diag(c, d))
            await helper.start()
            self._helpers.append(helper)
            seg = Segmenter(helper, src.channel, seg_cfg, self.m, on_partial=self._partial)
            seg_tasks.append(asyncio.create_task(seg.run(self._queue)))
        if not self._helpers:
            return
        self.ui.status(f"sources={[s.kind + ':' + s.channel for s in self.sources]} mode={self.cfg.mode} "
                       f"privacy={self.cfg.privacy} stt={self.stt} llm={'on:' + self.target.label if self.llm_enabled else 'off'} "
                       f"jev={('on' if self.jev_key else 'off(heuristic)') if self.jev_enabled else 'off(no detection)'} "
                       f"index={'on' if self.index else 'off'}")
        consumer = asyncio.create_task(self._consume())
        stop = self._stop_ev
        if install_signals:   # meetcue app ではアプリ側が signal を受けて request_stop する
            try:
                loop.add_signal_handler(2, stop.set)   # SIGINT
                loop.add_signal_handler(15, stop.set)  # SIGTERM
            except (NotImplementedError, RuntimeError):
                pass
        try:
            if seconds:
                waiter = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait({waiter, *seg_tasks}, timeout=seconds,
                                             return_when=asyncio.FIRST_COMPLETED)
                # file 音源はヘルパーの終了(bye)で seg_task が終わる → 少し待って消化
                await asyncio.sleep(0.5)
                waiter.cancel()
            else:
                waiter = asyncio.create_task(stop.wait())
                await asyncio.wait({waiter, *seg_tasks}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
        finally:
            # 停止の内訳(2026-09-26 Step 1: 「停止を押してもすぐ止まらない」)。phase=stop に各段の ms を 1 行で残す
            t_fin = time.perf_counter()
            if self._stop_t0 is None:   # 停止ボタン以外(ヘルパーの終了・Ctrl-C・秒数指定)で止まった
                self._stop_t0 = t_fin
            for t in seg_tasks:
                if not t.done():
                    t.cancel()
            for h in self._helpers:
                await h.stop()
            t_helpers = time.perf_counter()
            self._save_levels()
            await self._stop_aux()
            # 音の取り込みはここで終わり。画面の「録音中」はすぐ外し、作りかけの回答とサマリは「保存中」のまま
            # 裏で仕上げる(2026-09-26 keigoly様 選択: 停止を押してもすぐ止まらない問題)
            self._set_state(recording=False, saving=True)
            # キューに残った発話を消化してから止める
            t_aux = time.perf_counter()
            await self._queue.join()
            t_queue = time.perf_counter()
            self._plan_stop.set()   # 質問タブは停止時に打ち切る(回答の生成だけ最後まで待つ)
            pending = [t for t in self._gen_tasks if not t.done()] + [t for t in self._digs.values() if not t.done()]
            if self._plan_task and not self._plan_task.done():
                pending.append(self._plan_task)
            if self._gloss_task and not self._gloss_task.done():
                pending.append(self._gloss_task)
            timed_out = False
            if pending:
                try:
                    await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=25)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._gen_stop.set()
                    self._dig_stop.set()
                    timed_out = True
            consumer.cancel()
            t_end = time.perf_counter()
            ms = lambda a, b: round((b - a) * 1000, 1)  # noqa: E731
            self.m.emit("STOP", "stop", ms=ms(self._stop_t0, t_end), wake_ms=ms(self._stop_t0, t_fin),
                        helpers_ms=ms(t_fin, t_helpers), aux_ms=ms(t_helpers, t_aux), queue_ms=ms(t_aux, t_queue),
                        gen_wait_ms=ms(t_queue, t_end), pending=len(pending), timed_out=timed_out,
                        gen_after_stop=self._gen_after_stop)

    # 音量(helper の stderr・0.1 s ごと)。画面の波形へ流し、記録には 5 s ごとの集計だけ残す。
    # 相手側が SILENCE_WARN_S 秒続けて SILENCE_DB 未満なら警告(2026-09-26: 別の機器で再生した音が tap に入らず、
    # 無音のまま 5 分気づけなかった)。
    LEVEL_WINDOW_S = 5.0
    SILENCE_DB = -60.0
    SILENCE_WARN_S = 30.0

    def _level(self, channel: str, d: dict) -> None:
        db = float(d.get("db", -90))
        self._level_series.setdefault(channel, []).append(round(db))
        fn = getattr(self.ui, "level", None)
        if fn:
            fn(channel, db, float(d.get("peak_db", db)))
        st = self._levels.setdefault(channel, {"n": 0, "pow": 0.0, "max": -90.0, "t0": time.monotonic(),
                                               "quiet_since": None, "warned": False})
        st["n"] += 1
        st["pow"] += 10 ** (db / 10)
        st["max"] = max(st["max"], db)
        now = time.monotonic()
        if now - st["t0"] >= self.LEVEL_WINDOW_S:
            avg = 10 * math.log10(st["pow"] / st["n"]) if st["pow"] > 0 else -90.0
            self.m.emit("STT", "level_5s", channel=channel, db_avg=round(avg, 1), db_max=round(st["max"], 1), n=st["n"])
            if channel != "mic":
                if st["max"] < self.SILENCE_DB:
                    st["quiet_since"] = st["quiet_since"] or st["t0"]
                    if not st["warned"] and now - st["quiet_since"] >= self.SILENCE_WARN_S:
                        st["warned"] = True
                        self.m.emit("STT", "silence_warning", channel=channel, quiet_s=round(now - st["quiet_since"]))
                        self.ui.error(f"[{channel}] 相手側の音が {int(now - st['quiet_since'])} 秒届いていません。"
                                      "会議の音声がこの Mac から出ているか(別の機器で再生していないか)を確認してください",
                                      code="silence")
                else:
                    if st["warned"]:
                        self.m.emit("STT", "silence_end", channel=channel, quiet_s=round(now - (st["quiet_since"] or now)))
                        self.ui.status(f"[{channel}] 相手側の音が戻りました", code="silence_end")
                    st["quiet_since"], st["warned"] = None, False
            st.update(n=0, pow=0.0, max=-90.0, t0=now)

    def _save_levels(self) -> None:
        d = getattr(self.session, "dir", None)
        if d is None or not self._level_series:
            return
        import json as _json
        (Path(d) / "levels.json").write_text(
            _json.dumps({"step_s": 0.1, "channels": self._level_series}, separators=(",", ":")), encoding="utf-8")

    def _stt_engine(self, channel: str, engine: str, reason: str | None) -> None:
        """Soniox のチャネルが端末内の認識に切り替わった(FR-2b)。記録(meta)と画面の印に残す。"""
        meta = getattr(self.session, "meta", None)
        if meta is not None and isinstance(meta.get("stt"), dict):
            meta["stt"].setdefault("fallback", {})[channel] = reason
            self.session.write_meta()
        self._stt_fallback[channel] = reason or ""
        self._set_state(stt_fallback=dict(self._stt_fallback))

    def _diag(self, channel: str, d: dict) -> None:
        phase = d.get("phase") or "diag"
        if phase == "level":
            self._level(channel, d)
            return
        if phase == "record_start" and hasattr(self.session, "meta"):   # 録音の頭の時刻(マイクとスピーカーの頭合わせ)
            rel = os.path.relpath(str(d.get("path", "")), str(self.session.dir))
            self.session.meta.setdefault("audio", {})[channel] = {"path": rel, "t0_ms": d.get("t0_ms")}
            self.session.write_meta()
        if phase == "record_error":
            self.ui.error(f"[{channel}] 録音の保存に失敗: {d.get('error')}")
        self.m.emit("STT", f"helper_{phase}", channel=channel, **{k: v for k, v in d.items() if k not in ("phase", "t_ms")})
        if phase in ("abort", "error", "results_error"):
            self.ui.error(f"[{channel}] helper {phase}: {d}")

    # ---- 1 発話の処理 ---------------------------------------------------------
    async def _consume(self) -> None:
        while True:
            u: Utterance = await self._queue.get()
            try:
                await self._handle(u)
                self._maybe_plan()   # 質問タブ(自動)。別タスクで動かし、判定の列は止めない
                self._maybe_glossary()   # 用語タブ(自動)。同じく別タスク
            except Exception as e:  # noqa: BLE001 — 1 発話の失敗で止めない
                self.counts["failed"] += 1
                self.m.emit(u.rid, "handle_error", error=f"{type(e).__name__}: {e}")
                self.ui.error(f"#{u.rid} {type(e).__name__}: {e}")
            finally:
                self._queue.task_done()

    def _partial(self, channel: str, text: str) -> None:
        self.ui.partial(channel, self.replacer.apply(text))

    async def _handle(self, u: Utterance) -> None:
        self.counts["utterances"] += 1
        raw = u.text   # 置き換え辞書(2026-09-27): 記録・判定・回答候補の前に。置き換える前の文は raw に残す
        u.text = self.replacer.apply(raw)
        if u.text != raw:
            u.extra["raw"] = raw
        self.session.write_transcript(u)
        self.ui.utterance(u.channel, u.text, u.rid)
        prev = list(self.context)
        self.context.append(f"[{u.channel}] {u.text}")
        self.context = self.context[-20:]
        self._convo.append((u.channel, u.text))
        self._gloss_buf = (self._gloss_buf + [(u.channel, u.text)])[-60:]
        self._plan_chars += len(u.text)
        if u.channel == "mic":
            return
        if self.paused:
            self.m.emit(u.rid, "skipped_paused")
            self._remember(u, prev, None, [])
            return
        if not self.jev_enabled:   # 設定で判定をオフ: 文字起こしと録音だけ(深掘り ⌃⌥D は手動なので使える)
            self.m.emit(u.rid, "judge_skipped", reason="jev_off")
            self._remember(u, prev, None, [])
            return
        # 判定 -------------------------------------------------------------
        cfg = self.cfg
        summary: dict
        source = "heuristic"
        t0 = time.perf_counter()
        if self.jev_key:
            state = jev.build_state(u.text, [p.split("] ", 1)[-1] for p in prev], cfg.mode, u.channel)
            r = await asyncio.to_thread(jev.decide, state, api_key=self.jev_key, model=cfg.jev.model,
                                        timeout=cfg.jev.timeout_s)
            if r.ok:
                summary = jev.summarize(r.answers)
                source = "jev"
                self.ledger.record(r.cost_usd)
            else:
                summary = heuristic.judge(u.text, mode=cfg.mode, channel=u.channel)
                self.m.emit(u.rid, "jev_failed", error=r.error, rc=r.rc, detail=r.detail[:120])
            jev_ms = r.ms
        else:
            summary = heuristic.judge(u.text, mode=cfg.mode, channel=u.channel)
            jev_ms = None
        ms = round((time.perf_counter() - t0) * 1000, 1)
        trigger = jev.should_trigger(summary, act_min=cfg.jev.act_min, to_me_min=cfg.jev.to_me_min)
        self.counts["judged"] += 1
        self.m.emit(u.rid, "judge", ms=ms, ok=True, source=source, jev_ms=jev_ms, trigger=trigger,
                    speech_act=summary.get("speech_act"), speech_act_p=summary.get("speech_act_p"),
                    to_me=summary.get("to_me"), intent=summary.get("intent"))
        self.session.write_judgment(u.rid, {"source": source, "ms": ms, "trigger": trigger, "summary": summary})
        self.ui.judgment(u.rid, summary, trigger, ms, source)
        # 検索 -------------------------------------------------------------
        hits: list = []
        terms = query_terms(u.text)
        if self.index and (trigger or float(summary.get("needs_knowledge") or 0) >= 0.5):
            t1 = time.perf_counter()
            hits = self.index.search(u.text, k=cfg.index.top_k, terms=terms)
            rms = round((time.perf_counter() - t1) * 1000, 1)
            self.m.emit(u.rid, "retrieve", ms=rms, ok=True, hits=len(hits), terms=terms)
            self.ui.knowledge(u.rid, hits, terms, rms)
            if hits:
                self.session.write_cue(u.rid, {"kind": "knowledge", "ms": rms, "terms": terms,
                                               "items": [{"path": h.path, "heading": h.heading,
                                                          "snippet": h.snippet(terms, 200)} for h in hits]})
                if not trigger:
                    self.m.emit(u.rid, "e2e_first_cue", ms=int(time.time() * 1000) - u.t_final_ms, kind="knowledge")
        self._last_other = (u, prev, summary, hits)
        self._remember(u, prev, summary, hits)
        if not trigger:
            return
        self.counts["triggered"] += 1
        await self._start_generation(u, prev, summary, hits)

    UTT_KEEP = 300   # 深掘りできる相手の発言の数(古いものから忘れる)

    def _remember(self, u: Utterance, prev: list[str], summary: dict | None, hits: list) -> None:
        self._recent.append((u, prev, summary, hits))
        self._utts[u.rid] = (u, prev, summary, hits)
        while len(self._utts) > self.UTT_KEEP:
            self._utts.pop(next(iter(self._utts)))

    async def _start_generation(self, u: Utterance, prev: list[str], summary: dict, hits: list, *,
                                force: bool = False) -> None:
        cfg = self.cfg
        if self._stop_t0 is not None:
            self._gen_after_stop += 1
        # U4: 相手からの質問が最優先。考え中の質問タブは即座に打ち切る(回答の生成と API を取り合わない)
        if self._plan_task and not self._plan_task.done():
            self._plan_stop.set()
            self.m.emit(u.rid, "plan_preempt", plan=self._plan_pid)
        if force and not hits and self.index:
            hits = self.index.search(u.text, k=cfg.index.top_k, terms=query_terms(u.text))
        if not self.llm_enabled:
            self.m.emit(u.rid, "generate_skipped", reason="llm_off")
            return
        if not self.ledger.allowed():
            self.m.emit(u.rid, "generate_skipped", reason="budget_exceeded", spent=self.ledger.data["spent_usd"])
            self.ui.error(f"月予算 ${self.cfg.llm.monthly_budget_usd} を超過(${self.ledger.data['spent_usd']})。ナレッジカードのみ")
            return
        # 生成(前の生成が走っていれば打ち切る)。回答と逆質問は別ストリームで並行(総時間が半分) ----------
        if any(not t.done() for t in self._gen_tasks):
            self._gen_stop.set()
            self.m.emit(u.rid, "generate_preempt")
        self._gen_stop = threading.Event()
        stop_flag = self._gen_stop
        sel = cfg.selector
        use_sel = bool(sel.enabled and self.jev_key)
        n_a = sel.n_answers if use_sel else 3
        n_c = sel.n_counters if use_sel else 3
        common = dict(mode=cfg.mode, profile=cfg.profile, context=prev, utterance=u.text, judgment=summary,
                      hits=hits, n_answers=n_a, n_counters=n_c)
        msgs_a = prompts.build_messages(parts=("intent", "answers"), **common)
        msgs_c = prompts.build_messages(parts=("counters",), **common)
        self._gen_tasks = [
            asyncio.create_task(self._generate(u, msgs_a, stop_flag, prev, summary, use_sel, "answers")),
            asyncio.create_task(self._generate(u, msgs_c, stop_flag, prev, summary, use_sel, "counters")),
        ]
        self._answer_active = True
        asyncio.create_task(self._after_answers(list(self._gen_tasks)))

    # ---- 質問タブ(FR-6c・U3)と優先制御(U4) ----------------------------------------------------
    def _ui(self, name: str, *a, **kw) -> None:
        fn = getattr(self.ui, name, None)   # ターミナルだけの UI には無い出来事もある
        if fn:
            fn(*a, **kw)

    def _plan_blocked(self, *, manual: bool = False) -> str | None:
        """質問タブを今始められない理由(None = 始めてよい)。相手の質問への回答が常に先。"""
        if not (self.cfg.planner.enabled and self.llm_enabled):
            return "llm_off"
        if self.paused and not manual:
            return "paused"
        if self._answer_active or any(not t.done() for t in self._gen_tasks):
            return "answering"
        if not manual and time.monotonic() < self._plan_block_until:
            return "cooldown"
        if self._plan_task and not self._plan_task.done():
            return "running"
        if not self.ledger.allowed():
            return "budget"
        return None

    def _maybe_plan(self) -> None:
        """自動: 話が進んだら(前回から min_new_chars 字・interval_s 秒以上)main(Haiku 5.5)で考える。"""
        pc = self.cfg.planner
        if not self.plan_auto or self._plan_blocked():
            return
        if time.monotonic() - self._plan_last < pc.interval_s or self._plan_chars < pc.min_new_chars:
            return
        self._launch_plan("auto")

    def _request_plan(self, kind: str) -> None:
        """手動(今の話で質問を考える = Haiku / 少し深く = Sonnet / 深く考える = Opus)。回答中なら終わってから。"""
        why = self._plan_blocked(manual=True)
        if why == "answering":
            self._plan_pending = kind
            self.ui.status("相手の質問への回答を優先しています。終わったら質問を考えます")
            return
        if why == "running":
            if kind not in ("mid", "deep"):
                self.ui.status("質問を考えています…")
                return
            self._plan_stop.set()   # 少し深く・深く考える は自動の途中でも差し替える
            self._plan_pending = kind
            return
        if why:
            self.ui.status({"llm_off": "生成が止まっているため質問タブは使えません(LOCAL / 鍵なし)",
                            "budget": "月の予算を超えたため質問タブは止めています"}.get(why, why))
            return
        if not self._convo:
            self.ui.status("まだ会話がありません")
            return
        self._launch_plan(kind)

    def _launch_plan(self, kind: str) -> None:
        self._plan_seq += 1
        self._plan_pid = pid = f"P{self._plan_seq:02d}{new_rid()[:6]}"
        self._plan_last = time.monotonic()
        self._plan_chars = 0
        self._plan_stop = threading.Event()
        self._plan_task = asyncio.create_task(self._run_plan(pid, kind, self._plan_stop))

    async def _after_answers(self, tasks: list[asyncio.Task]) -> None:
        """回答の生成が終わったら冷却の後で自動を再開。回答中に押された手動の依頼はここで実行する。"""
        await asyncio.gather(*tasks, return_exceptions=True)
        if any(not t.done() for t in self._gen_tasks):   # 次の質問の生成が始まっている
            return
        self._answer_active = False
        self._plan_block_until = time.monotonic() + self.cfg.planner.cooldown_s
        if self._plan_pending and not self._plan_blocked(manual=True):
            kind, self._plan_pending = self._plan_pending, None
            self._launch_plan(kind)
        if self._gloss_pending and not (self._gloss_task and not self._gloss_task.done()):
            self._gloss_pending = False
            self._launch_glossary("manual")

    def _plan_transcript(self, limit_chars: int) -> list[str]:
        lines: list[str] = []
        total = 0
        for ch, text in reversed(self._convo):
            line = f"{'自分' if ch == 'mic' else '相手'}: {text}"
            if total + len(line) > limit_chars and lines:
                break
            lines.append(line)
            total += len(line)
        return list(reversed(lines))

    async def _run_plan(self, pid: str, kind: str, stop_flag: threading.Event) -> None:
        cfg, pc = self.cfg, self.cfg.planner
        role = kind if kind in llm.DEEP_ROLES else "main"   # auto / manual = main(Haiku)・mid = Sonnet・deep = Opus
        if self.target.provider == "openrouter":   # 質問タブのモデル指定(pc.model / mid_model / deep_model)は OpenRouter の名前
            model = {"deep": pc.deep_model, "mid": pc.mid_model}.get(kind) or pc.model or self.model
        else:
            model = self.target.model_for(role)
        transcript = self._plan_transcript({"deep": pc.deep_context_chars, "mid": pc.mid_context_chars}.get(kind, pc.context_chars))
        recent = " ".join(t for _, t in list(self._convo)[-6:])
        last_other = next((t for ch, t in reversed(self._convo) if ch != "mic"), recent)
        hits = self.index.search(recent, k=cfg.index.top_k, terms=query_terms(recent)) if self.index and recent else []
        msgs = prompts.build_plan_messages(mode=cfg.mode, profile=cfg.profile, transcript=transcript, hits=hits,
                                           n=pc.n, deep=kind == "deep", mid=kind == "mid")
        self.m.emit(pid, "plan_start", trigger=kind, model=model, lines=len(transcript), hits=len(hits))
        self._ui("plan_start", pid, kind, model)
        loop = asyncio.get_running_loop()
        buf: list[str] = []
        emitted = 0
        score_tasks: list[asyncio.Task] = []
        use_sel = bool(cfg.selector.enabled and self.jev_key)
        context = [f"{'自分' if ch == 'mic' else '相手'}: {t}" for ch, t in list(self._convo)[-3:]]

        def flush(final: bool = False) -> None:
            nonlocal emitted
            lines = "".join(buf).split("\n")
            complete = lines if final else lines[:-1]
            for line in complete[emitted:]:
                line = line.strip()
                if not line:
                    continue
                self._ui("plan_line", pid, line)
                items = prompts.parse_plan_lines(line)
                if items and use_sel and not stop_flag.is_set():
                    it = items[0]
                    cand = selector.Scored("counter", it["index"], f"{it['question']} | {it['aim']}")
                    score_tasks.append(asyncio.create_task(asyncio.to_thread(
                        selector.score_candidate, cand, utterance=last_other, context=context, mode=cfg.mode,
                        intent=None, api_key=self.jev_key, model=cfg.jev.model, timeout=cfg.selector.timeout_s,
                        base_weights=cfg.selector.weights or None)))
            emitted = len(complete)

        def on_delta(d: str) -> None:
            buf.append(d)
            if "\n" in d:
                loop.call_soon_threadsafe(flush)

        r = await asyncio.to_thread(
            llm.stream, self.target, msgs, role=role, model=model, timeout=cfg.llm.timeout_s,
            max_tokens={"deep": pc.deep_max_tokens, "mid": pc.mid_max_tokens}.get(kind, pc.max_tokens), on_delta=on_delta,
            should_stop=stop_flag.is_set, effort={"deep": pc.deep_reasoning, "mid": pc.mid_reasoning}.get(kind))
        flush(final=True)
        self.ledger.record(r.cost_usd)
        items = prompts.parse_plan_lines(r.text) if r.text else []
        self.m.emit(pid, "plan", ms=r.ms_total, ok=r.ok, error=r.error, rc=r.rc, detail=(r.detail or "")[:200] or None,
                    trigger=kind, model=r.model or model, warm=r.extra.get("warm"), proc_age_ms=r.extra.get("age_ms"),
                    provider=r.provider, ms_first_token=r.ms_first_token, input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens, cost_usd=r.cost_usd, items=len(items))
        ranking: list = []
        if score_tasks and r.ok:
            t0 = time.perf_counter()
            try:
                results = await asyncio.wait_for(asyncio.gather(*score_tasks, return_exceptions=True), timeout=8)
            except asyncio.TimeoutError:
                results = [t.result() if t.done() and not t.cancelled() and t.exception() is None else None
                           for t in score_tasks]
            cands = [c for c in results if isinstance(c, selector.Scored)]
            ranked = selector.rank(cands)
            sel_ms = round((time.perf_counter() - t0) * 1000, 1)
            sel_cost = sum(c.cost_usd or 0.0 for c in cands)
            self.ledger.record(sel_cost)
            ranking = [{"index": c.index, "total": c.total, "scores": c.scores, "error": c.error} for c in ranked]
            self.m.emit(pid, "plan_select", ms=sel_ms, ok=any(c.total is not None for c in cands),
                        candidates=len(cands), scored=sum(1 for c in cands if c.total is not None),
                        cost_usd=round(sel_cost, 6))
            if ranking:
                self._ui("plan_ranking", pid, ranking, pc.display, sel_ms)
        self.session.write_cue(pid, {"kind": "plan", "trigger": kind, "model": r.model or model, "ok": r.ok,
                                     "error": r.error, "items": items, "ranking": ranking,
                                     "ms_first_token": r.ms_first_token, "ms_total": r.ms_total,
                                     "cost_usd": r.cost_usd, "at_ms": int(time.time() * 1000)})
        self._ui("plan_done", pid, r.ok, r.error, r.ms_first_token, r.ms_total, r.cost_usd)
        if r.error == "cancelled" and self._plan_pending in ("mid", "deep"):
            # 少し深く・深く考える で差し替えた自動の後。この task が終わってから始める(ここで調べると、まだ走っている
            # 自分自身を「running」と見て始めなかった・2026-10-08 に見つけた不具合)
            asyncio.get_running_loop().call_soon(self._launch_pending_plan)

    def _launch_pending_plan(self) -> None:
        if self._plan_pending in ("mid", "deep") and not self._plan_blocked(manual=True):
            kind, self._plan_pending = self._plan_pending, None
            self._launch_plan(kind)

    # ---- 用語タブ(FR-6e・2026-10-08): 会話に出た専門用語に短い説明と自分のノートを付ける ----------------------------------
    @staticmethod
    def _gloss_norm(term: str) -> str:
        return "".join(ch for ch in term.lower() if ch not in " 　・-_/()（）「」")

    @staticmethod
    def _jargon_like(term: str) -> bool:
        """先に索引を引く候補語の形: 英字の語・3 字以上のカタカナ・3 字以上の漢字語(漢字 2 字の「運用」「移行」のような
        仕事の一般語を並べると Haiku が拾う語と受け取った・2026-10-08 合成音声の通しで実測)。"""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-\.]*", term):
            return len(term) >= 2 and not term.isdigit()
        return len(term) >= 3

    def _maybe_glossary(self) -> None:
        """自動: 前回から min_chars 字・interval_s 秒以上。回答の生成中・一時停止中・走っている間は始めない。"""
        gc = self.cfg.glossary
        if not (gc.enabled and self.gloss_auto) or self.paused:
            return
        if (self._gloss_task and not self._gloss_task.done()) or self._answer_active or any(not t.done() for t in self._gen_tasks):
            return
        if sum(len(t) for _, t in self._gloss_buf) < gc.min_chars or time.monotonic() - self._gloss_last < gc.interval_s:
            return
        self._launch_glossary("auto")

    def _request_glossary(self) -> None:
        """手動(今の話から用語を拾う)。回答中なら回答の後で。"""
        if self._gloss_task and not self._gloss_task.done():
            self.ui.status("用語を拾っています…")
            return
        if not (self._gloss_buf or self._convo):
            self.ui.status("まだ会話がありません")
            return
        if self._answer_active or any(not t.done() for t in self._gen_tasks):
            self._gloss_pending = True
            self.ui.status("相手の質問への回答を優先しています。終わったら用語を拾います")
            return
        self._launch_glossary("manual")

    def _launch_glossary(self, kind: str) -> None:
        lines = self._gloss_buf or list(self._convo)[-8:]   # 手動で新しい発言が無ければ直近を見直す
        self._gloss_buf = []
        self._gloss_last = time.monotonic()
        self._gloss_seq += 1
        gid = f"G{self._gloss_seq:02d}{new_rid()[:6]}"
        self._gloss_task = asyncio.create_task(self._run_glossary(gid, kind, list(lines)))

    def _gloss_notes(self, term: str) -> list:
        return self.index.lookup(term, k=self.cfg.glossary.notes_per_term) if self.index else []

    @staticmethod
    def _note(h, term: str) -> dict:
        return {"path": h.path, "heading": h.heading, "snippet": h.snippet([term], 160), "source": h.source}

    async def _run_glossary(self, gid: str, kind: str, lines: list[tuple[str, str]]) -> None:
        cfg, gc = self.cfg, self.cfg.glossary
        text = "\n".join(f"{'自分' if ch == 'mic' else '相手'}: {t}" for ch, t in lines)[-gc.context_chars:]
        low = text.lower()   # 候補語は英字が小文字になるので、会話の中の表記(SASE など)に戻す
        cands = [text[i:i + len(t)] if (i := low.find(t.lower())) >= 0 else t
                 for t in query_terms(text, max_terms=gc.candidates * 3)
                 if self._gloss_norm(t) not in self._gloss_seen and self._jargon_like(t)]
        pre: dict = {}
        for t in cands[:gc.candidates]:   # 候補語で索引を先に引く(ノートを根拠に説明させる)
            hs = self._gloss_notes(t)[:1]
            if hs:
                pre[t] = hs[0]
        use_llm = self.llm_enabled and self.ledger.allowed()
        self.m.emit(gid, "glossary_start", trigger=kind, chars=len(text), candidates=len(cands), vault=len(pre), llm=use_llm)
        self._ui("glossary_start", gid, kind)
        items: list[dict] = []
        mcp_tasks: list[asyncio.Task] = []

        def add(it: dict) -> None:
            norm = self._gloss_norm(it["term"])
            if not norm or norm in self._gloss_seen:
                return
            self._gloss_seen.add(norm)
            hs = self._gloss_notes(it["term"])
            it["notes"] = [self._note(h, it["term"]) for h in hs]
            it["at_ms"] = int(time.time() * 1000)
            items.append(it)
            self._ui("glossary_item", gid, {**it, "notes": list(it["notes"])})   # 写し(MCP のノートは後で glossary_notes)
            if not hs and self.mcp_search and len(mcp_tasks) < gc.mcp_per_run:   # 索引に無ければ MCP で探す(届いたら足す)
                mcp_tasks.append(asyncio.create_task(self._gloss_mcp(gid, it)))

        if not use_llm:   # 生成なし(LOCAL・キーなし・AI オフ・予算超過): ノートの題名か見出しと一致する語だけ
            for t, h in pre.items():
                if title_rank(h, t) <= 1:
                    add({"term": t, "reading": "", "field": "", "desc": h.snippet([t], 140),
                         "source": f"{h.path}#{h.heading}" if h.heading else h.path, "llm": False})
            r = None
        else:
            msgs = prompts.build_glossary_messages(text=text, vault=pre, known=sorted(self._gloss_seen)[-80:], n=gc.max_terms,
                                                   level=gc.level, snippet_chars=gc.snippet_chars)
            loop = asyncio.get_running_loop()
            buf: list[str] = []
            emitted = 0

            def flush(final: bool = False) -> None:
                nonlocal emitted
                ls = "".join(buf).split("\n")
                complete = ls if final else ls[:-1]
                for line in complete[emitted:]:
                    it = prompts.parse_glossary_line(line)
                    if it and len(items) < gc.max_terms:
                        add({**it, "llm": True})
                emitted = len(complete)

            def on_delta(d: str) -> None:
                buf.append(d)
                if "\n" in d:
                    loop.call_soon_threadsafe(flush)

            r = await asyncio.to_thread(llm.stream, self.target, msgs, role="main", timeout=cfg.llm.timeout_s,
                                        max_tokens=gc.max_tokens, on_delta=on_delta,
                                        should_stop=self._dig_stop.is_set)   # 停止のときの止め札(深掘りと共通)
            flush(final=True)
            self.ledger.record(r.cost_usd)
        if mcp_tasks:
            await asyncio.wait(mcp_tasks, timeout=gc.mcp_timeout_s + 0.5)
        self.m.emit(gid, "glossary", trigger=kind, ok=r.ok if r else True, error=r.error if r else None,
                    ms=r.ms_total if r else 0, ms_first_token=r.ms_first_token if r else None, model=(r.model if r else None),
                    input_tokens=r.input_tokens if r else None, output_tokens=r.output_tokens if r else None,
                    cost_usd=r.cost_usd if r else None, items=len(items), with_notes=sum(1 for it in items if it["notes"]),
                    mcp=len(mcp_tasks))
        if items or (r and not r.ok):
            self.session.write_cue(gid, {"kind": "glossary", "trigger": kind, "ok": r.ok if r else True,
                                         "error": r.error if r else None, "model": r.model if r else None, "items": items,
                                         "ms_total": r.ms_total if r else 0, "cost_usd": r.cost_usd if r else None,
                                         "at_ms": int(time.time() * 1000)})
        self._ui("glossary_done", gid, r.ok if r else True, r.error if r else None, len(items), r.ms_total if r else 0)

    async def _gloss_mcp(self, gid: str, it: dict) -> None:
        try:
            hs = await asyncio.wait_for(asyncio.to_thread(self.mcp_search, [it["term"]], self.cfg.glossary.notes_per_term),
                                        timeout=self.cfg.glossary.mcp_timeout_s)
        except Exception as e:  # noqa: BLE001 — 届かなければノートなしのまま(§5.4)
            self.m.emit(gid, "glossary_mcp", ok=False, error=f"{type(e).__name__}: {e}"[:160], term=it["term"])
            return
        it["notes"] = [self._note(h, it["term"]) for h in (hs or []) if h.path]
        if it["notes"]:
            self._ui("glossary_notes", gid, it["term"], it["notes"])

    # ---- 深掘り(FR-6d・2026-10-08): 回答タブの質問を Sonnet(少し深く)/ Opus(深く)で掘り下げる ----------------------
    DIG_LABEL = {"mid": "少し深く", "deep": "深く"}

    def _request_dig(self, rid: str, level: str) -> None:
        label, src = self.DIG_LABEL.get(level), self._utts.get(rid)
        if not label or not src:
            self.ui.status("深掘りする質問が見つかりません")
            return
        key = (rid, level)
        if key in self._digs:   # 冪等: 走っている間も済んだ後も重ねない
            done = self._digs[key].done()
            self.ui.status(f"この質問は「{label}」で{'掘り下げ済みです' if done else '掘り下げています'}")
            return
        if not self.llm_enabled:
            self.ui.status("生成が止まっているため深掘りはできません(LOCAL・キーなし・AI オフ)")
            return
        if not self.ledger.allowed():
            self.ui.status("月の予算を超えたため深掘りは止めています")
            return
        self.m.emit(rid, "dig_request", level=level)
        self._digs[key] = asyncio.create_task(self._run_dig(rid, level, *src))

    async def _dig_hits(self, did: str, level: str, u: Utterance, prev: list[str]) -> tuple[list, list[str]]:
        """Vault から多めに引く: 索引(上位 k 件)+ MCP(つながっていれば・mcp_timeout_s で打ち切り)。重複は除く。"""
        dc = self.cfg.dig
        k = dc.top_k_deep if level == "deep" else dc.top_k_mid
        last_other = next((p.split("] ", 1)[-1] for p in reversed(prev) if not p.startswith("[mic]")), "")
        every = list(dict.fromkeys(query_terms(u.text) + query_terms(last_other)))
        # 専門語らしい語(英字・3 字以上)だけで引く。一般語(移行・検討・運用)まで入れると OR 検索で的外れのノートが上に来た
        # (2026-10-08 合成音声の通し)。専門語らしい語が無ければ従来どおり全部
        terms = ([t for t in every if self._jargon_like(t)] or every)[:12]
        t0 = time.perf_counter()
        local = self.index.search(f"{u.text} {last_other}", k=k, terms=terms) if self.index and terms else []
        local_ms = round((time.perf_counter() - t0) * 1000, 1)
        mcp: list = []
        mcp_ms = mcp_err = None
        if self.mcp_search and terms:
            t1 = time.perf_counter()
            try:
                mcp = await asyncio.wait_for(asyncio.to_thread(self.mcp_search, terms[:dc.mcp_terms], k),
                                             timeout=dc.mcp_timeout_s) or []
            except asyncio.TimeoutError:
                mcp_err = "timeout"
            except Exception as e:  # noqa: BLE001 — MCP が使えなくても索引だけで続ける(§5.4)
                mcp_err = f"{type(e).__name__}: {e}"[:160]
            mcp_ms = round((time.perf_counter() - t1) * 1000, 1)
        seen = {h.path for h in local}
        extra = [h for h in mcp if h.path not in seen and not seen.add(h.path)][:4]
        self.m.emit(did, "dig_retrieve", ms=local_ms, ok=True, local=len(local), mcp=len(mcp), mcp_used=len(extra),
                    mcp_ms=mcp_ms, mcp_error=mcp_err, terms=terms)
        return list(local) + extra, terms

    async def _run_dig(self, rid: str, level: str, u: Utterance, prev: list[str], summary: dict | None, _hits: list) -> None:
        cfg, dc = self.cfg, self.cfg.dig
        did = f"{rid}-{level}"
        model = self.target.model_for(level)
        hits, terms = await self._dig_hits(did, level, u, prev)
        notes = [{"path": h.path, "heading": h.heading, "snippet": h.snippet(terms, 160), "source": h.source} for h in hits]
        self.m.emit(did, "dig_start", q_rid=rid, level=level, model=model, hits=len(hits))
        self._ui("dig_start", rid, level, model, notes)
        transcript = self._plan_transcript(dc.deep_context_chars if level == "deep" else dc.mid_context_chars)
        msgs = prompts.build_dig_messages(level=level, mode=cfg.mode, profile=cfg.profile, transcript=transcript,
                                          utterance=u.text, intent=(summary or {}).get("intent"), hits=hits,
                                          n_answers=dc.n_answers, n_evidence=dc.n_evidence, body_chars=dc.body_chars)
        loop = asyncio.get_running_loop()
        buf: list[str] = []
        emitted = 0

        def flush(final: bool = False) -> None:
            nonlocal emitted
            lines = "".join(buf).split("\n")
            complete = lines if final else lines[:-1]
            for line in complete[emitted:]:
                line = line.strip()
                if line and prompts.parse_dig_line(line):
                    self._ui("dig_line", rid, level, line)
            emitted = len(complete)

        def on_delta(d: str) -> None:
            buf.append(d)
            if "\n" in d:
                loop.call_soon_threadsafe(flush)

        deep = level == "deep"
        r = await asyncio.to_thread(
            llm.stream, self.target, msgs, role=level, model=model, timeout=dc.timeout_s,
            max_tokens=dc.deep_max_tokens if deep else dc.mid_max_tokens, effort=dc.deep_reasoning if deep else dc.mid_reasoning,
            on_delta=on_delta, should_stop=self._dig_stop.is_set)
        flush(final=True)
        self.ledger.record(r.cost_usd)
        parsed = prompts.parse_dig_lines(r.text) if r.text else prompts.parse_dig_lines("")
        self.m.emit(did, "dig", q_rid=rid, level=level, ms=r.ms_total, ok=r.ok, error=r.error, rc=r.rc,
                    detail=(r.detail or "")[:200] or None, model=r.model or model, provider=r.provider,
                    warm=r.extra.get("warm"), ms_first_token=r.ms_first_token, input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens, cost_usd=r.cost_usd, answers=len(parsed["answers"]),
                    evidence=len(parsed["evidence"]))
        self.session.write_cue(rid, {"kind": "dig", "level": level, "model": r.model or model, "ok": r.ok, "error": r.error,
                                     **parsed, "notes": notes, "ms_first_token": r.ms_first_token, "ms_total": r.ms_total,
                                     "cost_usd": r.cost_usd, "at_ms": int(time.time() * 1000), "raw": r.text})
        self._ui("dig_done", rid, level, r.ok, r.error, r.ms_first_token, r.ms_total, r.cost_usd)
        if not r.ok and r.error != "cancelled":   # 失敗(通信・空の出力など)はもう一度押せる
            self._digs.pop((rid, level), None)

    async def _generate(self, u: Utterance, msgs: list[dict], stop_flag: threading.Event,
                        prev: list[str], summary: dict, use_sel: bool, part: str) -> None:
        loop = asyncio.get_running_loop()
        buf: list[str] = []
        emitted = 0
        first_line_ms: list[int] = []
        score_tasks: list[asyncio.Task] = []
        cfg = self.cfg

        def start_scoring(line: str) -> None:
            # 行が完成した候補を、生成を待たずに Jev で採点する(並行)。
            parsed = prompts.parse_cue_lines(line)
            if parsed["answers"]:
                a = parsed["answers"][0]
                cand = selector.Scored("answer", len([t for t in score_tasks if t.get_name() == "answer"]) + 1,
                                       f"{a['title']} | {a['body']}")
            elif parsed["counters"]:
                c = parsed["counters"][0]
                cand = selector.Scored("counter", len([t for t in score_tasks if t.get_name() == "counter"]) + 1,
                                       f"{c['question']} | {c['aim']}")
            else:
                return
            t = asyncio.create_task(asyncio.to_thread(
                selector.score_candidate, cand, utterance=u.text, context=prev, mode=cfg.mode,
                intent=summary.get("intent"), api_key=self.jev_key, model=cfg.jev.model,
                timeout=cfg.selector.timeout_s, base_weights=cfg.selector.weights or None))
            t.set_name(cand.kind)
            score_tasks.append(t)

        def flush_lines(final: bool = False) -> None:
            nonlocal emitted
            text = "".join(buf)
            lines = text.split("\n")
            complete = lines[:-1] if not final else lines
            for line in complete[emitted:]:
                line = line.strip()
                if line:
                    if not first_line_ms:
                        first_line_ms.append(int(time.time() * 1000) - u.t_final_ms)
                        if u.rid not in self._first_cue:
                            self._first_cue[u.rid] = first_line_ms[0]
                            self.m.emit(u.rid, "e2e_first_cue", ms=first_line_ms[0], kind="cue")
                    self.ui.cue_line(u.rid, line)
                    if use_sel and not stop_flag.is_set():
                        start_scoring(line)
            emitted = len(complete)

        def on_delta(d: str) -> None:
            buf.append(d)
            if "\n" in d:
                loop.call_soon_threadsafe(flush_lines)

        r = await asyncio.to_thread(
            llm.stream, self.target, msgs, role="main", model=self.model,
            timeout=self.cfg.llm.timeout_s, max_tokens=self.cfg.llm.max_tokens,
            on_delta=on_delta, should_stop=stop_flag.is_set)
        flush_lines(final=True)
        self.ledger.record(r.cost_usd)
        parsed = prompts.parse_cue_lines(r.text) if r.text else {}
        self.m.emit(u.rid, "generate", ms=r.ms_total, ok=r.ok, error=r.error, rc=r.rc, detail=(r.detail or "")[:200] or None,
                    part=part, model=r.model, provider=r.provider, warm=r.extra.get("warm"), proc_age_ms=r.extra.get("age_ms"),
                    ms_first_token=r.ms_first_token, input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                    cost_usd=r.cost_usd, answers=len(parsed.get("answers", [])), counters=len(parsed.get("counters", [])))
        ranking: dict = {}
        partial = r.error == "cancelled"
        if use_sel and score_tasks and (r.ok or partial):
            t0 = time.perf_counter()
            try:
                # 打ち切り時は長く待たない(次の質問の表示を邪魔しない)
                results = await asyncio.wait_for(asyncio.gather(*score_tasks, return_exceptions=True),
                                                 timeout=1.5 if partial else 8)
            except asyncio.TimeoutError:
                results = [t.result() if t.done() and not t.cancelled() and t.exception() is None else None
                           for t in score_tasks]
            cands = [c for c in results if isinstance(c, selector.Scored)]
            sel_ms = round((time.perf_counter() - t0) * 1000, 1)
            sel_cost = sum(c.cost_usd or 0.0 for c in cands)
            self.ledger.record(sel_cost)
            for kind, kind_ja in (("answer", "回答"), ("counter", "逆質問")):
                ranked = selector.rank([c for c in cands if c.kind == kind])
                if ranked:
                    self.ui.ranking(u.rid, kind_ja, ranked, cfg.selector.display, sel_ms, partial=partial)
                    ranking[kind] = [{"index": c.index, "total": c.total, "scores": c.scores, "ms": c.ms,
                                      "error": c.error} for c in ranked]
            self.m.emit(u.rid, "select", ms=sel_ms, ok=any(c.total is not None for c in cands), part=part,
                        partial=partial, candidates=len(cands), scored=sum(1 for c in cands if c.total is not None),
                        cost_usd=round(sel_cost, 6),
                        top=next((c.index for c in selector.rank(cands)), None))
        self.session.write_cue(u.rid, {"kind": "cues", "part": part, "ok": r.ok, "error": r.error, "model": r.model,
                                       "ms_first_token": r.ms_first_token, "ms_total": r.ms_total,
                                       "ms_first_cue": first_line_ms[0] if first_line_ms else None,
                                       "cost_usd": r.cost_usd, **parsed, "ranking": ranking, "raw": r.text})
        if r.ok:
            self.counts["generated"] += 1
        elif partial:
            self.counts["cancelled"] = self.counts.get("cancelled", 0) + 1
        else:
            self.counts["failed"] += 1
        self.ui.cue_done(u.rid, r.ok, r.ms_first_token, r.ms_total, r.cost_usd, r.error)

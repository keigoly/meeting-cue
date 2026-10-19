"""meetcue app — 常駐して Web UI を出し続け、録音の開始・停止をウィンドウのボタンで行う(FR-7・U1)。

1 回の録音 = 1 セッション = 1 Pipeline。`meetcue run`(1 プロセス 1 セッション)はそのまま残す。
- POST /api/action {"action":"start","mode":…,"privacy":…} / {"action":"stop"} / {"action":"quit"}
  それ以外(toggle_pause / deepdive / mode)は録音中の Pipeline へ渡す。
- GET /api/sessions(一覧)/ GET /api/sessions/<id>(詳細)/ POST /api/sessions/<id>/title
- 一覧の右クリック(2026-09-26): POST /api/sessions/<id>/export {dest?}(dest が無ければ保存先を選ぶダイアログ)/
  POST /api/sessions/<id>/drive {account?}(Google Drive へ移す・LOCAL は断る)/ POST /api/sessions/<id>/trash(ゴミ箱へ)/
  GET /api/storage(Google Drive の有無)。操作ごとに端末ログへ所要と成否を出し、全画面へ sessions_changed を配る。
- Obsidian(FR-5b・2026-10-08): GET /api/obsidian(Vault・保管庫の一覧・索引・MCP の状態)/ POST /api/obsidian/choose
  (フォルダを選ぶ)/ POST /api/obsidian/index {full?}(裏で索引を作る)/ POST /api/obsidian/mcp/check(MCP の接続確認)/
  POST /api/obsidian/open {path}(ノートを Obsidian で開く)。録音には MCP の検索(pipeline.mcp_search)を渡す。
- 音声入力(FR-14b・2026-10-19): /api/dictation/…(meetcue/dictation_app.py)。アプリが起動している間、設定で「音声入力を使う」なら
  裏で待ち受ける(録音とは別のスレッド・別のヘルパー)。
Ctrl-C(SIGINT)と SIGTERM は録音を正規に止めてから終了する。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import re
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import captions, library, redo
from .config import Config, default_port
from .metrics import Metrics
from . import providers, secrets
from .cues import claude_cli, llm, openai_direct
from .dictation_app import DictationService
from .knowledge import mcp, obsidian
from .pipeline import Pipeline, Source, helper_hint
from .replacements import Replacer
from .secrets import openrouter_key
from .stall import LoopStallWatch
from .session import Session
from .ui.terminal import TerminalUI
from .ui.web import MultiUI, WebUI


# 画面の色(基本色・相手・自分)。~/.meeting-cue/ui.json に保存(2026-09-27 keigoly様: 既定はナギの色 = ミント・空色・藤紺。以前の既定は水色・緑・紫)
DEFAULT_COLORS = {"accent": "#3ed6c8", "other": "#62b6ff", "self": "#8f8cff"}   # ナギの色(2026-09-27 公開前に独自の配色へ)
# 画面の文言をナギ(アイコンのキャラクター)の台詞にするか。同じ ui.json の nagi(2026-09-26 keigoly様・⚙ で切替)
DEFAULT_NAGI = True
# 設定画面(FR-10b・2026-09-26): 生成 AI の接続先・生成 / Jev / Google Drive のオン・オフ・初回の案内を済ませたか。同じ ui.json
DEFAULT_APP = {"provider": "openrouter", "generate": True, "jev": True, "drive": True, "drive_account": "", "onboarded": False,
               # OpenAI 直接(第 2 段): モデルは利用者がキーのモデル一覧から選ぶ・価格(1M トークンあたり USD)は月予算の台帳用
               "openai_model": "", "openai_price_in": 0.0, "openai_price_out": 0.0,
               # 外観(2026-09-27 keigoly様: ダークモードも): auto = macOS に合わせる / light / dark
               "theme": "auto",
               # Obsidian(FR-5b・2026-10-08): Vault(空 = config.toml の vault_root)と MCP(既定オフ・既定の型は mcp-obsidian)
               "vault_root": "", "obsidian_mcp": False, "mcp_kind": "mcp-obsidian", "mcp_command": "", "mcp_url": "",
               "mcp_host": "127.0.0.1", "mcp_port": 27124, "mcp_tool": ""}
THEMES = ("auto", "light", "dark")
MCP_KINDS = ("mcp-obsidian", "stdio", "http")


def _valid(dv, v) -> bool:
    """ui.json の値の型を既定値に合わせて検める(価格は整数も可・負は不可・bool は数に数えない)。"""
    if isinstance(dv, float):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
    if isinstance(dv, int) and not isinstance(dv, bool):
        return isinstance(v, int) and not isinstance(v, bool)
    return isinstance(v, type(dv))
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
REPO = Path(__file__).resolve().parents[1]
# アップデートの変更内容に出さない本文の行(ファイル名・調査の段・試験の記録)
_TECH_LINE = re.compile(r"\.(py|swift|html|sh|md|toml)\b|^Step \d|^確認|^試験|^実測")


def _rebuild_paths() -> tuple[str, ...]:
    """作り直しが要る場所(更新の約束 update.toml の [mac] rebuild_paths・更新係と同じ)。無ければ従来の決まり。"""
    try:
        return tuple(tomllib.loads((REPO / "update.toml").read_text(encoding="utf-8"))["mac"]["rebuild_paths"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return ("helpers/macos/", "packaging/")


def _git(*args: str) -> str:
    """repo の git(アップデートを確認)。失敗は空文字。"""
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


class _CaptionTap:
    """録音中の確定した字幕(Pipeline の utterance)を翻訳へ渡す。字幕の画面が選んでいる音源だけ。"""

    def __init__(self, app: "App"):
        self.app = app

    def utterance(self, channel: str, text: str, rid: str) -> None:
        if self.app.caption_wants(channel):
            self.app._translator.submit(rid, channel, text)


class App:
    def __init__(self, cfg: Config, *, sources: list[Source], port: int | None = None, window: bool = True,
                 model: str | None = None, llm_enabled: bool = True, summary: bool = True, redo_after: bool = True):
        self.cfg = cfg
        self.sources = sources
        self.window = window
        self.model = model or cfg.llm.model
        self.llm_enabled = llm_enabled
        self.summary = summary
        self.redo_after = redo_after   # 録音後の文字起こしし直し(FR-9b)。config の [redo] enabled でも止められる
        self._redo_cancel: asyncio.Event | None = None   # し直しの最中だけ。アプリを終えるときに立てて取り消す
        self.term = TerminalUI(show_partials=False)
        self.web = WebUI(port=port or default_port(), page="app.html")
        self.web.on_action_body = self._action_threadsafe
        self.web.api = self._api
        self.web.file_route = self._file_route
        self.pipeline: Pipeline | None = None
        self.session: Session | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown: asyncio.Event | None = None
        self._window_proc: asyncio.subprocess.Process | None = None
        self._cli_pool: claude_cli.Pool | None = None   # 開発者向けの Claude サブスク(録音中だけ温める)
        # ライブ字幕(2026-09-27): 録音していない間は CaptionRunner(保存なし)、録音中は Pipeline の字幕を使う。
        # 画面(caption.html)が開いている間、数秒ごとに action "caption" が届く(借用)。途絶えたら止める
        self._cap_metrics = captions.caption_metrics(cfg)
        self._translator = captions.Translator(self.web, self._translate_target, metrics=self._cap_metrics)
        self._caption = captions.CaptionRunner(cfg, self.web, sources, on_final=self._translator.submit,
                                               metrics=self._cap_metrics)
        self._caption_want: dict | None = None
        self._caption_lock = asyncio.Lock()
        self._tr_cache: tuple = (0.0, None, "")   # (時刻, Target, 理由)。キーチェーンを字幕 1 行ごとに読まない
        self._boot_commit = ""   # アップデートを確認: 動いている版(run の開始で記録)
        self.replacer = Replacer(cfg.app_dir / "replacements.txt")   # 置き換え辞書(⚙ で編集。録音・字幕は同じファイルを読む)
        # Obsidian(FR-5b): 索引を裏で作る状態と、MCP の検索(つなぎっぱなし・設定やキーが変わったら作り直す)
        self._idx_lock = threading.Lock()
        self._idx_state: dict = {"building": False, "last": None}
        self._vs: mcp.VaultSearch | None = None
        self._vs_key: str = ""
        self._mcp_last: dict | None = None
        self.dictation = DictationService(cfg, self.web, self.term, guard=self._dictation_guard)   # 音声入力(FR-14b)

    # ---- 起動・終了 ----------------------------------------------------------------
    async def run(self) -> None:
        self._boot_commit = _git("rev-parse", "HEAD")
        self._loop = asyncio.get_running_loop()
        self._shutdown = asyncio.Event()
        self.web.start()
        self.web.set_state(app=True, recording=False, saving=False, session="", title="", started_ms=None, paused=False,
                           mode=self.cfg.mode, privacy=self.cfg.privacy,
                           sources=[s.kind + ":" + s.channel for s in self.sources])
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, self._shutdown.set)
            except (NotImplementedError, RuntimeError):
                pass
        self.term.status(f"meetcue app: {self.web.url}(Ctrl-C で終了。録音中なら止めてから終わる)")
        if self.window:
            await self._spawn_window()
        self.dictation.start()   # 設定で「音声入力を使う」なら裏で待ち受ける(Mac だけ)
        watchdog = asyncio.create_task(self._caption_watchdog())
        stall = asyncio.create_task(LoopStallWatch(on_stall=self._loop_stalled).run())
        await self._shutdown.wait()
        self.term.status("終了します")
        if self._redo_cancel:   # 録音後のし直しの最中なら取り消す(会議中の文字起こしのまま・サマリは作る)
            self._redo_cancel.set()
        watchdog.cancel()
        stall.cancel()
        async with self._caption_lock:
            await self._caption.stop()
        if self.pipeline:
            self.pipeline.request_stop()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=60)
            except asyncio.TimeoutError:
                self.term.error("録音の後始末が 60 秒で終わりませんでした")
        await asyncio.to_thread(self.dictation.stop)
        await self._stop_window()
        if self._cli_pool:
            self._cli_pool.close()
        if self._vs:
            self._vs.close()
        self.web.stop()

    def _dictation_guard(self) -> str | None:
        """音声入力を断る理由。LOCAL(仕事の会議)の録音中は、マイクの音(スピーカーから入る会議の声を含む)を Soniox に送らない。"""
        if self.web.state.get("recording") and self.web.state.get("privacy") == "local":
            return "LOCAL の録音中は音声入力を使えません"
        return None

    async def _spawn_window(self) -> None:
        if sys.platform == "win32":   # pywebview のウィンドウのヘルパー(アプリ用の仮想環境・FR-12)。診断はアプリのログへ
            argv = [str(self.cfg.window_python), str(self.cfg.helpers_dir / "window_helper" / "window_helper.py")]
            stderr = None
        else:
            argv = [str(self.cfg.helpers_dir / "overlay_helper" / "overlay-helper")]
            stderr = asyncio.subprocess.DEVNULL
        if not all(Path(a).exists() for a in argv):
            self.term.error(f"ウィンドウの helper が無い: {argv[-1]}({helper_hint()})。ブラウザで {self.web.url} を開いてください")
            return
        self._window_proc = await asyncio.create_subprocess_exec(
            *argv, "--window", "--url", self.web.url, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=stderr)
        asyncio.create_task(self._watch_window())

    async def _watch_window(self) -> None:
        """ウィンドウを閉じたらアプリを終える(録音中なら正規に止めてから)。"""
        assert self._window_proc
        await self._window_proc.wait()
        if self._shutdown and not self._shutdown.is_set():
            self.term.status("ウィンドウが閉じられました")
            self._shutdown.set()

    async def _window_cmd(self, line: str) -> None:
        p = self._window_proc
        if p and p.returncode is None and p.stdin:
            try:
                p.stdin.write((line + "\n").encode())
                await p.stdin.drain()
            except (ConnectionError, RuntimeError):
                pass

    async def _stop_window(self) -> None:
        p = self._window_proc
        if p and p.returncode is None:
            await self._window_cmd("quit")
            try:
                await asyncio.wait_for(p.wait(), timeout=2)
            except asyncio.TimeoutError:
                p.terminate()

    # ---- 操作 ------------------------------------------------------------------
    def _action_threadsafe(self, name: str, body: dict) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._action, name, body)

    def _action(self, name: str, body: dict) -> None:
        if name == "start":
            if self._task and not self._task.done():
                self.web.status("前の録音を保存しています。終わってから始めてください" if self.web.state.get("saving")
                                else "すでに録音中です")
                return
            self._task = asyncio.create_task(self._run_session(body))
            self._task.add_done_callback(lambda _t: asyncio.ensure_future(self._caption_sync()))   # 字幕だけに戻す
        elif name == "caption":
            self._caption_request(body)
        elif name == "stop":
            if self.pipeline:
                self.web.status("録音を停止しています…", code="stopping")
                self.pipeline.request_stop()
        elif name == "quit":
            if self._shutdown:
                self._shutdown.set()
        elif name == "update_check":   # 更新係(packaging/updater)が新しい版を fetch した → ホストが /api/state で見て確認する
            self.web.set_state(update_nudge=int(time.time() * 1000))
        elif name == "top":
            asyncio.ensure_future(self._window_cmd("top " + ("on" if body.get("on") else "off")))
            self.web.set_state(top=bool(body.get("on")))
        elif self.pipeline:
            self.pipeline.action(name, body)
        else:
            self.web.status("録音していません")

    async def _run_session(self, opts: dict) -> None:
        async with self._caption_lock:   # 字幕だけの動きを止めてから録音の音源を開く(同じマイク・タップを取り合わない)
            await self._caption.stop()
        mode = opts.get("mode") if opts.get("mode") in Pipeline.MODES else self.cfg.mode
        privacy = "local" if opts.get("privacy") == "local" else "private"
        cfg = dataclasses.replace(self.cfg, mode=mode, privacy=privacy)
        st = self.ui_settings()   # 設定画面のオン・オフ(生成と Jev を両方オフ = 録音だけ・外へ何も送らない)
        generate, jev_on = self.llm_enabled and st["generate"], st["jev"]
        local = privacy == "local"
        jev_key = openrouter_key() if jev_on and not local else None                      # Jev は OpenRouter
        if st["provider"] == "claude-cli":   # 開発者向け: 本人のサブスク(Claude Code)。キーは無く、印を渡す
            cli = self._claude_cli() if generate and not local else None
            key = claude_cli.MARKER if cli else None
            pool = self._cli_pool_get(cli) if cli else None
            target = llm.target_for(cfg, "claude-cli", key, {**st, "_pool": pool})
            if pool:   # 字幕の翻訳・用語タブもするなら 1 つずつ多く温める(回答候補の分を取らない)。鍵は (モデル, effort, 速い役か)
                pool.set_warm({llm.warm_key(target): max(0, cfg.llm.claude_cli_warm) + (1 if self._translator.lang else 0)
                               + (1 if cfg.glossary.enabled else 0)})
            elif generate and not local:
                self.web.status("Claude Code(claude)が見つからないため、回答候補とサマリは作りません")
        else:
            key = secrets.api_key(st["provider"])[0] if generate and not local else None  # 生成の接続先のキー
            target = llm.target_for(cfg, st["provider"], key, st)
        if generate and not local and not key and st["provider"] != "claude-cli":
            self.web.status(f"{llm.LABELS[st['provider']]} のキーが無いため、回答候補とサマリは作りません(⚙ で登録)")
        elif generate and not local and st["provider"] == "openai" and not target.model:
            self.web.status("OpenAI のモデルが選ばれていないため、回答候補とサマリは作りません(⚙ で選ぶ)")
            key = target.api_key = None
        # 会議の文字起こし(FR-2b・2026-10-18): 「録音を開始」で Soniox を選んだ会議だけ、音声を Soniox へ送る(LOCAL では使わない)
        stt, soniox_key = ("soniox" if opts.get("stt") == "soniox" and not local else "local"), None
        if stt == "soniox":
            soniox_key = secrets.api_key("soniox")[0]
            if not soniox_key:
                self.web.status("Soniox のキーが無いため、このパソコンで文字起こしします(⚙ で登録)")
                stt = "local"
        session = Session(cfg.sessions_root, mode=mode, privacy=privacy,
                          sources=[s.kind + ":" + s.channel + (":" + s.arg if s.arg else "") for s in self.sources],
                          model=self.model)
        title = (opts.get("title") or "").strip()
        if title:
            session.meta["title"] = title
            session.write_meta()
        ui = MultiUI(self.term, self.web, _CaptionTap(self))   # 録音中の確定した字幕も翻訳へ
        p = Pipeline(cfg, session, ui, self.sources, api_key=key, llm_enabled=generate, jev_enabled=jev_on,
                     model=self.model, target=target if st["provider"] != "openrouter" else None, jev_api_key=jev_key,
                     stt=stt, soniox_key=soniox_key)
        p.overlay = False   # 画面はアプリのウィンドウ
        p.mcp_search = self._mcp_search_fn(privacy)   # Obsidian の MCP(FR-5b)。LOCAL では stdio / localhost だけ
        self.session, self.pipeline = session, p
        self.web.reset_history()
        self.web.set_state(recording=True, session=session.dir.name, title=title or library.DEFAULT_TITLE,
                           started_ms=session.started_ms, mode=mode, privacy=privacy, paused=False,
                           llm=p.target.label if p.llm_enabled else "off", stt=p.stt, stt_fallback={})
        self.web.push({"type": "session_started", "id": session.dir.name})
        try:
            await p.run(install_signals=False)
        except Exception as e:  # noqa: BLE001 — 1 回の録音の失敗でアプリは止めない
            self.web.error(f"録音が異常終了しました: {type(e).__name__}: {e}")
        finally:
            session.close()
            self.session, self.pipeline = None, None
            self.web.set_state(recording=False, paused=False)
            if self._cli_pool:
                self._cli_pool.set_warm({})
        self.term.status(f"counts={p.counts} session={session.dir}")
        if self.redo_after:   # サマリの前に文字起こしを差し替える(サマリは差し替えた文から作る)
            await self._redo(session, p)
        sp = None
        if self.summary:
            from .summary import build_summary
            self.web.status("サマリを作成しています…", code="summary")
            t0, err = time.perf_counter(), None
            try:
                sp = await asyncio.to_thread(build_summary, session.dir, api_key=key, model=cfg.llm.fast_model,
                                             privacy=privacy, target=p.target)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                self.web.error(f"サマリの作成に失敗: {err}")
            m = Metrics(session.dir / "metrics.jsonl")   # 停止の内訳の続き(session.close で閉じた後なので開き直す)
            m.emit("STOP", "summary", ms=round((time.perf_counter() - t0) * 1000, 1), ok=err is None, error=err,
                   made=bool(sp))
            m.close()
        self.web.set_state(saving=False)   # 次の録音を始められる(一覧にもこの記録が出る)
        self.web.push({"type": "session_done", "id": session.dir.name, "summary": bool(sp)})

    async def _redo(self, session: Session, p: Pipeline) -> dict | None:
        """録音後の文字起こしし直し(FR-9b)。「保存しています…」の間に裏で行い、進み具合を画面の saving_note に出す。
        失敗・取り消し(アプリの終了)は会議中の文字起こしのまま。Mac(ヘルパーが --redo を持たない)・config で止めたとき・
        録音が無いときは、画面に何も出さず、記録(metrics の redo_transcribe)に理由だけ残す。"""
        m = Metrics(session.dir / "metrics.jsonl")
        if self._shutdown and self._shutdown.is_set():   # 終了の途中で止まった録音はし直さない(待たせない)
            m.emit("STOP", "redo_transcribe", ms=0, ok=False, reason="app_quit", lines=0, chars=0)
            m.close()
            return None
        quiet = (redo.unavailable(self.cfg) in ("disabled", "unsupported")
                 or not redo.tracks(session.dir, redo.read_meta(session.dir)))   # 録音しない設定の記録
        if not quiet:
            self.web.status("文字起こしを仕上げています…", code="redo")
            self.web.set_state(saving_note="文字起こしを仕上げています…")
        self._redo_cancel = asyncio.Event()
        shown = [0]

        def progress(pg: dict) -> None:   # 5% 刻みで画面へ(録音 2 本の通しの割合)
            pct = int(((pg["track"] - 1) + min(1.0, pg["at_s"] / max(pg["audio_s"], 1e-6))) / pg["tracks"] * 100)
            if pct >= shown[0] + 5:
                shown[0] = pct
                self.web.set_state(saving_note=f"文字起こしを仕上げています… {pct}%")

        try:
            res = await redo.redo_session(session.dir, self.cfg, replacer=self.replacer, metrics=m, cancel=self._redo_cancel,
                                          on_progress=progress, stop_t0=getattr(p, "_stop_t0", None))
        except Exception as e:  # noqa: BLE001 — し直しの失敗で保存は止めない
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            m.emit("STOP", "redo_transcribe", ms=0, ok=False, error=res["error"], lines=0, chars=0)
        finally:
            m.close()
            self._redo_cancel = None
            if not quiet:
                self.web.set_state(saving_note="")
        self.term.status("redo: " + " ".join(f"{k}={res.get(k)}" for k in ("ok", "reason", "error", "lines", "chars", "ms")
                                             if res.get(k) is not None))
        if res.get("error"):
            self.web.error(f"文字起こしの仕上げに失敗しました。会議中の文字起こしのままです({res['error']})")
        return res

    # ---- ライブ字幕 ------------------------------------------------------------------
    CAPTION_LEASE_S = 20.0   # 画面は 5 s ごとに合図する。これだけ途絶えたら閉じたとみなして止める

    def _caption_request(self, body: dict) -> None:
        if body.get("on", True) is False:
            self._caption_want = None
        else:
            ch = [c for c in (body.get("channels") or ["system"]) if c in ("system", "mic")] or ["system"]
            loc = body.get("locale") if body.get("locale") in captions.LOCALES else self.cfg.locale
            tr = body.get("translate") if body.get("translate") in captions.TRANSLATE else ""
            self._caption_want = {"channels": ch, "locale": loc, "translate": tr,
                                  "until": time.monotonic() + self.CAPTION_LEASE_S}
        asyncio.ensure_future(self._caption_sync())

    async def _caption_sync(self) -> None:
        """望まれている字幕の形(音源・言語)に合わせて CaptionRunner を動かす / 止める。録音中と保存中は録音側に任せる。"""
        async with self._caption_lock:
            w = self._caption_want
            if w and w["until"] < time.monotonic():
                self._caption_want = w = None
            self._translator.lang = w["translate"] if w else ""
            busy = self._task is not None and not self._task.done()
            want = None if (busy or not w) else (tuple(sorted(w["channels"])), w["locale"])
            if want != self._caption.key:
                await self._caption.stop()
                if want:
                    await self._caption.start(set(want[0]), want[1])
            if not busy:   # 録音していない間: 翻訳するときだけ回答と同じモデル(Haiku 5.5)を 1 つ温める(オンにした時点で。1 行目から待たせない)
                st = self.ui_settings()
                use = bool(self._translator.lang and st["provider"] == "claude-cli" and st["generate"] and self.llm_enabled)
                cli = self._claude_cli() if use and self._cli_pool is None else None
                if cli:
                    self._cli_pool_get(cli)
                if self._cli_pool:
                    key = llm.warm_key(llm.target_for(self.cfg, "claude-cli", claude_cli.MARKER, st))
                    self._cli_pool.set_warm({key: 1} if use else {})

    def _loop_stalled(self, s: float) -> None:
        """本体が止まっていた(stall.py)。20 s の時点の全スレッドの呼び出し位置は、このログの上の Timeout の節にある。"""
        self.term.error(f"本体が {s:.0f} s 止まっていました({time.strftime('%H:%M:%S')} に再開。止まった時点の呼び出し位置はこのログの上)")
        if self.pipeline:
            self.pipeline.m.emit("APP", "loop_stall", ms=round(s * 1000))

    async def _caption_watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self._caption_sync()

    def caption_wants(self, channel: str) -> bool:
        w = self._caption_want
        return bool(w and channel in w["channels"] and w["until"] >= time.monotonic())

    def _translate_target(self):
        """字幕の翻訳の接続先。LOCAL の録音中・AI の生成がオフなら訳さない(外へ送らない約束)。"""
        if self.session and self.session.meta.get("privacy") == "local":
            return None, "LOCAL の録音中は翻訳しません(外へ送りません)"
        t, target, why = self._tr_cache
        if time.monotonic() - t < 15:
            return target, why
        st = self.ui_settings()
        prov = st["provider"]
        target, why = None, ""
        if not (self.llm_enabled and st["generate"]):
            why = "AI の生成をオフにしているため翻訳しません(⚙)"
        elif prov == "claude-cli":
            cli = self._claude_cli()
            if cli:
                target = llm.target_for(self.cfg, "claude-cli", claude_cli.MARKER, {**st, "_pool": self._cli_pool_get(cli)})
            else:
                why = "Claude Code(claude)が見つからないため翻訳しません"
        else:
            key = secrets.api_key(prov)[0]
            if not key:
                why = f"{llm.LABELS[prov]} のキーが無いため翻訳しません(⚙)"
            else:
                target = llm.target_for(self.cfg, prov, key, st)
                if prov == "openai" and not target.model:
                    target, why = None, "OpenAI のモデルが選ばれていないため翻訳しません(⚙)"
        self._tr_cache = (time.monotonic(), target, why)
        return target, why

    # ---- アップデートを確認(2026-09-27): 手元の repo に、動いている版より新しいコミットがあるか ----------------
    def _update_target(self, head: str) -> tuple[str, str]:
        """入れる版(段 2): 実行用ツリー(branch stable)なら更新係が fetch した origin/stable、開発用ツリーなら HEAD。
        更新係が巻き戻した版(<app_dir>/updater/bad)は出さない。戻り値 = (入れる版, 止めた版)。"""
        if not head or _git("rev-parse", "--abbrev-ref", "HEAD") != "stable":
            return head, ""
        up = _git("rev-parse", "--verify", "-q", "refs/remotes/origin/stable")
        if not up or up == head or _git("merge-base", head, up) != head:   # fast-forward できるときだけ
            return head, ""
        try:
            bad = (self.cfg.app_dir / "updater" / "bad").read_text(encoding="utf-8").split()
        except OSError:
            bad = []
        return (head, up) if up in bad else (up, "")

    def _version(self, app_commit: str) -> dict:
        head, blocked = self._update_target(_git("rev-parse", "HEAD"))
        run = self._boot_commit
        base = app_commit if app_commit and _git("cat-file", "-t", app_commit) == "commit" else run
        subjects = _git("log", "--format=%h %s", f"{run}..{head}").splitlines() if run and head and run != head else []
        changed = _git("diff", "--name-only", f"{base}..{head}").splitlines() if base and head and base != head else []
        rebuild = any(f.startswith(_rebuild_paths()) for f in changed)
        # 変更内容(2026-09-27 アップデートの画面): 件名 = 太字の見出し・本文の 1 行目 = 説明
        since = base if rebuild else run
        notes = []
        if since and head and since != head:
            for rec in _git("log", "--format=%h%x1f%s%x1f%b%x1e", f"{since}..{head}").split("\x1e"):
                f = rec.strip("\n").split("\x1f")
                if len(f) < 2 or not f[0]:
                    continue
                bullets = [ln.strip()[2:] for ln in (f[2] if len(f) > 2 else "").splitlines() if ln.strip().startswith("- ")]
                first = next((b for b in bullets if not _TECH_LINE.search(b)), "")   # 使う人に向く最初の 1 行
                notes.append({"hash": f[0].strip(), "subject": f[1].strip(), "detail": first[:220]})
        return {"running": run[:7], "head": head[:7], "app": (app_commit or "")[:7], "behind": len(subjects),
                "subjects": subjects[:8], "notes": notes[:12], "rebuild": rebuild,
                "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
                "date": _git("log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M", head),
                "blocked": blocked[:7],
                "recording": bool(self._task and not self._task.done()),
                "uid": getattr(os, "getuid", lambda: None)()}   # 本体の持ち主(同じ Mac の別の利用者の本体と見分ける・2026-09-28)

    def _file_route(self, path: str):
        """GET /api/sessions/<id>/audio/<channel>.m4a → 録音した音声(Range 対応は WebUI 側)。"""
        parts = [unquote(x) for x in urlparse(path).path.split("/") if x]
        if len(parts) == 4 and parts[:3] == ["api", "dictation", "audio"]:   # 音声入力の 1 回分の録音(履歴の再生)
            f = self.dictation.audio(parts[3])
            return (f, "audio/wav") if f else None
        if len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3] == "audio":
            f = library.audio_file(self._roots(), parts[2], parts[4])
            return (f, "audio/mp4") if f else None
        return None

    # ---- 画面の設定(色・ナギの台詞) -------------------------------------------------------------------
    @property
    def _ui_path(self):
        return self.cfg.app_dir / "ui.json"

    def ui_settings(self) -> dict:
        try:
            saved = json.loads(self._ui_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        colors = {k: v for k, v in (saved.get("colors") or {}).items() if k in DEFAULT_COLORS and HEX_RE.match(str(v))}
        nagi = saved.get("nagi")
        out = {"colors": DEFAULT_COLORS | colors, "nagi": nagi if isinstance(nagi, bool) else DEFAULT_NAGI}
        for k, dv in DEFAULT_APP.items():
            v = saved.get(k)
            out[k] = (float(v) if isinstance(dv, float) else v) if _valid(dv, v) else dv
        if out["provider"] not in self._ready():
            out["provider"] = DEFAULT_APP["provider"]
        if out["theme"] not in THEMES:
            out["theme"] = DEFAULT_APP["theme"]
        if out["mcp_kind"] not in MCP_KINDS:
            out["mcp_kind"] = DEFAULT_APP["mcp_kind"]
        return out

    def _ready(self) -> set[str]:
        return providers.GENERATION_READY | ({"claude-cli"} if self.cfg.llm.subscription_cli else set())

    def _claude_cli(self) -> str | None:
        return claude_cli.find_cli(self.cfg.llm.claude_cli_path) if self.cfg.llm.subscription_cli else None

    def _cli_pool_get(self, cli: str) -> claude_cli.Pool:
        if self._cli_pool is None:
            self._cli_pool = claude_cli.Pool(cli, self.cfg.app_dir / "claude-cli-cwd")   # 個人の CLAUDE.md が無い作業フォルダ
        return self._cli_pool

    def settings_view(self) -> dict:
        """画面へ渡す設定 = 保存した設定 + キーの有無(中身は出さない)+ Google Drive の有無 + 初回の案内が要るか。"""
        cur = self.ui_settings()
        keys = secrets.key_status()
        drive = [a["label"] for a in self._drive_accounts()]
        a = self.cfg.anthropic
        return {**cur, "keys": keys, "keychain": secrets.keychain_supported(), "drive_accounts": drive,
                "providers_ready": sorted(self._ready()),
                "subscription_cli": {"enabled": self.cfg.llm.subscription_cli, "found": bool(self._claude_cli())},
                "anthropic_models": {"main": a.model, "fast": a.fast_model, "mid": a.mid_model, "deep": a.deep_model},
                "openrouter_models": {"main": self.cfg.llm.model, "fast": self.cfg.llm.fast_model,
                                      "mid": self.cfg.planner.mid_model, "deep": self.cfg.planner.deep_model},
                "obsidian": self._obsidian_view(),
                "need_onboarding": not cur["onboarded"] and not any(v["set"] for p, v in keys.items()
                                                                    if p not in ("obsidian", "soniox"))}

    def save_ui_settings(self, body: dict) -> tuple[int, object]:
        body = body or {}
        colors = body.get("colors") or {}
        bad = [k for k, v in colors.items() if k not in DEFAULT_COLORS or not HEX_RE.match(str(v))]
        if bad:
            return 400, {"error": f"bad colors: {bad}"}
        if "nagi" in body and not isinstance(body["nagi"], bool):
            return 400, {"error": "nagi must be true or false"}
        for k, dv in DEFAULT_APP.items():
            if k in body and not _valid(dv, body[k]):
                return 400, {"error": f"{k} の値が正しくありません"}
        if "provider" in body and body["provider"] not in self._ready():
            return 400, {"error": "接続先が正しくありません"}
        if "theme" in body and body["theme"] not in THEMES:
            return 400, {"error": "外観は auto / light / dark のどれかです"}
        if "vault_root" in body and body["vault_root"] and not Path(str(body["vault_root"])).expanduser().is_dir():
            return 400, {"error": "Vault のフォルダが見つかりません"}
        if "mcp_kind" in body and body["mcp_kind"] not in MCP_KINDS:
            return 400, {"error": "MCP の型が正しくありません"}
        if "mcp_port" in body and not 1 <= int(body["mcp_port"]) <= 65535:
            return 400, {"error": "ポートは 1〜65535 です"}
        cur = self.ui_settings()
        cur["colors"] |= {k: str(v).lower() for k, v in colors.items()}
        if "nagi" in body:
            cur["nagi"] = body["nagi"]
        for k, dv in DEFAULT_APP.items():
            if k in body:
                cur[k] = float(body[k]) if isinstance(dv, float) else body[k]
        self._ui_path.parent.mkdir(parents=True, exist_ok=True)
        self._ui_path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        view = self.settings_view()
        self.web.push({"type": "settings", **view})   # 開いている他の画面にも反映
        return 200, view

    # ---- API キー(キーチェーン)。中身は受け取るだけで、画面・ログへは返さない ------------------------------
    def _keys_api(self, op: str, body: dict) -> tuple[int, object]:
        prov = str(body.get("provider") or "")
        if prov not in secrets.PROVIDERS:
            return 400, {"error": "unknown provider"}
        t0 = time.monotonic()
        if op == "check" and prov == "obsidian":   # Local REST API のキーは MCP の接続確認で確かめる
            return self._mcp_check()
        if op == "check":
            res = providers.check_key(prov, secrets.api_key(prov)[0])
            self.term.status(f"key_check provider={prov} ok={res['ok']} status={res['status']} ms={res['ms']}")
            return 200, res
        try:
            if op == "save":
                secrets.keychain_set(prov, str(body.get("key") or ""))
            elif op == "delete":
                secrets.keychain_delete(prov)
        except (ValueError, RuntimeError) as e:
            return 400, {"error": str(e)}
        self.term.status(f"key_{op} provider={prov} ms={round((time.monotonic() - t0) * 1000)}")   # キーは出さない
        view = self.settings_view()
        self.web.push({"type": "settings", **view})
        return 200, view

    def _list_models(self, body: dict) -> tuple[int, object]:
        """設定画面の OpenAI のモデル選び: 登録したキーで使える文章生成のモデル(キーは OpenAI にだけ送る)。"""
        if body.get("provider") != "openai":
            return 400, {"error": "モデル一覧は OpenAI だけ"}
        key = secrets.api_key("openai")[0]
        if not key:
            return 409, {"error": "OpenAI のキーが登録されていません"}
        try:
            return 200, {"models": openai_direct.list_models(key)}
        except Exception as e:  # noqa: BLE001 — 取れなければ手で入力してもらう
            return 502, {"error": f"モデル一覧を取れませんでした({type(e).__name__})"}

    def _open_url(self, body: dict) -> tuple[int, object]:
        """各社のキーの取得ページ・Google Drive の入手先だけを既定のブラウザで開く(ウィンドウ内で遷移させない)。"""
        url = providers.KEY_PAGES.get(str(body.get("page") or ""))
        if not url:
            return 400, {"error": "unknown page"}
        if sys.platform == "win32":
            os.startfile(url)   # 既定のブラウザ
        else:
            subprocess.run(["open", url], check=False)
        return 200, {"ok": True}

    # ---- Obsidian(FR-5b・2026-10-08): Vault・索引・MCP・ノートを開く ---------------------------------------------
    def vault_root(self) -> Path:
        return obsidian.effective_vault_root(self.cfg)

    def _obsidian_view(self) -> dict:
        """画面へ渡す Obsidian の状態(キーの中身は出さない)。"""
        st = self.ui_settings()
        root = self.vault_root()
        idx: dict = {"exists": self.cfg.index_db.exists(), **self._idx_state}
        if idx["exists"]:
            try:
                from .knowledge.index import VaultIndex
                v = VaultIndex(self.cfg.index_db)
                idx.update(v.stats())
                v.close()
            except Exception:  # noqa: BLE001 — 作っている途中などで読めなくても画面は出す
                pass
        meta = obsidian.index_meta(self.cfg)
        idx.update(built_ms=meta.get("built_ms"), root=meta.get("root"), include=meta.get("include"))
        key, src = secrets.api_key("obsidian")
        spec = mcp.build_spec(st, key)
        return {"vault_root": str(root), "vault_exists": root.is_dir(), "vault_from": "settings" if st["vault_root"] else "config",
                "vaults": obsidian.detect_vaults(), "index": idx,
                "mcp": {"enabled": st["obsidian_mcp"], "kind": st["mcp_kind"], "ready": not spec.get("error"),
                        "error": spec.get("error"), "uvx": bool(mcp.find_uvx()), "key": {"set": bool(key), "source": src},
                        "last": self._mcp_last}}

    def _vault_search(self) -> mcp.VaultSearch | None:
        """設定が MCP オンなら共有の VaultSearch(設定かキーが変わっていたら作り直す)。"""
        st = self.ui_settings()
        if not st["obsidian_mcp"]:
            return None
        key = secrets.api_key("obsidian")[0]
        sig = json.dumps([st[k] for k in ("mcp_kind", "mcp_command", "mcp_url", "mcp_host", "mcp_port", "mcp_tool")]
                         + [hash(key) if key else None])
        if self._vs is None or sig != self._vs_key:
            if self._vs:
                self._vs.close()
            self._vs, self._vs_key = mcp.VaultSearch(mcp.build_spec(st, key), prefer_tool=st["mcp_tool"]), sig
        return self._vs

    def _mcp_search_fn(self, privacy: str):
        """録音に渡す検索(語のリスト, 件数)→ [Hit]。オフ・設定不足・LOCAL で外の URL なら None。裏で先に起動しておく。"""
        vs = self._vault_search()
        if vs is None or vs.spec.get("error"):
            return None
        if privacy == "local" and not mcp.is_local(vs.spec):
            self.web.status("LOCAL のため、外の MCP サーバーは使いません(Obsidian の検索は索引だけ)")
            return None
        threading.Thread(target=self._mcp_warm, args=(vs,), name="mcp-warm", daemon=True).start()
        return lambda terms, k: vs.search(terms, k)

    def _mcp_warm(self, vs: mcp.VaultSearch) -> None:
        t0 = time.monotonic()
        try:
            vs.warm()
            self.term.status(f"mcp_warm ok ms={round((time.monotonic() - t0) * 1000)}")
        except mcp.MCPError as e:
            self.term.error(f"mcp_warm ok=False ms={round((time.monotonic() - t0) * 1000)} error={mcp.friendly(str(e))[:160]}")

    def _mcp_check(self) -> tuple[int, object]:
        st = self.ui_settings()
        vs = mcp.VaultSearch(mcp.build_spec(st, secrets.api_key("obsidian")[0]), prefer_tool=st["mcp_tool"])
        try:
            res = vs.check()
        finally:
            vs.close()
        self._mcp_last = {k: res.get(k) for k in ("ok", "connected", "server", "version", "tools", "search_tool", "message", "ms")}
        self.term.status(f"mcp_check kind={st['mcp_kind']} ok={res['ok']} connected={res.get('connected')} ms={res['ms']}")
        self.web.push({"type": "obsidian", **self._obsidian_view()})
        return 200, res

    def _index_start(self, full: bool) -> tuple[int, object]:
        root = self.vault_root()
        if not root.is_dir():
            return 409, {"error": f"Vault が見つかりません: {root}"}
        with self._idx_lock:
            if self._idx_state["building"]:
                return 409, {"error": "索引を作っています"}
            self._idx_state.update(building=True, started_ms=int(time.time() * 1000))
        threading.Thread(target=self._index_build, args=(root, full), name="index-build", daemon=True).start()
        self.web.push({"type": "obsidian", **self._obsidian_view()})
        return 200, {"ok": True, "started": True}

    def _index_build(self, root: Path, full: bool) -> None:
        t0 = time.monotonic()
        try:
            res = {"ok": True, **obsidian.build_index(self.cfg, root, full=full)}
        except Exception as e:  # noqa: BLE001 — 失敗は画面に返し、アプリは止めない
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        res["ms"] = round((time.monotonic() - t0) * 1000)
        with self._idx_lock:
            self._idx_state.update(building=False, last=res)
        self.term.status(f"index_build ok={res['ok']} ms={res['ms']} files={res.get('files')} chunks={res.get('chunks')} "
                         f"added={res.get('added')} updated={res.get('updated')} removed={res.get('removed')} full={res.get('full')}")
        self.web.push({"type": "obsidian", **self._obsidian_view()})

    def _choose_vault(self) -> tuple[int, object]:
        d = self._choose_folder("Obsidian の Vault(保管庫)のフォルダを選んでください", home_default=True)
        if d is None:
            return 200, {"ok": False, "cancelled": True}
        return self.save_ui_settings({"vault_root": str(d)})

    def _open_note(self, body: dict) -> tuple[int, object]:
        ok, msg = obsidian.open_note(self.vault_root(), str(body.get("path") or ""))
        return (200, {"ok": True, "message": msg}) if ok else (400, {"error": msg})

    # ---- 記録の置き場(Mac + Google Drive)と右クリックの操作 -------------------------------------
    def _drive_accounts(self) -> list[dict]:
        return library.drive_accounts()

    def _roots(self) -> list:
        return [self.cfg.sessions_root, *(a["root"] for a in self._drive_accounts())]

    def _choose_folder(self, prompt: str = "録音を書き出す場所を選んでください", home_default: bool = False) -> Path | None:
        """フォルダを選ぶ(osascript の choose folder・アプリでもブラウザでも出る)。キャンセルは None。
        Windows は PowerShell の FolderBrowserDialog(手前に出すため TopMost の親を付ける)。"""
        if sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Windows.Forms; [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                  "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                  f"$d.Description = '{prompt}'; "
                  + ("$d.SelectedPath = $env:USERPROFILE; " if home_default else "$d.SelectedPath = Join-Path $env:USERPROFILE 'Downloads'; ") +
                  "$o = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}; "
                  "if ($d.ShowDialog($o) -eq 'OK') { $d.SelectedPath }")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, timeout=600)
            out = r.stdout.decode("utf-8", "replace").strip()
            return Path(out) if r.returncode == 0 and out else None
        script =('tell me to activate\n'
                  f'POSIX path of (choose folder with prompt "{prompt}" '
                  f'default location (path to {"home folder" if home_default else "downloads folder"}))')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None

    def _session_op(self, op: str, sid: str, body: dict) -> tuple[int, object]:
        cur = self.session.dir.name if self.session else None
        if sid == cur:
            return 409, {"error": "録音中の記録は操作できません"}
        t0 = time.monotonic()
        res: dict = {}
        try:
            if op == "export":
                dest = Path(body["dest"]).expanduser() if body.get("dest") else self._choose_folder()
                if dest is None:
                    return 200, {"ok": False, "cancelled": True}
                if sys.platform == "win32":   # PyAV で重ねる(STT ヘルパー専用の仮想環境)
                    mix = [str(self.cfg.stt_python), str(self.cfg.helpers_dir / "mix_helper" / "mix_helper.py")]
                else:
                    mix = self.cfg.helpers_dir / "mix_helper" / "mix-helper"
                res = library.export_session(self._roots(), sid, dest, mix)
                if not body.get("dest"):   # 書き出した場所を見せる(Finder / エクスプローラー)
                    if sys.platform == "win32":
                        subprocess.run(["explorer", f"/select,{res['path']}"], check=False)
                    else:
                        subprocess.run(["open", "-R", res["path"]], check=False)
            elif op == "drive":
                if not self.ui_settings()["drive"]:
                    return 409, {"error": "設定で Google Drive を使わない設定になっています"}
                accts = self._drive_accounts()
                i = body.get("account", 0)
                if not accts:
                    return 409, {"error": "Google Drive for desktop が見つかりません"}
                if not isinstance(i, int) or not 0 <= i < len(accts):
                    return 400, {"error": "bad account"}
                res = {"path": str(library.move_to_drive(self.cfg.sessions_root, sid, accts[i]["root"]))}
            elif op == "trash":
                res = {"path": str(library.trash_session(self._roots(), sid))}
        except FileNotFoundError:
            return 404, {"error": "no such session"}
        except PermissionError as e:
            return 403, {"error": str(e)}
        except Exception as e:  # noqa: BLE001 — 失敗は画面に返し、アプリは止めない
            self.term.error(f"session_op op={op} sid={sid} ok=False ms={round((time.monotonic() - t0) * 1000)} "
                            f"error={type(e).__name__}: {e}")
            return 500, {"error": f"{type(e).__name__}: {e}"}
        ms = round((time.monotonic() - t0) * 1000)
        self.term.status(f"session_op op={op} sid={sid} ok=True ms={ms} path={res.get('path')}")
        self.web.push({"type": "sessions_changed", "op": op, "id": sid})
        return 200, {"ok": True, "op": op, "ms": ms, **res}

    # ---- API(一覧・詳細・題名・設定) ----------------------------------------------------
    def _api(self, method: str, path: str, body: dict | None):
        parts = [unquote(x) for x in urlparse(path).path.split("/") if x]   # ["api", "sessions", id, ...]
        if parts[:2] == ["api", "dictation"]:   # 音声入力(FR-14b)
            return self.dictation.api(method, parts[2:], body, path)
        if parts == ["api", "settings"]:
            return (200, self.settings_view()) if method == "GET" else self.save_ui_settings(body or {})
        if method == "POST" and len(parts) == 3 and parts[:2] == ["api", "keys"] and parts[2] in ("save", "delete", "check"):
            return self._keys_api(parts[2], body or {})
        if method == "POST" and parts == ["api", "open"]:
            return self._open_url(body or {})
        if method == "POST" and parts == ["api", "models"]:
            return self._list_models(body or {})
        if parts[:2] == ["api", "obsidian"]:   # Obsidian(FR-5b)
            if method == "GET" and len(parts) == 2:
                return 200, self._obsidian_view()
            if method == "POST" and parts[2:] == ["open"]:
                return self._open_note(body or {})
            if method == "POST" and parts[2:] == ["choose"]:
                return self._choose_vault()
            if method == "POST" and parts[2:] == ["index"]:
                return self._index_start(bool((body or {}).get("full")))
            if method == "POST" and parts[2:] == ["mcp", "check"]:
                return self._mcp_check()
            return None
        if parts == ["api", "replacements"]:   # 置き換え辞書(⚙)。中身は個人の固有名詞なので ~/.meeting-cue に置く
            if method == "GET":
                return 200, self.replacer.view()
            text = str((body or {}).get("text") or "")
            if len(text) > 200_000:
                return 400, {"error": "置き換え辞書が大きすぎます"}
            self.replacer.save(text)
            v = self.replacer.view()
            self.term.status(f"置き換え辞書を保存: {v['rules']} 語・誤り {v['wrongs']} 通り・使えない行 {len(v['ignored'])}")
            return 200, v
        if parts == ["api", "version"] and method == "GET":
            q = dict(x.split("=", 1) for x in (urlparse(path).query or "").split("&") if "=" in x)
            return 200, self._version(q.get("app", ""))
        if parts == ["api", "storage"] and method == "GET":
            return 200, {"drive": [{"label": a["label"]} for a in self._drive_accounts()]}
        if parts[:2] != ["api", "sessions"]:
            return None
        if method == "POST" and len(parts) == 4 and parts[3] in ("export", "drive", "trash"):
            return self._session_op(parts[3], parts[2], body or {})
        root = self._roots()
        cur = self.session.dir.name if self.session else None
        if method == "GET" and len(parts) == 2:
            return 200, {"sessions": library.list_sessions(root), "current": cur}
        if method == "GET" and len(parts) == 4 and parts[3] == "levels":
            lv = library.levels(root, parts[2])
            return (200, lv) if lv is not None else (404, {"error": "no levels"})
        if method == "GET" and len(parts) == 3:
            d = library.session_detail(root, parts[2])
            return (200, d | {"recording": parts[2] == cur}) if d else (404, {"error": "no such session"})
        if method == "POST" and len(parts) == 4 and parts[3] == "title":
            title = str((body or {}).get("title") or "")
            if parts[2] == cur and self.session:   # 録音中は閉じるときに meta を書き直すので、手元の meta も直す
                self.session.meta["title"] = title.strip()[:120] or library.DEFAULT_TITLE
                self.session.write_meta()
                self.web.set_state(title=self.session.meta["title"])
                return 200, {"ok": True}
            return (200, {"ok": True}) if library.set_title(root, parts[2], title) else (404, {"error": "no such session"})
        return None


def main(cfg: Config, *, sources: list[Source], port: int, window: bool, model: str | None,
         llm_enabled: bool, summary: bool, redo_after: bool = True) -> int:
    app = App(cfg, sources=sources, port=port, window=window, model=model, llm_enabled=llm_enabled, summary=summary,
              redo_after=redo_after)
    t0 = time.time()
    asyncio.run(app.run())
    print(f"meetcue app: 終了({round(time.time() - t0)} s)")
    return 0


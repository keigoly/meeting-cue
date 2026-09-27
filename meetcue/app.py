"""meetcue app — 常駐して Web UI を出し続け、録音の開始・停止をウィンドウのボタンで行う(FR-7・U1)。

1 回の録音 = 1 セッション = 1 Pipeline。`meetcue run`(1 プロセス 1 セッション)はそのまま残す。
- POST /api/action {"action":"start","mode":…,"privacy":…} / {"action":"stop"} / {"action":"quit"}
  それ以外(toggle_pause / deepdive / mode)は録音中の Pipeline へ渡す。
- GET /api/sessions(一覧)/ GET /api/sessions/<id>(詳細)/ POST /api/sessions/<id>/title
- 一覧の右クリック(2026-09-26): POST /api/sessions/<id>/export {dest?}(dest が無ければ保存先を選ぶダイアログ)/
  POST /api/sessions/<id>/drive {account?}(Google Drive へ移す・LOCAL は断る)/ POST /api/sessions/<id>/trash(ゴミ箱へ)/
  GET /api/storage(Google Drive の有無)。操作ごとに端末ログへ所要と成否を出し、全画面へ sessions_changed を配る。
Ctrl-C(SIGINT)と SIGTERM は録音を正規に止めてから終了する。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import captions, library
from .config import Config
from .metrics import Metrics
from . import providers, secrets
from .cues import claude_cli, llm, openai_direct
from .pipeline import Pipeline, Source
from .replacements import Replacer
from .secrets import openrouter_key
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
               "theme": "auto"}
THEMES = ("auto", "light", "dark")


def _valid(dv, v) -> bool:
    """ui.json の値の型を既定値に合わせて検める(価格は整数も可・負は不可・bool は数に数えない)。"""
    if isinstance(dv, float):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
    return isinstance(v, type(dv))
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
REPO = Path(__file__).resolve().parents[1]
# アップデートの変更内容に出さない本文の行(ファイル名・調査の段・試験の記録)
_TECH_LINE = re.compile(r"\.(py|swift|html|sh|md|toml)\b|^Step \d|^確認|^試験|^実測")


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
    def __init__(self, cfg: Config, *, sources: list[Source], port: int = 8765, window: bool = True,
                 model: str | None = None, llm_enabled: bool = True, summary: bool = True):
        self.cfg = cfg
        self.sources = sources
        self.window = window
        self.model = model or cfg.llm.model
        self.llm_enabled = llm_enabled
        self.summary = summary
        self.term = TerminalUI(show_partials=False)
        self.web = WebUI(port=port, page="app.html")
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
        watchdog = asyncio.create_task(self._caption_watchdog())
        await self._shutdown.wait()
        self.term.status("終了します")
        watchdog.cancel()
        async with self._caption_lock:
            await self._caption.stop()
        if self.pipeline:
            self.pipeline.request_stop()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=60)
            except asyncio.TimeoutError:
                self.term.error("録音の後始末が 60 秒で終わりませんでした")
        await self._stop_window()
        if self._cli_pool:
            self._cli_pool.close()
        self.web.stop()

    async def _spawn_window(self) -> None:
        exe = self.cfg.helpers_dir / "overlay_helper" / "overlay-helper"
        if not exe.exists():
            self.term.error(f"ウィンドウの helper が無い: {exe}(helpers/macos で make)。ブラウザで {self.web.url} を開いてください")
            return
        self._window_proc = await asyncio.create_subprocess_exec(
            str(exe), "--window", "--url", self.web.url, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
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
        elif name == "top":
            asyncio.ensure_future(self._window_cmd("top " + ("on" if body.get("on") else "off")))
            self.web.set_state(top=bool(body.get("on")))
        elif self.pipeline:
            self.pipeline.action(name)
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
            if pool:   # 字幕の翻訳もするなら 1 つ多く温める(回答候補の分を取らない)
                pool.set_warm({target.model: max(0, cfg.llm.claude_cli_warm) + (1 if self._translator.lang else 0)})
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
        session = Session(cfg.sessions_root, mode=mode, privacy=privacy,
                          sources=[s.kind + ":" + s.channel + (":" + s.arg if s.arg else "") for s in self.sources],
                          model=self.model)
        title = (opts.get("title") or "").strip()
        if title:
            session.meta["title"] = title
            session.write_meta()
        ui = MultiUI(self.term, self.web, _CaptionTap(self))   # 録音中の確定した字幕も翻訳へ
        p = Pipeline(cfg, session, ui, self.sources, api_key=key, llm_enabled=generate, jev_enabled=jev_on,
                     model=self.model, target=target if st["provider"] != "openrouter" else None, jev_api_key=jev_key)
        p.overlay = False   # 画面はアプリのウィンドウ
        self.session, self.pipeline = session, p
        self.web.reset_history()
        self.web.set_state(recording=True, session=session.dir.name, title=title or library.DEFAULT_TITLE,
                           started_ms=session.started_ms, mode=mode, privacy=privacy, paused=False,
                           llm=p.target.label if p.llm_enabled else "off")
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
            if not busy:   # 録音していない間: 翻訳するときだけ Sonnet を 1 つ温める(オンにした時点で。1 行目から待たせない)
                st = self.ui_settings()
                use = bool(self._translator.lang and st["provider"] == "claude-cli" and st["generate"] and self.llm_enabled)
                cli = self._claude_cli() if use and self._cli_pool is None else None
                if cli:
                    self._cli_pool_get(cli)
                if self._cli_pool:
                    self._cli_pool.set_warm({self.cfg.anthropic.model: 1} if use else {})

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
    def _version(self, app_commit: str) -> dict:
        head = _git("rev-parse", "HEAD")
        run = self._boot_commit
        base = app_commit if app_commit and _git("cat-file", "-t", app_commit) == "commit" else run
        subjects = _git("log", "--format=%h %s", f"{run}..{head}").splitlines() if run and head and run != head else []
        changed = _git("diff", "--name-only", f"{base}..{head}").splitlines() if base and head and base != head else []
        rebuild = any(f.startswith(("helpers/macos/", "packaging/")) for f in changed)
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
                "recording": bool(self._task and not self._task.done())}

    def _file_route(self, path: str):
        """GET /api/sessions/<id>/audio/<channel>.m4a → 録音した音声(Range 対応は WebUI 側)。"""
        parts = [unquote(x) for x in urlparse(path).path.split("/") if x]
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
                "anthropic_models": {"main": a.model, "fast": a.fast_model, "deep": a.deep_model},
                "need_onboarding": not cur["onboarded"] and not any(v["set"] for v in keys.values())}

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
        subprocess.run(["open", url], check=False)
        return 200, {"ok": True}

    # ---- 記録の置き場(Mac + Google Drive)と右クリックの操作 -------------------------------------
    def _drive_accounts(self) -> list[dict]:
        return library.drive_accounts()

    def _roots(self) -> list:
        return [self.cfg.sessions_root, *(a["root"] for a in self._drive_accounts())]

    def _choose_folder(self) -> Path | None:
        """保存先のフォルダを選ぶ(osascript の choose folder・アプリでもブラウザでも出る)。キャンセルは None。"""
        script = ('tell me to activate\n'
                  'POSIX path of (choose folder with prompt "録音を書き出す場所を選んでください" '
                  'default location (path to downloads folder))')
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
                res = library.export_session(self._roots(), sid, dest, self.cfg.helpers_dir / "mix_helper" / "mix-helper")
                if not body.get("dest"):
                    subprocess.run(["open", "-R", res["path"]], check=False)   # Finder で書き出した場所を見せる
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
        if parts == ["api", "settings"]:
            return (200, self.settings_view()) if method == "GET" else self.save_ui_settings(body or {})
        if method == "POST" and len(parts) == 3 and parts[:2] == ["api", "keys"] and parts[2] in ("save", "delete", "check"):
            return self._keys_api(parts[2], body or {})
        if method == "POST" and parts == ["api", "open"]:
            return self._open_url(body or {})
        if method == "POST" and parts == ["api", "models"]:
            return self._list_models(body or {})
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
         llm_enabled: bool, summary: bool) -> int:
    app = App(cfg, sources=sources, port=port, window=window, model=model, llm_enabled=llm_enabled, summary=summary)
    t0 = time.time()
    asyncio.run(app.run())
    print(f"meetcue app: 終了({round(time.time() - t0)} s)")
    return 0


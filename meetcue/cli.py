"""meetcue CLI — run / app / redo / index / doctor / report。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from . import __version__
from .config import Config, default_port, load_config
from .metrics import percentiles
from .secrets import openrouter_key


def cmd_run(a: argparse.Namespace) -> int:
    from .pipeline import Pipeline, Source
    from .session import Session
    from .ui.terminal import TerminalUI

    cfg = load_config(Path(a.config) if a.config else None)
    if a.mode:
        cfg.mode = a.mode
    if a.privacy:
        cfg.privacy = a.privacy
    if a.pace:
        cfg.file_pace = a.pace
    if a.vault:
        cfg.vault_root = Path(a.vault).expanduser()
    if a.no_record:
        cfg.record_audio = False
    sources = [Source.parse(s) for s in (a.source or ["mic", "tap:zoom"])]
    key = openrouter_key()
    model = a.model or (cfg.llm.fast_model if a.fast else cfg.llm.model)
    session = Session(cfg.sessions_root, mode=cfg.mode, privacy=cfg.privacy,
                      sources=[s.kind + ":" + s.channel + (":" + s.arg if s.arg else "") for s in sources],
                      model=model, echo_metrics=a.echo_metrics)
    term = TerminalUI(show_partials=a.partials)
    web = None
    if a.ui in ("web", "overlay"):
        from .ui.web import MultiUI, WebUI
        web = WebUI(port=a.port)
        ui = MultiUI(term, web)
    else:
        ui = term
    ui.status(f"session {session.dir}")
    if cfg.privacy == "local":
        ui.status("LOCAL: クラウド呼び出しなし(Jev → heuristic / LLM off)")
    elif not key:
        ui.status("OPENROUTER_API_KEY が無い(~/.secrets/meeting-cue.env)。Jev → heuristic / LLM off")
    stt, soniox_key = a.stt or "local", None   # 会議の文字起こし(FR-2b): soniox は LOCAL では使わない
    if stt == "soniox" and cfg.privacy == "local":
        ui.status("LOCAL: Soniox には送らず、端末内で文字起こしします")
        stt = "local"
    elif stt == "soniox":
        from .secrets import api_key
        soniox_key = api_key("soniox")[0]
        if not soniox_key:
            ui.status("Soniox のキーが無い(キーチェーン local.meetcue/soniox か SONIOX_API_KEY)。端末内で文字起こしします")
            stt = "local"
    p = Pipeline(cfg, session, ui, sources, api_key=key, llm_enabled=not a.no_llm, model=model,
                 stt=stt, soniox_key=soniox_key)
    p.hotkeys = not a.no_hotkeys
    p.hotkey_emit = a.hotkey_emit or ""
    if web:
        web.on_action = p.action_threadsafe
        web.start()
        ui.status(f"web ui: {web.url}")
        p.overlay = a.ui == "overlay"
        p.overlay_url = web.url
    try:
        asyncio.run(p.run(seconds=a.seconds))
    finally:
        session.close()
        if web:
            web.stop()
        term.status(f"counts={p.counts} session={session.dir}")
    if not a.no_redo:   # 録音後の文字起こしし直し(FR-9b・アプリの停止と同じ順: し直し → サマリ)
        res = _redo(cfg, session.dir, p.replacer, stop_t0=getattr(p, "_stop_t0", None))
        term.status("redo: " + " ".join(f"{k}={res.get(k)}" for k in ("ok", "reason", "error", "lines", "chars", "ms")
                                        if res.get(k) is not None))
    print_report(session.dir)
    if not a.no_summary:
        from .summary import build_summary, save_to_vault
        sp = build_summary(session.dir, api_key=key, model=cfg.llm.fast_model, privacy=cfg.privacy)
        print(f"summary: {sp}")
        if a.save_vault:
            dest = save_to_vault(sp, cfg.vault_root, privacy=cfg.privacy, session_id=session.id)
            print(f"vault: {dest}")
    return 0


def cmd_app(a: argparse.Namespace) -> int:
    from .app import main as app_main
    from .pipeline import Source

    cfg = load_config(Path(a.config) if a.config else None)
    if a.mode:
        cfg.mode = a.mode
    if a.privacy:
        cfg.privacy = a.privacy
    if a.pace:
        cfg.file_pace = a.pace
    if a.no_record:
        cfg.record_audio = False
    sources = [Source.parse(s) for s in (a.source or ["mic", "tap-all"])]
    model = a.model or (cfg.llm.fast_model if a.fast else cfg.llm.model)
    return app_main(cfg, sources=sources, port=a.port, window=not a.no_window, model=model,
                    llm_enabled=not a.no_llm, summary=not a.no_summary, redo_after=not a.no_redo)


def _redo(cfg: Config, d: Path, replacer, *, force: bool = False, stop_t0: float | None = None) -> dict:
    from . import redo
    from .metrics import Metrics

    m = Metrics(Path(d) / "metrics.jsonl")
    try:
        return asyncio.run(redo.redo_session(d, cfg, replacer=replacer, metrics=m, force=force, stop_t0=stop_t0))
    finally:
        m.close()


def find_session(cfg: Config, arg: str) -> Path | None:
    """記録のフォルダ・記録の名前(YYYYMMDD_HHMMSS_<id>)・末尾の id のどれかから記録を探す。"""
    p = Path(arg).expanduser()
    if (p / "meta.json").exists():
        return p
    if arg and all(c.isalnum() or c == "_" for c in arg):
        for d in sorted(cfg.sessions_root.glob(f"*{arg}"), reverse=True):
            if (d / "meta.json").exists():
                return d
    return None


def cmd_redo(a: argparse.Namespace) -> int:
    """録音済みの記録の文字起こしをし直す(FR-9b)/ 表示に使う文字起こしを切り替える(--use live|redo)。"""
    from . import redo
    from .replacements import Replacer

    cfg = load_config(Path(a.config) if a.config else None)
    d = find_session(cfg, a.session)
    if d is None:
        print(f"記録が無い: {a.session}", file=sys.stderr)
        return 2
    if a.use:
        ok = redo.use_transcript(d, a.use)
        print(json.dumps({"session": d.name, "use": a.use, "ok": ok}, ensure_ascii=False))
    else:
        res = _redo(cfg, d, Replacer(cfg.app_dir / "replacements.txt"), force=True)   # 手で頼んだときは config の enabled によらない
        ok = res["ok"]
        print(json.dumps({"session": d.name, **res}, ensure_ascii=False))
    if ok and a.summary:
        sp = _rebuild_summary(cfg, d)
        print(json.dumps({"summary": str(sp) if sp else None}, ensure_ascii=False))
    return 0 if ok else 1


def _rebuild_summary(cfg: Config, d: Path) -> Path | None:
    """選んでいる文字起こしからサマリを作り直す(OpenRouter の鍵・meetcue run と同じ)。前のサマリは summary.live.md に残す。
    鍵が無ければ作り直さない(LLM の要約が入った今のサマリを、要約なしで上書きしない)。"""
    from .redo import read_meta
    from .summary import build_summary

    privacy = read_meta(d).get("privacy") or cfg.privacy
    key = openrouter_key() if privacy != "local" else None
    if privacy != "local" and not key:
        print("OpenRouter の鍵が無いため、サマリは作り直しません", file=sys.stderr)
        return None
    sm, keep = d / "summary.md", d / "summary.live.md"
    if sm.exists() and not keep.exists():
        keep.write_bytes(sm.read_bytes())
    return build_summary(d, api_key=key, model=cfg.llm.fast_model, privacy=privacy)


def cmd_index(a: argparse.Namespace) -> int:
    from .knowledge import obsidian
    from .knowledge.index import VaultIndex

    cfg = load_config(Path(a.config) if a.config else None)
    root = Path(a.vault).expanduser() if a.vault else obsidian.effective_vault_root(cfg)   # 設定画面 → config.toml
    if not root.exists():
        print(f"Vault が無い: {root}(⚙ の Obsidian・config.toml の vault_root か --vault)", file=sys.stderr)
        return 2
    cfg.index.exclude = list(cfg.index.exclude) + list(a.exclude or [])
    st = obsidian.build_index(cfg, root, full=a.full)
    idx = VaultIndex(cfg.index_db)
    print(json.dumps({"build": st, "db": str(cfg.index_db)}, ensure_ascii=False))
    if a.query:
        from .knowledge.index import query_terms
        terms = query_terms(a.query)
        for h in idx.search(a.query, k=cfg.index.top_k, terms=terms):
            print(f"- {h.path}#{h.heading} :: {h.snippet(terms)}")
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    cfg = load_config(Path(a.config) if a.config else None)
    ok = True

    def row(name: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and good
        print(f"{'OK ' if good else 'NG '} {name}: {detail}")

    row("python", sys.version_info >= (3, 12), sys.version.split()[0])
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        row("sqlite fts5", True, sqlite3.sqlite_version)
    except sqlite3.OperationalError as e:
        row("sqlite fts5", False, str(e))
    if sys.platform == "win32":   # Windows: 専用の仮想環境の python + ヘルパーのスクリプト + モデル(FR-12)
        model = cfg.app_dir / "models" / "faster-whisper-large-v3-turbo" / "model.bin"
        for name, p in (("stt python", cfg.stt_python), ("helper stt_helper.py", cfg.helpers_dir / "stt_helper" / "stt_helper.py"),
                        ("stt model", model)):
            row(name, p.exists(), str(p) if p.exists() else r"無い → packaging\windows\setup.ps1 を実行")
    else:
        for name in ("stt_helper/stt-helper", "tap_helper/tap-helper"):
            p = cfg.helpers_dir / name
            row(f"helper {name}", p.exists(), str(p) if p.exists() else f"無い → cd helpers/macos && make")
    from .knowledge import obsidian
    vault = obsidian.effective_vault_root(cfg)   # 設定画面(ui.json)→ config.toml
    row("vault", vault.exists(), str(vault))
    if cfg.index_db.exists():
        from .knowledge.index import VaultIndex
        row("index", True, json.dumps(VaultIndex(cfg.index_db).stats()))
    else:
        row("index", False, f"無い → meetcue index")
    key = openrouter_key()
    row("OPENROUTER_API_KEY", key is not None, "あり" if key else "無し(~/.secrets/meeting-cue.env)。クラウド機能は off")
    if key and a.online:
        from .judge import jev
        r = jev.decide(jev.build_state("費用はどのくらいでしょうか。", [], "participant", "system"), api_key=key,
                       model=cfg.jev.model, timeout=cfg.jev.timeout_s)
        row("jev", r.ok, f"{r.ms}ms {jev.summarize(r.answers).get('speech_act') if r.ok else r.error + ' ' + r.detail[:80]}")
    print("privacy:", cfg.privacy, "/ mode:", cfg.mode, "/ app_dir:", cfg.app_dir)
    return 0 if ok else 1


def print_report(session_dir: Path) -> None:
    mp = Path(session_dir) / "metrics.jsonl"
    if not mp.exists():
        print("metrics.jsonl が無い")
        return
    by_phase: dict[str, list[float]] = defaultdict(list)
    fails: dict[str, int] = defaultdict(int)
    first_cue = {"cue": [], "knowledge": []}
    for line in mp.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ph = r.get("phase")
        if ph == "e2e_first_cue":
            first_cue.setdefault(r.get("kind", "cue"), []).append(float(r["ms"]))
            continue
        if isinstance(r.get("ms"), (int, float)) and ph in ("judge", "retrieve", "generate", "select"):
            by_phase[ph].append(float(r["ms"]))
        if r.get("ok") is False:
            fails["cancelled" if r.get("error") == "cancelled" else ph] += 1
    print(f"--- report {session_dir}")
    for ph in ("judge", "retrieve", "generate", "select"):
        print(f"  {ph:9} {percentiles(by_phase.get(ph, []))} fails={fails.get(ph, 0)}"
              + (f" cancelled={fails['cancelled']}" if ph == "generate" and fails.get("cancelled") else ""))
    for k, v in first_cue.items():
        if v:
            print(f"  e2e_first_cue[{k}] {percentiles(v)}  (発話確定 → 最初のキュー行)")
    for line in mp.read_text(encoding="utf-8").splitlines():   # 停止の内訳(停止ボタン → し直し → 保存まで)
        if any(f'"phase": "{ph}"' in line for ph in ("stop", "redo_transcribe", "summary")):
            r = json.loads(line)
            print(f"  {r['phase']:9} " + " ".join(f"{k}={v}" for k, v in r.items() if k not in ("ts_ms", "rid", "phase")))
    seg = sum(1 for line in mp.read_text(encoding="utf-8").splitlines() if '"phase": "segment"' in line)
    print(f"  segments={seg}")
    sx: dict[str, list[dict]] = defaultdict(list)   # 会議の文字起こしを Soniox で(FR-2b)
    for line in mp.read_text(encoding="utf-8").splitlines():
        if '"phase": "soniox_' in line:
            r = json.loads(line)
            sx[r["phase"]].append(r)
    if sx:
        fin = [float(r["ms"]) for r in sx["soniox_finalize"] if isinstance(r.get("ms"), (int, float))]
        con = [float(r["ms"]) for r in sx["soniox_connect"] if r.get("ok")]
        print(f"  soniox    finalize→確定 {percentiles(fin)}  接続 {percentiles(con)} 失敗={sum(1 for r in sx['soniox_connect'] if not r.get('ok'))}"
              f" 切断={len(sx['soniox_down'])} 詰まり={len(sx['soniox_stall'])} つなぎ直し={len(sx['soniox_reconnect'])}")
        for r in sx["soniox_fallback"]:
            print(f"  soniox    [{r.get('channel')}] 端末内へ切り替え: {r.get('reason')}(補った final {r.get('filled')})")
        for r in sx["soniox_done"]:
            print(f"  soniox    [{r.get('channel')}] 送った音 {r.get('sent_s')} s・約 ${r.get('usd')}・接続 {r.get('sessions')} 回"
                  f"・final {r.get('finals')}・最後は {r.get('engine')}")


def cmd_dictate(a: argparse.Namespace) -> int:
    """音声入力(FR-14): キーを押している間に話した言葉を Soniox で文字にして、今のアプリに貼り付ける(Ctrl+C で終わる)。"""
    from .dictation import Dictation

    cfg = load_config(Path(a.config) if a.config else None)
    if a.no_paste:
        cfg.dictation.paste = False
    extra = (["--file", a.file] if a.file else []) + (["--emit", a.emit] if a.emit else [])
    return Dictation(cfg, helper_args=extra).run(seconds=a.seconds)


def cmd_report(a: argparse.Namespace) -> int:
    if a.session == "dictation":
        from .dictation import report

        print(report(load_config(Path(a.config) if a.config else None)))
        return 0
    d = Path(a.session).expanduser()
    if not d.exists():
        cfg = load_config()
        cands = sorted(cfg.sessions_root.glob("*"))
        d = cands[-1] if cands else d
    print_report(d)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="meetcue", description="会議中の質問を即座に判定し、Vault の知識と逆質問の候補を出す")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", help="config.toml のパス(既定 ~/.meeting-cue/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="セッションを開始する")
    r.add_argument("--source", "-s", action="append",
                   help="mic | room | file:PATH[:channel] | tap:NAME | tap-pid:N | tap-all(複数可。既定 mic + tap:zoom)")
    r.add_argument("--mode", choices=("participant", "presenter", "audience"))
    r.add_argument("--privacy", choices=("private", "local"))
    r.add_argument("--model", help="OpenRouter の model id")
    r.add_argument("--fast", action="store_true", help="fast_model(Haiku)を使う")
    r.add_argument("--no-llm", action="store_true", help="生成しない(判定と検索だけ)")
    r.add_argument("--seconds", type=float, help="この秒数で自動停止(テスト用)")
    r.add_argument("--pace", type=float, help="file 音源の再生倍速(1.0=実時間)")
    r.add_argument("--partials", action="store_true", help="partial を 1 行更新で表示")
    r.add_argument("--echo-metrics", action="store_true", help="metrics を stderr にも出す")
    r.add_argument("--vault", help="Vault のルート(config を上書き)")
    r.add_argument("--ui", choices=("terminal", "web", "overlay"), default="overlay",
                   help="terminal | web(ターミナル + ブラウザ)| overlay(+ 最前面パネル。既定)")
    r.add_argument("--port", type=int, default=default_port(), help="Web UI のポート(127.0.0.1 固定・既定は利用者ごと)")
    r.add_argument("--no-hotkeys", action="store_true", help="グローバルホットキーを使わない")
    r.add_argument("--hotkey-emit", help="テスト用: 'pause@2,deepdive@4' を秒後に自動発火")
    r.add_argument("--no-summary", action="store_true", help="終了時の summary.md を作らない")
    r.add_argument("--no-record", action="store_true", help="音声を保存しない(既定は audio/<channel>.m4a に保存)")
    r.add_argument("--no-redo", action="store_true", help="終了後に録音から文字起こしをし直さない(FR-9b・Windows)")
    r.add_argument("--stt", choices=("local", "soniox"),
                   help="会議の文字起こし: local = 端末内(既定)/ soniox = 音声を Soniox へ送る(FR-2b・LOCAL では使わない)")
    r.add_argument("--save-vault", action="store_true", help="summary.md を Vault 01_Projects/Meeting Cue/Sessions/ に保存")
    r.set_defaults(fn=cmd_run)

    ap_ = sub.add_parser("app", help="常駐してウィンドウを出す(録音の開始・停止・一覧・再生はウィンドウで)")
    ap_.add_argument("--source", "-s", action="append",
                     help="録音する音源(複数可。既定 mic + tap-all)。書式は run と同じ")
    ap_.add_argument("--mode", choices=("participant", "presenter", "audience"), help="開始時のモードの既定")
    ap_.add_argument("--privacy", choices=("private", "local"), help="開始時の既定")
    ap_.add_argument("--model", help="OpenRouter の model id")
    ap_.add_argument("--fast", action="store_true", help="fast_model(Haiku)を使う")
    ap_.add_argument("--no-llm", action="store_true", help="生成しない(判定と検索だけ)")
    ap_.add_argument("--pace", type=float, help="file 音源の再生倍速(テスト用)")
    ap_.add_argument("--port", type=int, default=default_port(), help="Web UI のポート(127.0.0.1 固定・既定は利用者ごと)")
    ap_.add_argument("--no-window", action="store_true", help="ウィンドウを出さない(ブラウザで開く・テスト用)")
    ap_.add_argument("--no-summary", action="store_true", help="停止時の summary.md を作らない")
    ap_.add_argument("--no-record", action="store_true", help="音声を保存しない(既定は保存・録音後に再生できる)")
    ap_.add_argument("--no-redo", action="store_true", help="停止後に録音から文字起こしをし直さない(FR-9b・Windows)")
    ap_.set_defaults(fn=cmd_app)

    rd = sub.add_parser("redo", help="録音済みの記録の文字起こしを録音からし直す(FR-9b・Windows)")
    rd.add_argument("session", help="記録のフォルダ・記録の名前・末尾の id")
    rd.add_argument("--use", choices=("live", "redo"), help="し直さず、表示・サマリ・書き出しに使う文字起こしを切り替える")
    rd.add_argument("--summary", action="store_true",
                    help="終わったらサマリを作り直す(OpenRouter の鍵・前のサマリは summary.live.md に残す)")
    rd.set_defaults(fn=cmd_redo)

    i = sub.add_parser("index", help="Vault の索引を作る / 更新する")
    i.add_argument("--vault")
    i.add_argument("--full", action="store_true", help="全消し再構築")
    i.add_argument("--exclude", action="append", help="追加の除外(相対パス)")
    i.add_argument("--query", "-q", help="構築後に検索して上位を表示")
    i.set_defaults(fn=cmd_index)

    d = sub.add_parser("doctor", help="前提(helper・索引・鍵・権限)を検査する")
    d.add_argument("--online", action="store_true", help="Jev に 1 回問い合わせて疎通を見る")
    d.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("report", help="セッションの所要 ms を集計する")
    p.add_argument("session", nargs="?", default="", help="セッションのディレクトリ(既定: 最新)。dictation = 音声入力の集計")
    p.set_defaults(fn=cmd_report)

    dc = sub.add_parser("dictate", help="音声入力(FR-14): キーを押している間に話した言葉を今のアプリに貼り付ける")
    dc.add_argument("--no-paste", action="store_true", help="貼り付けず、クリップボードにも触らない(試験用)")
    dc.add_argument("--file", help="試験用: マイクの代わりにこの音声を実時間で流す")
    dc.add_argument("--emit", help="試験用: 'down@1,up@4' のようにキーの押下と離しを秒後に出す")
    dc.add_argument("--seconds", type=float, help="この秒数で終わる(試験用)")
    dc.set_defaults(fn=cmd_dictate)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

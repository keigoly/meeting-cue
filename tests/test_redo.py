"""録音後の文字起こしし直し(REQUIREMENTS FR-9b・2026-10-16)。

偽のヘルパー(python のスクリプト・契約は FR-2 と同じ ready / final / bye)で、どの OS でも次を確かめる:
2 本を録音の頭のずれを足した時刻の順に並べて transcript.redo.jsonl に書き、会議中の transcript.jsonl は残すこと・
置き換え辞書・meta.json の切り替え・metrics の redo_transcribe・1 本でも失敗 / 取り消しなら何も変えないこと・
Mac(--redo を持たない)と録音が無い記録は飛ばすこと・アプリの停止の流れ(進み具合・終了で取り消し)。
語の時刻で行を分ける部分(helpers/windows/stt_helper の redo_lines)は標準ライブラリだけで試す。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from meetcue import redo
from meetcue.app import App
from meetcue.cli import find_session, main as cli_main
from meetcue.config import Config
from meetcue.metrics import Metrics
from meetcue.replacements import Replacer

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "helpers" / "windows" / "stt_helper" / "stt_helper.py"
WAV = REPO / "tests" / "fixtures" / "wav" / "meeting_ja.wav"
SID = "20261016_100000_0a1b2c3d"

FAKE = r'''
import json, sys, threading, time
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8")
a = sys.argv[1:]
ch = a[a.index("--channel") + 1]
assert a[a.index("--redo") + 1].endswith(".m4a") and "--redo-gap" in a
plan = json.load(open(PLAN, encoding="utf-8"))[ch]
out = lambda d: (sys.stdout.write(json.dumps(d, ensure_ascii=False) + "\n"), sys.stdout.flush())
err = lambda d: (sys.stderr.write(json.dumps(d, ensure_ascii=False) + "\n"), sys.stderr.flush())
if plan.get("abort"):
    err({"phase": "abort", "reason": "redo_failed", "error": "CUDA out of memory"})
    sys.exit(8)
quit_ = threading.Event()
def rd():
    for line in sys.stdin:
        if line.strip() == "quit":
            quit_.set()
            return
threading.Thread(target=rd, daemon=True).start()
err({"phase": "asset_ready", "ms": 5})
out({"type": "ready", "channel": ch, "audio_s": plan["audio_s"], "model": "fake-turbo", "source": "redo:x"})
for s, e, t, *temp in plan["lines"]:   # 4 つ目があれば、その区間を temperature の後退で認識したことにする
    time.sleep(plan.get("delay", 0))
    if quit_.is_set():
        err({"phase": "redo_cancelled"})
        out({"type": "bye"})
        sys.exit(0)
    out({"type": "final", "channel": ch, "text": t, "start_s": s, "end_s": e, "fed_s": plan["audio_s"]})
    err({"phase": "redo_segment", "start_s": s, "end_s": e, "audio_s": plan["audio_s"], "lines": 1,
         "temperature": temp[0] if temp else 0.0})
err({"phase": "redo_done", "ms": 12, "speech_s": 3.0, "lines": len(plan["lines"])})
if plan.get("no_bye"):
    sys.exit(3)
out({"type": "bye"})
'''

PLAN = {
    "system": {"audio_s": 30.0, "lines": [[1.0, 2.5, "本日はよろしくお願いします"], [10.0, 12.0, "ミーティングキューの導入時期はいつですか"]]},
    "mic": {"audio_s": 30.0, "lines": [[3.0, 5.0, "よろしくお願いします"], [12.0, 14.0, "来月からを考えています"]]},
}


def _cfg(tmp_path: Path, plan: dict) -> Config:
    """偽のヘルパーを STT ヘルパーの場所に置いた Config(Windows の起動の仕方で sys.executable が動かす)。"""
    hd = tmp_path / "helpers"
    (hd / "stt_helper").mkdir(parents=True)
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (hd / "stt_helper" / "stt_helper.py").write_text(f"PLAN = {str(tmp_path / 'plan.json')!r}\n" + FAKE, encoding="utf-8")
    return Config(app_dir=tmp_path / "home", helpers_dir=hd, stt_python=Path(sys.executable))


def _session(root: Path, *, audio: bool = True) -> Path:
    d = root / SID
    (d / "audio").mkdir(parents=True)
    meta = {"id": "0a1b2c3d", "started_ms": 1_000_000, "ended_ms": 1_060_000, "mode": "participant", "privacy": "private",
            "sources": ["mic:mic", "tap-all:system"], "title": "定例"}
    if audio:
        for ch in ("system", "mic"):
            (d / "audio" / f"{ch}.m4a").write_bytes(b"x")
        meta["audio"] = {"system": {"path": "audio\\system.m4a", "t0_ms": 1_000_000},   # Windows の記録の書き方
                         "mic": {"path": "audio/mic.m4a", "t0_ms": 1_000_500}}         # マイクは 0.5 s 遅れて始まった
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    live = [{"rid": "aaaa0001", "channel": "system", "text": "本日は", "start_seconds": 1.0, "end_seconds": 1.8},
            {"rid": "aaaa0002", "channel": "mic", "text": "ご視聴ありがとうございました", "start_seconds": 2.0, "end_seconds": 3.0}]
    (d / "transcript.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in live), encoding="utf-8")
    return d


def _metrics(d: Path) -> list[dict]:
    return [json.loads(x) for x in (d / "metrics.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def _run(d: Path, cfg: Config, **kw) -> dict:
    m = Metrics(d / "metrics.jsonl")
    try:
        return asyncio.run(redo.redo_session(d, cfg, metrics=m, platform="win32", **kw))
    finally:
        m.close()


def test_argv_and_when_it_cannot_run(tmp_path):
    cfg = _cfg(tmp_path, PLAN)
    argv = redo.helper_argv(cfg, "mic", Path("a/mic.m4a"), platform="win32")
    assert argv[:2] == [sys.executable, str(cfg.helpers_dir / "stt_helper" / "stt_helper.py")]
    assert argv[2:] == ["--locale", "ja-JP", "--channel", "mic", "--redo", str(Path("a/mic.m4a")), "--redo-gap", "1.5"]
    assert redo.helper_argv(cfg, "mic", Path("a.m4a"), platform="darwin") is None
    assert redo.unavailable(cfg, "win32") is None and redo.unavailable(cfg, "darwin") == "unsupported"
    cfg.redo.enabled = False
    assert redo.unavailable(cfg, "win32") == "disabled" and redo.unavailable(cfg, "win32", force=True) is None
    cfg.stt_python = tmp_path / "no-venv" / "python.exe"
    assert redo.unavailable(cfg, "win32", force=True) == "helper_missing"


def test_replaces_transcript_and_keeps_the_live_one(tmp_path):
    cfg = _cfg(tmp_path, PLAN)
    d = _session(cfg.sessions_root)
    live_before = (d / "transcript.jsonl").read_text(encoding="utf-8")
    rp = tmp_path / "replacements.txt"
    rp.write_text("Meeting Cue|ミーティングキュー\n", encoding="utf-8")
    progress: list[dict] = []
    res = _run(d, cfg, replacer=Replacer(rp), on_progress=progress.append, stop_t0=time.perf_counter() - 2.0)
    assert res["ok"] and res["lines"] == 4 and res.get("error") is None
    rows = [json.loads(x) for x in (d / redo.REDO_FILE).read_text(encoding="utf-8").splitlines()]
    # 時刻の順(マイクは録音の頭が 0.5 s 遅い: 相手 1.0 / 自分 3.5 / 相手 10.0 / 自分 12.5)。rid は redo- の連番
    assert [(r["channel"], r["start_seconds"]) for r in rows] == [("system", 1.0), ("mic", 3.0), ("system", 10.0), ("mic", 12.0)]
    assert [r["rid"] for r in rows] == ["redo-0001", "redo-0002", "redo-0003", "redo-0004"]
    assert all(r["source"] == "redo" and r["final"] for r in rows)
    assert rows[1]["t_ms"] == 1_000_500 + 3000                                   # 録音の頭 + 位置
    assert rows[2]["text"] == "Meeting Cueの導入時期はいつですか" and rows[2]["raw"] == "ミーティングキューの導入時期はいつですか"
    assert "raw" not in rows[0]
    assert (d / "transcript.jsonl").read_text(encoding="utf-8") == live_before   # 会議中の文は残す
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["transcript"] == "redo" and meta["title"] == "定例"
    assert meta["redo"]["lines"] == 4 and meta["redo"]["model"] == "fake-turbo" and meta["redo"]["gap_s"] == 1.5
    assert set(meta["redo"]["channels"]) == {"system", "mic"}
    m = [r for r in _metrics(d) if r["phase"] == "redo_transcribe"]
    assert len(m) == 1 and m[0]["ok"] is True and m[0]["chars"] == res["chars"] and m[0]["since_stop_ms"] >= 2000
    assert m[0]["channels"]["system"]["audio_s"] == 30.0 and m[0]["channels"]["mic"]["lines"] == 2
    assert progress and {p["tracks"] for p in progress} == {2} and progress[-1]["track"] == 2
    assert not list(d.glob("*.tmp"))


def test_collapse_loops_only_touches_runaway_repeats():
    assert redo.collapse_loops("はい、はい、はい、はい、") == "はい、"
    assert redo.collapse_loops("、".join(["はい"] * 20)) == "はい"
    assert redo.collapse_loops("そうですね、はい、はい、はい、はい、わかりました") == "そうですね、はい、わかりました"
    assert redo.collapse_loops("いいですか?いいですか?いいですか?いいですか?") == "いいですか?"
    for keep in ("はい、はい、はい", "はいはい", "Goooo", "1,000,000,000,000円です", "導入の時期について伺えればと思います"):
        assert redo.collapse_loops(keep) == keep


def test_counts_fallback_segments_and_collapses_loops(tmp_path):
    # 2026-10-16 Windows 実会議: 短い返事がまばらな最後の窓が temperature の後退で認識され、「はい、」× 20 の行が出た
    loop = "、".join(["はい"] * 20)
    plan = {**PLAN, "mic": {"audio_s": 30.0, "lines": [[3.0, 5.0, "よろしくお願いします"], [20.0, 25.0, loop, 1.0],
                                                       [26.0, 27.0, "失礼します", 1.0]]}}
    cfg = _cfg(tmp_path, plan)
    d = _session(cfg.sessions_root)
    res = _run(d, cfg)
    assert res["ok"]
    assert (res["channels"]["mic"]["fallback_segments"], res["channels"]["mic"]["loops"]) == (2, 1)
    assert (res["channels"]["system"]["fallback_segments"], res["channels"]["system"]["loops"]) == (0, 0)
    m = next(r for r in _metrics(d) if r["phase"] == "redo_transcribe")
    assert m["channels"]["mic"]["fallback_segments"] == 2 and m["channels"]["mic"]["loops"] == 1
    rows = [json.loads(x) for x in (d / redo.REDO_FILE).read_text(encoding="utf-8").splitlines()]
    looped = next(r for r in rows if r["start_seconds"] == 20.0)
    assert looped["text"] == "はい" and looped["raw"] == loop                   # 縮めた前の文は raw に残す
    assert res["chars"] == sum(len(r["text"]) for r in rows) and "raw" not in rows[-1]


def test_one_failed_track_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path, {**PLAN, "mic": {"abort": True}})
    d = _session(cfg.sessions_root)
    meta_before = (d / "meta.json").read_text(encoding="utf-8")
    res = _run(d, cfg)
    assert not res["ok"] and res["error"].startswith("mic: redo_failed") and "out of memory" in res["error"]
    assert not (d / redo.REDO_FILE).exists() and (d / "meta.json").read_text(encoding="utf-8") == meta_before
    m = next(r for r in _metrics(d) if r["phase"] == "redo_transcribe")
    assert m["ok"] is False and m["error"] == res["error"]
    cfg2 = _cfg(tmp_path / "b", {**PLAN, "system": {**PLAN["system"], "no_bye": True}})   # bye の前に落ちた
    d2 = _session(cfg2.sessions_root)
    res2 = _run(d2, cfg2)
    assert not res2["ok"] and "bye なし" in res2["error"] and not (d2 / redo.REDO_FILE).exists()


def test_cancel_stops_the_helper_and_changes_nothing(tmp_path):
    slow = {ch: {**p, "delay": 0.4, "lines": p["lines"] * 10} for ch, p in PLAN.items()}
    cfg = _cfg(tmp_path, slow)
    d = _session(cfg.sessions_root)

    async def main():
        cancel = asyncio.Event()
        m = Metrics(d / "metrics.jsonl")
        task = asyncio.create_task(redo.redo_session(d, cfg, metrics=m, cancel=cancel, platform="win32"))
        await asyncio.sleep(1.0)
        t = time.perf_counter()
        cancel.set()
        res = await asyncio.wait_for(task, 10)
        m.close()
        return res, time.perf_counter() - t

    res, waited = asyncio.run(main())
    assert not res["ok"] and res["reason"] == "cancelled" and waited < 3.0   # quit で区間の合間に止まる
    assert not (d / redo.REDO_FILE).exists() and "transcript" not in json.loads((d / "meta.json").read_text(encoding="utf-8"))


def test_skips_on_mac_and_without_audio(tmp_path):
    cfg = _cfg(tmp_path, PLAN)
    d = _session(cfg.sessions_root)
    m = Metrics(d / "metrics.jsonl")
    res = asyncio.run(redo.redo_session(d, cfg, metrics=m, platform="darwin"))
    m.close()
    assert res == {"ok": False, "lines": 0, "chars": 0, "channels": {}, "reason": "unsupported", "ms": res["ms"]}
    assert next(r for r in _metrics(d) if r["phase"] == "redo_transcribe")["reason"] == "unsupported"
    d2 = _session(tmp_path / "other", audio=False)
    assert _run(d2, cfg)["reason"] == "no_audio"


def test_use_transcript_switches_back_and_forth(tmp_path):
    cfg = _cfg(tmp_path, PLAN)
    d = _session(cfg.sessions_root)
    assert not redo.use_transcript(d, "redo")             # し直した文がまだ無い
    assert _run(d, cfg)["ok"]
    assert redo.use_transcript(d, "live") and json.loads((d / "meta.json").read_text(encoding="utf-8"))["transcript"] == "live"
    assert redo.use_transcript(d, "redo") and not redo.use_transcript(d, "other")


def test_cli_redo_and_find_session(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, PLAN)
    d = _session(cfg.sessions_root)
    assert find_session(cfg, SID) == d and find_session(cfg, "0a1b2c3d") == d and find_session(cfg, str(d)) == d
    assert find_session(cfg, "*") is None and find_session(cfg, "deadbeef") is None
    monkeypatch.setattr("meetcue.cli.load_config", lambda p=None: cfg)
    monkeypatch.setattr(redo, "helper_argv", lambda c, ch, a, platform=None: [sys.executable, str(
        cfg.helpers_dir / "stt_helper" / "stt_helper.py"), "--channel", ch, "--redo", str(a), "--redo-gap", "1.5"])
    cfg.redo.enabled = False                               # 手で頼んだときは config で止めていてもし直す
    assert cli_main(["redo", "0a1b2c3d"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] and out["lines"] == 4 and out["session"] == SID
    assert cli_main(["redo", "0a1b2c3d", "--use", "live"]) == 0
    assert json.loads((d / "meta.json").read_text(encoding="utf-8"))["transcript"] == "live"
    assert cli_main(["redo", "nothere"]) == 2


def test_app_runs_redo_in_saving_and_cancels_on_quit(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, PLAN)
    d = _session(cfg.sessions_root)
    app = App(cfg, sources=[], window=False)
    states: list[dict] = []
    monkeypatch.setattr(app.web, "set_state", lambda **kw: states.append(kw))
    monkeypatch.setattr(app.web, "status", lambda *a, **kw: states.append({"status": a[0], **kw}))
    seen: dict = {}

    async def fake_redo(dd, c, *, replacer, metrics, cancel, on_progress, stop_t0, **kw):
        seen.update(d=dd, stop_t0=stop_t0, cancel=cancel)
        for at in (3.0, 15.0, 30.0):
            on_progress({"channel": "system", "at_s": at, "audio_s": 30.0, "track": 1, "tracks": 2})
        return {"ok": True, "lines": 4, "chars": 40, "ms": 5}

    monkeypatch.setattr(redo, "redo_session", fake_redo)
    monkeypatch.setattr(redo, "unavailable", lambda c, platform=None, force=False: None)
    session, p = SimpleNamespace(dir=d), SimpleNamespace(_stop_t0=123.0)
    res = asyncio.run(app._redo(session, p))
    assert res["ok"] and seen["d"] == d and seen["stop_t0"] == 123.0 and app._redo_cancel is None
    notes = [s["saving_note"] for s in states if "saving_note" in s]
    assert notes == ["文字起こしを仕上げています…", "文字起こしを仕上げています… 5%", "文字起こしを仕上げています… 25%",
                     "文字起こしを仕上げています… 50%", ""]
    assert any(s.get("code") == "redo" for s in states)

    async def quitting():   # 終了の途中で止まった録音はし直さない
        app._shutdown = asyncio.Event()
        app._shutdown.set()
        return await app._redo(session, p)
    assert asyncio.run(quitting()) is None
    assert [r.get("reason") for r in _metrics(d) if r["phase"] == "redo_transcribe"] == ["app_quit"]


def _helper_module():
    spec = importlib.util.spec_from_file_location("win_stt_helper", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # 上の階層は標準ライブラリだけ(faster-whisper は関数の中で読む)
    return mod


def test_redo_lines_split_by_the_gap_between_words():
    h = _helper_module()
    W = lambda s, e, w: SimpleNamespace(start=s, end=e, word=w)   # noqa: E731
    # VAD でつないだ区間が除いた無音をまたいだ(「はい」は 89 s、「なるほど…」は 161 s)→ 語の間で 2 行に分ける
    seg = SimpleNamespace(start=89.0, end=163.0, text=" はい なるほど ありがとうございます",
                          words=[W(89.0, 89.4, " はい"), W(161.0, 161.6, " なるほど"), W(161.7, 163.0, " ありがとうございます")])
    assert h.redo_lines(seg, 1.5) == [(89.0, 89.4, "はい"), (161.0, 163.0, "なるほど ありがとうございます")]
    assert h.redo_lines(seg, 100.0) == [(89.0, 163.0, "はい なるほど ありがとうございます")]
    assert h.redo_lines(SimpleNamespace(start=1.0, end=2.0, text=" そうですね", words=None), 1.5) == [(1.0, 2.0, "そうですね")]
    assert h.redo_lines(SimpleNamespace(start=1.0, end=2.0, text=" ", words=[]), 1.5) == []


def _real_helper_ready() -> bool:
    cfg = Config()
    model = cfg.app_dir / "models" / "faster-whisper-large-v3-turbo" / "model.bin"
    return sys.platform == "win32" and cfg.stt_python.exists() and model.exists() and WAV.exists()


@pytest.mark.skipif(not _real_helper_ready(), reason="専用の仮想環境・モデル・合成音声がそろう Windows でだけ")
def test_real_helper_redo_speaks_the_contract():
    import subprocess
    cfg = Config()
    p = subprocess.run([str(cfg.stt_python), str(HELPER), "--channel", "system", "--redo", str(WAV)],
                       capture_output=True, timeout=300)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")[-2000:]
    evs = [json.loads(x) for x in p.stdout.decode("utf-8").splitlines() if x.strip()]
    assert evs[0]["type"] == "ready" and evs[0]["audio_s"] > 0 and evs[-1]["type"] == "bye"
    finals = [e for e in evs[1:-1] if e["type"] == "final"]
    assert finals and len(finals) == len(evs) - 2                               # partial は出さない
    starts = [e["start_s"] for e in finals]
    assert starts == sorted(starts) and all(e["start_s"] <= e["end_s"] <= e["fed_s"] + 0.5 for e in finals)
    text = unicodedata.normalize("NFKC", "".join(e["text"] for e in finals))
    for word in ("全体像", "トンネル", "費用", "リスク"):   # meeting_ja.txt の文面から
        assert word in text
    diags = [json.loads(x) for x in p.stderr.decode("utf-8").splitlines() if x.startswith("{")]
    done = next(d for d in diags if d.get("phase") == "redo_done")
    assert done["lines"] == len(finals) and any(d.get("phase") == "redo_segment" for d in diags)


# ---- Step 3: 一覧・詳細・サマリ・書き出し・CLI のサマリ(差し替えた文を使い、回答タブは会議中の発言) --------------------
def _redone(tmp_path: Path) -> tuple[Config, Path]:
    """し直しの済んだ記録: 会議中の質問(aaaa0003)に判定と回答候補が付いている。"""
    cfg = _cfg(tmp_path, PLAN)
    d = _session(cfg.sessions_root)
    with (d / "transcript.jsonl").open("a", encoding="utf-8") as fh:   # 会議中は質問が断片で届いた
        fh.write(json.dumps({"rid": "aaaa0003", "channel": "system", "text": "導入時期は", "start_seconds": 10.2,
                             "end_seconds": 11.5, "t_ms": 1_012_000}, ensure_ascii=False) + "\n")
    (d / "judgments.jsonl").write_text("".join(json.dumps(j, ensure_ascii=False) + "\n" for j in (
        {"rid": "aaaa0001", "trigger": False, "source": "jev", "summary": {"speech_act": "greeting", "to_me": 0.1}},
        {"rid": "aaaa0003", "trigger": True, "source": "jev", "summary": {"speech_act": "question", "to_me": 0.7, "intent": "時期"}})),
        encoding="utf-8")
    (d / "cues.jsonl").write_text(json.dumps({"rid": "aaaa0003", "kind": "cues", "part": "answers", "intent": "時期",
                                             "answers": [{"title": "来月", "body": "来月から", "source": "なし"}], "counters": []},
                                            ensure_ascii=False) + "\n", encoding="utf-8")
    assert _run(d, cfg)["ok"]
    return cfg, d


def test_library_shows_the_redone_transcript_and_keeps_questions_on_live(tmp_path):
    from meetcue import library
    cfg, d = _redone(tmp_path)
    row = library.list_sessions(cfg.sessions_root)[0]
    assert row["transcript_source"] == "redo" and row["utterances"] == 4
    det = library.session_detail(cfg.sessions_root, SID)
    assert [u["rid"] for u in det["transcript"]] == ["redo-0001", "redo-0002", "redo-0003", "redo-0004"]
    assert det["transcript"][2]["text"] == "ミーティングキューの導入時期はいつですか"
    assert det["transcript"][1]["at_s"] == 3.5                  # 録音の頭(0.5 s 遅れ)+ 3.0 s
    # 回答タブは会議中の発言(候補はその文に対して作った)。文字起こしの質問の印は時刻の重なる行へ付け替える
    assert [q["rid"] for q in det["questions"]] == ["aaaa0003"] and det["questions"][0]["text"] == "導入時期は"
    assert det["transcript"][2]["jrid"] == "aaaa0003" and det["judgments"]["aaaa0003"]["trigger"]
    assert det["transcript"][0]["jrid"] == "aaaa0001"            # 質問でない判定も付ける(重なりが最も長い行)
    assert "jrid" not in det["transcript"][1] and "jrid" not in det["transcript"][3]
    assert redo.use_transcript(d, "live")                        # 戻すと会議中の文
    det = library.session_detail(cfg.sessions_root, SID)
    assert det["transcript_source"] == "live" and [u["rid"] for u in det["transcript"]][:1] == ["aaaa0001"]
    (d / redo.REDO_FILE).unlink()
    redo.use_transcript(d, "live")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    meta["transcript"] = "redo"                                   # 印だけ残ってファイルが無い → 会議中の文
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    assert library.session_detail(cfg.sessions_root, SID)["transcript_source"] == "live"


def test_summary_and_export_use_the_redone_transcript(tmp_path):
    from meetcue import library
    from meetcue.summary import build_summary
    cfg, d = _redone(tmp_path)
    text = build_summary(d, api_key=None, model="m", privacy="private").read_text(encoding="utf-8")
    assert "発話 4 件 / 文字起こしは録音から認識し直した文" in text
    assert "### 導入時期は" in text and "★回答: **来月**" in text          # 質問とキューは会議中の発言
    body = text.split("## 文字起こし", 1)[1]
    assert "- [system] ミーティングキューの導入時期はいつですか" in body and "ご視聴" not in body
    md = library.transcript_markdown(library.session_detail(cfg.sessions_root, SID))
    lines = [ln.split("] ", 1)[1] for ln in md.splitlines() if ln.startswith("- [")]   # 書き出しも差し替えた文・時刻の順
    assert lines == ["相手: 本日はよろしくお願いします", "自分: よろしくお願いします",
                     "相手: ミーティングキューの導入時期はいつですか", "自分: 来月からを考えています"]


def test_cli_rebuilds_the_summary_only_with_a_key(tmp_path, monkeypatch, capsys):
    cfg, d = _redone(tmp_path)
    (d / "summary.md").write_text("## 要約\n- 会議中の文から作った要約", encoding="utf-8")
    monkeypatch.setattr("meetcue.cli.load_config", lambda p=None: cfg)
    monkeypatch.setattr("meetcue.cli.openrouter_key", lambda: None)
    assert cli_main(["redo", SID, "--use", "redo", "--summary"]) == 0
    assert (d / "summary.md").read_text(encoding="utf-8").startswith("## 要約\n- 会議中")   # 鍵なし: 上書きしない
    assert not (d / "summary.live.md").exists()
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    meta["privacy"] = "local"                                    # LOCAL は鍵なしで作る(要約なし・文字起こしは差し替えた文)
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    assert cli_main(["redo", SID, "--use", "redo", "--summary"]) == 0
    assert "LLM 要約なし" in (d / "summary.md").read_text(encoding="utf-8")
    assert (d / "summary.live.md").read_text(encoding="utf-8").startswith("## 要約\n- 会議中")   # 前のサマリは残す

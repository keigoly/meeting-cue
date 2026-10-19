"""録音後の文字起こしし直し(REQUIREMENTS FR-9b・2026-10-16)— 止めた後、マイクと相手の録音を 1 本ずつ丸ごと認識し直し、
記録の文字起こしを差し替える。

- 会議中の文字起こし(transcript.jsonl)は消さない。し直した文は transcript.redo.jsonl(同じ形 + source "redo")に書き、
  meta.json の transcript を "redo" にする("live" にすれば会議中の文に戻る・use_transcript)。書くのは全部の録音を
  認識し終えたときだけ(1 本でも失敗・取り消しなら何も変えない)。
- 話し手はトラックで決まる。行は録音の頭のずれ(meta.json の audio.<channel>.t0_ms)を足した時刻の順に並べ、rid は
  "redo-0001" から振る(会議中の 8 hex と重ならない)。暴走した繰り返し(同じ句が 4 回以上続く)を 1 回に縮め、置き換え
  辞書をかける。縮める・置き換える前の文は raw に残す。
- ヘルパーは STT ヘルパーの --redo(Windows・契約は FR-2 と同じ ready / final / bye・stdin の quit)。Mac のヘルパーは
  --redo を持たないので飛ばす(reason "unsupported")。
- 計測: metrics の phase=redo_transcribe(rid STOP・ms・since_stop_ms・chars・lines・ok・error / reason・チャネルごとの内訳。
  内訳の fallback_segments は temperature の後退で認識した区間の数・loops は暴走した繰り返しを縮めた行の数)。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable

from .config import Config
from .library import AUDIO_RE, REDO_FILE   # 記録の置き方は library と同じ
from .metrics import Metrics
from .replacements import Replacer
from .stt_reader import STTHelper

READY_TIMEOUT_S = 120.0   # ヘルパーがモデルと録音を読み終えるまで(実測: モデル 2.6〜7.8 s・録音 1 本 0.7 s)
# 認識の上限 = 60 s + 録音の長さ × この割合。実測は 18.7 分の録音 1 本で 14.6〜27.6 s(声の長さに比例・2026-10-16 RTX 4060 Ti)
TIMEOUT_PER_AUDIO_S = 0.5
# 同じ句(2〜15 字)が句読点をはさんで 4 回以上続く = 認識の暴走。短い返事がまばらな窓は temperature の後退(乱数)で
# 認識され、3 回に 1 回「はい、」× 20 の行になった(2026-10-16 Windows 実会議の最後の 30 s・spikes/REDO_TRANSCRIBE.md)
LOOP_RE = re.compile(r"(.{2,15}?)(?:[、。,.!?！？\s]*\1){3,}")


def collapse_loops(text: str) -> str:
    """暴走した繰り返しを 1 回に縮める(「はい、はい、はい、はい、」→「はい、」)。文字を含まない句(桁の ,000)は縮めない。"""
    return LOOP_RE.sub(lambda m: m.group(1) if any(c.isalpha() for c in m.group(1)) else m.group(0), text)


def helper_argv(cfg: Config, channel: str, audio: Path, platform: str | None = None) -> list[str] | None:
    """し直しのヘルパーの起動の仕方。Mac のヘルパーは --redo を持たないので None。"""
    if (platform or sys.platform) != "win32":
        return None
    return [str(cfg.stt_python), str(cfg.helpers_dir / "stt_helper" / "stt_helper.py"), "--locale", cfg.locale,
            "--channel", channel, "--redo", str(audio), "--redo-gap", str(cfg.redo.gap_s)]


def unavailable(cfg: Config, platform: str | None = None, *, force: bool = False) -> str | None:
    """し直さない理由。し直せるなら None。disabled(config で止めた・force で無視)/ unsupported(Mac)/ helper_missing。"""
    if not cfg.redo.enabled and not force:
        return "disabled"
    argv = helper_argv(cfg, "mic", Path("mic.m4a"), platform)
    if argv is None:
        return "unsupported"
    if not (Path(argv[0]).exists() and Path(argv[1]).exists()):
        return "helper_missing"
    return None


def read_meta(d: Path) -> dict:
    try:
        return json.loads((Path(d) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _replace(tmp: Path, dst: Path) -> None:
    """tmp → dst の置き換え。Windows では読んでいる最中の dst を置き換えられないことがあるので少し待ってやり直す。"""
    for i in range(20):
        try:
            tmp.replace(dst)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.1)


def write_meta(d: Path, meta: dict) -> None:
    tmp = Path(d) / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _replace(tmp, Path(d) / "meta.json")


def tracks(d: Path, meta: dict) -> list[tuple[str, Path, int | None]]:
    """し直す録音: [(チャネル, パス, 録音の頭の壁時計)]。meta.json の audio にあり、ファイルが残っているものだけ。"""
    out = []
    for ch, info in (meta.get("audio") or {}).items():
        name = re.split(r"[\\/]", (info or {}).get("path") or "")[-1]   # Windows の記録は "audio\\mic.m4a"
        p = Path(d) / "audio" / name
        if AUDIO_RE.match(name) and p.exists():
            out.append((ch, p, (info or {}).get("t0_ms")))
    return out


def use_transcript(d: Path, which: str) -> bool:
    """表示・サマリ・書き出しに使う文字起こしを切り替える(live = 会議中の文 / redo = し直した文)。"""
    if which not in ("live", "redo") or (which == "redo" and not (Path(d) / REDO_FILE).exists()):
        return False
    meta = read_meta(d)
    if not meta:
        return False
    meta["transcript"] = which
    write_meta(d, meta)
    return True


async def redo_session(d: Path, cfg: Config, *, replacer: Replacer | None = None, metrics: Metrics | None = None,
                       cancel: asyncio.Event | None = None, on_progress: Callable[[dict], None] | None = None,
                       stop_t0: float | None = None, force: bool = False, platform: str | None = None) -> dict:
    """記録 d の録音をし直し、全部そろったら文字起こしを差し替える。結果 {ok, reason?, error?, lines, chars, ms, channels} を
    返し、metrics に phase=redo_transcribe を 1 行残す。stop_t0 は停止を押した時刻(perf_counter・since_stop_ms 用)。"""
    d = Path(d)
    t0 = time.perf_counter()
    meta = read_meta(d)
    tr = tracks(d, meta)
    res: dict = {"ok": False, "lines": 0, "chars": 0, "channels": {}}
    reason = unavailable(cfg, platform, force=force) or (None if tr else "no_audio")
    if reason:
        res["reason"] = reason
    else:
        got: list[tuple[str, dict]] = []
        for i, (ch, path, _) in enumerate(tr):
            prog = None
            if on_progress:
                def prog(p: dict, i=i) -> None:
                    on_progress({**p, "track": i + 1, "tracks": len(tr)})
            r = await _redo_track(cfg, ch, path, cancel=cancel, on_progress=prog, platform=platform)
            res["channels"][ch] = r["stats"]
            if not r["ok"]:
                if r.get("cancelled"):
                    res["reason"] = "cancelled"
                else:
                    res["error"] = f"{ch}: {r.get('error')}"
                break
            got += [(ch, row) for row in r["rows"]]
        else:
            rows = _finish_rows(got, tr, replacer)
            tmp = d / (REDO_FILE + ".tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            _replace(tmp, d / REDO_FILE)
            res.update(ok=True, lines=len(rows), chars=sum(len(r["text"]) for r in rows))
            meta = read_meta(d)   # 読み直す(題名の変更などを消さない)
            meta["transcript"] = "redo"
            meta["redo"] = {"at_ms": int(time.time() * 1000), "ms": round((time.perf_counter() - t0) * 1000),
                            "lines": res["lines"], "chars": res["chars"], "gap_s": cfg.redo.gap_s,
                            "model": next((c.get("model") for c in res["channels"].values() if c.get("model")), None),
                            "channels": res["channels"]}
            write_meta(d, meta)
    res["ms"] = round((time.perf_counter() - t0) * 1000)
    if metrics:
        metrics.emit("STOP", "redo_transcribe", ms=res["ms"], ok=res["ok"], error=res.get("error"), reason=res.get("reason"),
                     since_stop_ms=round((time.perf_counter() - stop_t0) * 1000) if stop_t0 else None,
                     lines=res["lines"], chars=res["chars"], channels=res["channels"])
    return res


def _finish_rows(got: list[tuple[str, dict]], tr: list[tuple[str, Path, int | None]], replacer: Replacer | None) -> list[dict]:
    """2 本を録音の頭のずれを足した時刻の順に並べ、rid を振り、暴走した繰り返しを縮めて置き換え辞書をかける。"""
    t0s = {ch: t for ch, _, t in tr}
    base = min((t for t in t0s.values() if t is not None), default=None)
    off = {ch: (t - base) / 1000 if (t is not None and base is not None) else 0.0 for ch, t in t0s.items()}
    order = {ch: i for i, (ch, _, _) in enumerate(tr)}
    got.sort(key=lambda x: (x[1]["start_seconds"] + off.get(x[0], 0.0), order.get(x[0], 0)))
    rows = []
    for n, (ch, r) in enumerate(got, 1):
        raw = r["text"]
        text = collapse_loops(raw)
        text = replacer.apply(text) if replacer else text
        row = {"rid": f"redo-{n:04d}", "channel": ch, "text": text, "start_seconds": r["start_seconds"],
               "end_seconds": r["end_seconds"], "final": True, "source": "redo"}
        if t0s.get(ch) is not None:   # 録音の頭からの位置を壁時計に(画面の「録音からの時刻」の控え)
            row["t_ms"] = int(t0s[ch] + r["start_seconds"] * 1000)
        if text != raw:
            row["raw"] = raw
        rows.append(row)
    return rows


async def _redo_track(cfg: Config, ch: str, path: Path, *, cancel: asyncio.Event | None,
                      on_progress: Callable[[dict], None] | None, platform: str | None) -> dict:
    """録音 1 本をヘルパーでし直す。{ok, cancelled?, error?, rows: [{text, start_seconds, end_seconds}], stats}。"""
    st: dict = {"audio_s": None, "ms": None, "lines": 0, "chars": 0, "fallback_segments": 0, "loops": 0}
    abort: dict = {}

    def on_diag(dg: dict) -> None:
        ph = dg.get("phase")
        audio_s = dg.get("audio_s") or st["audio_s"]   # stdout の ready と stderr の診断は届く順が決まらない
        if ph == "redo_segment":
            if (dg.get("temperature") or 0) > 0:   # temperature の後退で認識した区間(乱数で文が変わり、暴走しやすい)
                st["fallback_segments"] += 1
            if on_progress and audio_s:
                try:   # 画面の更新の失敗で診断の読み取り(= ヘルパーの stderr)を止めない
                    on_progress({"channel": ch, "at_s": dg.get("end_s") or 0.0, "audio_s": audio_s})
                except Exception:  # noqa: BLE001
                    pass
        elif ph == "redo_done":
            st.update(speech_s=dg.get("speech_s"), decode_ms=dg.get("ms"))
        elif ph == "asset_ready":
            st["load_ms"] = dg.get("ms")
        elif ph == "abort":
            abort.update(dg)

    argv = helper_argv(cfg, ch, path, platform)
    h = STTHelper(argv, channel=ch, on_diag=on_diag)
    t = time.perf_counter()
    rows: list[dict] = []
    out: dict = {"ok": False, "rows": rows, "stats": st}
    try:
        await h.start()
    except OSError as e:
        out["error"] = f"start: {e}"
        return out
    events = h.events().__aiter__()
    waiter = asyncio.ensure_future(cancel.wait()) if cancel else None
    deadline = time.monotonic() + READY_TIMEOUT_S
    bye = timed_out = False
    try:
        while True:
            nxt = asyncio.ensure_future(events.__anext__())
            done, _ = await asyncio.wait({nxt, waiter} - {None}, timeout=max(0.0, deadline - time.monotonic()),
                                         return_when=asyncio.FIRST_COMPLETED)
            if nxt not in done:
                nxt.cancel()
                if waiter is not None and waiter in done:
                    out["cancelled"] = True
                else:
                    timed_out = True
                break
            try:
                ev = nxt.result()
            except StopAsyncIteration:   # ヘルパーが bye を出さずに終わった
                break
            typ = ev.get("type")
            if typ == "ready":
                st["audio_s"] = ev.get("audio_s")
                st["model"] = ev.get("model")
                deadline = time.monotonic() + 60 + TIMEOUT_PER_AUDIO_S * float(ev.get("audio_s") or 0)
            elif typ == "final" and (ev.get("text") or "").strip():
                start = float(ev.get("start_s") or 0.0)
                rows.append({"text": ev["text"].strip(), "start_seconds": start,
                             "end_seconds": float(ev.get("end_s") if ev.get("end_s") is not None else start)})
            elif typ == "bye":
                bye = True
                break
        rc = None if (timed_out or out.get("cancelled")) else await h.wait(timeout=10)
    finally:
        if waiter is not None:
            waiter.cancel()
        if not h.exited:   # 取り消し・時間切れ・本体の終了: quit → 待つ → 止める
            await h.stop()
    st.update(ms=round((time.perf_counter() - t) * 1000), lines=len(rows), chars=sum(len(r["text"]) for r in rows),
              loops=sum(1 for r in rows if collapse_loops(r["text"]) != r["text"]))
    if out.get("cancelled"):
        return out
    if timed_out:
        out["error"] = "timeout"
    elif abort:
        out["error"] = f"{abort.get('reason')}: {abort.get('error') or ''}".strip()
    elif not bye:   # bye の前に終わった(落ちた)。bye まで来ていれば行はそろっている
        out["error"] = f"exit {rc}(bye なし)"
    else:
        out["ok"] = True
    return out

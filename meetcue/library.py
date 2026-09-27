"""セッション記録の一覧・詳細・題名(meetcue app の左の一覧と録音後の画面)と、右クリックの書き出し・移動・削除。

~/.meeting-cue/sessions/<YYYYMMDD_HHMMSS>_<8hex>/ の meta.json と JSONL を読む(書くのは題名のみ)。
セッション id はディレクトリ名。正規表現で検査し、置き場の外は読まない。
置き場(root)は 1 つでも、[Mac の sessions, Google Drive の Meeting Cue!, …] の並びでもよい(2026-09-26・
右クリック「Google Drive に移動…」で移した記録も一覧に出す)。同じ id が複数にあれば先頭(Mac)を使う。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

SID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{8}$")
DEFAULT_TITLE = "無題"
DRIVE_FOLDER = "Meeting Cue!"               # Google Drive のマイドライブの中の置き場
MY_DRIVE_NAMES = ("マイドライブ", "My Drive")   # Google Drive for desktop の表示言語で名前が変わる


def _jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _roots(root) -> list[Path]:
    return [Path(r) for r in root] if isinstance(root, (list, tuple)) else [Path(root)]


def session_dir(root, sid: str) -> Path | None:
    if not SID_RE.match(sid or ""):
        return None
    for r in _roots(root):
        d = r / sid
        if (d / "meta.json").exists():
            return d
    return None


def drive_accounts(cloud: Path | None = None) -> list[dict]:
    """Google Drive for desktop の同期フォルダ(アカウントごと)。{"label": アカウント, "root": <マイドライブ>/Meeting Cue!}。
    ~/Library/CloudStorage/GoogleDrive-<アカウント>/ を毎回探す(アカウント名をコードや設定に書かない)。無ければ []。
    環境変数 MEETCUE_CLOUD_STORAGE で探す場所を変えられる(試験で本物の Drive に書かないため)。"""
    base = cloud or Path(os.environ.get("MEETCUE_CLOUD_STORAGE") or (Path.home() / "Library" / "CloudStorage"))
    out = []
    try:
        accts = sorted(p for p in base.glob("GoogleDrive-*") if p.is_dir())
    except OSError:
        return []
    for acct in accts:
        for my in MY_DRIVE_NAMES:
            if (acct / my).is_dir():
                out.append({"label": acct.name.removeprefix("GoogleDrive-"), "root": acct / my / DRIVE_FOLDER})
                break
    return out


def _meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


AUDIO_RE = re.compile(r"^(mic|system|room)(-\d+)?\.m4a$")


def _audio(d: Path) -> dict:
    """録音した音声: {channel: {"url": …, "t0_ms": 録音の頭の壁時計}}。t0_ms はマイクとスピーカーの頭合わせに使う。"""
    meta = _meta(d).get("audio") or {}
    out = {}
    for ch, info in meta.items():
        rel = (info or {}).get("path") or ""
        name = Path(rel).name
        if AUDIO_RE.match(name) and (d / "audio" / name).exists():
            out[ch] = {"url": f"/api/sessions/{d.name}/audio/{name}", "t0_ms": info.get("t0_ms")}
    return out


def audio_file(root, sid: str, name: str) -> Path | None:
    d = session_dir(root, sid)
    if d is None or not AUDIO_RE.match(name or ""):
        return None
    p = d / "audio" / name
    return p if p.exists() else None


def levels(root, sid: str) -> dict | None:
    d = session_dir(root, sid)
    if d is None or not (d / "levels.json").exists():
        return None
    try:
        return json.loads((d / "levels.json").read_text(encoding="utf-8"))
    except ValueError:
        return None


def summary_row(d: Path, where: str = "mac") -> dict:
    m = _meta(d)
    started = m.get("started_ms")
    ended = m.get("ended_ms")
    if ended is None:   # 録音中か、正常に閉じなかった記録。最後に書かれた時刻で長さを見積もる
        mp = d / "metrics.jsonl"
        ended_est = int(mp.stat().st_mtime * 1000) if mp.exists() else started
    else:
        ended_est = ended
    tr = d / "transcript.jsonl"
    n_utt = sum(1 for _ in tr.open(encoding="utf-8")) if tr.exists() else 0
    n_q = sum(1 for r in _jsonl(d / "judgments.jsonl") if r.get("trigger"))
    return {"id": d.name, "title": m.get("title") or DEFAULT_TITLE, "started_ms": started, "ended_ms": ended,
            "duration_s": round(((ended_est or 0) - (started or 0)) / 1000, 1) if started else None,
            "mode": m.get("mode"), "privacy": m.get("privacy"), "sources": m.get("sources", []),
            "utterances": n_utt, "questions": n_q, "audio": _audio(d), "where": where}


def list_sessions(root, limit: int = 200) -> list[dict]:
    """新しい順。先頭の置き場が Mac、2 番目以降は Google Drive(where = "drive")。"""
    seen: dict[str, tuple[Path, str]] = {}
    for i, r in enumerate(_roots(root)):
        try:
            names = [p for p in r.iterdir() if SID_RE.match(p.name) and (p / "meta.json").exists()] if r.exists() else []
        except OSError:
            continue
        for p in names:
            seen.setdefault(p.name, (p, "mac" if i == 0 else "drive"))
    rows = sorted(seen.values(), key=lambda x: x[0].name, reverse=True)[:limit]
    return [summary_row(d, where) for d, where in rows]


def _where(root, d: Path) -> str:
    return "mac" if d.parent == _roots(root)[0] else "drive"


def session_detail(root, sid: str) -> dict | None:
    d = session_dir(root, sid)
    if d is None:
        return None
    row = summary_row(d, _where(root, d))
    judg = {}
    for r in _jsonl(d / "judgments.jsonl"):
        s = r.get("summary") or {}
        judg[r["rid"]] = {"trigger": bool(r.get("trigger")), "source": r.get("source"),
                          "speech_act": s.get("speech_act"), "to_me": s.get("to_me"), "intent": s.get("intent")}
    cues: dict[str, dict] = {}
    plans: list[dict] = []
    t0 = row["started_ms"] or 0
    for c in _jsonl(d / "cues.jsonl"):
        if c.get("kind") == "plan":   # 質問タブ(U3)
            items = c.get("items") or []
            order = [x["index"] for x in (c.get("ranking") or []) if x.get("total") is not None]
            by_idx = {it.get("index"): it for it in items}
            ranked = [by_idx[i] | {"star": n < 3} for n, i in enumerate(order) if i in by_idx]
            rest = [it | {"star": False} for it in items if it.get("index") not in order]
            plans.append({"pid": c["rid"], "trigger": c.get("trigger"), "model": c.get("model"), "ok": c.get("ok"),
                          "error": c.get("error"), "at_s": round(((c.get("at_ms") or t0) - t0) / 1000, 1),
                          "items": ranked + rest})
            continue
        e = cues.setdefault(c["rid"], {"intent": "", "answers": [], "counters": [], "knowledge": []})
        if c.get("kind") == "knowledge":
            e["knowledge"] = c.get("items", [])
            continue
        if c.get("intent"):
            e["intent"] = c["intent"]
        for kind, key in (("answer", "answers"), ("counter", "counters")):
            items = c.get(key) or []
            if not items:
                continue
            order = [x["index"] for x in (c.get("ranking") or {}).get(kind, []) if x.get("total") is not None]
            ranked = [items[i - 1] | {"star": n < 3} for n, i in enumerate(order) if 0 < i <= len(items)]
            rest = [it | {"star": False} for j, it in enumerate(items, 1) if j not in order]
            e[key] = ranked + rest
    transcript = [{"rid": r["rid"], "channel": r["channel"], "text": r["text"],
                   "start_seconds": r.get("start_seconds"), "end_seconds": r.get("end_seconds"),
                   "at_s": round((r.get("t_ms", t0) - t0) / 1000, 1)} for r in _jsonl(d / "transcript.jsonl")]
    sm = d / "summary.md"
    return {**row, "transcript": transcript, "judgments": judg, "cues": cues, "plans": plans,
            "summary": sm.read_text(encoding="utf-8") if sm.exists() else None}


def set_title(root, sid: str, title: str) -> bool:
    d = session_dir(root, sid)
    if d is None:
        return False
    m = _meta(d)
    m["title"] = (title or "").strip()[:120] or DEFAULT_TITLE
    (d / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ---- 右クリック: 書き出し・Google Drive へ移動・削除(2026-09-26 keigoly様) ------------------------------
def _safe_name(s: str) -> str:
    s = re.sub(r"[\x00-\x1f/:\\]", " ", s or "").strip().strip(".")
    return re.sub(r"\s+", " ", s)[:80] or DEFAULT_TITLE


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    for n in range(2, 1000):
        q = p.with_name(f"{p.name} {n}")
        if not q.exists():
            return q
    raise FileExistsError(p)


def _stamp(ms) -> str:
    return time.strftime("%Y-%m-%d %H%M", time.localtime((ms or 0) / 1000)) if ms else ""


def _utt_positions(detail: dict) -> dict[str, float]:
    """発言の位置(秒・録音の頭から)。画面の録音後の表示(app.html の estimateStarts / uttPos)と同じ見積もり:
    音声認識の 1 回の確定から分けた文は同じ start/end を持つので、文字数の割合で開始を振り分け、チャネルの頭のずれを足す。"""
    audio = detail.get("audio") or {}
    t0s = {("mic" if ch in ("mic", "room") else "system"): a.get("t0_ms") for ch, a in audio.items()}
    base = min((t for t in t0s.values() if t is not None), default=None)
    off = {ch: ((t - base) / 1000 if (t is not None and base is not None) else 0.0) for ch, t in t0s.items()}
    groups: dict[tuple, list[dict]] = {}
    for u in detail.get("transcript") or []:
        groups.setdefault((u.get("channel"), u.get("start_seconds"), u.get("end_seconds")), []).append(u)
    pos: dict[str, float] = {}
    for (ch, st, en), us in groups.items():
        total = sum(len(u.get("text") or "") for u in us) or 1
        acc = 0
        for u in us:
            if st is None:
                pos[u["rid"]] = float(u.get("at_s") or 0)   # 時刻の無い記録は確定の時刻で
            else:
                start = st + (en - st) * acc / total if (en is not None and en > st) else st
                pos[u["rid"]] = max(0.0, start + off.get("mic" if ch in ("mic", "room") else "system", 0.0))
            acc += len(u.get("text") or "")
    return pos


def transcript_markdown(detail: dict) -> str:
    """書き出し用の文字起こし: 見出し(題名・日時・立場・長さ)+ [mm:ss] 自分 / 相手: 本文(時刻は画面の録音後の表示と同じ)。"""
    mode = {"participant": "会議に参加", "presenter": "登壇・発表", "audience": "講演を聴く"}.get(detail.get("mode"), "")
    dur = int(detail.get("duration_s") or 0)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(detail["started_ms"] / 1000)) if detail.get("started_ms") else ""
    head = [f"# {detail.get('title') or DEFAULT_TITLE}", "",
            " ・ ".join(x for x in (when, mode, f"{dur // 60:02d}:{dur % 60:02d}") if x), ""]
    pos = _utt_positions(detail)
    lines = []
    for u in detail.get("transcript") or []:
        t = round(pos.get(u["rid"], u.get("at_s") or 0))   # 画面の mmss と同じく四捨五入
        who = "自分" if u.get("channel") in ("mic", "room") else "相手"
        lines.append(f"- [{t // 60:02d}:{t % 60:02d}] {who}: {u.get('text', '')}")
    return "\n".join(head + (lines or ["(文字起こしはありません)"])) + "\n"


def export_session(root, sid: str, dest_parent: Path, mix_helper: Path) -> dict:
    """dest_parent/<題名> <日時>/ に 音声.m4a(相手と自分を頭合わせして 1 本に重ねる)・文字起こし.md・サマリ.md を置く。"""
    d = session_dir(root, sid)
    detail = session_detail(root, sid)
    if d is None or detail is None:
        raise FileNotFoundError(sid)
    out = _unique(Path(dest_parent) / _safe_name(f"{detail['title']} {_stamp(detail.get('started_ms'))}"))
    out.mkdir(parents=True)
    files, audio_ms = [], None
    meta_audio = _meta(d).get("audio") or {}
    ins = []
    for ch, info in meta_audio.items():
        name = Path((info or {}).get("path") or "").name
        if AUDIO_RE.match(name) and (d / "audio" / name).exists():
            ins.append((d / "audio" / name, (info or {}).get("t0_ms")))
    if ins:
        t0s = [t for _, t in ins if t is not None]
        t0 = min(t0s) if t0s else None
        args = [str(mix_helper), "--out", str(out / "音声.m4a")]
        for f, t in ins:
            off = (t - t0) / 1000 if (t is not None and t0 is not None) else 0
            args += ["--in", f"{f}@{off:.3f}"]
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
        res = json.loads((r.stdout.strip().splitlines() or ["{}"])[-1] or "{}")
        if r.returncode != 0 or not res.get("ok"):
            raise RuntimeError(f"音声の書き出しに失敗: {res.get('error') or r.stderr.strip()[:200]}")
        files.append("音声.m4a")
        audio_ms = res.get("ms")
    (out / "文字起こし.md").write_text(transcript_markdown(detail), encoding="utf-8")
    files.append("文字起こし.md")
    if detail.get("summary"):
        (out / "サマリ.md").write_text(detail["summary"], encoding="utf-8")
        files.append("サマリ.md")
    return {"path": str(out), "files": files, "audio_ms": audio_ms}


def move_to_drive(local_root: Path, sid: str, drive_root: Path) -> Path:
    """Mac の記録のフォルダを Google Drive の Meeting Cue!/<id>/ へ移す。LOCAL(仕事の会議)の記録は断る(FR-11)。"""
    d = session_dir(local_root, sid)
    if d is None:
        raise FileNotFoundError(sid)
    if _meta(d).get("privacy") == "local":
        raise PermissionError("LOCAL(仕事の会議)の記録は Mac の外に出せません")
    dest = Path(drive_root) / sid
    if dest.exists():
        raise FileExistsError(dest)
    Path(drive_root).mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(d), str(dest))   # 別のボリュームなので コピー → 元を消す
    except Exception:
        if d.exists() and dest.exists():  # 途中で失敗: 元が残っていれば、書きかけの移動先を片付ける
            shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def trash_session(root, sid: str, trash: Path | None = None) -> Path:
    """記録のフォルダをゴミ箱へ(元に戻せる)。Google Drive にある記録も同じ(Drive のアプリが削除を同期する)。"""
    d = session_dir(root, sid)
    if d is None:
        raise FileNotFoundError(sid)
    title = _meta(d).get("title") or DEFAULT_TITLE
    dest = _unique((trash or Path.home() / ".Trash") / _safe_name(f"Meeting Cue! {title} {sid}"))
    shutil.move(str(d), str(dest))
    return dest

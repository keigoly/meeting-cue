"""音声入力の履歴と使った量(REQUIREMENTS FR-14b・アプリの「音声入力」の履歴・ステータス)。

記録は ~/.meeting-cue/dictation/<日付>/<時分秒>_<rid>.json(+ 録音 .wav)。meetcue.dictation が 1 回ごとに書く。
使った量と速さは metrics.jsonl(phase = dict_*)から数える(記録を残さない設定でも数だけは数える・文は数えない)。
消すときはゴミ箱へ移す(元に戻せる)。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .metrics import percentiles

USD_PER_HOUR = 0.12   # Soniox のリアルタイム(語彙の分は別・FR-2b)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RID = re.compile(r"^[0-9a-f]{8}$")


def _files(root: Path) -> list[Path]:
    """新しい順の記録(.json)。"""
    days = sorted((d for d in Path(root).iterdir() if d.is_dir() and _DAY.match(d.name)), reverse=True) \
        if Path(root).is_dir() else []
    out: list[Path] = []
    for d in days:
        out.extend(sorted(d.glob("*_*.json"), reverse=True))
    return out


def _load(p: Path) -> dict | None:
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def item(p: Path, rec: dict) -> dict:
    """画面の 1 行。"""
    return {"rid": rec.get("rid"), "at": rec.get("started_at"), "app": rec.get("app") or "", "mode": rec.get("mode"),
            "trigger": rec.get("trigger"), "audio_s": rec.get("audio_s"), "text": rec.get("text") or "",
            "raw": rec.get("raw") or "", "canceled": rec.get("canceled"), "sent": rec.get("sent"),
            "realtime": bool(rec.get("realtime")), "ai": bool((rec.get("ai") or {}).get("ok")),
            "edit": bool(rec.get("edit")), "failed": rec.get("failed"),
            "audio": p.with_suffix(".wav").exists()}


def list_items(root: Path, q: str = "", limit: int = 50, offset: int = 0) -> dict:
    """検索(文・認識のまま・アプリ名に q を含む・大文字小文字と空白を無視)と、新しい順の一部。"""
    key = re.sub(r"\s", "", q or "").lower()
    rows: list[dict] = []
    total = 0
    for p in _files(root):
        rec = _load(p)
        if rec is None:
            continue
        if key and key not in re.sub(r"\s", "", " ".join(
                [rec.get("text") or "", rec.get("raw") or "", rec.get("app") or ""])).lower():
            continue
        if offset <= total < offset + limit:
            rows.append(item(p, rec))
        total += 1
    return {"items": rows, "total": total}


def find(root: Path, rid: str) -> Path | None:
    if not _RID.match(rid or ""):
        return None
    for d in (Path(root).iterdir() if Path(root).is_dir() else []):
        if d.is_dir() and _DAY.match(d.name):
            hit = next(iter(d.glob(f"*_{rid}.json")), None)
            if hit:
                return hit
    return None


def audio_path(root: Path, rid: str) -> Path | None:
    p = find(root, rid)
    w = p.with_suffix(".wav") if p else None
    return w if w and w.exists() else None


def latest(root: Path) -> dict | None:
    """最後の文字起こし(貼り付けの取り戻しに使う・取り消した分も含む)。"""
    for p in _files(root):
        rec = _load(p)
        if rec and (rec.get("text") or "").strip():
            return {"rid": rec.get("rid"), "text": rec["text"], "at": rec.get("started_at")}
    return None


def _trash_dir(trash: Path | None) -> Path:
    t = trash or Path.home() / ".Trash"
    t.mkdir(parents=True, exist_ok=True)
    return t


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        q = p.with_name(f"{p.stem} {i}{p.suffix}")
        if not q.exists():
            return q
    return p.with_name(f"{p.stem} {int(time.time())}{p.suffix}")


def delete(root: Path, rid: str, trash: Path | None = None) -> bool:
    """1 件をゴミ箱へ(文と録音)。"""
    p = find(root, rid)
    if p is None:
        return False
    t = _trash_dir(trash)
    for f in (p, p.with_suffix(".wav")):
        if f.exists():
            shutil.move(str(f), str(_unique(t / f"Meeting Cue! 音声入力 {f.parent.name} {f.name}")))
    return True


def clear(root: Path, trash: Path | None = None) -> int:
    """すべてゴミ箱へ(日付のフォルダごと)。移した記録の数。"""
    n = 0
    t = _trash_dir(trash)
    for d in sorted(Path(root).iterdir()) if Path(root).is_dir() else []:
        if d.is_dir() and _DAY.match(d.name):
            n += len(list(d.glob("*_*.json")))
            shutil.move(str(d), str(_unique(t / f"Meeting Cue! 音声入力 {d.name}")))
    return n


def stats(metrics_path: Path, now: float | None = None) -> dict:
    """今日 / 今月 / 全体の回数・文字・音声の秒・Soniox の費用の目安と、最近 100 回の速さ(p50 / p90)。"""
    now = now or time.time()
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    month = today[:7]
    blank = lambda: {"takes": 0, "chars": 0, "audio_s": 0.0, "canceled": 0}   # noqa: E731
    out = {"today": blank(), "month": blank(), "total": blank()}
    lat: dict[str, list[float]] = {"dict_total": [], "dict_connect": [], "dict_final": []}
    try:
        lines = Path(metrics_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ph = r.get("phase")
        day = datetime.fromtimestamp((r.get("ts_ms") or 0) / 1000).strftime("%Y-%m-%d")
        buckets = [out["total"]] + ([out["month"]] if day[:7] == month else []) + ([out["today"]] if day == today else [])
        for b in buckets:
            if ph == "dict_start":
                b["takes"] += 1
            elif ph == "dict_text":
                b["chars"] += int(r.get("chars") or 0)
            elif ph == "dict_final":
                b["audio_s"] += float(r.get("audio_s") or 0)
            elif ph == "dict_cancel":
                b["canceled"] += 1
        if ph in lat and isinstance(r.get("ms"), (int, float)) and r.get("ok", True):
            lat[ph].append(float(r["ms"]))
    for b in out.values():
        b["audio_s"] = round(b["audio_s"], 1)
        b["usd"] = round(b["audio_s"] / 3600 * USD_PER_HOUR, 4)
    out["latency"] = {k: percentiles(v[-100:]) for k, v in lat.items()}
    return out

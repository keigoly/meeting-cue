"""JSONL メトリクス — 「見える化」の土台(user CLAUDE.md Step 1)。

各フェーズは 1 行の JSONL を吐く: ts_ms / rid / phase / ms / ok / error (+任意)。
rid は発話 id(8 hex)。transcript / judgments / cues と同じ rid で突き合わせる。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_RESERVED = ("ts_ms", "rid", "phase")


def new_rid() -> str:
    return uuid.uuid4().hex[:8]


class Metrics:
    def __init__(self, log_path: Path | str, echo_stderr: bool = False):
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")
        self.echo = echo_stderr

    def emit(self, rid: str, phase: str, **fields) -> dict:
        rec = {"ts_ms": int(time.time() * 1000), "rid": rid, "phase": phase}
        for k in _RESERVED:
            fields.pop(k, None)
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
        if self.echo:
            print(line, file=sys.stderr, flush=True)
        return rec

    @contextmanager
    def timed(self, rid: str, phase: str, **fields):
        """所要 ms を計測し、終了時(例外含む)に 1 行記録する。例外は再送出する。"""
        extra: dict = {}
        t0 = time.perf_counter()
        ok, err = True, None
        try:
            yield extra
        except BaseException as e:
            ok, err = False, f"{type(e).__name__}: {e}"
            raise
        finally:
            payload = dict(fields)
            payload.update(extra)
            payload["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            payload["ok"] = ok
            payload["error"] = err
            self.emit(rid, phase, **payload)

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


def percentiles(values: list[float]) -> dict:
    """件数 / p50 / p90 / max。空なら None 埋め。"""
    if not values:
        return {"n": 0, "p50": None, "p90": None, "max": None}
    v = sorted(values)
    return {
        "n": len(v),
        "p50": round(v[len(v) // 2], 1),
        "p90": round(v[min(int(len(v) * 0.9), len(v) - 1)], 1),
        "max": round(v[-1], 1),
    }

"""セッション記録 — ~/.meeting-cue/sessions/<stamp>_<id>/ に JSONL を書く(docs/REQUIREMENTS.md §9)。

transcript.jsonl は segment のキー(channel / text / start_seconds / end_seconds)を含む。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .metrics import Metrics
from .segmenter import Utterance


class Session:
    def __init__(self, root: Path, *, mode: str, privacy: str, sources: list[str], model: str,
                 echo_metrics: bool = False):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.id = uuid.uuid4().hex[:8]
        self.dir = Path(root) / f"{stamp}_{self.id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.started_ms = int(time.time() * 1000)
        self.meta = {"id": self.id, "started_ms": self.started_ms, "mode": mode, "privacy": privacy,
                     "sources": sources, "model": model, "ended_ms": None}
        self.metrics = Metrics(self.dir / "metrics.jsonl", echo_stderr=echo_metrics)
        self._transcript = (self.dir / "transcript.jsonl").open("a", encoding="utf-8")
        self._judgments = (self.dir / "judgments.jsonl").open("a", encoding="utf-8")
        self._cues = (self.dir / "cues.jsonl").open("a", encoding="utf-8")
        self.write_meta()

    def write_meta(self) -> None:
        (self.dir / "meta.json").write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_transcript(self, u: Utterance) -> None:
        rec = {"rid": u.rid, "channel": u.channel, "text": u.text, "start_seconds": u.start_s,
               "end_seconds": u.end_s, "t_ms": u.t_final_ms, "forced": u.forced, "tail": u.tail, "final": True}
        if u.extra.get("raw"):   # 置き換え辞書を掛ける前の文(認識そのもの)
            rec["raw"] = u.extra["raw"]
        self._transcript.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._transcript.flush()

    def write_judgment(self, rid: str, rec: dict) -> None:
        self._judgments.write(json.dumps({"rid": rid, **rec}, ensure_ascii=False) + "\n")
        self._judgments.flush()

    def write_cue(self, rid: str, rec: dict) -> None:
        self._cues.write(json.dumps({"rid": rid, **rec}, ensure_ascii=False) + "\n")
        self._cues.flush()

    def close(self) -> None:
        self.meta["ended_ms"] = int(time.time() * 1000)
        self.write_meta()
        for fh in (self._transcript, self._judgments, self._cues):
            fh.close()
        self.metrics.close()


def utterance_dict(u: Utterance) -> dict:
    return asdict(u)

"""STT ヘルパーの止め方: 録音を閉じ終える前に強制終了しない(2026-10-08 Windows で m4a の目次が書かれず録音が聞けなくなった件)。

偽のヘルパーは sys.executable で動かす Python(Mac / Windows 共通)。stderr の record_start / record_done だけを真似る。
"""
from __future__ import annotations

import asyncio
import sys
import time

from meetcue.stt_reader import STTHelper

FAKE = r'''
import json, sys, time
mode = sys.argv[1]
if mode != "norec":
    print(json.dumps({"phase": "record_start", "path": "x.m4a"}), file=sys.stderr, flush=True)
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    if line.strip() == "quit":
        break
if mode == "slowclose":
    time.sleep(1.0)   # 溜まった音声を書き出している
    print(json.dumps({"phase": "record_done", "path": "x.m4a"}), file=sys.stderr, flush=True)
time.sleep(30)        # 残りの認識で終われない(止められるまで)
'''


def _stop(mode: str, monkeypatch, grace: float = 5.0) -> tuple[float, list]:
    monkeypatch.setattr(STTHelper, "QUIT_WAIT_S", 0.3)
    monkeypatch.setattr(STTHelper, "RECORD_GRACE_S", grace)
    diags: list[dict] = []

    async def main() -> float:
        h = STTHelper([sys.executable, "-c", FAKE, mode], channel="system", on_diag=diags.append)
        await h.start()
        async for ev in h.events():
            if ev.get("type") == "ready":
                break
        await asyncio.sleep(0.3)   # stderr の record_start を読ませる
        t = time.monotonic()
        await h.stop()
        assert h.proc is not None and h.proc.returncode is not None
        return time.monotonic() - t

    took = asyncio.run(main())
    return took, [d.get("phase") for d in diags]


def test_waits_for_the_recording_to_close_before_stopping(monkeypatch):
    took, phases = _stop("slowclose", monkeypatch)
    assert "record_done" in phases   # 止める前に録音を閉じ終えた
    assert 1.0 <= took < 4.0          # 待ち(ここでは 0.3 s)を過ぎても閉じ終えるまで待ち、閉じた後は残りの認識を待たない


def test_without_recording_the_wait_stays_short(monkeypatch):
    took, _ = _stop("norec", monkeypatch)
    assert took < 2.0


def test_recording_that_never_closes_is_stopped_after_the_grace(monkeypatch):
    took, phases = _stop("hang", monkeypatch, grace=1.0)
    assert "record_done" not in phases
    assert 1.2 <= took < 4.0

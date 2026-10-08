"""本体のイベントループが止まったときの見える化(stall.py・2026-10-08 Windows で本体が 14 分止まった件)。"""
from __future__ import annotations

import asyncio
import contextlib
import time

from meetcue.stall import LoopStallWatch


def _run(path, block_s: float) -> list[float]:
    stalls: list[float] = []

    async def main() -> None:
        with path.open("w", encoding="utf-8") as f:
            task = asyncio.create_task(LoopStallWatch(threshold_s=0.3, tick_s=0.05, file=f, on_stall=stalls.append).run())
            await asyncio.sleep(0.2)
            if block_s:
                time.sleep(block_s)   # ループを止める(2026-10-08 の本体と同じ形)
            await asyncio.sleep(0.2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(main())
    return stalls


def test_dumps_every_thread_and_reports_how_long_the_loop_stopped(tmp_path):
    out = tmp_path / "dump.txt"
    stalls = _run(out, block_s=0.8)
    text = out.read_text(encoding="utf-8")
    assert "Timeout" in text and "test_stall.py" in text   # 止めている行(この試験の time.sleep)が書き出しに出る
    assert len(stalls) == 1 and stalls[0] >= 0.75


def test_quiet_while_the_loop_runs(tmp_path):
    out = tmp_path / "dump.txt"
    assert _run(out, block_s=0) == []
    assert out.read_text(encoding="utf-8") == ""

"""本体のイベントループが止まったときの見える化(2026-10-08)。

Windows で録音中に本体が 14 分 24 秒止まり(発言・判定・音量・ログがすべて途絶え、ひとりでに戻った)、その間ヘルパーの
出力が読まれずに認識が溜まり、停止で強制終了されて録音が壊れた。止まった原因は記録からは分からなかった(Windows_Brief §8)。
ループが threshold_s 以上回らなかったら、その時点の全スレッドの呼び出し位置を書き出す。faulthandler のタイマーは
C のスレッドで動くので、どこかが GIL を握ったまま止まっていても書ける。戻ってきたら止まっていた秒数を on_stall に渡す。
"""
from __future__ import annotations

import asyncio
import faulthandler
import sys
import time
from typing import Callable, TextIO


class LoopStallWatch:
    def __init__(self, threshold_s: float = 20.0, tick_s: float = 1.0, file: TextIO | None = None,
                 on_stall: Callable[[float], None] | None = None):
        self.threshold_s, self.tick_s = threshold_s, tick_s
        self.file = file
        self.on_stall = on_stall

    def _arm(self) -> bool:
        """呼ぶたびに前の予約を取り消して付け直す。書き出し先にファイル番号が無い(pythonw の stderr = None 等)なら使わない。"""
        try:
            faulthandler.dump_traceback_later(self.threshold_s, file=self.file or sys.stderr)
            return True
        except (AttributeError, ValueError, OSError):
            return False

    async def run(self) -> None:
        armed = False
        last = time.monotonic()
        try:
            while True:
                armed = self._arm()
                await asyncio.sleep(self.tick_s)
                now = time.monotonic()
                if now - last >= self.threshold_s and self.on_stall:
                    self.on_stall(now - last)
                last = now
        finally:
            if armed:
                faulthandler.cancel_dump_traceback_later()

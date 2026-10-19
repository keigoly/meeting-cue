"""STT ヘルパー(Swift / 将来の Windows 実装)の起動と JSONL ストリーム受信。

ヘルパーの契約(docs/REQUIREMENTS.md FR-2):
  stdout 1 行 1 JSON: {"type":"ready"|"partial"|"final"|"bye", "channel":…, "text":…, "start_s":…, "end_s":…}
  stdin  1 行 1 コマンド: "finalize" / "quit"
  stderr 診断 JSON(別タスクで読み捨て、詰まりでのデッドロックを防ぐ)
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Callable, Optional


class STTHelper:
    QUIT_WAIT_S = 3.0
    # 録音(record_start)を閉じ終える(record_done)前に強制終了すると、m4a の目次が書かれず録音が丸ごと聞けなくなる
    # (2026-10-08 Windows: 本体が止まって認識が 14 分溜まり、3 s で強制終了された)。閉じ終えていなければここまで待つ。
    # 溜まった 880 s の音声の書き出しが 4.2 s
    RECORD_GRACE_S = 15.0

    def __init__(self, argv: list[str], *, channel: str,
                 on_diag: Optional[Callable[[dict], None]] = None):
        self.argv = list(argv)
        self.channel = channel
        self.on_diag = on_diag
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.ready: dict | None = None
        self.recording = False   # record_start を受けて record_done / record_error をまだ受けていない
        self._record_closed = asyncio.Event()

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    @property
    def exited(self) -> bool:
        return self.proc is not None and self.proc.returncode is not None

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                d = {"raw": line}
            phase = d.get("phase") if isinstance(d, dict) else None
            if phase == "record_start":
                self.recording = True
            elif phase in ("record_done", "record_error"):
                self.recording = False
                self._record_closed.set()
            if self.on_diag is not None:
                self.on_diag(d)

    async def events(self) -> AsyncIterator[dict]:
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev.setdefault("channel", self.channel)
            if ev.get("type") == "ready":
                self.ready = ev
            yield ev

    async def send(self, cmd: str) -> None:
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            try:
                self.proc.stdin.write((cmd + "\n").encode())
                await self.proc.stdin.drain()
            except (ConnectionError, BrokenPipeError, RuntimeError):
                pass

    async def finalize(self) -> None:
        await self.send("finalize")

    async def wait(self, timeout: float) -> int | None:
        """自分で終わるのを待ち、stderr の診断を読み切る(録音後のし直し: bye の後)。過ぎたら stop で止める。終了コードを返す。"""
        if not self.proc:
            return None
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.stop()
            return self.proc.returncode
        if self._stderr_task:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return self.proc.returncode

    async def stop(self) -> None:
        """quit → 3 秒待つ(録音を閉じ終えていなければ RECORD_GRACE_S まで)→ SIGTERM → 2 秒 → SIGKILL。マイク・タップを確実に解放する。"""
        if not self.proc:
            return
        if self.proc.returncode is None:
            await self.send("quit")
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=self.QUIT_WAIT_S)
            except asyncio.TimeoutError:
                if self.recording:
                    try:
                        await asyncio.wait_for(self._record_closed.wait(), timeout=self.RECORD_GRACE_S)
                        await asyncio.wait_for(self.proc.wait(), timeout=0.5)   # 閉じた後に自分で終われば強制しない
                    except asyncio.TimeoutError:
                        pass
            if self.proc.returncode is None:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    self.proc.kill()
        if self._stderr_task:
            self._stderr_task.cancel()

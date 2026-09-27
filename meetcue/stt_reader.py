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
    def __init__(self, argv: list[str], *, channel: str,
                 on_diag: Optional[Callable[[dict], None]] = None):
        self.argv = list(argv)
        self.channel = channel
        self.on_diag = on_diag
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.ready: dict | None = None

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
            if not line or self.on_diag is None:
                continue
            try:
                self.on_diag(json.loads(line))
            except json.JSONDecodeError:
                self.on_diag({"raw": line})

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

    async def stop(self) -> None:
        """quit → 3 秒待つ → SIGTERM → 2 秒 → SIGKILL。マイク・タップを確実に解放する。"""
        if not self.proc:
            return
        if self.proc.returncode is None:
            await self.send("quit")
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    self.proc.kill()
        if self._stderr_task:
            self._stderr_task.cancel()

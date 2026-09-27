"""Claude サブスク(Claude Code 経由)— 開発者向けの切り替え(既定オフ・自己責任・2026-09-26)。

公開するアプリが利用者に出すのは API キーだけ。`config.toml` の `[llm] subscription_cli = true` にした開発者だけが使う。
- 本人の Mac でログイン済みの Claude Code を `claude -p` で呼ぶだけ。認証情報には触れない(集めない・保存しない・中継しない)。
  Anthropic は他社アプリが Claude.ai のログインを提供することや、利用者の代わりにプランの認証情報で通信することを禁止している
  (code.claude.com の Agent SDK overview / Legal and compliance)。本人が自分の Claude Code を自分の用途に使う範囲にとどめる。
- 個人の文脈を混ぜない: `--safe-mode`(CLAUDE.md・フック・MCP・プラグインを読まない)・`--tools ""`(道具なし)・
  `--no-session-persistence`・専用の作業フォルダ。指示(system)は利用者の発言の頭に入れて送る(起動時の system は共通の一文)。
- 速さ(2026-09-26 実測・Sonnet 5): 毎回起動すると初トークン 4.5〜5.2 s。起動を済ませて待たせたプロセスなら送ってから 0.8〜1.7 s。
  → Pool が model ごとに温めたプロセスを持ち、1 回の依頼ごとに使い捨てて補充する(会話を溜めない)。温めが無ければ都度起動。
- 料金はプランの利用枠(API の台帳には数えない: cost_usd は None・extra["billing"] = "subscription")。
- raise しない: 失敗は openrouter.StreamResult(ok=False, error=…) で返す。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .openrouter import StreamResult, _int, _ms

MARKER = "claude-code-subscription"   # Pipeline の api_key 欄に入れる印(秘密ではない。LOCAL では None になり生成が止まる)
SYSTEM = "指示と入力は利用者のメッセージにあります。指示どおりに、指定された書式だけを出力してください。"
BASE_ARGS = ["-p", "--safe-mode", "--tools", "", "--no-session-persistence",
             "--input-format", "stream-json", "--output-format", "stream-json", "--include-partial-messages", "--verbose",
             "--system-prompt", SYSTEM]


def find_cli(hint: str = "") -> str | None:
    """claude の実行ファイル(設定の指定 → PATH → ~/.local/bin/claude)。"""
    for c in (hint, shutil.which("claude"), str(Path.home() / ".local" / "bin" / "claude")):
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return c
    return None


class _Proc:
    """1 回の依頼だけに使う claude -p(stream-json の入力を待つ状態で起動しておく)。"""

    def __init__(self, cli: str, model: str, cwd: Path, effort: str | None = None):
        self.model, self.effort = model, effort
        self.t_spawn = time.monotonic()
        args = [cli, *BASE_ARGS, "--model", model] + (["--effort", effort] if effort else [])
        env = {**os.environ, "NO_COLOR": "1"}
        self.p = subprocess.Popen(args, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)

    def alive(self) -> bool:
        return self.p.poll() is None

    def kill(self) -> None:
        try:
            if self.p.stdin:
                self.p.stdin.close()
        except OSError:
            pass
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.p.kill()


class Pool:
    """model ごとに温めたプロセスを warm 個持つ。take() で 1 つ渡してすぐ補充する。古いもの(max_age_s)は入れ替える。"""

    def __init__(self, cli: str, cwd: Path, warm: dict[str, int] | None = None, max_age_s: float = 600.0):
        self.cli, self.cwd, self.warm, self.max_age_s = cli, Path(cwd), dict(warm or {}), max_age_s
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._idle: dict[tuple, list[_Proc]] = {}
        self._lock = threading.Lock()
        self._closed = False

    def ensure(self) -> None:
        with self._lock:
            if self._closed:
                return
            for model, n in self.warm.items():
                key = (model, None)
                keep = []
                for pr in self._idle.get(key, []):
                    if pr.alive() and time.monotonic() - pr.t_spawn < self.max_age_s:
                        keep.append(pr)
                    else:
                        pr.kill()
                while len(keep) < n:
                    keep.append(_Proc(self.cli, model, self.cwd))
                self._idle[key] = keep

    def set_warm(self, warm: dict[str, int]) -> None:
        """録音の開始で温め始め、終わったら空にする(待たせているプロセスを片付ける)。"""
        with self._lock:
            self.warm = dict(warm)
            extra = [pr for (m, e), lst in self._idle.items() if m not in self.warm or e is not None for pr in lst]
            self._idle = {k: v for k, v in self._idle.items() if k[0] in self.warm and k[1] is None}
        for pr in extra:
            pr.kill()
        if self.warm:
            threading.Thread(target=self.ensure, name="claude-cli-warm", daemon=True).start()

    def take(self, model: str, effort: str | None = None) -> tuple[_Proc, bool]:
        """(プロセス, 温めてあったか)。温めが無ければその場で起動する(準備の分だけ遅い)。"""
        key = (model, effort)
        pr, warm = None, False
        with self._lock:
            lst = self._idle.get(key, [])
            while lst:
                cand = lst.pop(0)
                if cand.alive():
                    pr, warm = cand, True
                    break
                cand.kill()
        if pr is None:
            pr = _Proc(self.cli, model, self.cwd, effort)
        if model in self.warm and effort is None:
            threading.Thread(target=self.ensure, name="claude-cli-warm", daemon=True).start()
        return pr, warm

    def close(self) -> None:
        with self._lock:
            self._closed = True
            procs = [pr for lst in self._idle.values() for pr in lst]
            self._idle.clear()
        for pr in procs:
            pr.kill()


def _prompt(messages: list[dict]) -> str:
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    user = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") != "system")
    return f"# 指示\n{system}\n\n# 入力\n{user}" if system else user


def stream_chat(messages: list[dict], *, pool: Pool | None, model: str, timeout: int = 60, effort: str | None = None,
                on_delta: Callable[[str], None] | None = None,
                should_stop: Callable[[], bool] | None = None, **_ignored) -> StreamResult:
    if pool is None:
        return StreamResult(False, error="no_cli", model=model, detail="Claude Code(claude)が見つかりません")
    t0 = time.perf_counter()
    try:
        pr, warm = pool.take(model, effort)
    except OSError as e:
        return StreamResult(False, error="no_cli", model=model, detail=str(e)[:200])
    res = StreamResult(False, model=model, provider="Claude Code")
    res.extra.update(billing="subscription", warm=warm, age_ms=round((time.monotonic() - pr.t_spawn) * 1000))   # 起動からの時間
    lines: queue.Queue = queue.Queue()

    def reader() -> None:
        try:
            for line in pr.p.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=reader, name="claude-cli-read", daemon=True).start()
    parts: list[str] = []
    try:
        msg = {"type": "user", "message": {"role": "user", "content": _prompt(messages)}}
        pr.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        pr.p.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            if should_stop and should_stop():
                res.error = "cancelled"
                break
            if time.monotonic() > deadline:
                res.error, res.detail = "timeout", f"{timeout}s"
                break
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                res.error = res.error or "bad_response"
                res.detail = res.detail or "claude が応答の途中で終わりました"
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            typ = obj.get("type")
            if typ == "stream_event":
                ev = obj.get("event") or {}
                if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                    t = ev["delta"].get("text") or ""
                    if t:
                        if res.ms_first_token is None:
                            res.ms_first_token = _ms(t0)
                        parts.append(t)
                        res.chunks += 1
                        if on_delta:
                            on_delta(t)
                elif ev.get("type") == "message_start":
                    m = (ev.get("message") or {}).get("model")
                    if isinstance(m, str):
                        res.model = m
            elif typ == "result":
                u = obj.get("usage") or {}
                res.input_tokens = _int(u.get("input_tokens"))
                res.output_tokens = _int(u.get("output_tokens"))
                if obj.get("is_error"):
                    res.error, res.detail = "cli_error", str(obj.get("result") or obj.get("subtype"))[:300]
                break
    except (OSError, ValueError) as e:
        res.error, res.detail = res.error or "cli_error", str(e)[:200]
    finally:
        pr.kill()   # 使い捨て(会話を溜めない)
    res.ms_total = _ms(t0)
    res.text = "".join(parts).strip()
    if res.error:
        return res
    if not res.text:
        res.error = "empty_output"
        return res
    res.ok = True
    return res

"""ローカル Web UI — stdlib の HTTP サーバ + SSE(Server-Sent Events)。依存ゼロで Mac / Windows 共通。

- GET /            画面(static/index.html)
- GET /events      SSE。接続時に state と直近の履歴を流し、以後はライブ
- GET /api/state   現在の状態(JSON)
- GET /nagi/<表情>.png  ナギの絵(static/nagi/・名前は NAGI_MOODS だけ。無ければ idle.png)
- POST /api/action {"action": "toggle_pause"|"pause"|"resume"|"deepdive"|"mode"|"hide"} → on_action(name)
TerminalUI と同じメソッドを持ち、MultiUI で束ねてパイプラインから同じ呼び方で使う。
bind は 127.0.0.1 固定(外へ出さない)。
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _QuietServer(ThreadingHTTPServer):
    """画面(ウィンドウ・ブラウザ)が接続を切っただけの例外は記録しない(2026-09-27: Windows ではウィンドウを閉じるたびに
    ConnectionAbortedError の traceback がアプリのログへ出ていた)。それ以外の例外は従来どおり出す。"""

    def handle_error(self, request, client_address) -> None:
        import sys
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)
from pathlib import Path
from typing import Callable

STATIC = Path(__file__).resolve().parent / "static"
# ナギの絵(2026-09-27 見た目 B: 画面の吹き出しの隣に出す)。場面ごとの表情。決まった名前だけ返す(パスを組み立てない)
NAGI_MOODS = ("idle", "listen", "cue", "think", "done", "alert")
NAGI_RE = re.compile(r"/nagi/([a-z]+)\.png")
# 本体の持ち主(/api/state に載せる)。同じ Mac の別の利用者の本体が同じポートにいても見分けられるように(2026-09-28)
_UID = getattr(os, "getuid", lambda: None)()


class WebUI:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 8765,
                 on_action: Callable[[str], None] | None = None, history: int = 300, page: str = "index.html"):
        self.host, self.port = host, port
        self.page = page   # "/" で返す画面(meetcue run = index.html / meetcue app = app.html)
        self.on_action = on_action
        # meetcue app 用の拡張: 本文付きの操作(start の mode / privacy 等)と、追加の API(一覧・詳細・題名)
        self.on_action_body: Callable[[str, dict], None] | None = None
        self.api: Callable[[str, str, dict | None], tuple[int, object] | None] | None = None
        self.file_route: Callable[[str], tuple[Path, str] | None] | None = None   # GET の path → (ファイル, Content-Type)
        self.state: dict = {"mode": "", "privacy": "", "paused": False, "session": "", "llm": "", "started_ms": int(time.time() * 1000)}
        self._history: deque = deque(maxlen=history)
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._seq = 0

    # ---- サーバ ------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        ui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: D401 — 静かに
                return

            def _json(self, code: int, obj) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html", "/app.html", "/caption.html"):
                    body = (STATIC / (ui.page if self.path == "/" else self.path.lstrip("/"))).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif (m := NAGI_RE.fullmatch(self.path)) and m[1] in NAGI_MOODS:
                    f = STATIC / "nagi" / f"{m[1]}.png"
                    self._file(f if f.is_file() else STATIC / "nagi" / "idle.png", "image/png")   # 届いていない表情は待機中の絵で
                elif self.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                elif self.path == "/api/state":
                    self._json(200, {**ui.state, "uid": _UID})
                elif ui.file_route and (fr := ui.file_route(self.path)) is not None:
                    self._file(*fr)
                elif ui.api and self.path.startswith("/api/") and (r := ui.api("GET", self.path, None)) is not None:
                    self._json(*r)
                elif self.path.startswith("/events"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    q = ui._subscribe()
                    try:
                        self._sse({"type": "state", **ui.state})
                        for ev in list(ui._history):
                            self._sse(ev)
                        while True:
                            try:
                                ev = q.get(timeout=15)
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                                continue
                            self._sse(ev)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        ui._unsubscribe(q)
                else:
                    self._json(404, {"error": "not found"})

            def _file(self, path: Path, ctype: str) -> None:
                """音声などを返す。<audio> のシークに要る Range(bytes=a-b)に 206 で応える。"""
                try:
                    size = path.stat().st_size
                except OSError:
                    return self._json(404, {"error": "not found"})
                start, end = 0, size - 1
                rng = self.headers.get("Range", "")
                if rng.startswith("bytes="):
                    a, _, b = rng[6:].split(",")[0].partition("-")
                    try:
                        if a:
                            start, end = int(a), (int(b) if b else size - 1)
                        elif b:
                            start = max(0, size - int(b))
                    except ValueError:
                        start, end = 0, size - 1
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    with path.open("rb") as f:
                        f.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = f.read(min(65536, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _sse(self, ev: dict) -> None:
                data = json.dumps(ev, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "bad json"})
                if self.path != "/api/action":
                    if ui.api and (r := ui.api("POST", self.path, body)) is not None:
                        return self._json(*r)
                    return self._json(404, {"error": "not found"})
                action = str(body.get("action") or "")
                try:
                    if ui.on_action_body and action:
                        ui.on_action_body(action, body)
                    elif ui.on_action and action:
                        ui.on_action(action)
                except Exception as e:  # noqa: BLE001
                    return self._json(500, {"error": str(e)})
                self._json(200, {"ok": True, "action": action})

        self._server = _QuietServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="meetcue-web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def _subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def _unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _push(self, ev: dict, *, keep: bool = True) -> None:
        self._seq += 1
        ev = {"seq": self._seq, "t": int(time.time() * 1000), **ev}
        if keep:
            self._history.append(ev)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def reset_history(self) -> None:
        """新しいセッションの開始時に、前のセッションの出来事を再接続時に流さないよう消す。"""
        self._history.clear()

    def push(self, ev: dict, *, keep: bool = True) -> None:
        """アプリからの任意の出来事(session_started / session_done など)。keep=False は再接続時に流し直さない
        (ライブ字幕の caption / caption_tr のように、流れ続けて履歴を埋めるもの)。"""
        self._push(ev, keep=keep)

    # ---- 状態 --------------------------------------------------------------
    def set_state(self, **kv) -> None:
        self.state.update(kv)
        self._push({"type": "state", **self.state}, keep=False)

    # ---- TerminalUI と同じイベント API ----------------------------------------
    def partial(self, channel: str, text: str) -> None:
        self._push({"type": "partial", "channel": channel, "text": text}, keep=False)

    def level(self, channel: str, db: float, peak_db: float) -> None:
        """0.1 s ごとの音量(波形表示用・履歴に残さない)。"""
        self._push({"type": "level", "channel": channel, "db": db, "peak_db": peak_db}, keep=False)

    # code = 画面が文言を差し替えるための目印(ナギの台詞など)。text はそのまま残す(code を知らない画面・記録用)
    def status(self, msg: str, code: str | None = None) -> None:
        self._push({"type": "status", "text": msg} | ({"code": code} if code else {}))

    def error(self, msg: str, code: str | None = None) -> None:
        self._push({"type": "error", "text": msg} | ({"code": code} if code else {}))

    def utterance(self, channel: str, text: str, rid: str) -> None:
        self._push({"type": "utterance", "rid": rid, "channel": channel, "text": text})

    def judgment(self, rid: str, s: dict, trigger: bool, ms: float, source: str) -> None:
        self._push({"type": "judgment", "rid": rid, "trigger": trigger, "ms": ms, "source": source,
                    "speech_act": s.get("speech_act"), "speech_act_p": s.get("speech_act_p"),
                    "to_me": s.get("to_me"), "intent": s.get("intent"), "answer_length": s.get("answer_length")})

    def knowledge(self, rid: str, hits: list, terms: list[str], ms: float) -> None:
        self._push({"type": "knowledge", "rid": rid, "ms": ms, "terms": terms,
                    "items": [{"path": h.path, "heading": h.heading, "snippet": h.snippet(terms, 160)} for h in hits]})

    def cue_line(self, rid: str, line: str) -> None:
        self._push({"type": "cue_line", "rid": rid, "line": line})

    def ranking(self, rid: str, kind_ja: str, ranked: list, display: int, ms: float, partial: bool = False) -> None:
        self._push({"type": "ranking", "rid": rid, "kind": "answer" if kind_ja == "回答" else "counter",
                    "display": display, "ms": ms, "partial": partial,
                    "items": [{"index": c.index, "total": c.total, "scores": c.scores, "error": c.error} for c in ranked]})

    # ---- 質問タブ(U3) ------------------------------------------------------------
    def plan_start(self, pid: str, trigger: str, model: str) -> None:
        self._push({"type": "plan_start", "pid": pid, "trigger": trigger, "model": model})

    def plan_line(self, pid: str, line: str) -> None:
        self._push({"type": "plan_line", "pid": pid, "line": line})

    def plan_ranking(self, pid: str, items: list, display: int, ms: float) -> None:
        self._push({"type": "plan_ranking", "pid": pid, "items": items, "display": display, "ms": ms})

    def plan_done(self, pid: str, ok: bool, error: str | None, ms_first: float | None, ms_total: float,
                  cost: float | None) -> None:
        self._push({"type": "plan_done", "pid": pid, "ok": ok, "error": error, "ms_first": ms_first,
                    "ms_total": ms_total, "cost": cost})

    def cue_done(self, rid: str, ok: bool, ms_first: float | None, ms_total: float, cost: float | None,
                 error: str | None = None) -> None:
        self._push({"type": "cue_done", "rid": rid, "ok": ok, "ms_first": ms_first, "ms_total": ms_total,
                    "cost": cost, "error": error})


class MultiUI:
    """複数の UI に同じイベントを配る(ターミナル + Web)。"""

    def __init__(self, *uis):
        self.uis = [u for u in uis if u is not None]

    def __getattr__(self, name: str):
        def fan(*a, **kw):
            for u in self.uis:
                fn = getattr(u, name, None)
                if fn:
                    fn(*a, **kw)
        return fan

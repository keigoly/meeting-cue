"""MCP クライアント(FR-5b・2026-10-08 keigoly様)— Obsidian の MCP サーバーにつなぎ、ノートを検索する。stdlib のみ。

- JSON-RPC 2.0。stdio = サーバーを子プロセスで起動し 1 行 1 JSON / Streamable HTTP = POST 1 回ごとに JSON か SSE で返る。
- 手順: initialize(版 2025-06-18 を名乗り、サーバーの返す版に合わせる)→ notifications/initialized → tools/list → tools/call。
  サーバーからの要求(roots/list など)には「未対応」(-32601)を返して止めない。通知は読み捨てる。
- 既定の型は mcp-obsidian(Obsidian の Local REST API プラグイン + `uvx mcp-obsidian`)。検索の道具は名前と入力の形から選ぶ
  (`obsidian_simple_search` → … → 名前に search を含み文字列の query を取るもの)。
- VaultSearch はつなぎっぱなしにし、落ちていたら次の検索で起動し直す。1 回の検索は呼び出し側が打ち切る(pipeline の mcp_timeout_s)。
- 鍵(Local REST API のキー)は環境変数か HTTP の Authorization でだけ渡し、ログ・画面・例外の文に出さない。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .index import Hit

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "meeting-cue", "version": "1"}
# 接続確認の試しの検索に使う、どのノートにも当たらない語。Obsidian の簡易検索は全ノートを走査するので当たらない語でも
# 1 回 2.4〜3.5 s かかり(2026-10-08・4,816 ノート / 293 MB)、ほぼ全ノートに当たる語("a")は HTTP 500 になった
PROBE = "meetcue-probe-zqx"
PREFERRED_TOOLS = ("obsidian_simple_search", "search_vault_simple", "obsidian_global_search", "simple_search",
                   "search_notes", "search_vault", "search")


class MCPError(Exception):
    pass


class _Stdio:
    def __init__(self, argv: list[str], env: dict | None = None):
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, **kw)
        self._pending: dict[int, dict] = {}
        self._wlock = threading.Lock()
        # 読み書きに失敗したら壊れた印を付ける。Windows ではサーバーが終わった直後も poll() が None を返す間があり、
        # 次の検索で同じ接続を使い回して起動し直さなかった(2026-10-08 Windows の CI で見つけた)
        self.broken = False
        threading.Thread(target=self._read, name="mcp-read", daemon=True).start()

    def _read(self) -> None:
        assert self.p.stdout
        for line in self.p.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue   # サーバーのログ行など
            if not isinstance(msg, dict):
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                slot = self._pending.get(msg["id"])
                if slot:
                    slot["res"] = msg
                    slot["ev"].set()
            elif "id" in msg and "method" in msg:   # サーバーからの要求には未対応を返す
                try:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
                except MCPError:
                    pass
        self.broken = True   # stdout が閉じた = サーバーが終わった
        for slot in list(self._pending.values()):   # 終わったら待っている要求を起こす
            slot["ev"].set()

    def _send(self, obj: dict) -> None:
        with self._wlock:
            try:
                assert self.p.stdin
                self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.p.stdin.flush()
            except (OSError, ValueError) as e:
                self.broken = True
                raise MCPError("MCP サーバーが終了しています") from e

    def request(self, msg_id: int, method: str, params: dict, timeout: float) -> dict:
        slot: dict = {"ev": threading.Event(), "res": None}
        self._pending[msg_id] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            if not slot["ev"].wait(timeout):
                raise MCPError(f"{method}: {timeout:g} s で応答がありません")
            if slot["res"] is None:
                raise MCPError("MCP サーバーが終了しました(起動の設定・キーを確かめてください)")
            return slot["res"]
        finally:
            self._pending.pop(msg_id, None)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def alive(self) -> bool:
        return not self.broken and self.p.poll() is None

    def close(self) -> None:
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


class _Http:
    """Streamable HTTP: POST ごとに application/json か text/event-stream で返る。Mcp-Session-Id を引き継ぐ。"""

    def __init__(self, url: str, headers: dict | None = None):
        self.url, self.headers, self.session_id, self.version = url, dict(headers or {}), None, None

    def _post(self, obj: dict, timeout: float) -> dict | None:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.version:
            h["MCP-Protocol-Version"] = self.version
        req = urllib.request.Request(self.url, data=json.dumps(obj, ensure_ascii=False).encode("utf-8"), headers=h,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in ctype:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            msg = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if isinstance(msg, dict) and msg.get("id") == obj.get("id") and ("result" in msg or "error" in msg):
                            return msg
                    return None
                body = r.read()
                return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as e:
            raise MCPError(f"HTTP {e.code}" + ("(キーを確かめてください)" if e.code in (401, 403) else "")) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise MCPError(f"接続できませんでした({type(e).__name__})") from None

    def request(self, msg_id: int, method: str, params: dict, timeout: float) -> dict:
        res = self._post({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}, timeout)
        if not isinstance(res, dict):
            raise MCPError(f"{method}: 応答がありません")
        return res

    def notify(self, method: str, params: dict | None = None) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 10)
        except MCPError:
            pass

    def alive(self) -> bool:
        return True

    def close(self) -> None:
        pass


class MCPClient:
    def __init__(self, *, command: list[str] | None = None, url: str | None = None, env: dict | None = None,
                 headers: dict | None = None, timeout: float = 20.0):
        if not command and not url:
            raise MCPError("MCP サーバーのコマンドか URL を設定してください")
        self.command, self.url, self.env, self.headers, self.timeout = command, url, env, headers, timeout
        self._t: _Stdio | _Http | None = None
        self._id = 0
        self.server: dict = {}
        self.protocol = ""

    def start(self) -> "MCPClient":
        if self.command:
            try:
                self._t = _Stdio(list(self.command), self.env)
            except OSError as e:
                raise MCPError(f"起動できませんでした: {Path(self.command[0]).name}({type(e).__name__})") from None
        else:
            self._t = _Http(str(self.url), self.headers)
        res = self.call("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
                        timeout=self.timeout)
        self.server = res.get("serverInfo") or {}
        self.protocol = str(res.get("protocolVersion") or PROTOCOL_VERSION)
        if isinstance(self._t, _Http):
            self._t.version = self.protocol
        self._t.notify("notifications/initialized")
        return self

    def call(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        if self._t is None:
            raise MCPError("つながっていません")
        self._id += 1
        msg = self._t.request(self._id, method, params or {}, timeout or self.timeout)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise MCPError(f"{method}: {str(err.get('message') or msg['error'])[:200]}")
        return msg.get("result") or {}

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(5):   # 頁送り(nextCursor)
            res = self.call("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in res.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None) -> dict:
        res = self.call("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if res.get("isError"):
            raise MCPError(f"{name}: " + " ".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict))[:200])
        return res

    def alive(self) -> bool:
        return bool(self._t and self._t.alive())

    def close(self) -> None:
        if self._t:
            self._t.close()
        self._t = None


def pick_search_tool(tools: list[dict], prefer: str = "") -> tuple[str, str, dict] | None:
    """(道具の名前, 検索語を入れる引数の名前, 足す引数)。検索に使える道具が無ければ None。"""
    by_name = {t["name"]: t for t in tools}

    def query_arg(t: dict) -> str | None:
        props = ((t.get("inputSchema") or {}).get("properties") or {})
        if "query" in props:
            return "query"
        req = (t.get("inputSchema") or {}).get("required") or []
        return next((k for k in req if (props.get(k) or {}).get("type") == "string"), None)

    order = ([prefer] if prefer else []) + list(PREFERRED_TOOLS) + [n for n in by_name if "search" in n.lower()]
    for name in order:
        t = by_name.get(name)
        q = query_arg(t) if t else None
        if t and q:
            props = (t.get("inputSchema") or {}).get("properties") or {}
            extra = {"context_length": 300} if "context_length" in props else {}
            return name, q, extra
    return None


def parse_hits(result: dict, limit: int = 10) -> list[Hit]:
    """tools/call の結果 → [Hit(source="mcp")]。mcp-obsidian の [{filename, matches:[{context}]}] と、
    よくある {results:[{path, snippet}]} の形を読む。形が分からなければ本文を 1 件として返す。"""
    data = result.get("structuredContent")
    texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    if data is None:
        try:
            data = json.loads("\n".join(texts))
        except ValueError:
            data = None
    if isinstance(data, dict):
        data = next((data[k] for k in ("results", "matches", "files", "items", "notes") if isinstance(data.get(k), list)), data)
    hits: list[Hit] = []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, str):
                if it.endswith(".md"):
                    hits.append(Hit(it, "", "", 0.0, "mcp"))
                continue
            if not isinstance(it, dict):
                continue
            path = next((it[k] for k in ("filename", "path", "file", "filePath", "name") if isinstance(it.get(k), str)), "")
            if not path:
                continue
            ms = it.get("matches")
            if isinstance(ms, list):
                body = " … ".join(str(m.get("context") or m.get("text") or "") if isinstance(m, dict) else str(m)
                                  for m in ms[:3]).strip()
            else:
                body = str(next((it[k] for k in ("context", "snippet", "content", "text", "excerpt") if it.get(k)), ""))
            hits.append(Hit(path, "", body[:1200], 0.0, "mcp"))
            if len(hits) >= limit:
                break
        return hits
    text = "\n".join(texts).strip()
    return [Hit("", "", text[:1200], 0.0, "mcp")] if text else []


def friendly(err: str) -> str:
    """MCP サーバーの先(Obsidian の Local REST API)の失敗を、利用者が直せる言葉にする。"""
    e = err.lower()
    if any(x in e for x in ("connection refused", "newconnectionerror", "max retries", "failed to establish")):
        return ("MCP サーバーにはつながりましたが、Obsidian(Local REST API)に届きません。Obsidian を起動し、"
                "Local REST API プラグインが有効か・ポートが合っているかを確かめてください")
    if any(x in e for x in ("401", "403", "unauthorized", "forbidden")):
        return "Obsidian の Local REST API がキーを受け付けません。プラグインの設定画面のキーを貼り直してください"
    return err


def find_uvx() -> str | None:
    """uvx の実行ファイル(PATH → ~/.local/bin → Homebrew)。アプリから起動すると PATH が短いので場所も探す。"""
    exe = "uvx.exe" if sys.platform == "win32" else "uvx"
    for c in (shutil.which("uvx"), str(Path.home() / ".local" / "bin" / exe), "/opt/homebrew/bin/uvx", "/usr/local/bin/uvx"):
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return c
    return None


def build_spec(settings: dict, api_key: str | None, *, uvx: str | None = None) -> dict:
    """設定(ui.json)→ {command | url, env, headers, kind}。足りないものがあれば {"error": …}。キーは中に入るが外へ出さない。"""
    kind = settings.get("mcp_kind") or "mcp-obsidian"
    if kind == "mcp-obsidian":
        exe = uvx or find_uvx()
        if not exe:
            return {"kind": kind, "error": "uv(uvx)が見つかりません。uv を入れてください"}
        if not api_key:
            return {"kind": kind, "error": "Local REST API のキーが登録されていません"}
        env = {**os.environ, "OBSIDIAN_API_KEY": api_key, "OBSIDIAN_HOST": str(settings.get("mcp_host") or "127.0.0.1"),
               "OBSIDIAN_PORT": str(int(settings.get("mcp_port") or 27124))}
        return {"kind": kind, "command": [exe, "mcp-obsidian"], "env": env}
    if kind == "stdio":
        import shlex
        try:
            cmd = shlex.split(str(settings.get("mcp_command") or ""), posix=sys.platform != "win32")
        except ValueError:
            cmd = []
        cmd = [c[1:-1] if len(c) > 1 and c[0] == c[-1] == '"' else c for c in cmd]   # Windows の書き方("C:\…")も
        if not cmd:
            return {"kind": kind, "error": "MCP サーバーを起動するコマンドを入れてください"}
        env = {**os.environ, **({"OBSIDIAN_API_KEY": api_key} if api_key else {})}
        return {"kind": kind, "command": cmd, "env": env}
    url = str(settings.get("mcp_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"kind": kind, "error": "MCP サーバーの URL(http:// か https://)を入れてください"}
    return {"kind": "http", "url": url, "headers": {"Authorization": f"Bearer {api_key}"} if api_key else {}}


def is_local(spec: dict) -> bool:
    """LOCAL(FR-11)の録音中に使ってよいか: 子プロセス(stdio)か、この PC の URL だけ。"""
    if spec.get("command"):
        return True
    host = (urlparse(str(spec.get("url") or "")).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


class VaultSearch:
    """Obsidian の MCP サーバーでノートを探す(つなぎっぱなし・落ちたら次の検索で起動し直す)。スレッドから呼ぶ。"""

    def __init__(self, spec: dict, *, prefer_tool: str = "", factory=MCPClient):
        self.spec, self.prefer_tool, self.factory = spec, prefer_tool, factory
        self._client: MCPClient | None = None
        self._tool: tuple[str, str, dict] | None = None
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def _ensure(self) -> MCPClient:
        if self.spec.get("error"):
            raise MCPError(self.spec["error"])
        if self._client and self._client.alive():
            return self._client
        if self._client:
            self._client.close()
        self._client = None
        c = self.factory(command=self.spec.get("command"), url=self.spec.get("url"), env=self.spec.get("env"),
                         headers=self.spec.get("headers")).start()
        try:
            tools = c.list_tools()
            self._tool = pick_search_tool(tools, self.prefer_tool)
            if not self._tool:
                raise MCPError(f"検索に使える道具がありません(道具 {len(tools)} 個)")
        except MCPError:
            c.close()
            raise
        self._client = c
        self._tools_n = len(tools)
        return c

    def search(self, terms: list[str], k: int = 5, *, timeout: float = 10.0) -> list[Hit]:
        """語ごとに検索して和を取る(Obsidian の簡易検索は長い文だと当たらないため、語で引く)。パスで重複を除く。"""
        with self._lock:
            try:
                c = self._ensure()
                name, qarg, extra = self._tool   # type: ignore[misc]
                out: list[Hit] = []
                seen: set[str] = set()
                for t in [x for x in terms if x][:4]:
                    for h in parse_hits(c.call_tool(name, {qarg: t, **extra}, timeout=timeout), limit=k):
                        if h.path not in seen:
                            seen.add(h.path)
                            out.append(h)
                    if len(out) >= k:
                        break
                self.last_error = None
                return out[:k]
            except MCPError as e:
                self.last_error = str(e)
                if self._client and not self._client.alive():
                    self._client.close()
                    self._client = None
                raise

    def warm(self) -> None:
        """録音の開始で先にサーバーを起動しておく(uvx の起動を最初の検索に乗せない)。検索はしない。"""
        with self._lock:
            self._ensure()

    def check(self, probe: str = PROBE) -> dict:
        """設定画面の「接続を確認」: 起動 → initialize → tools/list → 検索の道具 → 試しの検索(Obsidian まで届くか)。
        キーは返さない。connected = MCP サーバーまではつながった。"""
        t0 = time.monotonic()
        with self._lock:
            info: dict = {"connected": False}
            try:
                if self._client:
                    self._client.close()
                    self._client = None
                c = self._ensure()
                name, qarg, extra = self._tool   # type: ignore[misc]
                info = {"connected": True, "server": c.server.get("name") or "", "version": c.server.get("version") or "",
                        "protocol": c.protocol, "tools": self._tools_n, "search_tool": name}
                n = len(parse_hits(c.call_tool(name, {qarg: probe, **extra}, timeout=10), limit=50)) if probe else None
                return {"ok": True, "ms": round((time.monotonic() - t0) * 1000), **info, "probe_hits": n,
                        "message": f"{info['server'] or 'MCP サーバー'} につながりました(道具 {self._tools_n} 個・検索 = {name})"}
            except MCPError as e:
                self.last_error = str(e)
                return {"ok": False, "ms": round((time.monotonic() - t0) * 1000), **info, "message": friendly(str(e))}

    def close(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
            self._client = None

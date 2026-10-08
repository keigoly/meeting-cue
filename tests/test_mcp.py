"""Obsidian の MCP クライアント(FR-5b・2026-10-08): stdio と Streamable HTTP・検索の道具の選び方・結果の読み方・起動し直し。

本物の Obsidian・キーには触れない(tests/fixtures/fake_mcp_server.py と、この中の小さな HTTP サーバー)。
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from meetcue.knowledge import mcp

FAKE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")]


def test_stdio_search_picks_tool_and_merges_terms():
    vs = mcp.VaultSearch({"command": FAKE})
    try:
        c = vs.check()
        assert c["ok"] and c["server"] == "fake-obsidian" and c["tools"] == 2 and c["search_tool"] == "obsidian_simple_search"
        assert c["connected"] and c["probe_hits"] == 2                                                            # 試しの検索まで
        bad = vs.check(probe="boom")                                                                              # Obsidian に届かない例
        assert not bad["ok"] and bad["connected"] and "search failed" in bad["message"]
        assert "Local REST API" in mcp.friendly("Error: HTTPSConnectionPool(...) NewConnectionError: Connection refused")
        hits = vs.search(["SASE", "ZTNA"], k=5)
        assert [h.path for h in hits] == ["03_Resources/SASE.md", "01_Projects/共通.md", "03_Resources/ZTNA.md"]   # 重複は 1 回
        assert hits[0].source == "mcp" and hits[0].body == "SASE の説明(ctx=300)"                                  # context_length を足す
        assert vs.search(["SASE", "ZTNA"], k=1)[0].path == "03_Resources/SASE.md"
        with pytest.raises(mcp.MCPError, match="search failed"):
            vs.search(["boom"])
        assert vs.search(["SASE"], k=2) and vs.last_error is None                                                  # 道具の失敗では止まらない
        vs.close()
        vs.warm()                                                                                                   # 録音の開始で先に起動(検索はしない)
        assert vs._client and vs._client.alive() and vs.search(["SASE"], k=1)[0].path == "03_Resources/SASE.md"
    finally:
        vs.close()


def test_stdio_restarts_after_server_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MCP_EXIT_ONCE", str(tmp_path / "exited"))
    import os
    vs = mcp.VaultSearch({"command": FAKE, "env": dict(os.environ)})
    try:
        assert vs.search(["A"], k=1)[0].path == "03_Resources/A.md"      # 1 回答えて終わる
        try:
            vs.search(["B"], k=1)                                          # 終わりかけなら失敗(呼び出し側は索引だけで続ける)
        except mcp.MCPError:
            pass
        assert vs.search(["C"], k=1)[0].path == "03_Resources/C.md"      # 遅くとも次の検索で起動し直す
        assert (tmp_path / "exited").exists()
    finally:
        vs.close()


def test_broken_pipe_marks_client_dead_even_if_process_looks_alive():
    """Windows: サーバーが終わった直後は poll() が None の間がある。読み書きの失敗で壊れた印を付け、次の検索で起動し直す。"""
    vs = mcp.VaultSearch({"command": FAKE})
    try:
        assert vs.search(["A"], k=1)
        t = vs._client._t
        t.broken = True                                                    # 書き込みに失敗した後と同じ状態(プロセスは生きて見える)
        assert t.p.poll() is None and not vs._client.alive()
        old = t.p
        assert vs.search(["B"], k=1)[0].path == "03_Resources/B.md"      # 使い回さずに起動し直す
        assert vs._client._t.p is not old
        old.wait(timeout=5)                                                # 古いプロセスは片付いている
    finally:
        vs.close()


def test_bad_command_and_spec_errors():
    assert not mcp.VaultSearch({"command": ["/nonexistent/mcp-server"]}).check()["ok"]
    assert "uv" in mcp.VaultSearch({"error": "uv(uvx)が見つかりません"}).check()["message"]
    s = mcp.build_spec({"mcp_kind": "mcp-obsidian", "mcp_port": 27125}, "k" * 64, uvx="/x/uvx")
    assert s["command"] == ["/x/uvx", "mcp-obsidian"] and s["env"]["OBSIDIAN_API_KEY"] == "k" * 64
    assert s["env"]["OBSIDIAN_PORT"] == "27125" and s["env"]["OBSIDIAN_HOST"] == "127.0.0.1" and mcp.is_local(s)
    assert "キー" in mcp.build_spec({"mcp_kind": "mcp-obsidian"}, None, uvx="/x/uvx")["error"]
    assert mcp.build_spec({"mcp_kind": "stdio", "mcp_command": "npx -y obsidian-mcp \"/a b\""}, None)["command"][-1] == "/a b"
    assert "コマンド" in mcp.build_spec({"mcp_kind": "stdio", "mcp_command": ""}, None)["error"]
    h = mcp.build_spec({"mcp_kind": "http", "mcp_url": "http://127.0.0.1:3001/mcp"}, "tok")
    assert h["headers"] == {"Authorization": "Bearer tok"} and mcp.is_local(h)
    assert not mcp.is_local(mcp.build_spec({"mcp_kind": "http", "mcp_url": "https://example.com/mcp"}, None))   # LOCAL では使わない
    assert "URL" in mcp.build_spec({"mcp_kind": "http", "mcp_url": "ftp://x"}, None)["error"]


def test_pick_tool_and_parse_shapes():
    tools = [{"name": "list", "inputSchema": {"properties": {}}},
             {"name": "find_notes_search", "inputSchema": {"properties": {"text": {"type": "string"}}, "required": ["text"]}}]
    assert mcp.pick_search_tool(tools) == ("find_notes_search", "text", {})
    assert mcp.pick_search_tool([{"name": "list"}]) is None
    r = {"content": [{"type": "text", "text": json.dumps({"results": [{"path": "a.md", "snippet": "S"}, {"x": 1}]})}]}
    assert [(h.path, h.body) for h in mcp.parse_hits(r)] == [("a.md", "S")]
    assert mcp.parse_hits({"structuredContent": ["b.md", "c.txt"]})[0].path == "b.md"
    assert mcp.parse_hits({"content": [{"type": "text", "text": "plain text"}]})[0].body == "plain text"
    assert mcp.parse_hits({"content": []}) == []


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen.append((msg.get("method"), self.headers.get("Mcp-Session-Id"), self.headers.get("Authorization"),
                              self.headers.get("MCP-Protocol-Version")))
        if "id" not in msg:
            self.send_response(202)
            self.end_headers()
            return
        if msg["method"] == "initialize":
            res = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "http-obsidian"}}
            body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "sess-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if msg["method"] == "tools/list":
            res = {"tools": [{"name": "search", "inputSchema": {"properties": {"query": {"type": "string"}}}}]}
        else:
            res = {"content": [{"type": "text", "text": json.dumps([{"path": "n/HTTP.md", "content": "via sse"}])}]}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"event: message\n")
        self.wfile.write(("data: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}) + "\n\n").encode())
        self.wfile.write(("data: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n\n").encode())


def test_streamable_http_with_sse_and_session():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
        vs = mcp.VaultSearch(mcp.build_spec({"mcp_kind": "http", "mcp_url": url}, "tok"))
        c = vs.check()
        assert c["ok"] and c["server"] == "http-obsidian" and c["protocol"] == "2025-03-26" and c["search_tool"] == "search"
        hits = vs.search(["x"], k=3)
        assert [(h.path, h.body, h.source) for h in hits] == [("n/HTTP.md", "via sse", "mcp")]
        seen = _Handler.seen
        assert seen[0] == ("initialize", None, "Bearer tok", None)
        assert all(s[1] == "sess-1" and s[3] == "2025-03-26" for s in seen[1:])   # 以後はセッションと版を付ける
    finally:
        srv.shutdown()

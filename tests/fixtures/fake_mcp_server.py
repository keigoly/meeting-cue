"""試験用の MCP サーバー(stdio・1 行 1 JSON)。mcp-obsidian の obsidian_simple_search と同じ形の結果を返す。

- 起動直後に JSON でないログ行を 1 行出す(クライアントは読み捨てる)。
- initialized を受けたら roots/list を 1 回要求する(クライアントは「未対応」を返して止めない)。
- 環境変数 FAKE_MCP_EXIT_ONCE=<印のファイル> なら、印が無いときだけ最初の tools/call に答えてから終わる(起動し直しの試験)。
"""
import json
import os
import sys

calls = 0
print("fake mcp server starting", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method is None:   # クライアントからの応答(roots/list への未対応)
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"got": msg.get("error", {}).get("code")}}),
              flush=True)
        continue
    if method == "initialize":
        res = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake-obsidian", "version": "0.1"}}
    elif method == "notifications/initialized":
        print(json.dumps({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"}), flush=True)
        continue
    elif method == "tools/list":
        res = {"tools": [{"name": "obsidian_list_files_in_vault", "inputSchema": {"type": "object", "properties": {}}},
                         {"name": "obsidian_simple_search", "inputSchema": {
                             "type": "object", "properties": {"query": {"type": "string"}, "context_length": {"type": "integer"}},
                             "required": ["query"]}}]}
    elif method == "tools/call":
        calls += 1
        q = msg["params"]["arguments"]["query"]
        ctx = msg["params"]["arguments"].get("context_length")
        if q == "boom":
            res = {"isError": True, "content": [{"type": "text", "text": "search failed"}]}
        else:
            data = [{"filename": f"03_Resources/{q}.md", "score": -1, "matches": [{"context": f"{q} の説明(ctx={ctx})"}]},
                    {"filename": "01_Projects/共通.md", "score": -2, "matches": [{"context": "共通のノート"}]}]
            res = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
        mark = os.environ.get("FAKE_MCP_EXIT_ONCE")
        if mark and not os.path.exists(mark):
            open(mark, "w").close()
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}, ensure_ascii=False), flush=True)
            sys.exit(0)
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}, ensure_ascii=False), flush=True)

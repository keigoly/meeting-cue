"""生成 AI の直接接続(設定画面 第 2 段・2026-09-26): Anthropic / OpenAI の SSE を手元の偽サーバで流して確かめる。

本物の API・キーには触れない。役割ごとの思考 / effort の渡し方、料金の計算、失敗の扱い、モデル一覧の絞り込みも見る。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from meetcue.config import Config
from meetcue.cues import anthropic_direct, llm, openai_direct, openrouter


class FakeAPI:
    """path → [(status, content_type, body)] を順に返す。受けた要求(ヘッダ・本文)を残す。"""

    def __init__(self):
        self.routes: dict[str, list] = {}
        self.seen: list[dict] = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else None
                api.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, ctype, payload = api.routes[self.path.split("?")[0]].pop(0)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(payload.encode("utf-8"))

            do_GET = do_POST = _reply

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def add(self, path, status, body, ctype="text/event-stream"):
        self.routes.setdefault(path, []).append((status, ctype, body))


@pytest.fixture
def api():
    a = FakeAPI()
    yield a
    a.srv.shutdown()


def sse(*events):
    out = []
    for e in events:
        if isinstance(e, str):
            out.append(f"data: {e}\n\n")
        else:
            out.append(f"event: {e.get('type', 'x')}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n")
    return "".join(out)


# ---- Anthropic -----------------------------------------------------------------------------------
def test_anthropic_stream_text_usage_and_headers(api):
    api.add("/v1/messages", 200, sse(
        {"type": "message_start", "message": {"id": "msg_1", "model": "claude-sonnet-5", "usage": {"input_tokens": 120}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "内緒"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "意図: 費用\n"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "回答1: 約 100 万円"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40}},
        {"type": "message_stop"}))
    got = []
    msgs = [{"role": "system", "content": "あなたは参謀"}, {"role": "user", "content": "費用は?"}]
    r = anthropic_direct.stream_chat(msgs, api_key="sk-ant-test", model="claude-sonnet-5", url=api.base + "/v1/messages",
                                     thinking={"type": "disabled"}, on_delta=got.append)
    assert r.ok and r.text == "意図: 費用\n回答1: 約 100 万円" and got == ["意図: 費用\n", "回答1: 約 100 万円"]
    assert (r.input_tokens, r.output_tokens, r.model, r.provider) == (120, 40, "claude-sonnet-5", "Anthropic")
    assert r.ms_first_token is not None and r.cost_usd is None                 # 料金は llm.stream が計算する
    req = api.seen[0]
    assert req["headers"]["x-api-key"] == "sk-ant-test" and req["headers"]["anthropic-version"] == "2023-06-01"
    b = req["body"]
    assert b["system"] == "あなたは参謀" and b["messages"] == [{"role": "user", "content": "費用は?"}]
    assert b["stream"] is True and b["thinking"] == {"type": "disabled"} and "temperature" not in b


def test_anthropic_refusal_error_event_and_http_error(api):
    api.add("/v1/messages", 200, sse(
        {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 5}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "途中"}},
        {"type": "message_delta", "delta": {"stop_reason": "refusal"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"}))
    api.add("/v1/messages", 200, sse(
        {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 5}}},
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
    api.add("/v1/messages", 401, json.dumps({"type": "error", "error": {"type": "authentication_error"}}), "application/json")
    url = api.base + "/v1/messages"
    r1 = anthropic_direct.stream_chat([{"role": "user", "content": "x"}], api_key="k", model="m", url=url)
    assert not r1.ok and r1.error == "refusal"
    r2 = anthropic_direct.stream_chat([{"role": "user", "content": "x"}], api_key="k", model="m", url=url)
    assert not r2.ok and r2.error == "http_error" and "overloaded" in r2.detail
    r3 = anthropic_direct.stream_chat([{"role": "user", "content": "x"}], api_key="k", model="m", url=url)
    assert not r3.ok and r3.rc == 401 and r3.error == "http_error"
    assert anthropic_direct.stream_chat([], api_key=None, model="m").error == "no_key"


# ---- OpenAI --------------------------------------------------------------------------------------
def test_openai_stream_usage_and_body(api):
    api.add("/v1/chat/completions", 200, sse(
        json.dumps({"model": "gpt-x", "choices": [{"delta": {"role": "assistant"}}]}),
        json.dumps({"choices": [{"delta": {"content": "質問1: 期限は"}}]}),
        json.dumps({"choices": [{"delta": {"content": "いつですか"}, "finish_reason": "stop"}]}),
        json.dumps({"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 12}}),
        "[DONE]"))
    r = openai_direct.stream_chat([{"role": "user", "content": "x"}], api_key="sk-oa", model="gpt-x",
                                  url=api.base + "/v1/chat/completions", max_tokens=500)
    assert r.ok and r.text == "質問1: 期限はいつですか" and (r.input_tokens, r.output_tokens) == (30, 12)
    b = api.seen[0]["body"]
    assert api.seen[0]["headers"]["authorization"] == "Bearer sk-oa"
    assert b["stream_options"] == {"include_usage": True} and b["max_completion_tokens"] == 500
    assert not ({"provider", "reasoning", "usage", "reasoning_effort"} & set(b))   # OpenRouter 独自の欄は送らない


def test_openai_reasoning_effort_retry_and_no_model(api):
    api.add("/v1/chat/completions", 400, json.dumps({"error": {"message": "Unsupported parameter: 'reasoning_effort'"}}),
            "application/json")
    api.add("/v1/chat/completions", 200, sse(json.dumps({"choices": [{"delta": {"content": "深い答え"}}]}), "[DONE]"))
    r = openai_direct.stream_chat([{"role": "user", "content": "x"}], api_key="k", model="gpt-x",
                                  url=api.base + "/v1/chat/completions", reasoning_effort="medium")
    assert r.ok and r.text == "深い答え" and r.extra.get("reasoning_dropped") is True
    assert api.seen[0]["body"]["reasoning_effort"] == "medium" and "reasoning_effort" not in api.seen[1]["body"]
    assert openai_direct.stream_chat([], api_key="k", model="").error == "no_model"


def test_openai_list_models_filters_text_models(api):
    ids = ["gpt-x", "gpt-x-mini", "o9", "o9-pro", "chatgpt-x", "gpt-x-audio-preview", "gpt-image-1", "text-embedding-3",
           "whisper-1", "tts-1", "gpt-x-realtime", "dall-e-3", "omni-moderation-latest", "gpt-x-search-preview"]
    api.add("/v1/models", 200, json.dumps({"data": [{"id": i} for i in ids]}), "application/json")
    assert openai_direct.list_models("k", url=api.base + "/v1/models") == ["o9-pro", "o9", "gpt-x-mini", "gpt-x", "chatgpt-x"]


# ---- 役割ごとの渡し方と料金(llm.stream) ------------------------------------------------------------
def test_llm_dispatch_roles_and_cost(monkeypatch):
    cfg = Config()
    seen = []

    def fake_anthropic(messages, **kw):
        seen.append(("anthropic", kw["model"], kw["thinking"], kw["effort"], kw["max_tokens"]))
        return openrouter.StreamResult(True, text="ok", model=kw["model"], input_tokens=1_000_000, output_tokens=100_000)

    def fake_openai(messages, **kw):
        seen.append(("openai", kw["model"], kw["reasoning_effort"], kw["max_tokens"]))
        return openrouter.StreamResult(True, text="ok", model=kw["model"], input_tokens=2_000_000, output_tokens=0)

    def fake_or(messages, **kw):
        seen.append(("openrouter", kw["model"], kw.get("reasoning")))
        return openrouter.StreamResult(True, text="ok", model=kw["model"], cost_usd=0.0123)

    monkeypatch.setattr(anthropic_direct, "stream_chat", fake_anthropic)
    monkeypatch.setattr(openai_direct, "stream_chat", fake_openai)
    monkeypatch.setattr(openrouter, "stream_chat", fake_or)

    ta = llm.target_for(cfg, "anthropic", "k", {})
    assert (ta.model, ta.fast_model, ta.deep_model) == ("claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5-5")
    r = llm.stream(ta, [], role="main")
    assert r.cost_usd == pytest.approx(2.0 + 1.0)                             # Sonnet 5: $2/M 入力 + $10/M 出力 × 0.1M
    llm.stream(ta, [], role="fast")
    llm.stream(ta, [], role="deep", effort="medium", max_tokens=4000)
    assert seen[:3] == [("anthropic", "claude-sonnet-5", {"type": "disabled"}, None, 1200),   # 回答: 思考を切る
                        ("anthropic", "claude-haiku-4-5", None, None, 1200),                  # サマリ: 指定しない
                        ("anthropic", "claude-opus-5-5", None, "medium", 4000)]               # 深く: effort だけ

    to = llm.target_for(cfg, "openai", "k", {"openai_model": "gpt-x", "openai_price_in": 1.5, "openai_price_out": 6})
    r = llm.stream(to, [], role="main", max_tokens=700)
    assert r.cost_usd == pytest.approx(3.0) and seen[3] == ("openai", "gpt-x", None, 4000)   # 推論の分の余裕
    llm.stream(to, [], role="deep", effort="high")
    assert seen[4] == ("openai", "gpt-x", "high", 4000)
    free = llm.target_for(cfg, "openai", "k", {"openai_model": "gpt-x"})                       # 価格が未入力
    assert llm.stream(free, [], role="main").cost_usd is None

    tr = llm.target_for(cfg, "openrouter", "k", {})
    assert llm.stream(tr, [], role="main").cost_usd == 0.0123                                 # OpenRouter は実額のまま
    llm.stream(tr, [], role="deep", effort="medium")
    assert seen[-1] == ("openrouter", cfg.planner.deep_model, {"effort": "medium"})


def test_estimate_cost_prefix_match():
    t = llm.Target("anthropic", "k", "claude-sonnet-5", prices={"claude-sonnet-5": [2.0, 10.0]})
    assert llm.estimate_cost(t, "claude-sonnet-5-20260601", 500_000, 0) == pytest.approx(1.0)   # 日付付きの名前でも
    assert llm.estimate_cost(t, "other", 1, 1) is None and llm.estimate_cost(t, "claude-sonnet-5", None, None) is None

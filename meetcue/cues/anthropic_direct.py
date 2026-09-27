"""Anthropic Messages API のストリーミングクライアント(直接接続・設定画面 第 2 段・2026-09-26)— stdlib のみ。

公開仕様どおりの HTTP + SSE(POST /v1/messages・x-api-key・anthropic-version: 2023-06-01・"stream": true)。
イベント: message_start(usage.input_tokens)→ content_block_delta(delta.type = text_delta の text だけを使う。
thinking の差分は捨てる)→ message_delta(usage.output_tokens・stop_reason)→ message_stop。途中の error イベントも見る。
- 思考: 回答・質問タブ(Sonnet 5)は {"type": "disabled"} で初トークンを速く。深く考える(Opus 5.5)は思考を切れない
  ので thinking を送らず output_config.effort で量を決める。Sonnet 5 / Opus 5.5 は temperature を受け付けないので送らない。
- 料金は応答に含まれない(呼び出し側 llm.stream が価格表から計算する)。
- raise しない: 失敗は openrouter.StreamResult(ok=False, error=…) で返す(OpenRouter と同じ形)。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from .openrouter import StreamResult, _int, _ms, _sse_lines

DEFAULT_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI 形式(system を messages に含む)→ Anthropic 形式(system は別の欄)。"""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    rest = [{"role": m["role"], "content": m.get("content") or ""} for m in messages if m.get("role") in ("user", "assistant")]
    return system, rest


def stream_chat(
    messages: list[dict],
    *,
    api_key: str | None,
    model: str,
    url: str = DEFAULT_URL,
    timeout: int = 60,
    max_tokens: int = 1200,
    thinking: dict | None = None,
    effort: str | None = None,
    on_delta: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> StreamResult:
    if not api_key:
        return StreamResult(False, error="no_key", model=model)
    system, msgs = _split_system(messages)
    body: dict = {"model": model, "max_tokens": max_tokens, "stream": True, "messages": msgs}
    if system:
        body["system"] = system
    if thinking is not None:
        body["thinking"] = thinking
    if effort:
        body["output_config"] = {"effort": effort}
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": API_VERSION,
                 "Accept": "text/event-stream"},
        method="POST",
    )
    t0 = time.perf_counter()
    res = StreamResult(False, model=model, provider="Anthropic")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        return StreamResult(False, rc=e.code, error="http_error", model=model, provider="Anthropic",
                            ms_total=_ms(t0), detail=detail)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return StreamResult(False, error="connect_error", model=model, provider="Anthropic", ms_total=_ms(t0),
                            detail=str(e)[:200])
    res.rc = getattr(resp, "status", 200)
    parts: list[str] = []
    stop_reason = None
    try:
        with resp:
            for line in _sse_lines(resp):
                if should_stop and should_stop():
                    res.error = "cancelled"
                    break
                if not line.startswith("data:"):
                    continue   # "event: …" 行と空行は data の type で足りる
                try:
                    obj = json.loads(line[5:].strip())
                except ValueError:
                    continue
                typ = obj.get("type")
                if typ == "message_start":
                    msg = obj.get("message") or {}
                    if isinstance(msg.get("model"), str):
                        res.model = msg["model"]
                    res.input_tokens = _int((msg.get("usage") or {}).get("input_tokens"))
                elif typ == "content_block_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        if res.ms_first_token is None:
                            res.ms_first_token = _ms(t0)
                        parts.append(delta["text"])
                        res.chunks += 1
                        if on_delta:
                            on_delta(delta["text"])
                elif typ == "message_delta":
                    stop_reason = (obj.get("delta") or {}).get("stop_reason") or stop_reason
                    out = _int((obj.get("usage") or {}).get("output_tokens"))
                    if out is not None:
                        res.output_tokens = out
                elif typ == "error":
                    res.error = "http_error"
                    res.detail = str(obj.get("error"))[:300]
                    break
                elif typ == "message_stop":
                    break
    except (TimeoutError, OSError) as e:
        res.error = res.error or "connect_error"
        res.detail = res.detail or str(e)[:200]
    res.ms_total = _ms(t0)
    res.text = "".join(parts).strip()
    res.extra["stop_reason"] = stop_reason
    if not res.error and stop_reason == "refusal":   # 安全上の理由で断られた(本文は使わない)
        res.error, res.detail = "refusal", "stop_reason=refusal"
    if res.error:
        res.ok = False
        return res
    if not res.text:
        res.ok, res.error = False, "empty_output"
        return res
    res.ok = True
    return res

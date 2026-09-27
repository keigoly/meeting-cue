"""OpenAI Chat Completions のストリーミングクライアント(直接接続・設定画面 第 2 段・2026-09-26)— stdlib のみ。

POST /v1/chat/completions(Bearer)・"stream": true・stream_options.include_usage で最後に usage を受け取る。
SSE は "data: {…}"(choices[].delta.content)→ usage の塊 → "data: [DONE]"(OpenRouter と同じ形だが、OpenRouter 独自の
provider / reasoning / usage.include は送らない)。
- モデルは利用者が設定画面でキーのモデル一覧から選ぶ(ここでは決め打ちしない)。
- 上限は max_completion_tokens(推論するモデルは推論の分もここに入る)。深く考えるでは reasoning_effort を送り、
  そのモデルが受け付けない(400 で reasoning_effort に触れる)ときは 1 回だけ外して送り直す。
- 料金は応答に含まれない(呼び出し側 llm.stream が設定画面の価格から計算する)。raise しない。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from .openrouter import StreamResult, _int, _ms, _sse_lines

DEFAULT_URL = "https://api.openai.com/v1/chat/completions"


def stream_chat(
    messages: list[dict],
    *,
    api_key: str | None,
    model: str,
    url: str = DEFAULT_URL,
    timeout: int = 60,
    max_tokens: int = 1200,
    reasoning_effort: str | None = None,
    on_delta: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> StreamResult:
    if not api_key:
        return StreamResult(False, error="no_key", model=model)
    if not model:
        return StreamResult(False, error="no_model", model=model, detail="OpenAI のモデルが選ばれていません")
    r = _once(messages, api_key, model, url, timeout, max_tokens, reasoning_effort, on_delta, should_stop)
    if reasoning_effort and r.rc == 400 and "reasoning" in (r.detail or ""):
        r2 = _once(messages, api_key, model, url, timeout, max_tokens, None, on_delta, should_stop)
        r2.extra["reasoning_dropped"] = True
        return r2
    return r


def _once(messages, api_key, model, url, timeout, max_tokens, reasoning_effort, on_delta, should_stop) -> StreamResult:
    body: dict = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
                  "max_completion_tokens": max_tokens}
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"},
        method="POST",
    )
    t0 = time.perf_counter()
    res = StreamResult(False, model=model, provider="OpenAI")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        return StreamResult(False, rc=e.code, error="http_error", model=model, provider="OpenAI",
                            ms_total=_ms(t0), detail=detail)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return StreamResult(False, error="connect_error", model=model, provider="OpenAI", ms_total=_ms(t0),
                            detail=str(e)[:200])
    res.rc = getattr(resp, "status", 200)
    parts: list[str] = []
    try:
        with resp:
            for line in _sse_lines(resp):
                if should_stop and should_stop():
                    res.error = "cancelled"
                    break
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(obj.get("error"), dict):
                    res.error, res.detail = "http_error", str(obj["error"])[:300]
                    break
                if isinstance(obj.get("model"), str):
                    res.model = obj["model"]
                for ch in obj.get("choices") or []:
                    delta = (ch.get("delta") or {}).get("content")
                    if delta:
                        if res.ms_first_token is None:
                            res.ms_first_token = _ms(t0)
                        parts.append(delta)
                        res.chunks += 1
                        if on_delta:
                            on_delta(delta)
                    if ch.get("finish_reason"):
                        res.extra["finish_reason"] = ch["finish_reason"]
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    res.input_tokens = _int(usage.get("prompt_tokens"))
                    res.output_tokens = _int(usage.get("completion_tokens"))
    except (TimeoutError, OSError) as e:
        res.error = res.error or "connect_error"
        res.detail = res.detail or str(e)[:200]
    res.ms_total = _ms(t0)
    res.text = "".join(parts).strip()
    if res.error:
        res.ok = False
        return res
    if not res.text:
        res.ok, res.error = False, "empty_output"
        return res
    res.ok = True
    return res


def list_models(api_key: str, *, url: str = "https://api.openai.com/v1/models", timeout: float = 10.0) -> list[str]:
    """設定画面のモデル選び用: キーで使えるモデルのうち、文章を生成するもの(音声・画像・埋め込みなどを除く)。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read() or b"{}").get("data") or []
    skip = ("audio", "realtime", "transcribe", "tts", "image", "embedding", "search", "moderation", "instruct", "whisper",
            "dall-e", "davinci", "babbage", "computer-use")
    ids = [m.get("id", "") for m in data if isinstance(m, dict)]
    keep = [i for i in ids if (i.startswith(("gpt-", "chatgpt-")) or (len(i) > 1 and i[0] == "o" and i[1].isdigit()))
            and not any(s in i for s in skip)]
    return sorted(set(keep), reverse=True)

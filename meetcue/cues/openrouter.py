"""OpenRouter chat/completions のストリーミングクライアント — stdlib のみ。

OpenRouter の運用規約(作者の別プロジェクトと同じ):
- provider.data_collection="deny"(入力を学習・保存する配信事業者を除外)
- 中国企業運営の配信事業者を既定除外(事業者名の指名リスト)
- reasoning は無効(思考トークンで content が空になる事故の予防)
- 応答 usage.cost(USD)を台帳に積めるよう返す(`usage: {include: true}`)

raise しない: 失敗は StreamResult(ok=False, error=...) で返す。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator

DEFAULT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
FAST_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_TIMEOUT_S = 60

DEFAULT_IGNORE_PROVIDERS = ("DeepSeek", "Z.AI", "Alibaba", "Baidu", "SiliconFlow", "StreamLake")


def _ignore_providers() -> list[str]:
    raw = os.environ.get("MEETCUE_OPENROUTER_IGNORE_PROVIDERS")
    if raw is None or not raw.strip():
        return list(DEFAULT_IGNORE_PROVIDERS)
    if raw.strip().lower() == "none":
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class StreamResult:
    ok: bool
    text: str = ""
    rc: int | None = None
    error: str | None = None  # http_error | connect_error | bad_response | empty_output | no_key | cancelled
    model: str = ""
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    ms_first_token: float | None = None
    ms_total: float = 0.0
    detail: str = ""
    chunks: int = 0
    extra: dict = field(default_factory=dict)


def stream_chat(
    messages: list[dict],
    *,
    api_key: str | None,
    model: str = DEFAULT_MODEL,
    url: str = DEFAULT_URL,
    timeout: int = DEFAULT_TIMEOUT_S,
    max_tokens: int = 1200,
    temperature: float | None = 0.4,
    on_delta: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    provider_order: list[str] | None = None,
    reasoning: dict | None = None,
) -> StreamResult:
    """SSE を逐次読み、content の差分を on_delta に渡す。should_stop() が True なら打ち切る。"""
    if not api_key:
        return StreamResult(False, error="no_key", model=model)
    provider_prefs: dict = {"data_collection": "deny"}
    ignore = _ignore_providers()
    if ignore:
        provider_prefs["ignore"] = ignore
    if provider_order:
        provider_prefs["order"] = list(provider_order)
    body: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        # 既定は思考なし(思考トークンで content が空になる事故の予防)。ただし Opus 5.5 は思考を切れない
        # (2026-09-26 実測: 400「Reasoning is mandatory for this endpoint」)ので、呼び出し側が {"effort": …} を渡す
        "reasoning": reasoning if reasoning is not None else {"enabled": False},
        "provider": provider_prefs,
        "usage": {"include": True},
    }
    if temperature is not None and reasoning is None:   # 思考ありでは temperature を送らない(Anthropic の制約)
        body["temperature"] = temperature
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Title": "meeting-cue",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    res = StreamResult(False, model=model)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        return StreamResult(False, rc=e.code, error="http_error", model=model,
                            ms_total=_ms(t0), detail=detail)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return StreamResult(False, error="connect_error", model=model, ms_total=_ms(t0),
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
                if "error" in obj and isinstance(obj["error"], dict):
                    res.error = "http_error"
                    res.detail = str(obj["error"])[:300]
                    break
                if isinstance(obj.get("model"), str):
                    res.model = obj["model"]
                if isinstance(obj.get("provider"), str):
                    res.provider = obj["provider"]
                for ch in obj.get("choices") or []:
                    delta = (ch.get("delta") or {}).get("content")
                    if delta:
                        if res.ms_first_token is None:
                            res.ms_first_token = _ms(t0)
                        parts.append(delta)
                        res.chunks += 1
                        if on_delta:
                            on_delta(delta)
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    res.input_tokens = _int(usage.get("prompt_tokens"))
                    res.output_tokens = _int(usage.get("completion_tokens"))
                    c = usage.get("cost")
                    if isinstance(c, (int, float)) and not isinstance(c, bool):
                        res.cost_usd = float(c)
    except (TimeoutError, OSError) as e:
        res.error = res.error or "connect_error"
        res.detail = res.detail or str(e)[:200]
    res.ms_total = _ms(t0)
    res.text = "".join(parts).strip()
    if res.error:
        res.ok = False
        return res
    if not res.text:
        res.ok = False
        res.error = "empty_output"
        return res
    res.ok = True
    return res


def _sse_lines(resp) -> Iterator[str]:
    """レスポンスを行単位で読む(SSE の "data: ..." 行)。"""
    buf = b""
    while True:
        chunk = resp.read(1024)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace").rstrip("\r")
    if buf:
        yield buf.decode("utf-8", errors="replace")


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None

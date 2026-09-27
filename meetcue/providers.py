"""生成 AI の接続先(設定画面・2026-09-26)。キーの「確認」だけを行う(各社の無料の認証確認)。

- OpenRouter: GET /api/v1/key(キーの情報)
- Anthropic: GET /v1/models(x-api-key + anthropic-version)
- OpenAI: GET /v1/models(Bearer)
stdlib の urllib だけ(依存を増やさない)。キーは送り先の 1 社にだけ送り、結果にもログにも出さない。
生成そのものは cues/llm.py(OpenRouter / Anthropic / OpenAI・第 2 段 2026-09-26)。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

LABELS = {"openrouter": "OpenRouter", "anthropic": "Anthropic", "openai": "OpenAI"}
GENERATION_READY = {"openrouter", "anthropic", "openai"}   # 生成に使える接続先(2026-09-26 第 2 段で 3 つとも)
KEY_PAGES = {                       # 「キーを取得」で開く各社のページ(画面から開けるのはここだけ)
    "openrouter": "https://openrouter.ai/keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "drive": "https://www.google.com/drive/download/",
}


def _request(provider: str, key: str) -> urllib.request.Request:
    if provider == "openrouter":
        return urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    if provider == "anthropic":
        return urllib.request.Request("https://api.anthropic.com/v1/models?limit=1",
                                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    if provider == "openai":
        return urllib.request.Request("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
    raise ValueError("unknown provider")


def check_key(provider: str, key: str | None, *, timeout: float = 10.0) -> dict:
    """{"ok": bool, "status": HTTP の状態 or None, "ms": …, "message": 画面に出す一言}。キーの中身は含めない。"""
    if not key:
        return {"ok": False, "status": None, "ms": 0, "message": "キーが登録されていません"}
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(_request(provider, key), timeout=timeout) as r:
            json.loads(r.read() or b"{}")
            status = r.status
    except urllib.error.HTTPError as e:
        ms = round((time.monotonic() - t0) * 1000)
        msg = "キーが正しくないか、使えない状態です" if e.code in (401, 403) else f"確認できませんでした(HTTP {e.code})"
        return {"ok": False, "status": e.code, "ms": ms, "message": msg}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {"ok": False, "status": None, "ms": round((time.monotonic() - t0) * 1000),
                "message": "接続できませんでした(ネットワークを確かめてください)"}
    return {"ok": 200 <= status < 300, "status": status, "ms": round((time.monotonic() - t0) * 1000),
            "message": f"{LABELS[provider]} につながりました"}

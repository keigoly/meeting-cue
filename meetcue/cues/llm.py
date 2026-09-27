"""生成 AI の接続先ごとの呼び出し(設定画面 第 2 段・2026-09-26)。どれも openrouter.StreamResult を返す(stdlib のみ)。

Target = 1 回の録音で使う接続先・キー・役割ごとのモデル・価格。役割: main(回答・質問タブ)/ fast(サマリ)/ deep(深く考える)。
- openrouter: 従来どおり(応答の usage.cost を台帳へ)。深く考えるは reasoning.effort。
- anthropic: 回答・質問タブは思考を切る。深く考えるは effort(Opus 5.5 は思考を切れない)。サマリ(Haiku)は思考の指定なし。
- openai: モデルは設定画面で選んだもの 1 つ。深く考えるは reasoning_effort(受け付けないモデルは外して送り直す)。
Anthropic / OpenAI は料金を返さないので、価格表(1M トークンあたり [入力, 出力])から cost_usd を計算する。価格が無ければ None。
Jev はここを通らない(どの接続先でも OpenRouter 経由)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import anthropic_direct, claude_cli, openai_direct, openrouter
from .openrouter import StreamResult

LABELS = {"openrouter": "OpenRouter", "anthropic": "Anthropic", "openai": "OpenAI", "claude-cli": "Claude サブスク"}


@dataclass
class Target:
    provider: str = "openrouter"
    api_key: str | None = None
    model: str = openrouter.DEFAULT_MODEL
    fast_model: str = openrouter.FAST_MODEL
    deep_model: str = ""
    prices: dict = field(default_factory=dict)          # {model: [入力 $/1M, 出力 $/1M]}
    provider_order: list[str] | None = None              # OpenRouter の配信事業者の指定
    pool: object | None = None                           # claude-cli: 温めた Claude Code のプロセス(claude_cli.Pool)

    def model_for(self, role: str) -> str:
        return {"fast": self.fast_model, "deep": self.deep_model or self.model}.get(role, self.model)

    @property
    def label(self) -> str:
        return f"{LABELS.get(self.provider, self.provider)}:{self.model}"


def target_for(cfg, provider: str, api_key: str | None, settings: dict | None = None) -> Target:
    """設定(ui.json)と config から Target を作る。"""
    settings = settings or {}
    if provider == "anthropic":
        a = cfg.anthropic
        return Target("anthropic", api_key, a.model, a.fast_model, a.deep_model, dict(a.prices))
    if provider == "claude-cli":   # 開発者向け: 本人の Claude Code(サブスク)。モデルは Anthropic と同じ ID・料金は数えない
        a = cfg.anthropic
        return Target("claude-cli", api_key, a.model, a.fast_model, a.deep_model, {}, None, (settings or {}).get("_pool"))
    if provider == "openai":
        m = str(settings.get("openai_model") or "")
        pin, pout = float(settings.get("openai_price_in") or 0), float(settings.get("openai_price_out") or 0)
        return Target("openai", api_key, m, m, m, {m: [pin, pout]} if m and (pin or pout) else {})
    return Target("openrouter", api_key, cfg.llm.model, cfg.llm.fast_model, cfg.planner.deep_model, {},
                  cfg.llm.provider_order or None)


def stream(target: Target, messages: list[dict], *, role: str = "main", timeout: int = 60, max_tokens: int = 1200,
           effort: str | None = None, on_delta: Callable[[str], None] | None = None,
           should_stop: Callable[[], bool] | None = None, model: str | None = None) -> StreamResult:
    """役割に合わせて接続先を呼ぶ。effort は role=deep のときの思考の量(low / medium / high)。"""
    m = model or target.model_for(role)
    deep = role == "deep"
    if target.provider == "anthropic":
        thinking = {"type": "disabled"} if role == "main" else None   # Haiku(fast)は指定しない・Opus(deep)は切れない
        r = anthropic_direct.stream_chat(messages, api_key=target.api_key, model=m, timeout=timeout, max_tokens=max_tokens,
                                         thinking=thinking, effort=effort if deep else None,
                                         on_delta=on_delta, should_stop=should_stop)
    elif target.provider == "claude-cli":
        r = claude_cli.stream_chat(messages, pool=target.pool, model=m, timeout=timeout, effort=effort if deep else None,
                                   on_delta=on_delta, should_stop=should_stop)
    elif target.provider == "openai":   # 推論するモデルは推論の分も上限に入るので、少なくとも 4000 を渡す(上限であって使う量ではない)
        r = openai_direct.stream_chat(messages, api_key=target.api_key, model=m, timeout=timeout, max_tokens=max(max_tokens, 4000),
                                      reasoning_effort=effort if deep else None, on_delta=on_delta, should_stop=should_stop)
    else:
        r = openrouter.stream_chat(messages, api_key=target.api_key, model=m, timeout=timeout, max_tokens=max_tokens,
                                   on_delta=on_delta, should_stop=should_stop, provider_order=target.provider_order,
                                   reasoning={"effort": effort} if deep and effort else None)
    if r.cost_usd is None:
        r.cost_usd = estimate_cost(target, r.model or m, r.input_tokens, r.output_tokens)
    return r


def estimate_cost(target: Target, model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = target.prices.get(model)
    if not price and model:   # 応答のモデル名が日付付きなどで揃わないときは、名前の前方一致で探す
        price = next((v for k, v in target.prices.items() if model.startswith(k) or k.startswith(model)), None)
    if not price or (input_tokens is None and output_tokens is None):
        return None
    return round((input_tokens or 0) * price[0] / 1e6 + (output_tokens or 0) * price[1] / 1e6, 6)

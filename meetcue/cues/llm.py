"""生成 AI の接続先ごとの呼び出し(設定画面 第 2 段・2026-09-26)。どれも openrouter.StreamResult を返す(stdlib のみ)。

Target = 1 回の録音で使う接続先・キー・役割ごとのモデル・価格。役割(2026-10-08 に 4 段へ):
  main(回答・質問タブ・用語・字幕の翻訳)/ fast(サマリ)/ mid(少し深く = Sonnet)/ deep(深く考える = Opus)。
- openrouter: 従来どおり(応答の usage.cost を台帳へ)。main / fast は思考を切る(Haiku 5.5 も受け付ける)。mid / deep は reasoning.effort。
- anthropic: main / fast は、思考を切れるモデル(Sonnet 5 まで)なら {"type": "disabled"}、adaptive 思考のモデル
  (Haiku 5.5 など)なら effort を Target.fast_effort に下げ、依頼文に FAST_HINT を足す。mid / deep は effort だけ。
- claude-cli: main / fast は effort を fast_effort に下げて FAST_HINT を足す(Claude Code は Haiku 5.5 の思考を切れない。
  2026-10-08 実測: 既定のままだと初トークン 6〜11 s → effort low + 一文で 0.53〜0.63 s)。温めるプロセスも同じ effort。
- openai: モデルは設定画面で選んだもの 1 つ。mid / deep は reasoning_effort(受け付けないモデルは外して送り直す)。
Anthropic / OpenAI は料金を返さないので、価格表(1M トークンあたり [入力, 出力])から cost_usd を計算する。価格が無ければ None。
Jev はここを通らない(どの接続先でも OpenRouter 経由)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import anthropic_direct, claude_cli, openai_direct, openrouter
from .openrouter import StreamResult

LABELS = {"openrouter": "OpenRouter", "anthropic": "Anthropic", "openai": "OpenAI", "claude-cli": "Claude サブスク"}
DEEP_ROLES = ("mid", "deep")
# 速さが要る役(main / fast)の依頼文に足す一文。adaptive 思考のモデルに考える時間を取らせない(effort を下げるのと両方で効く)
FAST_HINT = "- 考える時間は取らず、すぐに出力を書き始める。"


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
    mid_model: str = ""                                  # 少し深く(空 = deep → main)
    fast_effort: str | None = None                       # main / fast の effort(Anthropic 直・サブスクの adaptive 思考のモデル)

    def model_for(self, role: str) -> str:
        return {"fast": self.fast_model, "mid": self.mid_model or self.deep_model or self.model,
                "deep": self.deep_model or self.model}.get(role, self.model)

    def fast_effort_for(self, model: str) -> str | None:
        """main / fast の役で付ける effort(付けないなら None)。"""
        if self.provider == "claude-cli":
            return self.fast_effort
        if self.provider == "anthropic" and not thinking_off_ok(model) and supports_effort(model):
            return self.fast_effort
        return None

    @property
    def label(self) -> str:
        return f"{LABELS.get(self.provider, self.provider)}:{self.model}"


_THINKING_OFF = re.compile(r"^claude-(?:sonnet-5|haiku-4-5|sonnet-4-[56]|opus-4-[5-8])(?:-\d{8})?$")
_NO_EFFORT = re.compile(r"^claude-(?:haiku-4|sonnet-4-5|3)")


def thinking_off_ok(model: str) -> bool:
    """thinking: {"type": "disabled"} を受け付けるモデル(Sonnet 5 まで)。Haiku 5.5 / Sonnet 5.5 / Opus 5.5 は使わない。"""
    return bool(_THINKING_OFF.match(model or ""))


def supports_effort(model: str) -> bool:
    """output_config.effort を受け付けるか(Haiku 4.5・Sonnet 4.5 以前は 400)。"""
    return not _NO_EFFORT.match(model or "")


def with_fast_hint(messages: list[dict]) -> list[dict]:
    """最初の system の末尾に FAST_HINT を足した写し(system が無ければ足す)。元の list は変えない。"""
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            if FAST_HINT not in str(m.get("content") or ""):
                m["content"] = f"{m.get('content') or ''}\n{FAST_HINT}"
            return out
    return [{"role": "system", "content": FAST_HINT}, *out]


def warm_key(target: Target, role: str = "main") -> tuple[str, str | None, bool]:
    """claude-cli の温めるプロセスの鍵(モデル, effort, 速い役か)。stream が take するものと同じにする。"""
    m = target.model_for(role)
    eff = None if role in DEEP_ROLES else target.fast_effort_for(m)
    return m, eff, eff is not None


def target_for(cfg, provider: str, api_key: str | None, settings: dict | None = None) -> Target:
    """設定(ui.json)と config から Target を作る。"""
    settings = settings or {}
    if provider == "anthropic":
        a = cfg.anthropic
        return Target("anthropic", api_key, a.model, a.fast_model, a.deep_model, dict(a.prices),
                      mid_model=a.mid_model, fast_effort=a.fast_effort)
    if provider == "claude-cli":   # 開発者向け: 本人の Claude Code(サブスク)。モデルは Anthropic と同じ ID・料金は数えない
        a = cfg.anthropic
        return Target("claude-cli", api_key, a.model, a.fast_model, a.deep_model, {}, None, (settings or {}).get("_pool"),
                      mid_model=a.mid_model, fast_effort=a.fast_effort)
    if provider == "openai":
        m = str(settings.get("openai_model") or "")
        pin, pout = float(settings.get("openai_price_in") or 0), float(settings.get("openai_price_out") or 0)
        return Target("openai", api_key, m, m, m, {m: [pin, pout]} if m and (pin or pout) else {}, mid_model=m)
    return Target("openrouter", api_key, cfg.llm.model, cfg.llm.fast_model, cfg.planner.deep_model, {},
                  cfg.llm.provider_order or None, mid_model=cfg.planner.mid_model)


def stream(target: Target, messages: list[dict], *, role: str = "main", timeout: int = 60, max_tokens: int = 1200,
           effort: str | None = None, on_delta: Callable[[str], None] | None = None,
           should_stop: Callable[[], bool] | None = None, model: str | None = None) -> StreamResult:
    """役割に合わせて接続先を呼ぶ。effort は role=mid / deep のときの思考の量(low / medium / high)。"""
    m = model or target.model_for(role)
    deep = role in DEEP_ROLES
    fast_eff = None if deep else target.fast_effort_for(m)
    if fast_eff:   # adaptive 思考のモデルを速い役で使う: effort を下げ、すぐ書き始めさせる
        messages = with_fast_hint(messages)
    if target.provider == "anthropic":
        if deep:
            thinking, eff = None, effort                  # Opus 5.5 / Sonnet 5.5 は思考を切れない → effort だけ
        elif role == "main" and thinking_off_ok(m):
            thinking, eff = {"type": "disabled"}, None    # Sonnet 5 などは従来どおり思考を切る
        else:
            thinking, eff = None, fast_eff                # Haiku 5.5: effort low(Haiku 4.5 のサマリは指定しない)
        r = anthropic_direct.stream_chat(messages, api_key=target.api_key, model=m, timeout=timeout, max_tokens=max_tokens,
                                         thinking=thinking, effort=eff, on_delta=on_delta, should_stop=should_stop)
    elif target.provider == "claude-cli":
        r = claude_cli.stream_chat(messages, pool=target.pool, model=m, timeout=timeout, effort=effort if deep else fast_eff,
                                   fast=fast_eff is not None, on_delta=on_delta, should_stop=should_stop)
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
    if not price and model:   # 応答のモデル名が日付付きなどで揃わないときは、名前の前方一致で探す(長い名前を先に)
        for k in sorted(target.prices, key=len, reverse=True):
            if model.startswith(k) or k.startswith(model):
                price = target.prices[k]
                break
    if not price or (input_tokens is None and output_tokens is None):
        return None
    return round((input_tokens or 0) * price[0] / 1e6 + (output_tokens or 0) * price[1] / 1e6, 6)

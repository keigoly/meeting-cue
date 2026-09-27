"""Jev(TypeSafe AI・System One)判定クライアント — stdlib のみ。文章は生成しない。

Jev の API を呼ぶ小さなクライアント(stdlib のみ)。
state + 型付き質問(noul / choice / score)を 1 パスで投げ、確率と確信度を受ける。
行き先は OpenRouter `alpha/decisions`(既定・model `typesafe/jev-1.13`)。
raise しない: 失敗は DecisionResult(ok=False) で返し、呼び出し側は heuristic に落とす。

質問の id は記録のキーになるので変えない(docs/REQUIREMENTS.md §8)。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_TIMEOUT_S = 10
MAX_STATE_CHARS = 1500  # 日本語 約 1.4 tok/字。会議全文は渡さない

QUESTIONS: dict = {
    "speech_act": {
        "type": "choice",
        "instructions": "この発話(utterance)は何か。発話の内容だけで判断する。",
        "criteria": {
            "question": "相手に答えを求める質問",
            "request": "何かをしてほしいという依頼・要望",
            "statement": "説明・主張・報告・感想(答えを求めていない)",
            "agreement": "同意・相づち・了解(はい、なるほど、承知しました 等)",
            "smalltalk": "挨拶・雑談・進行の合図",
            "other": "上のどれでもない",
        },
    },
    "to_me": {
        "type": "noul",
        "instructions": (
            "この発話は、私(mode で示した立場の人。presenter=登壇者、participant=会議の当事者、"
            "audience=聴講者)に向けられた問いかけ・依頼か。"
        ),
    },
    "intent": {
        "type": "choice",
        "instructions": "発話者が本当に知りたいこと・意図として最も近いもの。",
        "criteria": {
            "clarify": "言葉の意味・定義・前提の確認",
            "how": "手順・方法・やり方",
            "why": "理由・背景・根拠",
            "feasibility": "できるかどうか・実現性・制約",
            "cost": "費用・工数・価格・時間",
            "compare": "比較・代替案・違い",
            "risk": "懸念・リスク・問題点・安全性",
            "schedule": "時期・納期・スケジュール",
            "opinion": "評価・意見・おすすめ",
            "challenge": "反論・指摘・疑義",
            "experience": "事例・実績・経験",
            "other": "上のどれでもない",
        },
    },
    "needs_knowledge": {
        "type": "noul",
        "instructions": "答えるのに、私の資料・過去の経験・メモ(Obsidian Vault)を参照する必要があるか。",
    },
    "urgency": {
        "type": "score",
        "instructions": "今すぐ返答が要る度合い。会話を止めないために即答が求められるほど高い。",
        "criteria": ["low", "medium", "high"],
    },
    "answer_length": {
        "type": "choice",
        "instructions": "この発話への適切な返答の長さ。",
        "criteria": {"short": "1〜2 文", "medium": "数文", "long": "構造化した説明が要る"},
    },
}


@dataclass(frozen=True)
class DecisionResult:
    ok: bool
    answers: dict = field(default_factory=dict)
    rc: int | None = None
    error: str | None = None  # http_error | connect_error | bad_response | no_answers | no_key
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    ms: float = 0.0
    detail: str = ""


def build_state(utterance: str, prev: list[str], mode: str, channel: str) -> dict:
    """Jev に渡す state。当該発話 + 直前 2 発話 + モード + チャネルだけ(≤ MAX_STATE_CHARS)。"""
    prev2 = [p[-300:] for p in prev[-2:]]
    utt = utterance[:MAX_STATE_CHARS]
    return {
        "mode": mode,
        "channel": channel,
        "prev": prev2,
        "utterance": utt,
        "note": "channel=system/room は相手(他者)の発話。mic は私自身の発話。",
    }


def decide(
    state: object,
    *,
    api_key: str | None,
    questions: dict | None = None,
    url: str = OPENROUTER_DECISIONS_URL,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> DecisionResult:
    if not api_key:
        return DecisionResult(False, error="no_key", model=model)
    body = json.dumps(
        {"model": model, "state": state, "questions": questions or QUESTIONS},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Title": "meeting-cue",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        return DecisionResult(False, rc=e.code, error="http_error", model=model,
                              ms=_ms(t0), detail=detail)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return DecisionResult(False, error="connect_error", model=model, ms=_ms(t0),
                              detail=str(e)[:200])
    ms = _ms(t0)
    try:
        data = json.loads(raw)
        answers = data["answers"]
    except (ValueError, KeyError, TypeError):
        return DecisionResult(False, rc=status, error="bad_response", model=model, ms=ms,
                              detail=raw[:300])
    if not isinstance(answers, dict) or not answers:
        return DecisionResult(False, rc=status, error="no_answers", model=model, ms=ms,
                              detail=raw[:300])
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    cost = usage.get("cost")
    return DecisionResult(
        True,
        answers=answers,
        rc=status,
        model=data.get("model") if isinstance(data.get("model"), str) else model,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        ms=ms,
    )


def summarize(answers: dict) -> dict:
    """答えを平たくする(記録・判定用)。壊れた answer は落とす。

    choice → 選択肢と確率 (`<id>`, `<id>_p`, `<id>_conf`) / noul → 確率 / score → 値と段階名。
    """
    out: dict = {}
    for qid, ans in (answers or {}).items():
        if not isinstance(ans, dict):
            continue
        t = ans.get("type")
        try:
            if t == "noul":
                out[qid] = round(float(ans.get("noul")), 3)
            elif t == "choice":
                out[qid] = ans.get("choice")
                probs = ans.get("probabilities") or {}
                sel = ans.get("choice")
                if isinstance(probs, dict) and sel in probs:
                    out[f"{qid}_p"] = round(float(probs[sel]), 3)
                if ans.get("confidence") is not None:
                    out[f"{qid}_conf"] = round(float(ans["confidence"]), 3)
            elif t == "score":
                out[qid] = round(float(ans.get("score")), 3)
                legend = ans.get("legend") or {}
                if isinstance(legend, dict) and legend:
                    nearest = min(legend, key=lambda k: abs(float(k) - float(ans.get("score"))))
                    out[f"{qid}_label"] = legend[nearest]
        except (TypeError, ValueError):
            continue
    return out


def should_trigger(summary: dict, *, act_min: float = 0.6, to_me_min: float = 0.6) -> bool:
    """キュー生成の起動条件: speech_act が question/request で確率 ≥ act_min かつ to_me ≥ to_me_min。"""
    act = summary.get("speech_act")
    p = float(summary.get("speech_act_p") or 0.0)
    to_me = float(summary.get("to_me") or 0.0)
    return act in ("question", "request") and p >= act_min and to_me >= to_me_min


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None

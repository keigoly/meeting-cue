"""選ぶ係(Jev セレクター)— 生成した候補を Jev に基準ごとに採点させ、重み付き合計で並べ替える。

出典: Vault `03_Resources/YouTube/AI/2026-09-25_しゃべらない最新AI「Jev」はガチの議論に使えることが分かった.md`
(海外Yパパさん)。同じ Claude が書いた候補 5 本から「どれを出すか」だけを Jev に選ばせると、Claude 自身に
選ばせるより 27 勝 13 敗。理由は「全体の印象」でなく「基準ごとの点数の足し算」で選ぶためブレず、書き手
自身の好みに引っ張られないこと。1 回 0.7 秒・0.03 円。**どの場面でどの基準を重く見るかは人が決める**。

ここでは 1 候補につき Jev を 1 回(score 質問 4〜5 問)呼び、候補ごとに並行して走らせる。
失敗した候補は採点なし(None)として末尾に回す(判定が取れなくても表示は続く)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import jev

# 採点の物差し(score 型・段階は低 → 高)。id は記録のキーになるので変えない。
ANSWER_QUESTIONS: dict = {
    "on_point": {"type": "score", "instructions": "候補は相手の質問に正面から答えているか。",
                 "criteria": ["ずれている", "部分的に答えている", "正面から答えている"]},
    "grounded": {"type": "score", "instructions": "根拠は具体的か(数値・事例・自分の資料に基づく記述があるか)。",
                 "criteria": ["根拠がない", "一般論", "具体的な根拠がある"]},
    "speakable": {"type": "score", "instructions": "会議の場でそのまま口に出せる長さと自然さか。",
                  "criteria": ["読み上げにくい", "手直しすれば言える", "そのまま言える"]},
    "safe": {"type": "score", "instructions": "断定しすぎ・事実誤認・約束しすぎの恐れが低いか。",
             "criteria": ["危うい", "注意が要る", "安全"]},
    "persuasive": {"type": "score", "instructions": "相手を納得させる説得力があるか。",
                   "criteria": ["弱い", "普通", "強い"]},
}
COUNTER_QUESTIONS: dict = {
    "reveals_intent": {"type": "score", "instructions": "相手の本当の意図・前提を引き出す質問か。",
                       "criteria": ["引き出さない", "少し引き出す", "核心を突く"]},
    "answerable": {"type": "score", "instructions": "相手がその場ですぐ答えられる質問か。",
                   "criteria": ["答えにくい", "普通", "すぐ答えられる"]},
    "advances": {"type": "score", "instructions": "答えが返れば次の一手(合意・具体化・見積り)に進めるか。",
                 "criteria": ["進まない", "少し進む", "大きく進む"]},
    "polite": {"type": "score", "instructions": "失礼でなく、会議の場と相手の立場に合っているか。",
               "criteria": ["場にそぐわない", "普通", "適切"]},
}

# 重みの既定。場面(mode)と意図(intent)で上書きする = 「どの基準を重く見るかは人が決める」。
DEFAULT_WEIGHTS: dict = {
    "answer": {"on_point": 1.0, "grounded": 1.0, "speakable": 0.7, "safe": 0.7, "persuasive": 0.8},
    "counter": {"reveals_intent": 1.0, "answerable": 0.7, "advances": 0.8, "polite": 0.5},
}
MODE_OVERRIDES: dict = {
    "presenter": {"answer": {"persuasive": 1.0, "safe": 1.0}, "counter": {"answerable": 0.9}},
    "participant": {"answer": {"on_point": 1.2}, "counter": {"advances": 1.0}},
    "audience": {"counter": {"reveals_intent": 1.2, "advances": 1.0, "polite": 0.7}},
}
INTENT_OVERRIDES: dict = {
    "risk": {"answer": {"safe": 1.0, "grounded": 1.2}},
    "cost": {"answer": {"grounded": 1.2, "safe": 0.9}, "counter": {"advances": 1.1}},
    "clarify": {"answer": {"on_point": 1.3, "speakable": 0.9}, "counter": {"reveals_intent": 1.2}},
    "challenge": {"answer": {"persuasive": 1.1, "safe": 0.9}},
    "feasibility": {"answer": {"grounded": 1.1, "safe": 0.9}},
    "schedule": {"answer": {"speakable": 0.9, "safe": 1.0}},
}


def weights_for(kind: str, mode: str, intent: str | None, base: dict | None = None) -> dict:
    w = dict((base or DEFAULT_WEIGHTS).get(kind, {}))
    w.update(MODE_OVERRIDES.get(mode, {}).get(kind, {}))
    w.update(INTENT_OVERRIDES.get(intent or "", {}).get(kind, {}))
    return w


@dataclass
class Scored:
    kind: str                 # answer | counter
    index: int                # 回答N / 逆質問N の N
    text: str
    scores: dict = field(default_factory=dict)   # 基準 → 0..1
    total: float | None = None                   # 重み付き合計(採点なしは None)
    ms: float = 0.0
    error: str | None = None
    cost_usd: float | None = None


def build_state(*, utterance: str, context: list[str], candidate: str, kind: str, mode: str) -> dict:
    return {
        "mode": mode,
        "prev": [c[-300:] for c in context[-3:]],
        "question": utterance[:800],
        "candidate_kind": "私がこれから言う回答" if kind == "answer" else "私がこれから相手に返す逆質問",
        "candidate": candidate[:800],
    }


def score_candidate(cand: Scored, *, utterance: str, context: list[str], mode: str, intent: str | None,
                    api_key: str | None, model: str, timeout: int, base_weights: dict | None = None) -> Scored:
    """Jev で 1 候補を採点する(同期・スレッドで呼ぶ)。失敗は error に残して total=None。"""
    qs = ANSWER_QUESTIONS if cand.kind == "answer" else COUNTER_QUESTIONS
    state = build_state(utterance=utterance, context=context, candidate=cand.text, kind=cand.kind, mode=mode)
    r = jev.decide(state, api_key=api_key, questions=qs, model=model, timeout=timeout)
    cand.ms = r.ms
    cand.cost_usd = r.cost_usd
    if not r.ok:
        cand.error = r.error
        return cand
    s = jev.summarize(r.answers)
    # Jev の score は段階の添字スケール(3 段階なら 0..2)で返る(2026-09-25 実測)。0..1 に正規化する。
    cand.scores = {k: round(float(s[k]) / max(1, len(qs[k]["criteria"]) - 1), 3)
                   for k in qs if isinstance(s.get(k), (int, float))}
    cand.total = weighted_total(cand.scores, weights_for(cand.kind, mode, intent, base_weights))
    return cand


def weighted_total(scores: dict, weights: dict) -> float | None:
    if not scores:
        return None
    return round(sum(float(scores.get(k, 0.0)) * float(w) for k, w in weights.items()), 3)


def rank(cands: list[Scored]) -> list[Scored]:
    """合計の降順。採点なし(None)は元の順で末尾。"""
    scored = sorted([c for c in cands if c.total is not None], key=lambda c: (-c.total, c.index))
    unscored = sorted([c for c in cands if c.total is None], key=lambda c: c.index)
    return scored + unscored

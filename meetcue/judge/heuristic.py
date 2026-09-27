"""ヒューリスティック判定 — Jev が使えないとき(鍵なし・不通・機微モード)の縮退経路。

出力は jev.summarize() と同じキー(speech_act / speech_act_p / to_me / intent / needs_knowledge /
urgency / answer_length)に `source="heuristic"` を添える。精度は Jev に劣る前提で、
閾値の意味を揃えるため確率は 0.0 / 0.5 / 0.85 の 3 段だけを返す。
"""
from __future__ import annotations

import re

_Q_TAIL = re.compile(
    r"(？|\?|ですか|でしょうか|ますか|ませんか|ないですか|んですか|のですか|かな|"
    r"教えて(ください|いただけ|もらえ)?|伺(い|え)|お聞き|聞かせて|"
    r"どう(です|思|でしょ|なる|なっ|いう|して)|いかが|"
    r"なぜ|なんで|どうして|いつ|どこ|どちら|どれ|何(が|を|で|の|に|か|です|でしょう)|"
    r"いくら|どのくらい|どの程度|ありますか|できますか|可能ですか|大丈夫ですか)\s*[。．.]?\s*$"
)
_REQ = re.compile(r"(してください|お願いします|してほしい|いただけますか|いただけませんか|もらえますか)\s*[。．.]?\s*$")
_AGREE = re.compile(r"^(?:(?:はい|ええ|うん|なるほど|承知(?:しました|いたしました)|了解(?:です|しました)?|そうですね|わかりました|分かりました|ありがとうございます|ですね|はいはい)[、。．.!！\s]*)+$")

_INTENT = [
    ("cost", re.compile(r"費用|コスト|価格|料金|いくら|工数|何人|何時間|予算")),
    ("schedule", re.compile(r"いつ|納期|スケジュール|時期|期限|何日|何週|何ヶ月|何か月")),
    ("risk", re.compile(r"リスク|懸念|問題|大丈夫|安全|危険|障害|競合|影響")),
    ("compare", re.compile(r"違い|比べ|比較|どちら|代替|他の|ほかの|それとも")),
    ("feasibility", re.compile(r"でき(ます|る)|可能|対応(でき|して)|実現|制約|要件")),
    ("how", re.compile(r"どうやって|方法|手順|やり方|どのように|設定|構成")),
    ("why", re.compile(r"なぜ|なんで|どうして|理由|背景|根拠")),
    ("experience", re.compile(r"事例|実績|経験|導入例|ケース")),
    ("opinion", re.compile(r"どう思|意見|評価|おすすめ|お勧め|感想")),
    ("clarify", re.compile(r"とは|意味|定義|つまり|ということ|確認")),
]


def judge(utterance: str, *, mode: str = "participant", channel: str = "system") -> dict:
    t = (utterance or "").strip()
    out: dict = {"source": "heuristic"}
    if not t:
        out.update(speech_act="other", speech_act_p=0.5, to_me=0.0)
        return out
    if _AGREE.match(t):
        out.update(speech_act="agreement", speech_act_p=0.85, to_me=0.0)
    elif _Q_TAIL.search(t):
        out.update(speech_act="question", speech_act_p=0.85, to_me=0.85)
    elif _REQ.search(t):
        out.update(speech_act="request", speech_act_p=0.85, to_me=0.85)
    else:
        out.update(speech_act="statement", speech_act_p=0.5, to_me=0.0)
    intent = "other"
    for name, rx in _INTENT:
        if rx.search(t):
            intent = name
            break
    out["intent"] = intent
    out["needs_knowledge"] = 0.5 if out["speech_act"] in ("question", "request") else 0.0
    out["urgency"] = 0.5
    out["urgency_label"] = "medium"
    out["answer_length"] = "medium" if len(t) > 20 else "short"
    return out

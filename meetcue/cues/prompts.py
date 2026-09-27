"""キュー生成のプロンプト(docs/REQUIREMENTS.md FR-6)。

出力は行単位の軽い書式にする(JSON だと 1 件目が完成するまで表示できない)。
  意図: <1 行>
  回答1: <見出し> | <骨子 2〜3 文> | 根拠: <ノートのパス#見出し か「なし」>
  回答2: …
  回答3: …
  逆質問1: <質問> | <狙い 1 行>
  逆質問2: …
  逆質問3: …
"""
from __future__ import annotations

import re

MODE_JA = {
    "participant": "会議の当事者(相手から質問・依頼を受ける側)",
    "presenter": "登壇者(聴衆から質問を受ける側)",
    "audience": "聴講者(登壇者に質問したい側)",
}

SYSTEM = """あなたは会議中の私(ユーザー)の隣に座る補佐役です。相手の発話に対し、私がその場で使える短い補助を日本語で出します。
規則:
- 出力は指定の行書式だけ。前置き・補足・Markdown 見出しは書かない。
- 回答候補は「私が口に出せる文」にする。断定できないことは「〜と認識しています」「確認します」と言い切る形で書く。
- 根拠に使えるのは与えられたナレッジだけ。無ければ「なし」と書き、一般知識で補う場合は骨子に「(一般論)」と付ける。
- 逆質問は、相手の意図を確かめる・前提を揃える・次の一手を引き出す、のいずれかを狙う。
- 各行は 1 行に収める(改行しない)。"""

USER_TEMPLATE = """# 私の立場
{mode_ja}(mode={mode})

# 私のプロフィール(要約)
{profile}

# 直前の文脈(古い → 新しい)
{context}

# 相手の発話(これに対応する)
{utterance}

# 判定(Jev)
speech_act={speech_act} / intent={intent} / answer_length={answer_length} / urgency={urgency}

# ナレッジ(私の Obsidian Vault から検索・上位 {n_hits} 件)
{knowledge}

# 出力書式(この順・各 1 行。回答は {n_answers} 本、逆質問は {n_counters} 本。互いに切り口を変える。本数 0 の種類は書かない)
{intent_line}
{answer_lines}
{counter_lines}"""

_LINE = re.compile(r"^(意図|回答\d+|逆質問\d+)\s*[:：]\s*(.*)$")


def build_messages(*, mode: str, profile: str, context: list[str], utterance: str,
                   judgment: dict, hits: list, n_answers: int = 3, n_counters: int = 3,
                   parts: tuple[str, ...] = ("intent", "answers", "counters")) -> list[dict]:
    """parts で出力の部分を選ぶ。回答と逆質問を別々のリクエストで並行生成すると総時間が半分になる。"""
    ctx = "\n".join(f"- {c}" for c in context[-10:]) or "- (なし)"
    if hits:
        kn = "\n".join(
            f"[{i + 1}] {h.path}#{h.heading or '(冒頭)'}\n{h.body[:600]}"
            for i, h in enumerate(hits)
        )
    else:
        kn = "(該当なし)"
    user = USER_TEMPLATE.format(
        mode_ja=MODE_JA.get(mode, mode), mode=mode, profile=profile or "(未設定)",
        context=ctx, utterance=utterance,
        speech_act=judgment.get("speech_act", "?"), intent=judgment.get("intent", "?"),
        answer_length=judgment.get("answer_length", "?"),
        urgency=judgment.get("urgency_label", judgment.get("urgency", "?")),
        n_hits=len(hits), knowledge=kn, n_answers=n_answers if "answers" in parts else 0,
        n_counters=n_counters if "counters" in parts else 0,
        intent_line="意図: <相手が本当に知りたいこと 1 行>" if "intent" in parts else "",
        answer_lines="\n".join(f"回答{i}: <見出し> | <骨子> | 根拠: <パス#見出し か なし>" for i in range(1, n_answers + 1))
        if "answers" in parts else "",
        counter_lines="\n".join(f"逆質問{i}: <質問> | <狙い>" for i in range(1, n_counters + 1))
        if "counters" in parts else "",
    )
    # 空行を畳む(parts で外した行)
    user = "\n".join(line for line in user.splitlines() if line.strip() or not line.startswith(""))
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse_cue_lines(text: str) -> dict:
    """行書式を {intent, answers:[{title, body, source}], counters:[{question, aim}]} に。途中でも呼べる。"""
    out: dict = {"intent": "", "answers": [], "counters": []}
    for raw in text.splitlines():
        m = _LINE.match(raw.strip())
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "意図":
            out["intent"] = val
        elif key.startswith("回答"):
            parts = [p.strip() for p in val.split("|")]
            title = parts[0] if parts else val
            body = parts[1] if len(parts) > 1 else ""
            src = parts[2] if len(parts) > 2 else ""
            src = re.sub(r"^根拠\s*[:：]\s*", "", src)
            out["answers"].append({"title": title, "body": body, "source": src})
        elif key.startswith("逆質問"):
            parts = [p.strip() for p in val.split("|")]
            out["counters"].append({"question": parts[0] if parts else val,
                                    "aim": parts[1] if len(parts) > 1 else ""})
    return out


# ---- 質問タブ(FR-6c・U3): こちらから聞くべき質問の先回り ------------------------------------------
# 書式: 質問N: <質問> | <ねらい> | <頃合い>(1 行 1 件。書き上がった行から画面に出す)
PLAN_SYSTEM = """あなたは会議中の私(ユーザー)の隣に座る補佐役です。会話の流れを読み、私がこれから相手に投げると良い質問を日本語で先回りして出します。
規則:
- 出力は指定の行書式だけ。前置き・補足・Markdown 見出しは書かない。
- 質問は私がそのまま口に出せる 1 文にする(60 字程度まで)。
- 直近の話題に根ざし、前提を確かめる・論点を深める・次の一手(合意・具体化・見積り・期限・担当)を引き出す、のいずれかを狙う。会話の中で既に答えが出ていることは聞かない。
- 私の立場に合う丁寧さにする。
- ナレッジ(私の Vault)に関係があれば、それを踏まえた質問を 1 本以上含める。
- 各行は 1 行に収める(改行しない)。"""

PLAN_DEEP = """
- いまは「深く考える」を頼まれている。会話全体の論点を整理し、見落とされている前提・リスク・利害・決めるべきことを突く、鋭い質問にする。表面的な確認質問は避ける。"""

PLAN_TEMPLATE = """# 私の立場
{mode_ja}(mode={mode})

# 私のプロフィール(要約)
{profile}

# ここまでの会話(古い → 新しい。「相手」= 相手の発言、「自分」= 私の発言)
{transcript}

# ナレッジ(私の Obsidian Vault から検索・上位 {n_hits} 件)
{knowledge}

# 出力書式(各 1 行・{n} 本・互いに切り口を変える。ねらい = なぜ今これを聞くか 1 行、頃合い = いつ聞くと良いか短く)
{lines}"""

_PLAN_LINE = re.compile(r"^質問(\d+)\s*[:：]\s*(.*)$")


def build_plan_messages(*, mode: str, profile: str, transcript: list[str], hits: list, n: int = 5,
                        deep: bool = False) -> list[dict]:
    """transcript は「相手: …」「自分: …」の行(古い → 新しい)。"""
    if hits:
        kn = "\n".join(f"[{i + 1}] {h.path}#{h.heading or '(冒頭)'}\n{h.body[:500]}" for i, h in enumerate(hits))
    else:
        kn = "(該当なし)"
    user = PLAN_TEMPLATE.format(
        mode_ja=MODE_JA.get(mode, mode), mode=mode, profile=profile or "(未設定)",
        transcript="\n".join(transcript) or "(まだ会話がない)", n_hits=len(hits), knowledge=kn, n=n,
        lines="\n".join(f"質問{i}: <質問> | <ねらい> | <頃合い>" for i in range(1, n + 1)))
    return [{"role": "system", "content": PLAN_SYSTEM + (PLAN_DEEP if deep else "")},
            {"role": "user", "content": user}]


def parse_plan_lines(text: str) -> list[dict]:
    """質問N の行を [{index, question, aim, timing}] に。途中でも呼べる。"""
    out = []
    for raw in text.splitlines():
        m = _PLAN_LINE.match(raw.strip())
        if not m:
            continue
        parts = [p.strip() for p in m.group(2).split("|")]
        out.append({"index": int(m.group(1)), "question": parts[0] if parts else "",
                    "aim": parts[1] if len(parts) > 1 else "", "timing": parts[2] if len(parts) > 2 else ""})
    return out

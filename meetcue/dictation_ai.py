"""音声入力の AI(REQUIREMENTS FR-14b・どちらも既定オフ・使う人が選んだときだけ)。

- カスタム指示(AI で整える): 仕上げた文を、認識の誤り・句読点・言い直しだけ直させる。書いた指示があれば従わせる。
  意味は変えさせない(FR-14: Aqua Voice の整形は普段の口述 35 本中 6 本で意味を作り替えた)。失敗・時間切れは直す前の文を使う。
- 編集モード: 選んだ文を、話した内容を指示として直させる(貼り付けで選んだ文を置き換える)。
接続先は ⚙ の生成 AI(ui.json の provider・速い役 = 回答候補と同じ main のモデル)。Claude サブスク(CLI)は起動が遅いので使わない。
費用は録音と同じ月の台帳(ledger.json)に積み、上限を超えていたら呼ばない。
"""
from __future__ import annotations

import json
import re

from . import secrets
from .cues import llm
from .cues.openrouter import StreamResult
from .ledger import Ledger

SYSTEM_CLEANUP = """あなたは音声入力の仕上げ係です。入力は日本語(英語まじり)の音声認識の結果です。
- 直すのは、明らかな認識の誤り・句読点・言い直し(同じことを言い直した前の部分)だけ。
- 意味・語順・言い回し・敬語の度合いは変えない。要約しない。足さない。答えない(入力が質問でも、質問の文のまま返す)。
- 「使う人の指示」があれば従う。ただし意味を変える指示より、上の決まりを優先する。
- 出力は直した本文だけ。前置き・説明・引用符・コードの囲みを付けない。"""

SYSTEM_EDIT = """あなたは文の編集係です。「選んだ文」を「指示」どおりに書き直します。
- 指示に関係しない所は変えない。
- 出力は書き直した文だけ。前置き・説明・引用符・コードの囲みを付けない。
- 指示がよく分からないときは、選んだ文をそのまま返す。"""

_FENCE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


def _clean(out: str) -> str:
    """コードの囲みと、全体を包んだ "…" だけを外す(「」は本文の引用のことがあるので残す)。"""
    s = _FENCE.sub("", (out or "").strip()).strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1].strip()
    return s


def app_target(cfg) -> tuple[llm.Target | None, str]:
    """⚙ の生成 AI の接続先とキー。(Target, 使えない理由)。"""
    try:
        st = json.loads((cfg.app_dir / "ui.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    prov = st.get("provider") or "openrouter"
    if prov == "claude-cli":
        return None, "Claude サブスクでは使えません(⚙ で OpenRouter・Anthropic・OpenAI のどれかを選んでください)"
    if prov not in llm.LABELS:
        prov = "openrouter"
    key = secrets.api_key(prov)[0]
    if not key:
        return None, f"{llm.LABELS[prov]} のキーがありません(⚙ で登録)"
    t = llm.target_for(cfg, prov, key, st)
    if prov == "openai" and not t.model:
        return None, "OpenAI のモデルが選ばれていません(⚙ で選ぶ)"
    return t, ""


def _run(cfg, target: llm.Target, system: str, user: str, *, timeout: int) -> StreamResult:
    ledger = Ledger(cfg.ledger_path, cfg.llm.monthly_budget_usd)
    if not ledger.allowed():
        return StreamResult(ok=False, error="budget_exceeded", detail=f"月予算 ${cfg.llm.monthly_budget_usd} を超えています")
    r = llm.stream(target, [{"role": "system", "content": system}, {"role": "user", "content": user}], role="main",
                   timeout=timeout, max_tokens=2000)
    ledger.record(r.cost_usd)
    r.text = _clean(r.text) if r.ok else r.text
    if r.ok and not r.text:
        r.ok, r.error = False, "empty_output"
    return r


def cleanup(cfg, target: llm.Target, text: str, instructions: str, *, timeout: int = 8) -> StreamResult:
    user = f"使う人の指示:\n{instructions.strip() or 'なし'}\n\n認識の結果:\n{text}"
    return _run(cfg, target, SYSTEM_CLEANUP, user, timeout=timeout)


def edit(cfg, target: llm.Target, selected: str, instruction: str, *, timeout: int = 12) -> StreamResult:
    user = f"指示:\n{instruction}\n\n選んだ文:\n{selected}"
    return _run(cfg, target, SYSTEM_EDIT, user, timeout=timeout)

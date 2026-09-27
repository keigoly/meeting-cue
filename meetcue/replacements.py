"""置き換え辞書(2026-09-27 keigoly様): 音声認識がよく間違える固有名詞を、正しい語に置き換える。

置き場所は ~/.meeting-cue/replacements.txt(個人の固有名詞を含むので repo に入れない)。⚙ の設定画面で編集する。
書き方は 1 行 = `正しい語|誤り|誤り…`(eval の固有名詞の一覧と同じ)。`#` で始まる行は注釈。

- 確定した発言(記録・質問の判定・回答候補・サマリ・書き出し)と、途中の表示(本体の画面・ライブ字幕)の両方に掛ける。
- 空白の有無は区別しない(「ミーティング Q」と「ミーティングQ」)。英字の大文字・小文字も区別しない。
- 1 回の走査で長い誤りから置き換える(置き換えた語をもう一度置き換えない)。
- 誤って広く当たらないよう、空白を除いて 1 文字の誤りは使わない(使えない行として画面に出す)。
- ファイルを手で直しても、次の発言から効く(更新時刻が変わったら読み直す)。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

MAX_RULES = 500


def parse(text: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """(使える規則 [(正しい語, [誤り…])], 使えない行)。"""
    rules, ignored = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("|")]
        canon, wrongs = parts[0], []
        for w in parts[1:]:
            if len(re.sub(r"\s", "", w)) < 2 or w == canon:
                continue
            wrongs.append(w)
        if not canon or not wrongs:
            ignored.append(s)
            continue
        rules.append((canon, wrongs))
        if len(rules) >= MAX_RULES:
            break
    return rules, ignored


def compile_rules(rules: list[tuple[str, list[str]]]) -> tuple[re.Pattern | None, dict[str, str]]:
    """誤り → 正しい語 の 1 本の正規表現(長いものから・空白は任意)と、照合用の引き当て表。"""
    table: dict[str, str] = {}
    for canon, wrongs in rules:
        for w in wrongs:
            table.setdefault(_key(w), canon)
    if not table:
        return None, {}
    alts = sorted(table, key=len, reverse=True)
    pat = "|".join(_pattern(k) for k in alts)
    return re.compile(pat, re.IGNORECASE), table


def _pattern(key: str) -> str:
    """空白は任意。英字と数字だけの誤りは、英単語の一部には当てない(「POC」で pocket を変えない)。"""
    body = r"\s*".join(re.escape(ch) for ch in key)
    return rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])" if re.fullmatch(r"[A-Za-z0-9]+", key) else body


def _key(s: str) -> str:
    return re.sub(r"\s", "", s).lower()


class Replacer:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime: float | None = None
        self._pat: re.Pattern | None = None
        self._table: dict[str, str] = {}
        self.rules: list[tuple[str, list[str]]] = []
        self.ignored: list[str] = []
        self._reload()

    def _reload(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            self._mtime, self._pat, self._table, self.rules, self.ignored = None, None, {}, [], []
            return
        if st.st_mtime == self._mtime:
            return
        self._mtime = st.st_mtime
        self.rules, self.ignored = parse(self.path.read_text(encoding="utf-8"))
        self._pat, self._table = compile_rules(self.rules)

    def apply(self, text: str) -> str:
        self._reload()
        if not self._pat or not text:
            return text
        return self._pat.sub(lambda m: self._table.get(_key(m.group(0)), m.group(0)), text)

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def save(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text if text.endswith("\n") or not text else text + "\n", encoding="utf-8")
        tmp.replace(self.path)
        self._mtime = None
        self._reload()

    def view(self) -> dict:
        self._reload()
        return {"text": self.text(), "rules": len(self.rules), "wrongs": sum(len(w) for _, w in self.rules),
                "ignored": self.ignored, "path": str(self.path)}

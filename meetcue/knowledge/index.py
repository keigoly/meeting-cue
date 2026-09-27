"""Vault のローカル索引 — SQLite FTS5(stdlib のみ)。

日本語は形態素解析器なしで引けるよう **文字 bigram** に展開して索引する(CJK の古典的手法)。
trigram トークナイザは 2 文字語(費用・構成・課題)が引けないので採らない。
ASCII の語はそのまま小文字の単語として索引する。

チャンク = 見出し単位(上限 chunk_chars)。テーブル:
  files(path PRIMARY KEY, mtime, chunks)          … 増分更新用
  chunks(path, heading, body UNINDEXED, ngram)     … FTS5(unicode61)。body は原文、ngram は索引用
索引は Vault 本文の複製なので ~/.meeting-cue/index/ に置き、repo・Vault に出さない。
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ]+")
_ASCII_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-\.]{1,}")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.M)
_WIKILINK = re.compile(r"\[\[([^\]|]+)(\|[^\]]*)?\]\]")
_MDLINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")

DEFAULT_INCLUDE = ("01_Projects", "02_Ideas", "03_Resources", "04_Context")
DEFAULT_EXCLUDE = (
    "04_Context/Session_Log", "04_Context/Discord_Log", "04_Context/Compact_Snapshots",
    "04_Context/Memo_Private", "00_Inbox/_processed", "_Archive",
    "03_Resources/YouTube",
)


def ngram_text(text: str) -> str:
    """索引・検索の共通変換: CJK の連は文字 bigram(1 文字なら単字)、ASCII は小文字の単語。"""
    out: list[str] = []
    for m in re.finditer(r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ]+|[A-Za-z0-9_][A-Za-z0-9_\-\.]*", text):
        s = m.group(0)
        if _CJK.match(s):
            if len(s) == 1:
                out.append(s)
            else:
                out.extend(s[i:i + 2] for i in range(len(s) - 1))
        else:
            out.append(s.lower())
    return " ".join(out)


_HIRA = re.compile(r"[\u3040-\u309f\u30fc]")
_PARTICLE_SPLIT = re.compile(
    r"(?:について|における|に関する|という|として|ですか|でしょうか|ました|ません|ます|です|する|して|した|"
    r"から|まで|より|など|では|には|とは|の|が|を|に|へ|と|で|も|や|は|か)")


def _trim_kana(term: str) -> str:
    """語の前後のひらがな(活用語尾・付属語の残り)を落とす。例: 'る費用' → '費用'、'扱います' → '扱'。"""
    t = term
    while t and _HIRA.match(t[0]):
        t = t[1:]
    while t and _HIRA.match(t[-1]):
        t = t[:-1]
    return t


def query_terms(text: str, *, max_terms: int = 12) -> list[str]:
    """発話から検索語を取り出す(形態素解析器なしの近似)。

    CJK の連を付属語で割り、前後のひらがなを落とし、漢字・カタカナ・ASCII を含む 2 文字以上だけ残す。
    ひらがなだけの語(どこ・あり・くらい)は雑音になるので捨てる。長い語(固有名詞・複合語)を優先する。
    """
    terms: list[str] = []
    seen: set[str] = set()
    for m in _CJK.finditer(text):
        for piece in _PARTICLE_SPLIT.split(m.group(0)):
            piece = _trim_kana(piece.strip())
            if len(piece) >= 2 and piece not in seen and not all(_HIRA.match(c) for c in piece):
                seen.add(piece)
                terms.append(piece)
    for m in _ASCII_WORD.finditer(text):
        w = m.group(0).lower()
        if len(w) >= 2 and w not in seen:
            seen.add(w)
            terms.append(w)
    terms.sort(key=len, reverse=True)
    return terms[:max_terms]


def _clean(md: str) -> str:
    md = _FRONTMATTER.sub("", md, count=1)
    md = _WIKILINK.sub(lambda m: m.group(1), md)
    md = _MDLINK.sub(lambda m: m.group(1), md)
    md = re.sub(r"```.*?```", " ", md, flags=re.S)
    md = re.sub(r"<[^>]+>", " ", md)
    return md


def chunk_markdown(md: str, *, chunk_chars: int = 800) -> list[tuple[str, str]]:
    """(heading, body) のリスト。見出しで割り、長い節は段落境界で chunk_chars 以下に割る。"""
    text = _clean(md)
    parts: list[tuple[str, str]] = []
    pos = 0
    heading = ""
    for m in _HEADING.finditer(text):
        body = text[pos:m.start()].strip()
        if body:
            parts.append((heading, body))
        heading = m.group(2).strip()
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        parts.append((heading, tail))
    out: list[tuple[str, str]] = []
    for h, body in parts:
        if len(body) <= chunk_chars:
            out.append((h, body))
            continue
        buf = ""
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            if not para:
                continue
            if buf and len(buf) + len(para) + 1 > chunk_chars:
                out.append((h, buf))
                buf = ""
            if len(para) > chunk_chars:
                for i in range(0, len(para), chunk_chars):
                    out.append((h, para[i:i + chunk_chars]))
                continue
            buf = f"{buf}\n{para}" if buf else para
        if buf:
            out.append((h, buf))
    return out


@dataclass(frozen=True)
class Hit:
    path: str
    heading: str
    body: str
    rank: float

    def snippet(self, terms: list[str], width: int = 160) -> str:
        b = self.body.replace("\n", " ")
        for t in terms:
            i = b.find(t)
            if i >= 0:
                s = max(0, i - width // 3)
                return ("…" if s else "") + b[s:s + width] + ("…" if s + width < len(b) else "")
        return b[:width] + ("…" if len(b) > width else "")


class VaultIndex:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.db_path)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, chunks INTEGER)")
        self.con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
            "path UNINDEXED, heading UNINDEXED, body UNINDEXED, ngram, tokenize='unicode61')"
        )

    # ---- 構築 ---------------------------------------------------------------
    def build(self, vault_root: Path | str, *, include=DEFAULT_INCLUDE, exclude=DEFAULT_EXCLUDE,
              chunk_chars: int = 800, full: bool = False) -> dict:
        """増分構築(mtime が変わったファイルだけ)。full=True で全消し再構築。"""
        root = Path(vault_root)
        t0 = time.perf_counter()
        if full:
            self.con.execute("DELETE FROM files")
            self.con.execute("DELETE FROM chunks")
        known = {p: m for p, m in self.con.execute("SELECT path, mtime FROM files")}
        seen: set[str] = set()
        added = updated = removed = 0
        n_chunks = 0
        for inc in include:
            base = root / inc
            if not base.exists():
                continue
            for f in base.rglob("*.md"):
                rel = f.relative_to(root).as_posix()
                if any(rel == ex or rel.startswith(ex.rstrip("/") + "/") for ex in exclude):
                    continue
                seen.add(rel)
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if rel in known and abs(known[rel] - mtime) < 1e-6:
                    continue
                try:
                    md = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                chunks = chunk_markdown(md, chunk_chars=chunk_chars)
                self.con.execute("DELETE FROM chunks WHERE path=?", (rel,))
                self.con.executemany(
                    "INSERT INTO chunks(path, heading, body, ngram) VALUES (?,?,?,?)",
                    [(rel, h, b, ngram_text(f"{Path(rel).stem} {h} {b}")) for h, b in chunks],
                )
                self.con.execute("INSERT OR REPLACE INTO files(path, mtime, chunks) VALUES (?,?,?)",
                                 (rel, mtime, len(chunks)))
                n_chunks += len(chunks)
                if rel in known:
                    updated += 1
                else:
                    added += 1
        for rel in set(known) - seen:
            self.con.execute("DELETE FROM chunks WHERE path=?", (rel,))
            self.con.execute("DELETE FROM files WHERE path=?", (rel,))
            removed += 1
        self.con.commit()
        if full:
            # FTS5 は DELETE 後も索引セグメントに旧語が残る(1.2 GB の YouTube 分を消しても 520 MB 残った実測)。
            # optimize でセグメントを併合してから VACUUM でファイルを縮める。
            self.con.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
            self.con.commit()
            self.con.execute("VACUUM")
        total_files = self.con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        total_chunks = self.con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"added": added, "updated": updated, "removed": removed, "chunks_written": n_chunks,
                "files": total_files, "chunks": total_chunks,
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    # ---- 検索 ---------------------------------------------------------------
    def search(self, text: str, *, k: int = 5, terms: list[str] | None = None) -> list[Hit]:
        """発話テキストから検索語を作り、bm25 で上位 k 件。AND で足りなければ OR に落とす。"""
        terms = terms if terms is not None else query_terms(text)
        if not terms:
            return []
        phrases = ['"' + ngram_text(t).replace('"', "") + '"' for t in terms]
        for joiner in (" AND ", " OR "):
            q = joiner.join(phrases)
            try:
                rows = self.con.execute(
                    "SELECT path, heading, body, bm25(chunks) AS r FROM chunks WHERE chunks MATCH ? "
                    "ORDER BY r LIMIT ?",
                    (q, k * 4),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                # bigram は「シーム」で「シームレス」も拾う。語そのものを含むチャンクを前に出す
                # (bm25 は負値で小さいほど良い。完全一致 1 語につき 3.0 のボーナス)。
                hits = []
                for p, h, b, r in rows:
                    exact = sum(1 for t in terms if t in b or t in h or t.lower() in b.lower())
                    hits.append(Hit(p, h, b, float(r) - 3.0 * exact))
                hits.sort(key=lambda x: x.rank)
                return hits[:k]
        return []

    def stats(self) -> dict:
        f = self.con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        c = self.con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {"files": f, "chunks": c, "bytes": size}

    def close(self) -> None:
        self.con.close()

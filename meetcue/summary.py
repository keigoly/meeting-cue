"""会議後サマリ — セッション記録から summary.md を作る(FR-9)。任意で Vault に保存する(Q8)。

- 決定的な部分(Q&A 一覧・★ の候補・文字起こし)はコードで組む。
- 要約・決定・宿題は LLM(privacy=local か鍵なしなら省略し、その旨を書く)。
- Vault 保存は `01_Projects/Meeting Cue/Sessions/<日付>_<id>.md`。privacy=local のときは題名と日付だけ。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .cues import llm as llm_client
from .cues import openrouter

SUMMARY_SYSTEM = "あなたは会議の記録係です。与えられた文字起こしから、日本語で簡潔に要約します。事実に無いことは書きません。"
SUMMARY_USER = """# 文字起こし(古い → 新しい・[channel] は話者の出所。mic=私、system/room=相手)
{transcript}

# 出力(Markdown・見出しはこの 3 つだけ)
## 要約
- 3〜6 行
## 決定事項
- 無ければ「(なし)」
## 宿題・次の一手
- 誰が・何を。無ければ「(なし)」"""


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def build_summary(session_dir: Path, *, api_key: str | None, model: str, privacy: str,
                  llm: bool = True, target: llm_client.Target | None = None) -> Path:
    """target(設定画面の接続先・2026-09-26)があればその fast モデルで要約する。無ければ従来どおり OpenRouter の model。"""
    d = Path(session_dir)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
    transcript = _read_jsonl(d / "transcript.jsonl")
    cues = _read_jsonl(d / "cues.jsonl")
    judgments = {j["rid"]: j for j in _read_jsonl(d / "judgments.jsonl")}
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime((meta.get("started_ms") or 0) / 1000))
    dur_min = round(((meta.get("ended_ms") or int(time.time() * 1000)) - (meta.get("started_ms") or 0)) / 60000, 1)

    lines = [f"# Meeting Cue! セッション {started}", "",
             f"- id: `{meta.get('id', d.name)}` / mode: {meta.get('mode')} / privacy: {meta.get('privacy')} / "
             f"約 {dur_min} 分 / 発話 {len(transcript)} 件", ""]

    # LLM 要約
    text_for_llm = "\n".join(f"[{t['channel']}] {t['text']}" for t in transcript)
    if llm and api_key and privacy != "local" and text_for_llm.strip():
        msgs = [{"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": SUMMARY_USER.format(transcript=text_for_llm[-12000:])}]
        if target is not None:
            r = llm_client.stream(target, msgs, role="fast", max_tokens=900)
        else:
            r = openrouter.stream_chat(msgs, api_key=api_key, model=model, max_tokens=900, provider_order=["Anthropic"])
        if r.ok:
            lines += [r.text.strip(), "", f"<!-- summary: {r.model} {r.ms_total}ms ${r.cost_usd} -->", ""]
        else:
            lines += ["## 要約", f"- (LLM 要約に失敗: {r.error})", ""]
    else:
        lines += ["## 要約", "- (LLM 要約なし: " + ("privacy=local" if privacy == "local" else "鍵なし / 無効") + ")", ""]

    # Q&A(キューが出た発話)
    qa = []
    by_rid: dict[str, dict] = {}
    for c in cues:
        if c.get("kind") == "cues":
            by_rid.setdefault(c["rid"], {}).update({c.get("part") or "all": c})
    for t in transcript:
        rec = by_rid.get(t["rid"])
        if not rec:
            continue
        j = judgments.get(t["rid"], {}).get("summary", {})
        block = [f"### {t['text']}", f"- 判定: {j.get('speech_act')} / 意図 {j.get('intent')}"]
        for part_key in ("answers", "all"):
            c = rec.get(part_key)
            if not c:
                continue
            if c.get("intent"):
                block.append(f"- 意図の読み: {c['intent']}")
            top = _top(c, "answer")
            for a in top:
                block.append(f"- ★回答: **{a.get('title', '')}** — {a.get('body', '')}" +
                             (f"(根拠: {a['source']})" if a.get("source") and a["source"] != "なし" else ""))
        for part_key in ("counters", "all"):
            c = rec.get(part_key)
            if not c:
                continue
            for q in _top(c, "counter"):
                block.append(f"- ★逆質問: {q.get('question', '')}" + (f" — {q['aim']}" if q.get("aim") else ""))
        qa.append("\n".join(block))
    lines += ["## 質問とキュー", ""] + (qa or ["(キューが出た発話はありません)"]) + [""]

    # 文字起こし
    lines += ["## 文字起こし", ""] + [f"- [{t['channel']}] {t['text']}" for t in transcript] + [""]
    out = d / "summary.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _top(c: dict, kind: str, n: int = 3) -> list[dict]:
    items = c.get("answers" if kind == "answer" else "counters") or []
    ranking = (c.get("ranking") or {}).get(kind) or []
    if ranking:
        order = [r["index"] for r in ranking if r.get("total") is not None][:n]
        picked = [items[i - 1] for i in order if 0 < i <= len(items)]
        if picked:
            return picked
    return items[:n]


def save_to_vault(summary_path: Path, vault_root: Path, *, privacy: str, session_id: str) -> Path:
    dest_dir = Path(vault_root) / "01_Projects" / "Meeting Cue" / "Sessions"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M")
    dest = dest_dir / f"{stamp}_{session_id}.md"
    if privacy == "local":
        dest.write_text(f"# Meeting Cue! セッション {stamp}(機微モード)\n\n本文は `~/.meeting-cue/sessions/` にのみ保存。\n",
                        encoding="utf-8")
    else:
        dest.write_text(summary_path.read_text(encoding="utf-8"), encoding="utf-8")
    return dest

# Meeting Cue! — Claude Code への指示

## 開発フロー

1. 変更は Step 1(見える化: まず記録で問題の所在を数値で確かめる)→ Step 2(原因の箇所だけを最小限に直す)→ Step 3(周辺の整合: 設定例・README・要件・試験を合わせる)の順。具体は `DEVELOPMENT.md` §4。
2. 新しいブランチは専用の worktree で作ることを勧める。

## 禁止事項

- 発話ログ・Vault 本文・索引(`~/.meeting-cue/`・`spikes/logs/`)を repo にコミットする。
- API キーや個人の情報を repo・ログ・画面に出す(キーは Mac のキーチェーンか `~/.secrets/meeting-cue.env`)。
- `privacy = "local"` のときにクラウドへ HTTP を出す経路を足す(縮退の約束を壊す)。
- 依存パッケージを増やす(stdlib のみ。増やすなら REQUIREMENTS.md §7 を直してから)。

## このリポジトリ固有の指示

- **設計の正本は `docs/REQUIREMENTS.md`**。要件が変わるときは先に §4/§5 を直す。
- 起動と検証: README の手順。実会議の前に `python -m meetcue.cli doctor --online`。再現は合成音声(`say -v Kyoko` + `[[slnc ms]]`)。
- 計測の型: 全段 `rid` 付き JSONL(`meetcue/metrics.py`)。新しい段を足したら `phase` を 1 つ決めて `report` に載せる。
- STT ヘルパーの契約(stdout JSONL・stdin finalize/quit)は Mac / Windows 共通。ヘルパーを変えるときは契約を変えない。
- 罠: STT は節の途中にも句点を打つ(句点だけで切らない)/ 日本語は自動 final が出ない(finalize を送る)/ OpenRouter は `provider.order=["Anthropic"]` を外すと初トークンが 3 秒台になる / Jev の `to_me` は 0.5 前後に寄る。

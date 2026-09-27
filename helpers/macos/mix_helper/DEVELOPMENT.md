# helpers/macos/mix_helper/ — DEVELOPMENT.md

## 1. このディレクトリの役割

`mix-helper`: 記録の「録音を書き出す…」(一覧の右クリック・2026-09-26)で、相手(system)と自分(mic)の音声を頭合わせして 1 本の `.m4a` に重ねる Swift の小さな道具。AVFoundation だけ(追加依存なし)。呼ぶのは `meetcue/library.py` の `export_session`。

- 使い方: `mix-helper --out <出力.m4a> --in <入力.m4a>@<開始のずれ 秒> [--in …]`(ずれ = `meta.json` の `audio.<ch>.t0_ms` の差。画面の再生と同じ頭合わせ)
- 結果: stdout に JSON 1 行 `{"ok": true, "ms": …, "duration_ms": …, "inputs": n}` / `{"ok": false, "error": "…"}`、終了コード 0 / 1
- ビルド: `cd helpers/macos && make`(バイナリは git に入れない)

## 2. 現在の問題点(2026-09-26)

- 2 本を足し合わせるだけで音量の調整はしない(相手と自分が同時に大きな声だと割れる可能性)。実会議の書き出しで気になったら AVAudioMix で下げる。
- 入力は録音と同じ 16 kHz モノラル AAC。出力も同じ形式(書き出しの音質は録音の音質を超えない)。

## 3. バグ修正時の手順(user CLAUDE.md の Step 1〜3)

1. **見える化**: 同じ記録で `mix-helper` を手で実行し、stdout の JSON(`error`・`duration_ms`)と `afinfo 出力.m4a`(トラック数・長さ)を見る。アプリ側は端末ログ `~/.meeting-cue/logs/app-*.log` の `session_op op=export`(所要・成否)。
2. **最小改修**: 重ね方・形式は `main.swift` だけ。ずれの計算や呼び出しは `library.py` の `export_session`。
3. **周辺整合**: 契約(引数・JSON)を変えたら `library.py` と `tests/test_session_ops.py`(偽の mix-helper)を合わせる。

## 4. 関連ドキュメント

- ルートの [DEVELOPMENT.md](../../../DEVELOPMENT.md)・設計 [docs/REQUIREMENTS.md](../../../docs/REQUIREMENTS.md)(FR-9 記録の書き出し・移動・削除)

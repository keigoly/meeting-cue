# Phase 0 スパイク結果(2026-09-25)

環境: MacBook Air M5 24 GB・macOS 27.0・Swift 6.4・Python 3.12(uv)・OpenRouter(既存の鍵を `~/.secrets/meeting-cue.env` に写して使用)。
記録: `spikes/logs/*.jsonl`(発話本文と答えを含む=較正用。公開 repo には入れない)。

## 総括

| # | 対象 | 判定 | 要点 |
|---|---|---|---|
| a | Jev の日本語判定 | **GO** | 10 件中 speech_act 10/10 正解・p50 255 ms・1 判定 $0.00005。`to_me` は自分宛の質問で 0.53〜0.64、無関係で ≤ 0.17 → 閾値 0.5 |
| b | Vault FTS5 索引 | **GO** | YouTube 除外: 1,183 ファイル / 14,977 チャンク・構築 13.5 s・DB 72 MB・検索 p50 0.7 ms。YouTube 込み: 構築 560 s・DB 1.24 GB・検索 p50 4.3 ms / p90 36 ms・コメント単位の雑音 → **既定除外** |
| c | OpenRouter 経由 Claude | **GO(条件付き)** | `provider.order=["Anthropic"]` が必須。Sonnet 5 初トークン 1.23 s(AWS 経由だと 3.28 s)・Haiku 4.5 0.96 s。書式(回答 3 + 逆質問 3)は両方とも通る |
| d | SpeechAnalyzer(ファイル供給) | **GO** | 25.4 s の合成音声で partial 追従 0 ms・140 partial・全文正しく確定(SIEM→「シーム」は TTS 由来)。asset 準備 89 ms |
| e | E2E(Phase 1 パイプライン) | **GO** | 合成会議(質問 3 件)で 3/3 検出・発話確定→最初のキュー行 **p50 2.33 s / p90 2.87 s**(予算 2.5 / 4.0 s)。judge 230 ms / retrieve 3 ms / generate 総計 7.5 s(ストリーミング)。初回は句点での早切りで質問が割れた(修正済み・下記) |

## a. Jev(`spikes/spike_jev.py`・`logs/jev_20260925_200044.jsonl`)

| # | 発話 | 期待 | speech_act(p) | to_me | intent | ms |
|---|---|---|---|---|---|---|
| 1 | この構成でGlobalProtectの分割トンネルはどう扱いますか。 | question/自分宛 | question(1.0) | 0.62 | how | 218 |
| 2 | 導入にかかる費用はどのくらいでしょうか。 | question/自分宛 | question(1.0) | 0.56 | cost | 271 |
| 3 | その仕組みは既存のSIEMと競合するリスクはありませんか。 | question/自分宛 | question(1.0) | 0.64 | risk | 239 |
| 4 | 事例があれば教えてください。 | request/自分宛 | request(0.78) | 0.57 | experience | 224 |
| 5 | 本日は当社のAIエージェント基盤についてお話しします。 | statement | statement(0.78) | 0.07 | other | 279 |
| 6 | なるほど、承知しました。 | agreement | agreement(1.0) | 0.07 | — | 259 |
| 7 | 今日の議題は三つあります。… | statement | statement(0.96) | 0.10 | other | 255 |
| 8 | それでは次の議題に移りましょう。 | smalltalk | smalltalk(0.91) | 0.11 | other | 253 |
| 9 | 皆さんは普段どのツールで監視されていますか。(登壇者が聴衆へ) | question/自分宛でない | question(1.0) | 0.49 | experience | 258 |
| 10 | いつ頃までに回答をいただけますか。 | question/自分宛 | question(0.92) | 0.55 | schedule | 231 |

- `to_me` は確率が中庸(0.5 前後)に寄る。閾値 0.6 だと 7/10、**0.5 で 10/10**。件数が 10 件なので Phase 2 で 30 件の手ラベルで較正する(既定 0.5)。
- ヒューリスティック(縮退経路)は speech_act 7/10(依頼を質問と誤り、相づちの複合文を statement と誤る → 後者は修正済み)。
- 費用: 10 判定 $0.00047。1 時間 1,000 発話でも $0.05。

## b. 索引(`spikes/spike_index.py`)

- 対象: `01_Projects` / `02_Ideas` / `03_Resources` / `04_Context`(ログ類と仕事のフォルダを除外)。YouTube(1,877 本・203 MB)は別計測(`logs/index_with_youtube.txt`)。
- 方式: 見出し単位チャンク(≤ 800 字)+ 文字 bigram を FTS5 unicode61 で索引。trigram トークナイザは 2 文字語(費用・構成)が引けないため不採用。
- 検索の当たりは「分割トンネル」「GlobalProtect」「Syncthing」のような固有名詞・複合語で良好、「費用」「リスク」のような一般語はポッドキャストの雑音を拾う → Phase 2 で分野判定による絞り込みと埋め込み再ランクを検討。

## c. LLM(`spikes/spike_llm.py`・`logs/llm_20260925_*.jsonl`)

| model | provider | 初トークン | 最初の行 | 総計 | in/out tok | 費用 |
|---|---|---|---|---|---|---|
| anthropic/claude-sonnet-5 | Claude Platform on AWS(自動) | 3,283 ms | 4,897 ms | 11.2 s | 2,002 / 564 | $0.0096 |
| anthropic/claude-sonnet-5 | **Anthropic(order 指定)** | **1,231 ms** | 2,107 ms | 8.9 s | 2,002 / 563 | $0.0096 |
| anthropic/claude-haiku-4.5 | Amazon Bedrock(自動) | 1,276 ms | 1,864 ms | 5.9 s | 1,672 / 486 | $0.0041 |
| anthropic/claude-haiku-4.5 | **Anthropic(order 指定)** | **957 ms** | 1,502 ms | 6.5 s | 1,672 / 541 | $0.0044 |

- 既定は Sonnet 5 + `provider.order=["Anthropic"]`(config)。速さ優先なら `--fast`(Haiku)。
- 出力は行書式(意図 / 回答 1〜3 / 逆質問 1〜3)で、両モデルとも 3+3 件が揃った。Vault の根拠が薄いときは「(一般論)」と明示された。

## d. STT(`helpers/macos/stt_helper --file`・`logs/stt_file_run1.jsonl`)

- 25.44 s の合成音声(`say -v Kyoko`)を実時間供給。partial 140 回・lag 0 ms・final 1 回(日本語は自動 final が出ない = セグメンターが finalize を送る前提を再確認)。
- 認識は全文正しい(「SIEM」は TTS が「シーム」と読むため文字も「シーム」)。

## e. E2E(`meetcue run --source file:tests/fixtures/meeting_ja.aiff:system`)

| run | session | 発話 | TRIGGER / 生成 | e2e 最初のキュー p50 / p90 | 所見 |
|---|---|---|---|---|---|
| 1 | `20260925_200850_19513ba3` | 10 | 4 / 3(1 打ち切り) | 2,584 / 2,616 ms | 句点だけで速く切ったため質問 1 が「…の文化。」「トンネルは…」に割れた |
| 2 | `20260925_201249_cab700a1` | 11 | 3 / 3 | 2,296 / 2,701 ms | 句点ルールを直しても割れる。原因は file/tap の partial が約 1 秒周期のバースト(ギャップ p50 976 / max 1,000 ms)で、500〜900 ms のしきい値がチャンクの切れ目で発火していた |
| 3 | `20260925_201600_7fb0715f` | 8 | 3 / 3 | **2,327 / 2,867 ms** | tap/file 用しきい値 1,200 / 1,600 / 1,100 ms(`[segmenter_tap]`)で 3 質問とも 1 発話に確定 |

- 内訳(run 3): judge p50 230 ms + 生成の初トークン 1.3〜1.9 s + 最初の行の完成。生成総計 p50 7.5 s(ストリーミングで逐次表示)。
- 修正 2 点: (1) 「用言で終わり、かつ句点付き」のときだけ速い経路(`segmenter.py`)(2) 音源の種類でしきい値を分ける(マイク `[segmenter]` / tap・file `[segmenter_tap]`・`pipeline.py`)。`segment` イベントに `gap_p50_ms` / `gap_max_ms` / `partials` を載せ、以後はログから cadence が読める。
- 費用: 3 run で生成 9 件 約 $0.08 + 判定 29 件 $0.0015。

## f. 選ぶ係(Jev セレクター・2026-09-25 追加)

keigoly様 提示の Vault ノート「しゃべらない最新AI『Jev』はガチの議論に使えることが分かった」(候補 5 本から Jev が基準別採点で選ぶと 27 勝 13 敗)を組み込んだ。

| run | session | 構成 | 生成 / 打ち切り | 生成総時間 p50 | select p50 | e2e 最初のキュー p50 |
|---|---|---|---|---|---|---|
| 4 | `20260925_202848_cbdc2e7f` | 回答 5 + 逆質問 5 を 1 ストリーム | 1 / 2 | 10.0 s | 228 ms | 2.70 s |
| 5 | `20260925_203102_d9046b1a` | **回答 5 と逆質問 5 を別ストリームで並行** | 6 / 0 | **7.1 s** | **249 ms** | 2.62 s |

- 採点は各候補の行が完成した時点で並行して走らせるため、生成完了から並べ替え表示まで **約 250 ms**。候補 10 本の採点費用は約 $0.0005。
- run 4 は 1 ストリームで 10 本を生成すると 10〜12 s かかり、次の質問が来て打ち切られた(3 問中 2 問)。回答と逆質問を別リクエストで並行にして 7 s に短縮し、打ち切り 0 に。打ち切られた場合も採点済みの候補だけで途中順位を出す。
- Jev の `score` は段階の添字スケール(3 段階なら 0〜2)で返る → 0〜1 に正規化して重み付き合計(回答は最大約 4.2・逆質問は約 3.0)。
- 費用: 1 質問あたり生成 2 本 約 $0.017 + 判定 $0.00005 + 採点 $0.0005 ≈ **$0.018**(1 時間 30 質問で $0.54。予算 $0.5 の目安をわずかに超えるため、`--fast`(Haiku)なら約 $0.008)。

## g. Phase 3 UX の通し(2026-09-25・overlay + hotkey + summary)

`meetcue run --ui overlay --hotkey-emit "pause@18,deepdive@27,pause@29" --save-vault`(session `20260925_204338_2f3c6503`)

- 最前面パネル(`overlay_shown` frame 980×560・level screenSaver)にブラウザと同じ画面が出る。左に文字起こし(判定行付き)、右にキューのカード(★ 付き・⧉ でコピー・ナレッジのチップ)。スクリーンショットで目視確認(個人情報を含むため repo には置かない)。
- ⌃⌥P 相当の自動発火で **一時停止 → 質問 2 は判定されず** → ⌃⌥D の深掘りで「直近の質問らしい発話」(質問 2)を拾って生成(一時停止中でも効く)→ ⌃⌥P で再開 → 質問 3 は通常どおり。
- 深掘りの生成中に質問 3 が来て回答ストリームが打ち切られたが、採点済み 4 本で「途中まで」の順位を表示できた。
- 終了時に `summary.md`(Haiku 4.5 の要約・決定・宿題 + 質問と ★ キュー + 文字起こし)を生成し、Vault `01_Projects/Meeting Cue/Sessions/` に保存(テスト分は削除済み)。
- 発見: (1) hotkey の `pause` は toggle にしないと 2 回目で再開しない(修正済み)(2) 「まず」が単独の発話になる → 3 字以下の談話標識は次へ繰り越す(修正済み)(3) bigram 検索が「シーム」で「シームレス」を拾う → 語そのものを含むチャンクを前に出す再ランクを追加(4) 起動直前の音量変更で tap の `device_start` が 82 s 停滞(run1)。音量を触らなければ 749 ms。

## 申し送り(Phase 1 → 2)

1. `to_me` の閾値較正(30 件の手ラベル)。登壇者モードでは「聴衆への問いかけ」を除外できるか。
2. E2E 2.5 s 予算に対し p50 2.3〜2.6 s。候補: `--fast`(Haiku)既定化・プロンプト短縮・回答 1 を意図より先に出す。
5. 選ぶ係の重み(mode / intent)は仮置き。実会議で「★ が付いた候補を実際に使ったか」を記録して較正する。
3. 検索の雑音(一般語)。分野判定(`topic`)で絞る・埋め込み再ランク。
4. 実マイク・実 Zoom(process tap)での通し試験(TCC 許可を含む)。

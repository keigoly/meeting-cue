# eval/ — DEVELOPMENT.md

## 1. このディレクトリの役割

実際の音声(YouTube の模擬面接・一人語り)を流したセッション記録を、**字幕から作った正解表**と突き合わせて採点する回帰の物差し。区切り・判定・生成を直したら、同じ音声で前後を比べる(2026-09-26 のセグメンター修正で使った道具を残したもの)。

| ファイル | 役割 |
|---|---|
| `score.py` | 質問ごとの 検出 / 起動 / 最初のキューまでの ms / 生成の成否、誤起動の一覧、区切りの健全性、`act_min` × `to_me_min` の掃引 |
| `monologue.py` | 質問のない一人語り用: 起動の一覧(= 誤起動の候補)、to_me の分布、1 回の確定が抱えた音声の長さ(区切りの遅れ)、マイクへの回り込み |
| `gt/*.json` | 正解表(`items`: n / 字幕の時刻 / kind = question・ambiguous / 質問の文面) |

公開版には第三者の動画の書き起こし(`gt/iroots_*.json`)を含めない。使うときは `fetch_audio.sh` で音声を取り、`captions.py` の下書きから自分で正解表を作る。合成会議の `gt/fixture_meeting_ja.json` は同梱。
| `captions.py` | YouTube の字幕を無音で区切って表示する(正解表の下書き用) |
| `fetch_audio.sh` | 評価用の音声を `~/.meeting-cue/eval/audio/` に取り直す(repo・Vault には入れない) |
| `mic_cer.py` | 自分の声(マイク)の文字起こしを読み上げ原稿と比べ、文字誤り率(CER)と固有名詞の取れ具合を出す(2026-09-26)。原稿・語彙は `~/.meeting-cue/eval/reading/`(個人の固有名詞を含むので repo に入れない) |

## 2. 使い方

```sh
eval/fetch_audio.sh                                   # 初回だけ(iroots 模擬面接・Jev 解説の冒頭 5 分)
A=~/.meeting-cue/eval/audio
uv run --python 3.12 --no-project python -m meetcue.cli run --source file:$A/qp8xasjyQig.m4a:system \
    --mode participant --ui terminal --no-hotkeys --no-summary          # 14 分・実時間・生成あり(約 $0.3)
python3 eval/score.py latest eval/gt/iroots_qp8xasjyQig.json

uv run --python 3.12 --no-project python -m meetcue.cli run --source file:$A/jev_5min.m4a:system \
    --ui terminal --no-hotkeys --no-llm --no-summary                    # 5 分・判定だけ
python3 eval/monologue.py latest
```

### 自分の声の文字起こし精度(2026-09-26 Step 1)

読み上げ原稿 `~/.meeting-cue/eval/reading/script.txt` をアプリで録音し(題名「読み上げ比較」)、同じ `mic.m4a` を方式を変えて文字にして比べる。

```sh
SP=<scratch>; R=~/.meeting-cue/eval/reading; M=~/.meeting-cue/sessions/<記録>/audio/mic.m4a
swiftc -parse-as-library -O spikes/spike_stt_compare.swift -o $SP/stt-compare
for m in st-fast st dict; do $SP/stt-compare --file $M --mode $m > $SP/$m.jsonl; done     # 録音後にまとめて(方式違い)
# 録音中の区切りの再現: file 音源の mic チャネルは [segmenter_self] を使う(2026-09-27 から。それ以前は [segmenter_tap])。
# 旧設定を再現するなら config の [segmenter_self] に 500/900/300 を入れて流す
MEETCUE_HOME=$SP/emu uv run --python 3.12 --no-project python -m meetcue.cli --config $SP/emu/config.toml run \
    --source file:$M:mic --privacy local --no-llm --no-summary --no-record --no-hotkeys --ui terminal
python3 eval/mic_cer.py --ref $R/script.txt --terms $R/terms.txt \
    --hyp live=~/.meeting-cue/sessions/<記録>/transcript.jsonl --hyp st-fast=$SP/st-fast.jsonl ...
```

分かっていること(テスト3 の実音声・正解なしの目視): 同じエンジン・同じ設定でも、録音後にまとめて文字にすると録音中より大幅に良い。録音中の自分の声は 0.3〜0.9 s の間で区切るため文の途中で切れ、文脈を失う(再現でも同じ細切れになる)。相手側と同じ 1.1〜1.6 s にすると録音後に近づく。`st`(fastResults なし)は合成音声で 2 回とも同じ箇所の文字が抜け、`dict`(DictationTranscriber)は長い自然な話し方で半分以上が抜けた。語彙(contextualStrings)は英字でもカタカナでもほぼ効かなかった。

読み上げ(2026-09-27・keigoly様 の声・91 s・原稿 409 字)の基準値。区切りの再現は回ごとに揺れる(同時に動かす処理の数で部分結果の間隔が変わり、切る位置がずれる)ので、1 回で決めず複数回の平均で見る。「以外」= 固有名詞以外の日本語の CER(区切りの良し悪しはここで見る)。

| 方式 | CER | 以外 | 固有名詞の範囲 | 固有名詞 17 語 |
|---|---|---|---|---|
| 旧: マイク用 500/900/300 ms(録音中の実物 + 再現 5 回) | 25.4〜30.8%(平均 28.6%) | 12.2〜16.3%(平均 14.9%) | 59〜69% | 6〜7 |
| 新: `[segmenter_self]` 1,200/1,600/1,100 ms(再現 6 回) | 24.2〜27.9%(平均 26.4%) | 12.6〜13.6%(平均 13.2%) | 51〜65% | 7〜9 |
| 録音後にまとめて `st-fast`(2 回・語彙ありも同じ) | 24.0% | 10.5% | 58% | 8 |
| 録音後にまとめて `st` | 24.4% | — | — | 8 |
| 録音後にまとめて `dict` / `dict-atyp` / 語彙あり | 28.9% | — | — | 6 |

実機の確認(2026-09-27・新しい設定で録音 80 s・原稿からの言い換えを含む): 録音中 33.7%(以外 20.4%)。同じ音声を再現すると新 30.6〜31.3%(以外 17.7〜20.1%)・旧 39.1〜43.8%(以外 26.2〜33.3%)・録音後にまとめて 31.3%(以外 17.3%)。録音ごとの数字は読み方(言い換え)で動くので、設定の良し悪しは同じ音声の再現で比べる。

残る誤りは固有名詞(人名・学科名・業界の略語が一般の語に化ける・Claude → クロール・Meeting Cue → ミーティング級)と、原稿と違う言い回しで読んだ箇所(「しています」→「しております」など。CER に数えている)。

基準値(2026-09-26・ファイル再生): iroots は取りこぼし 0〜1/17・誤起動 2〜3/20・最初のキュー p50 2.4〜2.6 s。Jev 冒頭 5 分は誤起動 0・1 回の確定が抱える音声 p50 9.1 s / max 19.9 s(修正前 21.5 s / 37 s)。

## 3. 現在の問題点

- 対応づけは文字 bigram の重なり(0.45 以上)。STT の誤変換が多い発話は対応しないことがある(「対応しなかった発話」の一覧で確かめる)。
- ファイル再生は tap と音の取り込み口が違う(区切り・判定・生成は同じ)。tap 特有の問題(無音・音量)は実再生で確かめる。
- Jev の to_me は同じ文でも 0.49〜0.51 と揺れる。1 回の結果で閾値を決めない(2 回以上の掃引を見る)。
- 正解表は字幕の文面の抜き書き。GitHub 公開(Phase 5)の前に扱いを見直す。

## 4. バグ修正時の手順(user CLAUDE.md の Step 1〜3)

1. **見える化**: 採点がおかしいときは、まず「対応しなかった system 発話」と各質問の「対応した発話」を読み、対応づけ(bigram)の問題か記録の問題かを分ける。
2. **最小改修**: 対応づけの問題は `score.py` の `MATCH_MIN` / `norm` だけ。正解表の誤りは `gt/*.json` を直す(時刻は字幕の開始時刻)。
3. **周辺整合**: 物差しを変えたら §2 の基準値を取り直して書き換える。

## 5. 関連ドキュメント

- ルートの [DEVELOPMENT.md](../DEVELOPMENT.md)(§4 Step 1 の見える化)・設計 [docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md)(§10 Phase 2 の合格条件)

# Windows の日本語 STT の比較(Step 1・2026-09-27)

Windows 版の STT ヘルパー(`helpers/windows/stt_helper`・契約は FR-2 のまま)に使う音声認識を選ぶための計測。**ここでは実装しない**(方式を選んでいただいてから Step 2)。

> **決定(2026-09-27 keigoly様)**: A(large-v3-turbo・区切り 0.3 s)。音声認識のパッケージは STT ヘルパー専用の仮想環境にだけ入れる。VAD は sherpa-onnx を足さず、faster-whisper に同梱の Silero(`silero_vad_v6.onnx`)を 1 窓ずつ呼ぶ(一括処理との差 0.0・1 窓 0.12 ms を確認)。反映先は REQUIREMENTS FR-12 / FR-13 / §7。

- 環境: Windows 11 Home(10.0.26200)・Ryzen 7 3800X(8 コア 16 スレッド)・64 GB・RTX 4060 Ti 8 GB(ドライバ 617.14)・Python 3.12・sherpa-onnx 1.13.8・faster-whisper 1.2.1(CTranslate2 4.8.2・cuBLAS 12.9・cuDNN 9.26)。
- 計測中もデスクトップ常駐のアプリが GPU を 35〜45 %・約 1.9 GB 使っていた。GPU は各計測の読み込み前 3 s の平均との差で示す。
- 記録: `spikes/logs/win_stt_*.jsonl`(git 外・40 本)。実行係 `spikes/spike_win_stt.py`・SAPI `spikes/spike_win_stt_sapi.ps1`・採点 `spikes/spike_win_stt_score.py`。
- 本体の依存は増やしていない(`pyproject.toml` は無変更)。計測用のパッケージは repo の外の仮想環境にだけ入れた(§再現)。

## 総括

| # | 候補 | 判定 | 要点 |
|---|---|---|---|
| A | faster-whisper **large-v3-turbo**(CUDA・float16)+ Silero VAD | **推奨** | CER **1.1 %**(2 回とも)・句読点と疑問符が付く・質問 3 問とも無傷。final は話し終わりから **p50 0.73 s / p90 0.90 s**。partial の初出 0.94 s・約 0.6 s ごと。GPU 2.4 GB・計算の占有 37 %。2 チャネル同時でも CER 同じ・final p50 1.0 s。依存が大きい(約 3.7 GB)・NVIDIA の GPU が要る |
| B | faster-whisper **kotoba-whisper v2.0**(日本語の蒸留版・CUDA) | 次点 | 区切り 0.3 s なら CER 1.3 %・A より 3 割軽い。ただし 20 s の区間では文を丸ごと落とす(区切り 0.5 s で long 17 %)・partial が最大 6 s 止まる・句読点が付いたり付かなかったりする |
| C | **Windows 内蔵**(SAPI の日本語認識・オフライン) | 縮退の候補 | **依存ゼロ**・CPU 8 %・反応は最速(partial 初出 0.49 s・0.25 s ごと)。CER 8.2 %(2 回とも同じ)。句読点を出さない・文末の「す」を落とす・固有名詞に弱い(シーム→チーム)。finalize を受ける手段がない |
| D | sherpa-onnx **ReazonSpeech**(CPU・int8)+ Silero VAD | 縮退の候補 | 公式どおり前後に 0.9 s の無音を足すと CER 6.5 %(足さないと 26〜42 %)。CPU だけで final p50 0.65 s。句読点を出さない・文の間が 0.3 s 未満で続くと先頭の文を落とす(statement 24 %) |
| E | sherpa-onnx 多言語ストリーミング Zipformer(CPU) | 不採用 | 唯一の真のストリーミングで 1 回 40 ms と軽いが、文の途中を丸ごと落とす(CER 63 %・質問 2 が消える) |
| — | A / B を CPU(int8)で | 不採用 | 認識 1 回 8.7 s。final が 22 s 以上遅れ、実時間に追いつかない(GPU の無い PC では A / B は使えない) |
| — | WinRT のディクテーション(`Windows.Media.SpeechRecognition`) | 対象外 | 認識がクラウド(LOCAL の約束に反する)。入力もマイクだけでファイルを流せない |

**推奨: A(large-v3-turbo・区切り 0.3 s)で Step 2 に進む。** 精度・句読点(セグメンターの句点の速い経路がそのまま働く)・製品名を英字で返す(`Global Protect`。Vault の検索に有利)の 3 点で他を上回り、2 チャネル同時でも収まる。GPU の無い PC(公開後の利用者)向けの縮退は C か D を後で選ぶ(Step 2 の必須ではない)。

## 測り方

- 素材: `tests/fixtures/wav/` の 3 本(meeting 37 s・statement 10 s・long 60 s・計 11 発話)。0.1 s ずつ実時間で供給し(実マイクと同じく、チャンクの音声が終わる時刻に渡す)、末尾に 1.5 s の無音を足す。
- 話し始め・話し終わりの正解: WAV の音量(20 ms ごとに -50 dBFS 超を声、1.0 s 未満の間はつないで 1 発話)。
- 指標:
  - **最初の partial**: 話し始めから、その発話の音声を含む最初の結果が出るまで。
  - **partial の間隔**: 発話の間の partial どうしの間(文字が変わったときだけ出すので、止まった時間も含む)。
  - **final**: 話し終わりから、その終わりを含む final が出るまで(各エンジン自身の区切り)。
  - **CER**: final をつないだ文字列と正解(`.txt`)の編集距離 ÷ 正解の文字数。NFKC・漢数字→算用数字・句読点と空白を除く。**補正後**は `Global Protect`→グローバルプロテクト・`SIEM`→シームもそろえる(正解は読み上げの音で書いてあるため)。
  - **認識 1 回** / **計算の占有**: `decode_ms` と、その合計 ÷ 音声の長さ(SAPI は内部なので測れない)。
- 擬似ストリーミング(A・B・D): Silero VAD が話し始めを見つけたら、区間の頭から今までを 0.3 s ごとに認識し直して partial(Whisper は greedy)。VAD が区間を閉じたら区間全体を認識して final(Whisper は beam 5)。区間は最長 20 s で切る。
- 区切り(区間を閉じる無音): 全候補共通の 0.5 s と、文の間(0.12〜0.46 s)で切れる 0.3 s の 2 通り。E は 0.5 s で語の途中で切れたため既定の 1.2 s。C は SAPI 内部の既定(0.15 / 0.5 s)。
- 各 1 回(r1)。有力候補(A・B・C)は 2 回目(r2)も流し、ばらつきを確かめた(値はほぼ同じ)。

## 結果(主な構成・全素材の合計)

| 候補 | 最初の partial ms p50 / p90 | partial の間隔 ms p50 / p90 / 最大 | final ms p50 / p90 | CER 補正後(補正前) | 認識 1 回 ms partial / final | 計算の占有 | CPU %(1 コア = 100)平均 / 最大 | GPU 増分 % / メモリ MiB | 句読点 |
|---|---|---|---|---|---|---|---|---|---|
| A turbo・区切り 0.3 s | 943 / 1,049 | 610 / 779 / 1,890 | **728 / 901** | **1.1 %**(4.0 %) | 235 / 248 | 37 % | 40 / 106 | +4 / 2,377 | あり |
| A turbo・区切り 0.5 s | 945 / 978 | 613 / 811 / 1,823 | 914 / 1,114 | **0.7 %**(3.6 %) | 249 / 252 | 37 % | 43 / 106 | +6 / 2,375 | あり |
| A turbo・2 チャネル同時(meeting) | 1,198 / 1,354 | 716 / 917 / 1,420 | 1,013 / 1,228 | 0.7 %(10.4 %) | 390 / 549 | 44 % | 49 / 103 | +20 / 4,730(2 本) | あり |
| B kotoba・区切り 0.3 s | 963 / 1,013 | 601 / 773 / **6,152** | 699 / 780 | 1.3 %(1.3 %) | 208 / 230 | 27 % | 38 / 121 | +22 / 2,322 | 一部 |
| B kotoba・区切り 0.5 s | 889 / 954 | 598 / 1,673 / 6,032 | 857 / 1,009 | **10.2 %** | 206 / 233 | 24 % | 38 / 100 | +25 / 2,276 | 一部 |
| C SAPI | **491 / 508** | **246 / 290** / 762 | 946 / 1,099 | 8.2 %(8.2 %) | — | — | **8** / 76 | —(CPU のみ) | なし |
| D reazon・0.3 s・無音 0.9 s | 787 / 861 | 517 / 775 / 4,337 | 645 / 824 | 6.5 %(6.5 %) | 142 / 200 | 27 % | 181 / 964 | —(CPU のみ) | なし |
| D reazon・区切り 0.5 s・無音なし | 738 / 783 | 522 / 1,041 / 6,597 | 829 / 979 | 41.9 % | 139 / 170 | 25 % | 187 / 731 | —(CPU のみ) | なし |
| E 多言語ストリーミング | 576 / 4,536(取れず 2) | 305 / 598 / 4,801 | 2,021 / 11,698(出ず 3) | 62.8 % | 41 / 40 | 6 % | 133 / 275 | —(CPU のみ) | なし |
| A turbo・CPU int8(meeting) | 9,335(取れず 6) | — | 22,556 / 36,375 | 0.7 % | 8,693 / 9,064 | 196 % | 396 / 419 | — | あり |

素材ごとの内訳と全 16 構成の数値は §付録。

### 合成会議の質問 3 問が文面どおり取れたか(meeting・final)

| 候補 | 質問 1(分割トンネル) | 質問 2(費用) | 質問 3(シームと競合) |
|---|---|---|---|
| A turbo | ○(`Global Protect`) | ○ | ○ |
| B kotoba | ○ | ○ | ○ |
| C SAPI | △「まず確認**できず**が…」 | ○ | △「既存の**チーム**と…」 |
| D reazon(無音 0.9 s) | ○ | ○ | △「既存の**チーム**と…」 |
| E 多言語ストリーミング | △「まず確認ですが」が消える | × 消える | △ 後半のみ |

## 所見

### A. large-v3-turbo(推奨)

- 全素材でほぼ全文正解。誤りは long の「社内→車内」「各回→次回」程度。句読点・疑問符(`?`)が付き、セグメンターの文末の分割と「句点付きの文末で速く確定」がそのまま働く。
- 区切り 0.3 s にすると final が 0.2 s 早まり(p50 0.91 → 0.73 s)、1 つの final が抱える音声も最長 20 s → 13 s に縮む(Mac で問題の「話し続けると確定が遅れる」を VAD 側で抑えられる)。CER はほぼ変わらない。
- partial の初出は 0.9 s 前後(VAD が声を見つけてから 0.3 s 後に最初の認識、1 回 0.24 s)。更新は約 0.6 s ごと。反応だけなら C に劣る。
- partial で同じ句のくり返し(Whisper の幻覚)が、区切り 0.3 s の 2 回で partial 274 回中 2 回(0.5 s では 132 回中 0 回)。final では 0 回。
- 2 チャネル同時(両方が同時に話し続ける最も重い条件)では、1 回の認識が 0.24 → 0.4〜0.55 s に延び、final p50 1.0 s / p90 1.2 s・partial の初出 1.2 s。GPU メモリは 2 本で +4.7 GB(他のアプリの分と合わせて 6.7 / 8 GB)。実会議では両方が同時に話し続けることは少ないが、VRAM の余裕は小さい。

### B. kotoba-whisper v2.0

- 短い区間では A と同等以上(meeting・statement は誤りほぼゼロ)で、計算は A の 7 割。
- 20 s の区間(区切り 0.5 s)では「次に、来月の移行作業についてです」など文を丸ごと落とす(long の CER 17 %)。蒸留版は長い区間に弱い。
- partial が最大 6 s 止まる(区間の先頭の文だけを返し続ける)。句読点が付かない区間がある(セグメンターの速い経路が働かない)。

### C. Windows 内蔵(SAPI・`MS-1041-80-DESK`)

- 追加の依存ゼロ(Windows PowerShell 5.1 + `System.Speech`)。要るのは日本語の音声認識エンジン(この機械には入っていた。無い PC では言語の設定で音声認識を足す)。CPU 8 %・メモリ約 200 MB。
- partial は 0.25 s ごとで最も滑らか。ただし結果の書き換えが多い(「改善」→「改選期」→「改善」)。
- 句読点を出さない。文末の「です」の「す」を落とすことが多い(「以上で」「採用計画で」)。long では「件です→県立」「各回→学会」「以上です→異常で」など。
- finalize(強制確定)を受ける API がない。確定は SAPI 内部の無音(0.5 s)だけで決まる。

### D. sherpa-onnx ReazonSpeech

- このモデルは、1 回に渡す音声の頭に無音が無いと先頭の句を落とす(公式ライブラリは前後に 0.9 s を足している)。足すと CER 41.9 % → 6.5 %。
- それでも、文の間が 0.3 s 未満で続く statement では先頭の文を落とす(区切り 0.3 s でも VAD が切れない)。句読点を出さない。
- GPU 不要・依存なし(sherpa-onnx は Python の依存ゼロ・C API の DLL 同梱)・モデル 160 MB(int8)。GPU の無い PC の縮退としては C より精度が高い。

### E. sherpa-onnx 多言語ストリーミング

- 反応と計算は最良(1 回 40 ms・占有 6 %)だが、日本語で文の途中を丸ごと落とす。区切りを使わず全体を 1 回で認識しても同じ部分が消えるため、モデル自体の弱さ。

## 契約(FR-2)との対応(Step 2 の見込み)

| 項目 | A / B / D(VAD + 再認識) | C(SAPI) |
|---|---|---|
| `ready` / `bye` | そのまま | そのまま(計測の `.ps1` が既に契約の形で出す) |
| `partial` | 0.3 s ごとの再認識で文字が変わったら | `SpeechHypothesized` |
| `final` | VAD が区間を閉じたとき(無音 0.3 s) | `SpeechRecognized`(内部の無音 0.5 s) |
| stdin `finalize` | 今の区間をその場で認識して final・VAD を戻す(受けられる) | 受ける手段がない(最後の partial を final として出すしかない) |
| `start_s`(話し始め) | VAD の区間の始まり(32 ms 単位) | final だけ `AudioPosition` が取れる |
| 音量(stderr `phase=level`)・録音 | エンジンと独立(ヘルパーが入力から計算) | 同じ |

- セグメンターのしきい値: A の partial は約 0.6 s ごと(最大 1.9 s)なので、マイク用の短いしきい値(0.3〜0.9 s)だと partial の間で finalize が出る。Windows はヘルパーの final(VAD)を主にし、セグメンターは tap 用のしきい値(1.2〜1.6 s)を使う見込み。Step 2 で合成会議の通しを見て決める。
- 1 チャネル 1 プロセスのまま(Mac と同じ)なら、A はモデルを 2 回読む(VRAM 2.4 GB × 2)。1 プロセスで 2 チャネルを扱えば 1 回で済むが、契約(1 プロセス = 1 チャネル)を変えずにやるなら 2 プロセスのまま。

## 依存と配布(REQUIREMENTS §7 に関わる)

| | A / B | C | D |
|---|---|---|---|
| Python パッケージ | faster-whisper・ctranslate2・onnxruntime・av・tokenizers・huggingface-hub・numpy・nvidia-cublas-cu12・nvidia-cudnn-cu12(+ VAD) | なし | sherpa-onnx(依存なし) |
| 容量 | パッケージ約 2.1 GB(cuBLAS 0.7・cuDNN 1.1)+ モデル 1.5〜1.6 GB | 0 | 28 MB + モデル 160 MB |
| GPU | NVIDIA 必須(CPU では実時間に届かない) | 不要 | 不要 |
| ライセンス | MIT(faster-whisper・CTranslate2・両モデル)。CUDA の DLL は NVIDIA の配布条件(利用者が pip で取る形にする) | Windows 同梱 | Apache-2.0(sherpa-onnx・モデル) |

- 本体(`meetcue`)は標準ライブラリのまま、**ヘルパーだけ別の仮想環境**で動かす形にすれば `pyproject.toml` の依存は増えない(Mac の Swift ヘルパーが別バイナリなのと同じ位置付け)。それでも §7 に「Windows の STT ヘルパーは別環境で faster-whisper 等を使う」と書き足してから入れる。
- VAD は計測では sherpa-onnx の Silero を使った(28 MB・依存なし)。A を選ぶ場合も、VAD だけ sherpa-onnx を使うか、faster-whisper 同梱の Silero(onnxruntime)を逐次用に包むかを Step 2 で決める。

## 選んでいただきたいこと

1. **方式**: A(推奨)/ B / C / D
2. **依存の入れ方**: ヘルパー専用の仮想環境に入れる(本体は標準ライブラリのまま)でよいか。よければ先に §7 を直す
3. **GPU の無い PC への縮退**(公開後): C(依存ゼロ)か D(精度が上)を後で選ぶ。Step 2 では作らない

## 申し送り(Step 2 へ)

- 合成音声は無音がほぼ完全な 0 で、VAD に有利。実マイクと WASAPI のループバックで partial の間隔・VAD の区切り・幻覚を測り直す。
- 2 チャネル同時の VRAM(6.7 / 8 GB)。他に GPU を使うアプリ(ゲーム・動画処理)と重なると足りなくなる恐れ。partial の再認識を 0.3 → 0.5 s に延ばす・マイク側だけ軽いモデルにする、が逃げ道。
- Whisper の幻覚対策: 無音は VAD で送らない・final は beam 5 のまま。partial の幻覚は画面に一瞬出うる。

## 再現

計測用の仮想環境とモデルは repo の外(以下 `<spike>`)に置く。

```
uv venv --python 3.12 <spike>/.venv
uv pip install --python <spike>/.venv/Scripts/python.exe sherpa-onnx==1.13.8 faster-whisper==1.2.1 ^
    nvidia-cublas-cu12==12.9.2.10 nvidia-cudnn-cu12==9.26.0.51 psutil numpy
# モデル: github.com/k2-fsa/sherpa-onnx の release「asr-models」から silero_vad.onnx・
#   sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01・sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10 を <spike>/models/ へ。
#   Hugging Face の mobiuslabsgmbh/faster-whisper-large-v3-turbo・kotoba-tech/kotoba-whisper-v2.0-faster を HF_HOME=<spike>/hf へ。
set MEETCUE_SPIKE_HOME=<spike>
<spike>/.venv/Scripts/python.exe spikes/spike_win_stt.py --engine whisper-turbo --min-silence 0.3 --wav tests/fixtures/wav/meeting_ja.wav
<spike>/.venv/Scripts/python.exe spikes/spike_win_stt.py --engine sherpa-reazon --min-silence 0.3 --pad-s 0.9 --wav tests/fixtures/wav/long_ja.wav
<spike>/.venv/Scripts/python.exe spikes/spike_win_stt.py --engine sapi --wav tests/fixtures/wav/statement_ja.wav
python spikes/spike_win_stt_score.py spikes/logs/win_stt_*.jsonl --texts
```

- Whisper は計測中 `HF_HUB_OFFLINE=1`(ネットへ出ない)。SAPI の `.ps1` は単体でも動く(契約の形の JSONL を stdout に出す)。
- ログ名の印: `r1` / `r2` = 1 回目 / 2 回目・`ms03` = 区切り 0.3 s・`pad09` = 前後に 0.9 s の無音・`cpuint8` = CPU・`dual*` = 2 本同時。

## 付録: 全構成の数値

### 全素材の合計

| 構成 | 最初の partial ms p50 / p90 | partial の間隔 ms p50 / p90 / 最大 | final ms p50 / p90 | CER 補正後(補正前) | 認識 1 回 ms(partial / final) | 計算の占有 | CPU % 平均 / 最大 | GPU 増分 % 平均 / メモリ MiB | RSS MiB |
|---|---|---|---|---|---|---|---|---|---|
| `sapi[r1]` | 491 / 508 | 246 / 290 / 762 | 946 / 1,099 | 8.2%(8.2%) | — / — | — | 8 / 76 | -1 / 182 | 214 |
| `sapi[r2]` | 480 / 506 | 247 / 281 / 760 | 955 / 1,098 | 8.2%(8.2%) | — / — | — | 7 / 81 | -2 / 128 | 216 |
| `sherpa-reazon[r1]` | 738 / 783 | 522 / 1,041 / 6,597 | 829 / 979 | 41.9%(41.9%) | 139 / 170 | 25% | 187 / 731 | -2 / 38 | 534 |
| `sherpa-reazon[r1ms03]` | 726 / 781 | 434 / 821 / 4,021 | 594 / 666 | 26.3%(26.3%) | 92 / 130 | 22% | 161 / 898 | 21 / 79 | 461 |
| `sherpa-reazon[r1ms03pad09]` | 787 / 861 | 517 / 775 / 4,337 | 645 / 824 | 6.5%(6.5%) | 142 / 200 | 27% | 181 / 964 | 13 / 69 | 472 |
| `sherpa-stream[r1]` | 576 / 4,536(取れず 2) | 305 / 598 / 4,801 | 2,021 / 11,698(出ず 3) | 62.8%(62.8%) | 41 / 40 | 6% | 133 / 275 | 13 / 31 | 486 |
| `whisper-kotoba[r1]` | 889 / 954 | 598 / 1,673 / 6,032 | 857 / 1,009 | 10.2%(10.2%) | 206 / 233 | 24% | 38 / 100 | 25 / 2,276 | 792 |
| `whisper-kotoba[r1ms03]` | 963 / 1,013 | 601 / 773 / 6,152 | 699 / 780 | 1.3%(1.3%) | 208 / 230 | 27% | 38 / 121 | 22 / 2,322 | 788 |
| `whisper-kotoba[r2ms03]` | 927 / 1,098 | 595 / 622 / 6,050 | 685 / 739 | 1.3%(1.3%) | 204 / 222 | 27% | 37 / 91 | 20 / 2,242 | 789 |
| `whisper-kotoba[cpuint8ms03]`(meeting) | 9,349(取れず 6) | — | 24,130 / 37,574 | 0.7%(0.7%) | 8,708 / 9,189 | 199% | 395 / 422 | -3 / 20 | 1,146 |
| `whisper-turbo[r1]` | 945 / 978 | 613 / 811 / 1,823 | 914 / 1,114 | 0.7%(3.6%) | 249 / 252 | 37% | 43 / 106 | 6 / 2,375 | 790 |
| `whisper-turbo[r1ms03]` | 943 / 1,049 | 610 / 779 / 1,890 | 728 / 901 | 1.1%(4.0%) | 235 / 248 | 37% | 40 / 106 | 4 / 2,377 | 788 |
| `whisper-turbo[r2ms03]` | 872 / 1,091 | 605 / 892 / 1,818 | 753 / 831 | 1.1%(4.0%) | 227 / 242 | 37% | 39 / 100 | -2 / 2,405 | 789 |
| `whisper-turbo[dualmic]`(meeting) | 1,198 / 1,354 | 716 / 917 / 1,420 | 1,013 / 1,228 | 0.7%(10.4%) | 390 / 549 | 44% | 49 / 103 | 20 / 4,730 | 782 |
| `whisper-turbo[dualsystem]`(meeting) | 1,203 / 1,351 | 731 / 920 / 1,444 | 1,019 / 1,197 | 0.7%(10.4%) | 426 / 555 | 44% | 48 / 103 | 20 / 4,730 | 781 |
| `whisper-turbo[cpuint8ms03]`(meeting) | 9,335(取れず 6) | — | 22,556 / 36,375 | 0.7%(10.4%) | 8,693 / 9,064 | 196% | 396 / 419 | 22 / 0 | 1,197 |

### 素材ごとの CER(補正後)

| 構成 | meeting(134 字) | statement(50 字) | long(265 字) |
|---|---|---|---|
| A turbo・0.3 s(r1 / r2) | 0.7 % / 0.7 % | 0.0 % / 0.0 % | 1.5 % / 1.5 % |
| A turbo・0.5 s | 0.7 % | 0.0 % | 0.8 % |
| B kotoba・0.3 s(r1 / r2) | 0.7 % / 0.7 % | 0.0 % / 0.0 % | 1.9 % / 1.9 % |
| B kotoba・0.5 s | 0.7 % | 0.0 % | 17.0 % |
| C SAPI(r1 / r2) | 5.2 % / 5.2 % | 6.0 % / 6.0 % | 10.2 % / 10.2 % |
| D reazon・0.3 s・無音 0.9 s | 3.7 % | 24.0 % | 4.5 % |
| D reazon・0.5 s・無音なし | 23.9 % | 24.0 % | 54.3 % |
| E 多言語ストリーミング | 38.8 % | 46.0 % | 78.1 % |

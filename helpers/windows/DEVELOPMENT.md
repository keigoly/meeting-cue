# helpers/windows — DEVELOPMENT.md

## 1. このディレクトリの役割

Windows 版のヘルパーの置き場。Mac の `helpers/macos/`(Swift)と同じ位置付けで、本体(`meetcue`)とは **標準入出力の契約だけ** でつながる。

| ディレクトリ | 役割 | 状態 |
|---|---|---|
| `stt_helper/` | 音声認識(faster-whisper large-v3-turbo・CUDA + Silero VAD)と取り込み(PyAudioWPatch で WASAPI のマイク / 出力機器のループバック)。契約は docs/REQUIREMENTS.md FR-2(stdout JSONL `ready` / `partial` / `final` / `bye`・stdin `finalize` / `quit`・stderr の診断と `phase=level`)。`--record` で m4a を保存 | 2026-09-27 ファイル・マイク・ループバックまで |
| `window_helper/` | 本体の画面を pywebview(WebView2)のウィンドウに出す(Mac の overlay_helper に当たる)。アプリ用の仮想環境 `~/.meeting-cue/app-venv` で動く(`requirements.txt`)。画面の `window.webkit.messageHandlers.meetcue.postMessage` の写しを差し込み、`cmd` top / main を受ける。メニュー「表示 → ライブ字幕 / 常に手前」。stdin `top on|off` / `caption` / `quit` | 2026-09-27 |
| `mix_helper/` | 書き出しで 2 本の音声を 1 本の m4a に重ねる(Mac の mix-helper と同じ約束・PyAV)。STT の仮想環境で動く | 2026-09-27 |
| (次の版)トレイ・ホットキー | タスクトレイ常駐・システム全体のホットキー・字幕の固定(クリックを下へ通す) | 未着手 |

- `stt_helper.py` は **専用の仮想環境**(`~/.meeting-cue/stt-venv`・`packaging/windows/setup.ps1` が作る)の python で動く。入れるものは `stt_helper/requirements.txt`(版を固定)。本体からは import しない。
- モデルは `~/.meeting-cue/models/faster-whisper-large-v3-turbo`(セットアップで固定した版を取る)。会議中は `HF_HUB_OFFLINE=1` でネットへ出ない。
- 本体は `meetcue/pipeline.py` の `Source.argv`(Windows の分岐)で `[<専用環境の python>, stt_helper.py, --locale, --channel, ...]` を起動する。python の場所は config の `stt_python` で変えられる。

## 2. 現在の問題点(2026-09-27)

- 取り込み(2026-09-27): 本体の `mic` はマイク、`tap-all` / `tap:NAME` / `tap-pid:N` はどれも **出力機器のループバック**(再生中の音をまとめて。WASAPI のループバックは機器単位なので、Mac の `tap:zoom` のように会議アプリだけを取ることはできない・`phase=loopback_scope` で知らせる)。機器は既定か、config の `loopback_device` / `mic_device`(名前の一部・`--list-devices` で一覧)。機器の元の形式(44.1 / 48 kHz・2〜8 ch)で取り、平均して 1 ch → PyAV で 16 kHz。
- ループバックは何も鳴っていない間データを渡さないので、壁時計に合わせて無音を足す(`capture_done` の `filled_s`)。音声の時間軸は最初のデータが届いた時刻から始まる(録音の `record_start` の `t0_ms` も同じ頭)。
- 実測(2026-09-27・この機械): 音の出ない仮想ドライバ(SYNCROOM)に合成会議を流してループバックで取り、7 文とも正しく文字にした(話し終わりから final まで 0.5〜0.8 s・再生後に無音 2.7 s を足した)。本体の通し(`--source tap-all`・簡易判定)で質問 3/3・誤起動 0・話し終わり → 発話の確定 p50 0.63 s。既定のマイク(48 kHz 2 ch)も開けて、音量の診断が 0.1 s ごとに出る。実際の会議アプリ・実際の声では未試験。
- マイクは Windows の設定(プライバシー → マイク → デスクトップ アプリにアクセスを許可)がオフだと無音になる恐れ(未確認)。
- 専用の仮想環境の外の python で動かされたら `phase=abort reason=packages_missing`(終了コード 7)。機器を開けなければ `capture_open_failed`(6)。
- NVIDIA の GPU が要る(CPU では実時間に届かない)。1 チャネル VRAM 約 2.4 GB。2 チャネル同時で他のアプリの分と合わせて 8 GB の GPU の 8 割を使う(`spikes/WINDOWS_STT.md`)。
- partial の初出は話し始めから約 0.9 s、更新は約 0.6 s ごと(Mac のマイクより遅い)。セグメンターのマイク用しきい値(0.3〜0.9 s)だと partial の間で finalize を送ることがあるが、ヘルパーは VAD でも final を出すので確定は遅れない。
- VAD は faster-whisper の内部(`faster_whisper.vad.get_vad_model().session`)を直接呼ぶ。faster-whisper の版を上げたら、1 窓ずつの値が一括処理と一致するかを確かめ直す。
- **stdin の `finalize` は、声が続いていれば次の短い切れ目(無音 0.10 s)まで待ってから閉じる**(最長 1.5 s・診断 `phase=finalize` の `how` = `now` / `gap` / `deadline` と `wait_ms`)。Whisper は語の終わりを先回りして書くため、話している最中でも partial の文字が止まり、セグメンターが間と取り違えて `finalize` を送る(2026-09-27 合成会議: 「…よくわかりま」の途中で切れ、残りから「わかりました。」が重複)。上限はセグメンターの `safety_ms`(最短 2.5 s)に収まる値。実測: long では 4 回とも文の終わりで閉じた(待ち 0.3〜1.7 s)。1.5 s 以上切れ目の無い話の途中では期限で閉じ、語の途中で切れる(meeting で 4 回中 2 回)。改善案: 期限のときは直近 1 s で最も静かな窓で切る(実マイクの計測の後に判断)。
- 本体を Windows の既定の文字コード(cp932)で動かすと、ターミナル表示と一部の試験が落ちる(本体側の既存の問題・CI は `PYTHONUTF8=1` で回している)。Windows の起動の入口では python を UTF-8 モード(`-X utf8`)で起動する(Step 3)。

## 3. バグ修正時の手順(user CLAUDE.md の Step 1〜3)

1. **見える化**: ヘルパーを単体で動かし、stdout(契約)と stderr(診断)を分けて見る。
   `~/.meeting-cue/stt-venv/Scripts/python.exe helpers/windows/stt_helper/stt_helper.py --file tests/fixtures/wav/meeting_ja.wav --channel system`
   取り込みは `--list-devices` で機器を見て、`--loopback all --loopback-device <名前>` / `--mic-device <名前>` で開く(`capture_start` に機器・Hz・ch)。スピーカーを鳴らさずに試すときは、音の出ない出力機器(仮想のオーディオドライバなど)に音声を流してそのループバックを取る。
   本体から動かしたときは、セッションの `metrics.jsonl` の `helper_*`(`asset_ready` の読み込み ms・`abort` の理由・`results_done` の件数)と `phase=segment` を突き合わせる。精度と遅れは `spikes/spike_win_stt_score.py` の物差しで比べる。
2. **最小改修**: 区切りは `--min-silence` / `--max-speech` / `--partial-every`、認識は `Streamer.recognize` だけを直す。契約(出す JSON の形・stdin のコマンド)は変えない(Mac と共通)。
3. **周辺整合**: 引数を足したら `meetcue/pipeline.py` の `Source.argv`(Windows の分岐)・`tests/test_windows_stt_helper.py`・このファイル・REQUIREMENTS FR-12 を合わせる。パッケージの版を変えたら `requirements.txt` と §7。

## 4. 関連ドキュメント

- 設計: [docs/REQUIREMENTS.md](../../docs/REQUIREMENTS.md)(FR-2 契約・FR-12 Windows・FR-13 公開・§7 依存の方針)
- 選定の計測: [spikes/WINDOWS_STT.md](../../spikes/WINDOWS_STT.md)
- セットアップ: [packaging/windows/DEVELOPMENT.md](../../packaging/windows/DEVELOPMENT.md)
- Mac の同じ役割: `helpers/macos/stt_helper/main.swift`

# Meeting Cue! — DEVELOPMENT.md

## 1. このディレクトリの役割

会議・講演・オンライン会議の音声をリアルタイムに文字起こしし、相手の発言が「自分への質問」だと Jev で判定したら、Obsidian Vault の知識と意図の読みを添えて回答候補・逆質問候補を即座に出す道具(keigoly様 個人用 → 将来 GitHub 公開)。設計の正本は [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)。


## 3. 現在の問題点(2026-09-25 時点)

- 要件定義は 2026-09-25 に確定(§12 は仮置きを採用)。次は Phase 2(実会議での運用試験と較正)。起動は `packaging/`(`Meeting Cue!.app`)から。
- 実マイクは未試験。相手の声は `tap-all` で取る(`tap:NAME` は GUI 本体 1 プロセスだけを取るため、Chrome/Meet は helper から音が出て無音の恐れ)。`tap-all` は 2026-09-25 に合成音声で GO。
- **話し続ける音声で発話の確定が最大 59 秒遅れる**(2026-09-26・session `20260926_003417`・Jev 解説動画 4 分)。セグメンターは partial の停止(tap 1.2〜1.6 s)でしか finalize を送らず、約 1 秒ごとに更新が続くと 4 分で 2 回しか発火しない。残りは SpeechAnalyzer 自身の final(30〜60 s ごと)頼み。`max_chars` は確定後の分割だけ。e2e_first_cue は「確定から」なのでこの遅れが見えない。誤起動は 0/43(to_me 最大 0.4)。
- 録音後の再生(U2・2026-09-26): 文字起こしの時刻は音声認識の確定(最大約 20 s)単位でしか来ないため、1 つの確定から分けた文の開始は文字数の割合で見積もっている(画面側)。音源が 2 本ともファイルの試験では、短い方が終わった時点で録音全体が止まる(従来からの仕様・実音源では起きない)。U2 以前の記録には音声が無い。
- 本番で「別の機器で再生した音声」は tap に入らない(Mac 自身の出力だけ)。tap に無音が届いても helper は音量を記録しないため、記録から直接は判別できない(10 s ごとの「あ」が目印)。スピーカー再生だと相手の声がマイクに回り込む(46 件中 42 件)。
- E2E(発話確定 → 最初のキュー行)が p50 2.58 s で予算 2.5 s をわずかに超える(spikes/PHASE0_RESULTS.md §e)。
- Jev の `to_me` は 10 件でしか較正していない(閾値 0.5 は仮)。
- Vault 検索は一般語(費用・リスク)で雑音を拾う。
- 実マイク・実 Zoom(process tap)は未試験(tap はファイル再生の経路で GO・`tap_create status=0`)。
- Windows(2026-09-27・初版): STT ヘルパー(faster-whisper large-v3-turbo)・取り込み(PyAudioWPatch)・ウィンドウ(pywebview)・キーの保管(資格情報マネージャー)・書き出し・セットアップとスタートメニュー(`packaging/windows/`)。タスクトレイ・ホットキー・自動更新・字幕の固定・Google Drive は次の版。画面の一部の文言が Mac 向けのまま(HTML は共通)。本体は Windows の既定の文字コード(cp932)だと落ちるので UTF-8 モードで動かす(起動の入口は対応済み)。詳細は `helpers/windows/DEVELOPMENT.md`・`packaging/windows/DEVELOPMENT.md`。
- 起動直前に音量を変えると Core Audio の再構成で tap の `device_start` が 80 秒以上待たされる(2026-09-25 実測・run1)。会議アプリと音量を整えてから起動する。
- 秘密は `~/.secrets/meeting-cue.env`。索引 `~/.meeting-cue/index/vault.sqlite` は Vault 本文の複製なので repo に出さない。

## 4. バグ修正時の手順(user CLAUDE.md 準拠・一気に直さない)

### Step 1: 調査とログ追加(見える化)
- まず `~/.meeting-cue/sessions/<session>/metrics.jsonl` を読む。全段が `rid`(発話 id)・`phase`・`ms`・`ok`・`error` を持つ。`python -m meetcue.cli report <session>` で judge / retrieve / generate の p50/p90 と `e2e_first_cue` が出る。
- セグメンターの切り所は `phase=segment`(`tail_kind` / `forced` / `dropped`)と `pause_finalize`(`idle_ms` / `threshold_ms`)を突き合わせる。STT 側は `helper_*`(Swift の stderr 診断)。
- Jev の判定は `judgments.jsonl`(`summary` の確率)。誤起動・取りこぼしは発話本文と並べて手ラベルを付ける。
- 再現は合成音声で: `say -v Kyoko -o tests/fixtures/x.aiff "…[[slnc 1500]]…"` → `meetcue run --source file:tests/fixtures/x.aiff:system`。
- 結果を確認してから Step 2 へ。

### Step 2: 原因箇所のみ最小限の改修
- 段の境界を越えて直さない: `segmenter.py`(切り所)/ `judge/`(判定・閾値・`selector.py` の物差しと重み)/ `knowledge/index.py`(検索語・索引)/ `cues/prompts.py`(書式・文面)/ `pipeline.py`(結線・打ち切り・操作・音量の振り分けと無音警告・質問タブの起動条件と優先制御 `_plan_blocked` / `_after_answers`)/ `app.py`(常駐・開始/停止・API・右クリックの操作 `_session_op`)+ `library.py`(記録の一覧・詳細・題名・書き出し・Google Drive へ移動・ゴミ箱。置き場は Mac + Drive の並び)/ `ui/web.py` + `ui/static/app.html`(本体のウィンドウ)・`index.html`(旧・半透明パネル)/ `summary.py`(会議後サマリ)/ `replacements.py`(置き換え辞書: 確定と途中の文を正しい語へ。Pipeline・ライブ字幕・⚙ の API が同じファイルを読む)/ `captions.py`(ライブ字幕: 録音していない間の字幕だけの動き `CaptionRunner` と翻訳 `Translator`)+ `ui/static/caption.html`(字幕の画面)/ `secrets.py`(API キー: キーチェーン → 環境変数 → `~/.secrets`・キーを画面やログに出さない)+ `providers.py`(接続先とキーの「確認」)+ `cues/llm.py`(接続先ごとの生成・役割 main / fast / deep・料金の計算)+ `cues/anthropic_direct.py`・`cues/openai_direct.py`(stdlib の SSE クライアント。契約は openrouter.StreamResult と同じ)+ `cues/claude_cli.py`(開発者向けの Claude サブスク: 本人の Claude Code を温めて使い捨てる Pool。`[llm] subscription_cli` で有効)+ `app.py` の `settings_view` / `_keys_api`(設定画面・FR-10b)/ `helpers/macos/*`(Swift ヘルパー。契約は stdout JSONL・stdin quit。音量は stderr `phase=level`。`mix_helper` は書き出し用で stdout に結果 JSON 1 行)/ `packaging/`(起動ラッパー)。
- しきい値は `config.toml` で変える(コードの既定値は実測の根拠をコメントで残す)。
- 冪等性: 発話は `rid` で一意。同じ発話に二重生成しない。新しい質問が来たら古い生成は `cancelled` で打ち切る(失敗に数えない)。
- 結果を確認してから Step 3 へ。

### Step 3: 周辺の整合性確認と改修
- `config.example.toml`・README・REQUIREMENTS.md の該当節・`tests/` を合わせる。ホットキーを変えたら README と docs/USAGE.md の表と `hotkey_helper` / `overlay_helper` の両方(衝突しないよう ⌃⌥ + P/D/M は hotkey、L/H/R は overlay)。
- 動作確認(合成音声 → 実会議)が終わるまで旧経路は残す。

## 5. 開発の作法

- Jev / OpenRouter クライアントは stdlib の小さな実装(`judge/jev.py` / `cues/openrouter.py`)。
- 依存は増やさない(Python は stdlib のみ・テストは pytest)。ヘルパーは `helpers/macos && make`。
- 公開前提: 個人パス・鍵・Vault 本文・発話ログ(`spikes/logs`)を repo に入れない。`config.example.toml` に PII を書かない。

## 6. 関連ドキュメント

- 設計: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)・実測: [spikes/PHASE0_RESULTS.md](spikes/PHASE0_RESULTS.md)
- 回帰の物差し: [eval/DEVELOPMENT.md](eval/DEVELOPMENT.md)(YouTube の模擬面接・一人語りを流した記録を字幕の正解表で採点。区切り・判定を直したら前後を比べる)

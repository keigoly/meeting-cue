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
- Vault 検索は一般語(費用・リスク)で雑音を拾う。深掘りと用語は 2026-10-08 から専門語らしい語(英字・3 字以上)だけで引く(一般語を入れると的外れのノートが上に来た)が、`youtube` のような英字の一般語はまだ雑音を拾う。
- 既定の生成モデルは 2026-10-08 から **Claude Haiku 5.5**(回答・質問タブ・用語・サマリ・翻訳)。Sonnet 5.5 / Opus 5.5 は押したときの深掘り。Claude Code(サブスク)は Haiku 5.5 の思考を切れないので、速い役は `--effort low` + 「すぐ書き始める」一文(依頼文と起動時の system の両方)で初トークン 0.54〜0.64 s。どちらかを外すと 4〜11 s に戻る(`spikes/PHASE0_RESULTS.md` §h)。用語のノートの抜粋を長くしても(300 字)考え込んで 4.5 s 台になる。
- Obsidian の MCP(FR-5b): クライアントは stdlib(`meetcue/knowledge/mcp.py`)。既定の型 mcp-obsidian は Obsidian の Local REST API が動いていないと「MCP サーバーにはつながるが検索は失敗」になる(「接続を確認」が直し方を出す)。失敗は索引だけで続ける。
- 音声認識が崩した固有名詞(GlobalProtect → 「グロールプラテクト」)は用語タブにそのまま出ることがある(説明で誤表記と補う)。置き換え辞書で直す。
- 実マイク・実 Zoom(process tap)は未試験(tap はファイル再生の経路で GO・`tap_create status=0`)。
- Windows(2026-09-27・初版): STT ヘルパー(faster-whisper large-v3-turbo)・取り込み(PyAudioWPatch)・ウィンドウ(pywebview)・キーの保管(資格情報マネージャー)・書き出し・セットアップとスタートメニュー(`packaging/windows/`)。タスクトレイ・ホットキー・自動更新・字幕の固定・Google Drive は次の版。画面の文言は 2026-09-28 に両方の OS に通じる書き方へ(「このパソコン」「OS の鍵の保管庫(Mac はキーチェーン・Windows は資格情報マネージャー)」・HTML は共通のまま)。本体は Windows の既定の文字コード(cp932)だと落ちるので UTF-8 モードで動かす(起動の入口は対応済み)。詳細は `helpers/windows/DEVELOPMENT.md`・`packaging/windows/DEVELOPMENT.md`。
- **録音後の文字起こしし直し**(2026-10-16・FR-9b): 実会議(Windows・18.7 分)で自分の発言が細切れになり、1 割の行に Whisper の幻覚の句が入った(CER 36.6%)。Windows は止めた後に録音を 1 本ずつ丸ごと認識し直し(`meetcue/redo.py` → STT ヘルパーの `--redo`)、`transcript.redo.jsonl` に差し替える(18.6%・幻覚 0。参照も機械の書き起こしなので下限は 13.5〜16.6%・`spikes/REDO_TRANSCRIBE.md`)。会議中の判定・回答候補は会議中の文のまま(会議中の幻覚と断片は次の修正 B・C)。Mac は未対応(会議中の文のまま)。し直しの間(18.7 分の記録で 42〜50 s)は次の録音を始められない。短い返事がまばらな窓は temperature の後退(乱数)で認識されるので、し直すたびにその窓の文が変わり、まれに繰り返しが暴走する(「はい、」× 20・6 回に 1 回)。差し替える文を作るところ(`redo._finish_rows`)で 1 回に縮める(元の文は `raw`)。
- **会議中の文字起こしを Soniox で**(2026-10-18・FR-2b): 「録音を開始」で会議ごとに選ぶ(既定はこのパソコン・LOCAL では選べない)。取り込みと録音は STT ヘルパーのまま、`--pcm-out` で同じ音を本体へも渡し、`meetcue/soniox_stt.py` が Soniox へ流して契約の partial / final に直す(確定はセグメンターの finalize)。届かないとき(400〜403 はすぐ・ほかは 45 s つなぎ直せないとき)はチャネルごとに端末内の認識へ切り替え、出せていなかった区間を端末内の final で補う。Mac と Windows 実機で通しを確認済み(合成音声・記者会見・違うキーでの切り替え・Windows は録音と止めた後の仕上げも)。音を送らない間は keepalive を送る(音も keepalive も無いと Soniox は約 21 s で 408 で閉じる。Windows のループバックは無音だと音を渡さない)。Mac の tap-helper の `--pcm-out` も実音で確認(10-18)。費用は記録の `soniox_done` に目安を残すだけで、台帳(月の上限)にはまだ数えない。
- **音声入力に Aqua Voice の機能をそろえた**(2026-10-19・FR-14b): キーバインドの設定(有効化 1〜3 個・Fn など修飾キーだけ・押したまま Space でハンズフリー・ハンズフリーで始める・最後の文字起こしを貼り付け・Esc で取り消し)・有効化の方式(インスタント / リアルタイム)・送信コマンドと自動 Enter・言語・辞書・置換(定型文)・カスタム指示(AI・既定オフ)・編集モード(AI・既定オフ)・ディープコンテキスト(既定オフ)・カジュアル・履歴・ネットワーク・ステータス・音・フローティングバー・ほかの音を消す・クリップボード履歴・マイクの選択・プライバシーモード。アプリが起動している間は裏で待ち受ける(`dictation_app.py`)。**実際のキーを押す確認と、貼り付け・音・バー・消音・マイクのテストの実機の確認はまだ**(試験は合成音声・ふだん使わないキー(F16〜F19)・`--no-paste --no-sound --no-bar` で通した)。リアルタイムは話の間ごとに finalize する(Soniox は確定が約 5 s 遅れるため)。別の担当の読み直しで 15 件の指摘を受けて直した(送信コマンドは文の区切りの後だけ・貼る前に前面のアプリを確かめる・消音の戻し・最後の音・編集モードの遅れた判定・自分で押したキー・クリップボードの戻し方・LOCAL の録音中は断る・ローカルの API の Host と JSON の検め・キーを止めたままにしない・押している間の登録し直し・設定の検め・1 回分の誤りの受け止め)。
- 起動直前に音量を変えると Core Audio の再構成で tap の `device_start` が 80 秒以上待たされる(2026-09-25 実測・run1)。会議アプリと音量を整えてから起動する。
- 秘密は `~/.secrets/meeting-cue.env`。索引 `~/.meeting-cue/index/vault.sqlite` は Vault 本文の複製なので repo に出さない。

## 4. バグ修正時の手順(user CLAUDE.md 準拠・一気に直さない)

### Step 1: 調査とログ追加(見える化)
- まず `~/.meeting-cue/sessions/<session>/metrics.jsonl` を読む。全段が `rid`(発話 id)・`phase`・`ms`・`ok`・`error` を持つ。`python -m meetcue.cli report <session>` で judge / retrieve / generate の p50/p90 と `e2e_first_cue` が出る。
- 深掘りは `dig_request` → `dig_retrieve`(索引と MCP の件数・`mcp_ms`・`mcp_error`)→ `dig_start` → `dig`(`ms_first_token`・`evidence`・`answers`)、用語は `glossary_start`(候補語・先に引けたノート)→ `glossary`(`items`・`with_notes`・`mcp`)。キーは `<rid>-<段>` / `G<連番>`。
- セグメンターの切り所は `phase=segment`(`tail_kind` / `forced` / `dropped`)と `pause_finalize`(`idle_ms` / `threshold_ms`)を突き合わせる。STT 側は `helper_*`(Swift の stderr 診断)。
- 録音後のし直し(FR-9b)は `phase=redo_transcribe`(`ms`・止めてからの `since_stop_ms`・`chars`・`lines`・`ok`・`error` / `reason`・`channels` にチャネルごとの `audio_s` / `ms` / `load_ms` / `speech_s` / temperature の後退で認識した区間の数 `fallback_segments` / 縮めた行の数 `loops`)。`reason` = `unsupported`(Mac)/ `disabled` / `no_audio` / `cancelled` / `app_quit`。区間ごとの確率はヘルパーを単体で `--redo` で動かして stderr の `redo_segment` を見る。精度は `eval/redo_cer.py`(参照と比べる・暴走した繰り返しの行は `loop_lines`)。
- 本体が止まった(発言も音量の `level_5s` も途絶えた)ら `phase=loop_stall`(`ms`)と、アプリのログ(Windows は `~/.meeting-cue/logs/app-<日時>.log`)の `Timeout (0:00:20)!` の節を見る。ループが 20 s 回らなかった時点の全スレッドの呼び出し位置が書かれている(`meetcue/stall.py`・faulthandler なので GIL を握ったまま止まっても書ける)。2026-10-08 の Windows の 14 分の停止はこれが無く原因を特定できなかった。
- Jev の判定は `judgments.jsonl`(`summary` の確率)。誤起動・取りこぼしは発話本文と並べて手ラベルを付ける。
- 音声入力(FR-14 / 14b)は `~/.meeting-cue/dictation/metrics.jsonl` の `dict_*`(`python -m meetcue.cli report dictation`)。1 回分は `dict_start` → `dict_context` → `dict_connect` → (リアルタイムなら `dict_pause` → `dict_insert`)→ `dict_stop` → `dict_final`(`fins` = 届いた "<fin>" の数)→ `dict_text`(`send`)→ (`dict_ai` / `dict_edit`)→ `dict_paste` / `dict_total`。キーの登録の結果は `dict_bindings`、取り消しは `dict_cancel`(`kind` = tap / user / abort)。ヘルパーを単体で試すときは、**ふだん使わないキーだけ**を `--bindings` で渡し(F13〜F20 か ⌃⌥⌘ + F キー。⌃⌥V・Esc・Space・文字のキーは試験で登録しない)、`--file <音声> --emit "activate.down@1,activate.up@4" --no-paste --no-sound --no-bar` で動かす。フローティングバーの見た目は `dictation-helper --render-bar <dir>`(画面に出さず PNG に描く)。消音とマイクのテストは keigoly様 に断ってから。ナギのペット(FR-14c)の窓の見え方は `--render-pet <dir> --pet-dir meetcue/ui/static/nagi`、重さは `~/.meeting-cue/dictation/resource.jsonl`(phase `pet_resource`・1 分ごとの CPU %・phys_footprint MB・起こされた回数・ペットの状態。`report dictation` の最後の行)。ペットの重さを直すときは、まずこの記録で「どの状態(state)・見えているか(visible)・動かしてよいか(moving)」の行が重いかを見てから、その状態の動き(`Pet.motion` / `scheduleIdle`)だけを変える。単体で出すときは `--pet-dir` と stdin の `{"cmd":"pet","mode":"always"}`・`{"cmd":"bar","state":"rec"}` などで表情を変え、`--resource-s 15` で測る(位置の覚えは `--state-dir` を scratchpad に)。
- 再現は合成音声で: `say -v Kyoko -o tests/fixtures/x.aiff "…[[slnc 1500]]…"` → `meetcue run --source file:tests/fixtures/x.aiff:system`。
- 結果を確認してから Step 2 へ。

### Step 2: 原因箇所のみ最小限の改修
- 段の境界を越えて直さない: `segmenter.py`(切り所)/ `judge/`(判定・閾値・`selector.py` の物差しと重み)/ `knowledge/index.py`(検索語・索引)/ `cues/prompts.py`(書式・文面)/ `pipeline.py`(結線・打ち切り・操作・音量の振り分けと無音警告・質問タブの起動条件と優先制御 `_plan_blocked` / `_after_answers`)/ `app.py`(常駐・開始/停止・API・右クリックの操作 `_session_op`)+ `library.py`(記録の一覧・詳細・題名・書き出し・Google Drive へ移動・ゴミ箱。置き場は Mac + Drive の並び)/ `ui/web.py` + `ui/static/app.html`(本体のウィンドウ)・`index.html`(旧・半透明パネル)/ `summary.py`(会議後サマリ)/ `redo.py`(録音後の文字起こしし直し: ヘルパーの起動・2 本の並べ替え・差し替え・取り消し・metrics。認識と行の分け方は Windows の STT ヘルパーの `--redo`。表示に使う文字起こしの決め方は `library.transcript_source`)/ `replacements.py`(置き換え辞書: 確定と途中の文を正しい語へ。Pipeline・ライブ字幕・⚙ の API が同じファイルを読む)/ `soniox_stt.py`(会議の文字起こしを Soniox で・FR-2b: ヘルパーの `audio` を Soniox へ流し、語を契約の partial / final に直す・つなぎ直し・端末内への切り替え。Soniox の接続そのものは `soniox.py`、WebSocket は `wsclient.py`。音声入力 `dictation.py` と共通)/ 音声入力 `dictation.py`(キーの押し方の状態・1 回分の処理・リアルタイムの間ごとの finalize・貼り付けの番号の突き合わせ・記録)+ `dictation_settings.py`(設定とキーの書き方・検め・既定)+ `dictation_history.py`(履歴・使った量)+ `dictation_ai.py`(AI で整える・編集モード)+ `dictation_app.py`(アプリでの待ち受け・`/api/dictation/…`)+ `helpers/macos/dictation_helper`(キー・マイク・貼り付け・画面の文脈・音・フローティングバー・消音・ナギのペット `Pet`。キャンセルと切り替えのキーは録っている間・押している間だけ登録)/ `captions.py`(ライブ字幕: 録音していない間の字幕だけの動き `CaptionRunner` と翻訳 `Translator`)+ `ui/static/caption.html`(字幕の画面)/ `knowledge/obsidian.py`(Obsidian の保管庫の一覧・Vault の決め方・索引の作り方・ノートを開く)+ `knowledge/mcp.py`(MCP クライアントと検索・FR-5b)/ `pipeline.py` の `_run_dig`(深掘り FR-6d)・`_run_glossary`(用語 FR-6e)/ `secrets.py`(API キー: キーチェーン → 環境変数 → `~/.secrets`・キーを画面やログに出さない)+ `providers.py`(接続先とキーの「確認」)+ `cues/llm.py`(接続先ごとの生成・役割 main / fast / deep・料金の計算)+ `cues/anthropic_direct.py`・`cues/openai_direct.py`(stdlib の SSE クライアント。契約は openrouter.StreamResult と同じ)+ `cues/claude_cli.py`(開発者向けの Claude サブスク: 本人の Claude Code を温めて使い捨てる Pool。`[llm] subscription_cli` で有効)+ `app.py` の `settings_view` / `_keys_api`(設定画面・FR-10b)/ `helpers/macos/*`(Swift ヘルパー。契約は stdout JSONL・stdin quit。音量は stderr `phase=level`。`mix_helper` は書き出し用で stdout に結果 JSON 1 行)/ `packaging/`(起動ラッパー)。
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

# 詳しい使い方

概要と写真は [README](../README.md) にあります。ここでは画面ごとの操作・設定・記録の場所・ターミナルからの使い方をまとめます。設計の正本は [REQUIREMENTS.md](REQUIREMENTS.md) です。

## アプリ(Meeting Cue!.app)

```sh
packaging/make_mac_app.sh        # ~/Applications/Meeting Cue!.app を作る(swiftc。repo を動かしたら作り直す)
```

`Meeting Cue!.app` をダブルクリック → **本体のウィンドウ**だけが開きます(Terminal は開きません。本体の `meetcue app` はアプリが裏で動かし、ログは `~/.meeting-cue/logs/app-*.log`)。

- 右上の「録音を開始」→ 題名・自分の立場(参加 / 登壇 / 聴講)・仕事の会議(LOCAL)を選んで開始。音源は `mic` + `tap-all`
- 録音中: 左に経過時間とマイク / スピーカーの波形、右に「文字起こし / 質問 / 回答」のタブ。文字起こしは新しい発言が一番上に足され、古いものは下へ流れる(下へスクロールして読んでいる間は位置が動かない。録音後の画面は古い順)。相手から質問されると回答タブに件数の印が付く(Jev の判定が最優先。タブは自分で切り替える)。録音の開始画面の題名は Enter では始まらず、「開始」ボタンで始める。「録音を開始」の右の ▾ から**クイック録音**(題名と立場を選ばずにすぐ始める = 会議に参加・題名なし。仕事の会議 LOCAL も)。停止を押すとすぐ録音が止まり、作りかけの回答候補とサマリは「保存しています…」の間に仕上げてから録音後の画面へ移る。
- 質問タブ: 会話から「今こちらから聞くとよい質問」を先回りで出す(自動は Sonnet・最大 1 分に 1 回 / 「今の話で質問を考える」/ 「深く考える」= Opus)。Jev の採点で上位 3 件に ★。相手から質問されたら質問タブは即座に中断し、回答が終わるまで待つ。「常に手前」で全画面の Zoom の上にも出る。操作メニュー = 一時停止・深掘り・モード(ホットキーも同じ)
- 録音後: 左の一覧から選ぶと、再生ボタンと波形(クリック、またはつかんで左右に動かすとその位置へ)・上部の「両方 / スピーカー / マイク」・文字起こし(行をクリックでその発言から再生)・回答・サマリ。空白キーで再生/一時停止。題名はクリックするとその場で変えられる(Enter で確定・Esc でやめる)
- 音声は常に保存(`~/.meeting-cue/sessions/<日時>_<id>/audio/{mic,system}.m4a`・1 チャネル 1 時間約 20 MB)。保存しないときは `--no-record`
- 終了: ウィンドウを閉じる(録音中なら正規に止めてサマリを作ってから終わる)。二重起動は起動前に止める
- 相手側の音が 30 秒届かないと警告を出す(別の機器で再生した音は tap に入らない)
- 色と外観: 既定はナギの色(基本 ミント・相手の発言 空色・自分の発言 藤紺)。左上の ⚙ から 3 色と外観(システムに合わせる / ライト / ダーク)を変えられる(`~/.meeting-cue/ui.json` に保存・開いている画面とメニュー・ライブ字幕・アップデートの画面すべてに反映)
- 設定(⚙): **AI** = 回答候補・質問タブ・サマリを AI で作る / 相手の質問を判定する(Jev)のオン・オフ(両方オフ = **録音だけ**・外へ何も送らない)、生成 AI の接続先と **API キー**(Mac のキーチェーンに保存・画面に出さない・「確認」は各社の無料の認証確認だけ。接続先は OpenRouter / Anthropic 直接 / OpenAI 直接。OpenAI はキーのモデル一覧から選び、価格を入れると月の予算上限に数える。Jev はどの接続先でも OpenRouter のキー)/ **Google Drive** = 移動を使うか・既定のアカウント / **表示** = 色・ナギ。はじめて起動したとき(キーも設定も無いとき)は案内が出る(⚙ の「はじめの案内」でもう一度)。既存の `~/.secrets/meeting-cue.env` のキーもそのまま使える
- **開発者向け: 自分の Claude の月額プランで動かす(既定オフ・自己責任)**: `~/.meeting-cue/config.toml` の `[llm]` に `subscription_cli = true` を書くと、接続先に「Claude サブスク(Claude Code 経由・開発者向け)」が出る。この Mac でログイン済みの Claude Code(`claude -p`)を呼ぶだけで、アプリは認証情報に触れない。録音中は Claude Code を 2 つ先に起動して待たせるので、最初の候補までの時間は API とほぼ同じ(実測 2.2〜2.3 s)。Anthropic は他社アプリが Claude.ai のログインを提供すること・利用者の代わりにプランで通信することを禁止しているため、公開版の利用者向けには出さず、**自分の用途だけ**に使う([Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)・[Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance))
- ナギの台詞: 案内の文言(待機中・録音の開始/停止・質問の検知・回答候補・質問タブ・相手の声が届かない警告)を、アイコンのキャラクター「ナギ」の言葉で出す。⚙ の「ナギの台詞で案内する」でオン/オフ(既定はオン・同じ `ui.json`)
- マイクとシステム音声録音の許可は **Meeting Cue!** に付く(初回の録音開始でダイアログ。`make_mac_app.sh` で作り直すと再度求められることがある)
- メニューバー(画面上部のアイコン・録音中は右上に赤い点): 押すと角丸のパネルが開く。頭に状態(待機中 / 録音中の経過時間 / 保存中)、録音を開始と LOCAL で録音の 2 枚のタイル(録音中は停止のタイル)/ ライブ字幕 / 記録のフォルダを開く / ログイン時に起動(スイッチ)/ 設定 ⌘, ・更新を確認・終了 ⌘Q。外を押すか Esc で閉じる。本体のウィンドウは Dock のアイコンか「設定を開く」から
- ライブ字幕: **録音していなくても**スピーカー / マイクの音声を字幕にする(保存しない)。下の操作台でスピーカー・マイクの切替 / 言語(日本語・English)/ **翻訳**(確定した字幕を生成 AI で日本語・英語へ。Claude サブスクなら Sonnet。LOCAL の録音中と AI オフでは訳さない)/ 文字の大きさ / 常に手前(全画面の会議の上にも出る)/ ✦ 回答候補(録音中の最新の質問と ★)/ 録音の開始・停止。録音を始めると録音の字幕に切り替わる。帯の空いている所をつかむと動かせる。⌃⌥L で固定⇄移動・⌃⌥H で表示/非表示
- 置き換え辞書(⚙): 音声認識がよく間違える固有名詞を正しい語に置き換える(1 行 = `正しい語|誤り|誤り…`・`~/.meeting-cue/replacements.txt`)。記録・質問の判定・回答候補・サマリ・書き出し・画面とライブ字幕の途中表示に効く。置き換える前の文は transcript の `raw` に残る
- アップデートを確認: 手元の repo(実行用ツリーなら更新係が取ってきた `stable`)に動いている版より新しいコミットがあれば、変更内容(コミットの件名と説明)と「今すぐ更新して再起動 / 終了時に更新 / この版はスキップ」を出す。起動の 30 s 後と 1 分ごと(更新係の合図があればすぐ)にも確かめ、新しい版は一度だけ知らせる(録音中・スキップした版は出さない)。「自動で更新する」がオンなら知らせずに、録音・保存中でなく前面で使っておらず、ライブ字幕も開いていないときに裏で入れ替える(フォーカスを奪わない・閉じていたウィンドウは開かない・使っている間は終了時に反映)。入れた版が起動の点検か版の確認で失敗したら、1 つ前の版に戻して通知する(その版は二度と入れない)。最新ならメニューから押したときだけ「最新の版です」(画面の部品 = Swift が変わっていれば作り直してから開き直す・`packaging/update_app.sh` → `packaging/updater/`・記録は `~/.meeting-cue/logs/updater-<日付>.jsonl` と `host-<日付>.jsonl`)
- 一覧の記録を右クリック: **録音を書き出す…**(保存先を選ぶ → `<題名> <日時>/` に 音声 1 本の .m4a・文字起こし・サマリ)/ **Google Drive に移動…**(Google Drive for desktop の `マイドライブ/Meeting Cue!/` へ移す・一覧に ☁ 付きで残りそのまま開ける・LOCAL の記録は移せない)/ **削除**(確認してゴミ箱へ)
- ウィンドウを閉じてもメニューバーに残る(録音も続く)。終了はメニューの「Meeting Cue! を終了」か ⌘Q(録音中なら保存とサマリを待ってから終わる)
- デバッグ(Terminal にログを出す): `packaging/launch.sh open app`。従来の起動のしかた(リハーサル等)は `packaging/launch.sh open rehearsal`。詳細は [packaging/DEVELOPMENT.md](../packaging/DEVELOPMENT.md)

```sh
uv run --python 3.12 --no-project python -m meetcue.cli app            # 手で起動する場合(--no-window でブラウザ用)
```

停止は Ctrl-C。終了時に `summary.md`(要約・決定・宿題・質問とキュー・文字起こし)を作り、`--save-vault` なら Vault の `01_Projects/Meeting Cue/Sessions/` にも置きます(機微モードでは題名だけ)。記録は `~/.meeting-cue/sessions/<日時>_<id>/`(transcript / judgments / cues / metrics の JSONL)。`python -m meetcue.cli report` で所要 ms の p50/p90 が出ます。

## Windows

- **入れる**: `powershell -NoProfile -ExecutionPolicy Bypass -File packaging\windows\setup.ps1`(README の「セットアップ」)。作るもの: 音声認識の専用環境 `~/.meeting-cue/stt-venv`・モデル `~/.meeting-cue/models/`・ウィンドウの環境 `~/.meeting-cue/app-venv`・スタートメニューの **Meeting Cue!**(`packaging\windows\launch.pyw` を開く)。何度実行しても大丈夫。
- **開く・閉じる**: スタートメニューの Meeting Cue!。本体のウィンドウを閉じるとアプリも終わる(録音中なら止めて保存してから)。ライブ字幕と「常に手前」は画面の右上のボタンから(2026-09-28 にウィンドウのメニューをやめた。タイトルバーは画面の外観に合わせてダーク / ライト)。既に開いているときにもう一度開くと、ウィンドウだけがもう 1 枚開く。タスクバーに置くときは、スタートメニューの Meeting Cue! を右クリック →「タスクバーにピン留めする」(動いているウィンドウからでも同じものが留まる)。2026-09-28 より前の `setup.ps1` で入れてピン留めした人は、タスクバーの Python のアイコンが残るので、それを「ピン留めを外す」→ `setup.ps1` をもう一度 → ピン留めし直す。
- **音**: 自分 = 既定のマイク、相手 = 既定の出力機器(スピーカー・ヘッドセット)で鳴っている音をまとめて。機器を変えるときは `~/.meeting-cue/config.toml` に `loopback_device = "Headset"` / `mic_device = "USB"`(名前の一部)。機器の一覧は `~/.meeting-cue/stt-venv/Scripts/python.exe helpers/windows/stt_helper/stt_helper.py --list-devices`。マイクが無音のときは Windows の設定(プライバシー → マイク → デスクトップ アプリにアクセスを許可)を確かめる。
- **API キー**: ⚙ で登録すると Windows の資格情報マネージャー(`local.meetcue/<接続先>`)に入る(画面・ログには出さない)。`~/.secrets/meeting-cue.env` も読める。
- **記録とログ**: 記録は Mac と同じ `~/.meeting-cue/sessions/`。アプリの出力は `~/.meeting-cue/logs/app-<日時>.log`(20 個まで)。
- **更新**: `git pull` のあとに `setup.ps1` をもう一度(自動アップデートは次の版)。
- **アンインストール**: スタートメニューの Meeting Cue! を消し、`~/.meeting-cue/stt-venv`・`app-venv`・`models`・`webview` を消す(記録 `sessions` は残る)。資格情報マネージャーの `local.meetcue/…` も消す。
- **まだ無いもの**: タスクトレイ・システム全体のホットキー・自動アップデート・字幕の固定(クリックを下へ通す)・Google Drive への移動。

## ホットキー(Mac・システム全体・Accessibility 許可不要)

| キー | 動作 |
|---|---|
| ⌃⌥P | 一時停止 / 再開(文字起こしは続く。判定と生成を止める。自分が長く話す間に) |
| ⌃⌥D | 今のを深掘り(直近の相手の発話を強制的にキュー生成) |
| ⌃⌥M | モード切替(参加者 → 登壇者 → 聴講) |
| ⌃⌥L | パネルを固定(クリック透過)⇄ 移動可 |
| ⌃⌥H | パネルの表示 / 非表示 |
| ⌃⌥R | パネルの再読込 |

パネルの候補は「⧉」でクリップボードにコピーできます。★ は Jev の採点で上位の候補です。

## ターミナルから使う

```sh
# 1. Swift ヘルパーをビルド(Xcode Command Line Tools)
(cd helpers/macos && make)

# 2. 設定と鍵
mkdir -p ~/.meeting-cue && cp config.example.toml ~/.meeting-cue/config.toml   # vault_root を直す
printf 'OPENROUTER_API_KEY=sk-or-...\n' > ~/.secrets/meeting-cue.env && chmod 600 ~/.secrets/meeting-cue.env

# 3. Vault の索引(初回は全量。以後は差分)
uv run --python 3.12 --no-project python -m meetcue.cli index

# 4. 前提の検査
uv run --python 3.12 --no-project python -m meetcue.cli doctor --online

# 5. 合成音声で通す(テスト。素材は先に tests/fixtures/make_fixtures.sh で作る)。既定で最前面パネル(overlay)+ ブラウザ用 Web UI(http://127.0.0.1:8765/・同じ Mac の 2 人目の利用者は 8766。`MEETCUE_PORT` で変えられる)が出る
uv run --python 3.12 --no-project python -m meetcue.cli run --source file:tests/fixtures/meeting_ja.aiff:system --mode participant

# 6. 実会議(マイク=自分 + Zoom の出力=相手)
uv run --python 3.12 --no-project python -m meetcue.cli run --source mic --source tap:zoom --mode participant --save-vault
#    Teams なら tap:teams、会議アプリの内部プロセス構成に左右されたくなければ tap-all
#    仕事の会議(クラウドへ出さない): --privacy local
#    画面: --ui terminal(ターミナルだけ)/ web(ブラウザで開く)/ overlay(既定・最前面パネル)
```

## 権限(macOS)

- マイク: `stt-helper` の初回起動でダイアログ
- システム音声録音: `tap-helper` の初回 `AudioHardwareCreateProcessTap` で System Settings > Privacy & Security に出る。未許可だと `tap_create` が失敗する(`tap-helper --list` で音声を出しているプロセスを確認できる)
- 罠: 起動直前に音量を変える(`osascript set volume` 等)と Core Audio の再構成で `device_start` が 80 秒以上待たされることがある(2026-09-25 実測)。会議アプリを起動し、音量を決めてから `meetcue run` する

## ファイルの構成

```
meetcue/            Python 本体(uv・依存ゼロ)
  stt_reader.py     ヘルパーの JSONL 受信(Mac / Windows 共通の契約)
  segmenter.py      ポーズ検出・強制 finalize・文分割・相づち除去
  judge/            jev.py(判定 API・stdlib)/ heuristic.py(縮退)
  knowledge/        index.py(FTS5・文字 bigram)
  cues/             openrouter.py(ストリーミング)/ prompts.py(行書式)
  judge/selector.py 選ぶ係(候補を Jev が基準別採点 → 重み付き合計で並べ替え)
  pipeline.py       オーケストレーター / cli.py / session.py / ledger.py / summary.py
  ui/terminal.py    ターミナル表示 / ui/web.py(stdlib HTTP + SSE)/ ui/static/index.html(画面)
helpers/macos/      Swift: stt_helper(マイク・--file)/ tap_helper(process tap)/ hotkey_helper / overlay_helper(NSPanel + WKWebView)/ mix_helper(書き出しの音声を 1 本に重ねる)/ Makefile
helpers/windows/    Python(本体とは別の仮想環境): stt_helper(faster-whisper + Silero VAD・PyAudioWPatch の取り込み)/ window_helper(pywebview)/ mix_helper(PyAV)
packaging/          起動ラッパー: launch.sh(Terminal で起動)/ make_mac_app.sh(Meeting Cue!.app を作る)/ windows/(setup.ps1・launch.pyw)
spikes/             Phase 0 の計測スクリプトと結果
tests/              pytest(uv run --with pytest python -m pytest)
```

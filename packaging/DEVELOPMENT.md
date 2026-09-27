# packaging/ — DEVELOPMENT.md

## 1. このディレクトリの役割

meetcue を「すぐ起動できる形」にする起動ラッパーの置き場。本体(`meetcue/`)には手を入れず、決まったコマンドを呼ぶだけ。

| ファイル | 役割 |
|---|---|
| `launch.sh` | 起動の実体。`serve` = アプリから呼ばれる本体の起動(Terminal なし・python を直接 exec して SIGINT を届ける・ログ `~/.meeting-cue/logs/app-*.log` を 20 個まで)。`open <preset>` で Terminal.app の新しいウィンドウを開き、`run <preset>` で meetcue を動かす。既定の preset は `app`(`meetcue app` = 本体のウィンドウ)。二重起動(ポート 8765 使用中)は起動前に止める |
| `make_mac_app.sh` | `~/Applications/Meeting Cue!.app` を swiftc で組み立てる。本体は `helpers/macos/overlay_helper/main.swift` のホストモード: Info.plist の `MeetcueLaunchScript` を `launch.sh serve` で子プロセスとして動かし、その画面を出す(Terminal を開かない・2026-09-26)。アイコンは `icon/` の AppIcon.icns と MenuBarTemplate(@2x).png を Resources へ入れる |
| `launch.sh`(補足) | `serve` は、更新係の印(`~/.meeting-cue/updater/unverified` = 起動で確かめていない版)があれば、`packaging/updater/run.sh verify-pending --pid $$` を裏で動かしてから本体を exec する(2026-09-27) |
| `update_app.sh` | メニューの「アップデートを確認」→ 再起動の入口(アプリが pid・作り直しの要否・開き直し方を渡す)。中身は更新係の `apply` |
| `updater/` | 更新係(2026-09-27 段 2)。実行用ツリー(branch `stable`)へ取り込み・作り直し・開き直し・巻き戻し。約束は根元の `update.toml`。詳細は [updater/DEVELOPMENT.md](updater/DEVELOPMENT.md) |
| `icon/` | アプリとメニューバーのアイコン(成果物 + 原画から作り直す `make_icons.sh`)。詳細は [icon/DEVELOPMENT.md](icon/DEVELOPMENT.md) |

preset: `app`(既定)/ `participant` / `presenter` / `audience` / `work`(`--privacy local`)/ `rehearsal`(doctor + 60 秒・`--no-llm`)/ `doctor`(検査だけ)。`app` 以外は `launch.sh open <preset>` で使う。音源は `launch.sh` の `SOURCES`(既定 `mic` + `tap-all`)。

生成物の置き場: app は `~/Applications`(git 外)、Terminal 用の `.command` と `doctor.log` は `~/.meeting-cue/launch/`(非 git・複製しない)。

## 2. 現在の問題点(2026-09-26)

- 許可(マイク・システム音声録音)は、アプリから起動すると **Meeting Cue!** に、`launch.sh open …`(デバッグ)から起動すると **Terminal.app** に付く。ad-hoc 署名なので `make_mac_app.sh` で作り直すと許可をもう一度求められることがある。
- アプリはメニューの「終了」/ ⌘Q で終わる(子に SIGINT → 録音を正規に止めてサマリを作る → 終了。最大 2 分待って SIGTERM)。**ウィンドウを閉じても終わらずメニューバーに残る**(2026-09-26 のメニューバー追加から)。本体が先に落ちたらウィンドウに案内とログの場所を出す。
- メニューバーは本体の `/api/state` を 1 秒ごとに見てアイコンを切り替える(録音中は右上に赤い点)。操作は `/api/action` を呼ぶだけ。「ログイン時に起動」は `SMAppService.mainApp`(ad-hoc 署名のため、システム設定での許可を求められることがある)。
- **(2026-09-27 修正)シグナル(SIGTERM / SIGINT)や、main queue で開いたアラートの最中の終了で、本体が止まった後もアプリが残っていた**。main queue のブロック(シグナルの DispatchSource・`DispatchQueue.main.async` の中の `runModal`)の中から `.terminateLater` の入れ子の run loop に入ると、子の終了通知(`DispatchQueue.main.async` の `childExited`)が同じ main queue で待たされて実行されなかった。2026-09-27 は古い版の「アップデートを確認」のアラートを開いたまま osascript quit で再現(AppleEvent がタイムアウト)。修正: 子の終了通知を `RunLoop.main.perform(inModes: [.common, .modalPanel])` + `CFRunLoopWakeUp` で積む(入れ子の run loop でも実行される)。確認: 修正前の版は `kill -TERM <app の pid>` で子だけ終わりアプリが残る(8 s 後も残る)→ 修正後は 1 s 未満で終わる。osascript quit も 1 s。
- 既にポート 8765 で本体が動いていれば(デバッグ起動中など)、アプリはつなぐだけで本体を起動しない(閉じても本体は止めない)。
- 「常に手前」の間だけ Dock から消える(全画面の会議アプリの上に出すため accessory に切り替える)。全画面の Zoom の上に出るかは実機で未確認。
- 名前は「Meeting Cue!」(2026-09-26 改名)。`make_mac_app.sh` は旧名の `Meeting Cue.app`(同じ bundle id)があれば `Meeting Cue!.app` へ改名してから中身だけ作り直す。Dock に置いた項目やログイン項目が付いてこないときは、Dock に置き直す・メニューの「ログイン時に起動」を入れ直す。シェルで開くときは `open -b local.meetcue.app`(zsh の対話では `!` をそのまま書くと履歴展開される)。
- app は repo の絶対パスを埋め込む。repo を動かしたら `make_mac_app.sh` を再実行する。2026-09-27(段 2)から、アプリは**実行用ツリー `~/Apps/meeting-cue`** から作る(それ以外の repo から作ると注意が出て、自動更新から外れる)。作り直しは同時に 1 つだけ(`lockf`・終了時の自動の作り直しと手の作り直しが重なって、片方の `rm -rf Contents` がもう片方の途中を消した)。アイコンは 2026-09-26 に採用案(D-01d / C-D-01d-02)を組み込んだ。Finder / Dock に古いアイコンが残るときは app を作り直すか Dock から外して入れ直す。
- Windows の exe は未着手(Phase 4 で Python 本体の起動方法を決めてから)。

## 3. バグ修正時の手順(user CLAUDE.md の Step 1〜3)

1. **見える化**: アプリの不具合はまず `~/.meeting-cue/logs/app-*.log`(本体の出力)を見る。Terminal で見たいときは `packaging/launch.sh open app`。`MEETCUE_DRYRUN=1 packaging/launch.sh run <preset>` で実際に流すコマンドを見る。`launch.sh open <preset>` が作る `~/.meeting-cue/launch/meetcue-<preset>.command` の中身を見る。前提の問題は `~/.meeting-cue/launch/doctor.log`。
2. **最小改修**: 起動引数の問題は `launch.sh` の `preset_args` / `SOURCES` だけを直す。本体の不具合は `meetcue/` 側の DEVELOPMENT.md(ルート)の手順に回す。
3. **周辺整合**: preset を変えたら docs/USAGE.md の「アプリ(Meeting Cue!.app)」を合わせる。ウィンドウ側(`overlay_helper/main.swift`)や Info.plist を変えたら `make_mac_app.sh` で app を作り直し、許可のダイアログが出直すことを案内する。

## 4. 関連ドキュメント

- ルートの [DEVELOPMENT.md](../DEVELOPMENT.md)・[README.md](../README.md)
- 設計: [docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md)(FR-8 モードと操作・FR-11 機微モード)

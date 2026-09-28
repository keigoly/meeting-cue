# packaging/windows — DEVELOPMENT.md

## 1. このディレクトリの役割

Windows 版を「ダウンロードして準備し、起動できる形」にする置き場(Mac の `packaging/make_mac_app.sh` などに当たる)。配り方は docs/REQUIREMENTS.md FR-13(2026-09-27 確定): **clone か GitHub の ZIP で取り、`setup.ps1` を 1 回実行する**(インストーラと GitHub Releases の配布物は当面作らない)。

| ファイル | 役割 |
|---|---|
| `setup.ps1` | 前提の検査(Windows 10 2004 以降・NVIDIA の GPU と VRAM)→ uv で Python 3.12 → STT ヘルパー専用の仮想環境 `~/.meeting-cue/stt-venv`(`helpers/windows/stt_helper/requirements.txt` の固定版)→ モデル `~/.meeting-cue/models/faster-whisper-large-v3-turbo`(固定した版)→ GPU に読み込む自己診断。やり直しても壊れない(入っていれば飛ばす・`-Force` で環境を作り直す)。初回の取得は約 3.7 GB |
| `setup.ps1`(続き) | アプリ用の仮想環境 `~/.meeting-cue/app-venv`(`helpers/windows/window_helper/requirements.txt` = pywebview)→ アイコン(`docs/images/icon.png` を ICO に包む・`~/.meeting-cue/MeetingCue.ico`)→ スタートメニューの **Meeting Cue!**(`app-venv\Scripts\pythonw.exe launch.pyw`・作業フォルダ = この repo・AppUserModelID = ウィンドウと同じ `keigoly.MeetingCue` を `window_helper.py --stamp-shortcut` で書く)。`-NoShortcut` で登録しない |
| `launch.pyw` | 起動の入口(ショートカットが pythonw で開く)。本体 `python -X utf8 -m meetcue.cli app` を **見えないコンソール**(CREATE_NO_WINDOW)で動かし、出力を `~/.meeting-cue/logs/app-<日時>.log`(20 個)へ。pythonw から直接だと、ヘルパーごとに黒い窓が開くため。既にポート 8765 が使われていれば、本体は起動せずウィンドウだけ開く |

- `.ps1` は ASCII だけで書く(Windows PowerShell 5.1 は BOM の無い UTF-8 を cp932 として読む)。画面に出す文も英語。
- CUDA の部品とモデルは利用者の手元で PyPI と Hugging Face から取る(こちらで再配布しない)。会議中はネットへ出ない(ヘルパーは `HF_HUB_OFFLINE=1`)。

## 2. 現在の問題点(2026-09-27)

- 2026-09-27: スタートメニューから開けるアプリになった(ウィンドウ・ライブ字幕・常に手前・キーの保管・書き出し)。実測: 起動から画面まで約 0.5 s・閉じると本体とヘルパーが 1.8 s で終わる・子の黒い窓は出ない。タスクトレイ・ホットキー・自動更新は次の版。
- 2026-09-28: 再起動してもタスクバーが Python のアイコンのままだった。実物を調べると、タスクバーのピン留め `Python.lnk` が ID = `keigoly.MeetingCue`・実行先 = 引数なしの python.exe だった(動いているウィンドウをピン留めしたとき、同じ ID のショートカットが無いので Windows がプロセスの exe を留めた)。ウィンドウ自身のアイコンは正しく、同じ ID のピン留めにまとめられて Python の絵になっていた。→ ショートカットにも同じ ID を書くようにした。既に留めた Python のアイコンは手で外す(プログラムからは外せない)。
- ショートカットは `setup.ps1` を実行した repo を指す。repo を動かしたら `setup.ps1` をやり直す(Mac の make_mac_app.sh と同じ)。
- 開発者向けのコマンドで本体を動かすときは `-X utf8` を付ける(無いと cp932 で落ちる): `uv run --python 3.12 --no-project python -X utf8 -m meetcue.cli app`。
- uv が無ければ案内して止まる(勝手に入れない)。git が無い人は GitHub の ZIP で取る。ZIP の人は自動更新が効かないので取り直し。
- 配り方(clone / ZIP + setup・インストーラと Releases は当面なし)は 2026-09-27 に keigoly様 が確定。

## 3. バグ修正時の手順(user CLAUDE.md の Step 1〜3)

1. **見える化**: `setup.ps1` は段ごとに `==> <段>` を出し、失敗した段で `NG:` を出して止まる。モデルの取得と自己診断はヘルパーの 1 行 JSON(`phase=setup_model` / `self_test` / `abort`)を見る。
2. **最小改修**: 失敗した段だけを直す。パッケージの版は `helpers/windows/stt_helper/requirements.txt`、モデルの版は `stt_helper.py` の `MODEL_REVISION`。
3. **周辺整合**: 手順や前提を変えたら README の Windows の節・docs/USAGE.md・REQUIREMENTS FR-13 を合わせる。公開版に入ることを確かめる(`packaging/export_public.py` の EXCLUDE に入れない)。

## 4. 関連ドキュメント

- ヘルパー: [helpers/windows/DEVELOPMENT.md](../../helpers/windows/DEVELOPMENT.md)
- 設計: [docs/REQUIREMENTS.md](../../docs/REQUIREMENTS.md)(FR-12・FR-13・§7)
- Mac の同じ役割: [packaging/DEVELOPMENT.md](../DEVELOPMENT.md)

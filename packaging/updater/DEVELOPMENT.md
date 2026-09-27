# packaging/updater/ — DEVELOPMENT.md

## 1. このディレクトリの役割

更新係(Mac / Windows 同時アップデートの段 2・2026-09-27)。両方の OS の試験に合格したコミット(`stable`)を、Mac の**実行用ツリー** `~/Apps/meeting-cue`(branch `stable`)へ取り込み、アプリの作り直し・開き直し・版の確認・失敗時の巻き戻しを行う。アプリの中身は知らず、repo の根元の `update.toml`(更新の約束)だけを読む。依存は stdlib のみ。

| ファイル | 役割 |
|---|---|
| `updater.py` | 本体。`check`(launchd が 1 分おき)/ `apply`(アプリの終了の後)/ `verify-pending`(次の起動で確かめる)/ `install` / `uninstall` / `status` / `notify-test`(Discord に試験の 1 通) |
| `run.sh` | 入口。uv の python 3.12 を毎回探して `updater.py --tree <このツリー>` を動かす(launchd と `packaging/update_app.sh` から) |

流れ:

```
launchd(1 分おき・update.toml の check_interval_s)→ run.sh check
   stable が進んだ? ─ いいえ → 記録だけ
   └ はい → git fetch(作業ツリーは変えない)
        ├ アプリが動いている → POST /api/action {"action": "update_check"}(合図)
        │     アプリ(ホスト)が 1 秒ごとの /api/state で合図を見て /api/version を確かめる
        │     ├ 自動で行う = オン・録音 / 保存中でない・前面でない・ライブ字幕なし → 裏で再起動(update_app.sh <pid> [rebuild] bg|hidden)
        │     ├ 自動で行う = オン・それ以外 → 1 分ごとに見直す。先に終了すれば終了時に入れる
        │     └ 自動で行う = オフ → アップデートの画面(スキップ / 終了時 / 今すぐ)
        └ アプリが止まっている → その場で apply(開き直しなし)→ 起動で確かめていない版の印を残す
update_app.sh → run.sh apply: 終了を待つ → git merge --ff-only origin/stable → 作り直し(rebuild_paths が変わったときだけ)
   → 点検(smoke)→ 開き直し → /api/version の running が新しい版か(本体が落ちた記録 host_child_exit が出たら
     すぐ失敗・固まって応答しないときの上限 90 s)
   └ どこかで失敗 → git reset --hard <前の版> → 要るなら作り直し → 開き直し → 通知。その版を <data>/updater/bad に書き、二度と入れない
起動で確かめていない版(アプリが止まっている間・終了時に入れた版、開き直した直後に終了された版)
   → 印 <data>/updater/unverified {sha, good = 確かめ済みの最後の版}
   → 次にアプリを開くと launch.sh serve が印を見つけ、裏で run.sh verify-pending --pid <本体>
      ├ 動いた → 印を消す
      ├ 利用者が終了した → 印を残す(次の起動でもう一度)
      └ 落ちた → good へ戻す(作り直しが要れば作り直す)→ 裏で開き直す(フォーカスを奪わない)→ 通知。その版は二度と入れない
```

- 取り込みは**必ずアプリの終了の後**(動いているアプリの足元の Python・画面の HTML を書き換えない)。
- 開発用ツリー(branch が `stable` 以外)では取り込みも巻き戻しもしない(`apply` は作り直しと開き直しだけ = 従来の「アップデートを確認」と同じ)。
- 更新は 1 つずつ(`<data>/updater/lock`)。アプリの作り直しも 1 つずつ(`make_mac_app.sh` が `lockf`)。
- 同じ版での自動の再起動は 1 回だけ(ホストの UserDefaults `meetcueAutoRestartHead`)。開き直しても「新しい版あり」のままなら終了時へ回す。

記録: `~/.meeting-cue/logs/updater-<日付>.jsonl`(1 行 1 段・`rid`・`phase`・`ok`・`ms`・直近 14 日)。アプリ側は `host-<日付>.jsonl`(`update_check` / `update_nudge` / `update_wait` / `update_restart` / `update_on_quit` / `host_child_exit` / `host_quit`・同じ確認の `rid` が `update_app.sh` → `apply` に渡る)。作り直しの出力は `update-<日時>.log`(アプリから)/ `updater-launchd.log`(launchd から)。

## 2. 現在の問題点(2026-09-27)

- 更新係はツリーの中のコードを動かすので、`stable` に入った更新係自身の不具合は巻き戻せない(試験の関門で止める)。
- アプリが止まっている間に `check` が作り直していると、その数秒の間にアプリを開くと起動に失敗することがある(開き直せば直る)。
- 起動直後に本体が落ちる版は、アプリ(ホスト)の記録 `host-<日付>.jsonl` の `host_child_exit`(0 以外の status・開き直した後の行だけ)で見分ける(`update.toml` の `exit_log` / `exit_phase`)。2026-09-27 までは落ちても 90 s 待っていた。本体が落ちずに固まる版は今も 90 s 待つ。
- 起動で確かめていない版は、次の起動で確かめる(2026-09-27)。起点はアプリの記録の `host_spawn`(本体の pid = `launch.sh` の `$$`・`update.toml` の `spawn_phase`)。起動で確かめていない版が 2 つ続いたら、戻す先は確かめ済みの最後の版(1 つ前の確かめていない版ではない)。アプリを開かないまま次の版が来たら、印はその版に移る。
- 開き直した直後(版の確認の前)に利用者がアプリを終了したときは、アプリの記録 `host_quit`(`update.toml` の `quit_phase`)を見て確認をやめ、巻き戻さず開き直さない(記録は `verify` の `quit`・`apply` の `verified: false`)。終了の操作で止めた本体の終了(`host_child_exit` の `quitting: true`・SIGINT で status 2 になる)は「落ちた」に数えない。2026-09-27 までは良い版を巻き戻して悪い版にし、終了したアプリを開き直していた。確かめられなかった版は次の起動で確かめる(上の行)。
- Windows(段 3)は未着手。`fcntl` が無いのでロックは効かない(`msvcrt` で作る)。

## 3. バグ修正時の手順(user CLAUDE.md の Step 1〜3)

1. **見える化**: `packaging/updater/run.sh status`(今の版・`origin/stable`・止めた版・直近の記録)。`updater-<日付>.jsonl` を `rid` で追い、アプリ側の `host-<日付>.jsonl` と `t_ms` で並べる。launchd が動いているかは `launchctl print gui/$(id -u)/local.meetcue.updater`。
2. **最小改修**: 取り込みの判断は `Updater.check` / `_apply_locked`、戻し方は `rollback`、アプリへの合図は `nudge` だけを直す。アプリの約束(URL・コマンド・作り直しの場所)は `update.toml` を直す(コードに書かない)。アプリがいつ入れるかは `helpers/macos/overlay_helper/main.swift` の `Host.handleUpdate`。
3. **周辺整合**: `tests/test_updater.py` を足す(一時フォルダの origin・開発用・実行用で再現)。`update.toml` の `rebuild_paths` は更新係とアプリの `/api/version`(`rebuild`)の両方が読む(作り直すと ad-hoc 署名が変わり、マイクなどの許可を求め直されることがあるので、アプリの中身が変わる所だけにする)。launchd の設定を変えたら `run.sh install` をやり直す。

## 4. 関連ドキュメント

- ルートの [DEVELOPMENT.md](../../DEVELOPMENT.md)・[packaging/DEVELOPMENT.md](../DEVELOPMENT.md)・[update.toml](../../update.toml)

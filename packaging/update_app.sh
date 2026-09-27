#!/bin/bash
# メニューの「アップデートを確認」→ 再起動(2026-09-27)。アプリがバックグラウンドで呼び、中身は更新係の apply
# (packaging/updater/updater.py)に任せる: アプリ(pid)の終了を待つ → 実行用ツリー(branch stable)なら origin/stable を
# 取り込む → 要るならヘルパーとアプリを作り直す → 点検 → 開き直して版を確かめる → 失敗なら 1 つ前へ戻す。
# 動いているアプリの足元のファイルを変えないよう、取り込みは必ず終了の後(段 2)。
#
#   packaging/update_app.sh <pid> [rebuild] [norelaunch|none|bg|hidden]   記録: ~/.meeting-cue/logs/update-<日時>.log(作り直しの出力)
#                                                                         と updater-<日付>.jsonl(rid・段・所要 ms)
#   norelaunch / none = 「終了時にインストール」: 開き直さない / bg = 裏で開き直す / hidden = ウィンドウを出さずに開き直す
set -u
PID="${1:-}"
MODE="${2:-}"
RELAUNCH="${3:-}"
LOG="${MEETCUE_HOME:-$HOME/.meeting-cue}/logs/update-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
args=(apply)
[ -n "$PID" ] && args+=(--pid "$PID")
[ "$MODE" = rebuild ] && args+=(--rebuild)
case "$RELAUNCH" in
  norelaunch|none) args+=(--relaunch none) ;;
  bg|hidden) args+=(--relaunch "$RELAUNCH") ;;
  *) args+=(--relaunch front) ;;
esac
exec /bin/bash "$(dirname "$0")/updater/run.sh" "${args[@]}" >>"$LOG" 2>&1

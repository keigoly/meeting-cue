#!/bin/bash
# メニューの「アップデートを確認」→ 再起動(2026-09-27)。アプリ(pid)の終了を待ち、要るならヘルパーとアプリを作り直して
# から開き直す。アプリがバックグラウンドで呼ぶ(アプリが動いている間に実行ファイルを上書きしないため、終了を待つ)。
#
#   packaging/update_app.sh <pid> [rebuild] [norelaunch]      記録: ~/.meeting-cue/logs/update-<日時>.log
#   norelaunch = 「終了時にインストール」: 作り直すだけで開き直さない
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PID="${1:-}"
MODE="${2:-}"
RELAUNCH="${3:-}"
LOG="$HOME/.meeting-cue/logs/update-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
{
  echo "update: pid=$PID mode=${MODE:-restart} $(date '+%F %T')"
  for _ in $(seq 1 300); do   # 録音の保存とサマリを待つ(アプリの終了は最大 120 s)
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.5
  done
  if [ "$MODE" = rebuild ]; then
    if (cd "$REPO/helpers/macos" && make) && "$REPO/packaging/make_mac_app.sh"; then
      echo "rebuilt"
    else
      echo "作り直しに失敗(前の版で開き直す)"
    fi
  fi
  if [ "$RELAUNCH" != norelaunch ]; then open -b local.meetcue.app; fi
  echo "done $(date '+%F %T')"
} >>"$LOG" 2>&1

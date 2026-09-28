#!/bin/bash
# Meeting Cue! の起動スクリプト(Meeting Cue!.app から呼ばれる。手でも使える)。
#
#   launch.sh open <preset>   Terminal.app の新しいウィンドウで「launch.sh run <preset>」を開く(app はこちらを呼ぶ)
#   launch.sh run  <preset>   この端末で meetcue を動かす(Ctrl-C で停止 → report と summary.md を表示)
#   launch.sh serve           Meeting Cue!.app(ウィンドウ側)から呼ばれる。Terminal を開かず meetcue app --no-window を
#                             動かし、出力は ~/.meeting-cue/logs/app-*.log へ(直近 20 個を残す)。SIGINT で正規に止まる
#
# preset: app(既定・ウィンドウで録音の開始/停止・一覧・録音後の画面)|
#         participant | presenter | audience | work(LOCAL・クラウドへ送らない)| rehearsal(doctor + 60 秒・生成なし)| doctor
# MEETCUE_DRYRUN=1 で実行せずにコマンドだけ表示する。
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${MEETCUE_HOME:-$HOME/.meeting-cue}"
LAUNCH_DIR="$APP_DIR/launch"
# 画面のポートは利用者ごと: 8765 + (uid − 501) を 100 で回す(最初の利用者は 8765)。MEETCUE_PORT で上書き(アプリのホストが渡す)。
# 同じ決まりが meetcue/config.py の default_port・overlay_helper・update.toml の {port} にある(2026-09-28)
if [[ "${MEETCUE_PORT:-}" =~ ^[0-9]+$ ]] && (( MEETCUE_PORT >= 1024 && MEETCUE_PORT <= 65535 )); then
  PORT="$MEETCUE_PORT"
else
  PORT=$(( 8765 + ( ( $(id -u) - 501 ) % 100 + 100 ) % 100 ))
fi
# 相手の声は tap-all(Mac 全体の音)。tap:zoom は GUI 本体 1 プロセスしか取らず、Chrome/Meet は helper から音が出るため無音の恐れ(2026-09-25)
SOURCES=(--source mic --source tap-all)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

preset_args() {
  case "$1" in
    participant) echo "--mode participant" ;;
    presenter)   echo "--mode presenter" ;;
    audience)    echo "--mode audience" ;;
    work)        echo "--mode participant --privacy local" ;;
    rehearsal)   echo "--mode participant --seconds 60 --no-llm --no-summary" ;;
    *) return 1 ;;
  esac
}

cmd_open() {
  local preset="$1"
  [[ "$preset" == doctor || "$preset" == app ]] || preset_args "$preset" >/dev/null || { echo "unknown preset: $preset" >&2; exit 2; }
  mkdir -p "$LAUNCH_DIR"
  local f="$LAUNCH_DIR/meetcue-$preset.command"
  printf '#!/bin/bash\nexec /bin/bash %q run %q\n' "$REPO/packaging/launch.sh" "$preset" > "$f"
  chmod 700 "$f"
  if [[ "${MEETCUE_DRYRUN:-}" == 1 ]]; then
    echo "open -a Terminal $f"; cat "$f"; return 0
  fi
  open -a Terminal "$f"
}

pause_close() {
  echo
  read -r -p "Enter でこのウィンドウの処理を終えます " _ || true
}

cmd_run() {
  local preset="$1"
  printf '\033]0;Meeting Cue! — %s\007' "$preset"
  local uv; uv="$(command -v uv)" || { echo "uv が見つかりません(~/.local/bin/uv を確認)"; pause_close; exit 1; }
  cd "$REPO" || exit 1
  local py=("$uv" run --python 3.12 --no-project python -m meetcue.cli)
  mkdir -p "$LAUNCH_DIR"

  if [[ "$preset" == doctor ]]; then
    "${py[@]}" doctor --online 2>&1 | tee "$LAUNCH_DIR/doctor.log"
    return "${PIPESTATUS[0]}"
  fi
  if [[ "$preset" == app ]]; then
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "既に起動中です(ポート $PORT が使用中)。開いているウィンドウを使うか、先に閉じてから起動してください。"
      pause_close; exit 1
    fi
    echo "Meeting Cue! — ウィンドウで「録音を開始」を押してください。"
    echo "  終了: ウィンドウを閉じる か このウィンドウで Ctrl-C(録音中なら正規に止めてから終わる)"
    echo "  このウィンドウは記録の表示用です。閉じるとアプリも終わります。"
    echo
    local run=("${py[@]}" app --port "$PORT" "${SOURCES[@]}")
    if [[ "${MEETCUE_DRYRUN:-}" == 1 ]]; then printf '%q ' "${run[@]}"; echo; return 0; fi
    trap ':' INT
    "${run[@]}"
    local rc=$?
    trap - INT
    echo; echo "終了(rc=$rc)"
    pause_close
    return "$rc"
  fi
  local args; args="$(preset_args "$preset")" || { echo "unknown preset: $preset"; pause_close; exit 2; }
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "既に起動中です(ポート $PORT が使用中)。先に開いたウィンドウで Ctrl-C を押して止めてから起動してください。"
    pause_close; exit 1
  fi

  # shellcheck disable=SC2206 — args は上の固定文字列だけ
  local run=("${py[@]}" run --port "$PORT" "${SOURCES[@]}" $args)
  echo "Meeting Cue! — $preset"
  echo "  停止: Ctrl-C(終了時に report と summary.md を表示)"
  echo "  ⌃⌥P 一時停止 / ⌃⌥D 深掘り(取りこぼしの印)/ ⌃⌥M モード / ⌃⌥H パネル表示 / ⌃⌥L 固定⇄移動"
  [[ "$preset" == work ]] && echo "  LOCAL: Jev・生成・要約のクラウド呼び出しなし(文字起こしと Vault 検索だけ)"
  [[ "$preset" == rehearsal ]] && echo "  リハーサル: 前提の検査 → 60 秒(自分の声が [mic]、動画などの音が [system] に出るか確認)"
  echo "  画面共有はウィンドウ単位で(画面全体だとパネルが映る恐れ)。他の音源は止め、ヘッドホンを使う。"
  echo
  if [[ "${MEETCUE_DRYRUN:-}" == 1 ]]; then
    printf '%q ' "${run[@]}"; echo; return 0
  fi
  if [[ "$preset" == rehearsal ]]; then
    "${py[@]}" doctor --online 2>&1 | tee "$LAUNCH_DIR/doctor.log"
    echo
  fi
  # Ctrl-C は meetcue が受けて後始末(report・summary)をする。bash は落ちずに最後の案内まで進む。
  trap ':' INT
  "${run[@]}"
  local rc=$?
  trap - INT
  echo
  echo "終了(rc=$rc)。記録: $APP_DIR/sessions/ の最新(report: python -m meetcue.cli report)"
  pause_close
  return "$rc"
}

cmd_serve() {
  cd "$REPO" || exit 1
  mkdir -p "$APP_DIR/logs"
  ls -1t "$APP_DIR/logs"/app-*.log 2>/dev/null | tail -n +21 | while read -r f; do rm -f "$f"; done
  local log="$APP_DIR/logs/app-$(date +%Y%m%d-%H%M%S).log"
  # uv を挟むと SIGINT の届き方が変わるので、uv が管理する python を直接 exec する(依存は stdlib のみ)
  local py; py="$(uv python find 3.12 2>>"$log")" || { echo "python 3.12 が見つかりません(uv python install 3.12)" >>"$log"; exit 1; }
  echo "serve: $(date '+%F %T') python=$py port=$PORT sources=${SOURCES[*]}" >>"$log"
  # 起動で確かめていない版(更新係の印・2026-09-27)があれば、この起動で確かめる。$$ は exec の後の本体と同じ pid。
  # 動けば印を消し、落ちたら更新係が確かめ済みの最後の版へ戻す(packaging/updater/DEVELOPMENT.md)
  if [[ -f "$APP_DIR/updater/unverified" && -f "$REPO/packaging/updater/run.sh" ]]; then
    nohup /bin/bash "$REPO/packaging/updater/run.sh" verify-pending --pid $$ >>"$log" 2>&1 &
  fi
  exec "$py" -m meetcue.cli app --no-window --port "$PORT" "${SOURCES[@]}" >>"$log" 2>&1
}

case "${1:-}" in
  open) cmd_open "${2:-app}" ;;
  run)  cmd_run "${2:-app}" ;;
  serve) cmd_serve ;;
  *) echo "usage: $0 open|run <app|participant|presenter|audience|work|rehearsal|doctor> | serve" >&2; exit 2 ;;
esac

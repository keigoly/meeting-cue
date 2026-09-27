#!/bin/bash
# アイコン一式を原画から作り直す。この directory に AppIcon.icns と MenuBarTemplate.png / @2x.png を書く
# (packaging/make_mac_app.sh が Meeting Cue!.app の Resources へ入れる)。
#
#   packaging/icon/make_icons.sh [アプリ用の原画] [メニューバー用の原画] [確認画像の出力先]
#
# 既定の原画は受け取り口 ~/.meeting-cue/icon-inbox/ の採用案(2026-09-26): アプリ = D-01d-x4.png(3520 x 3520)、
# メニューバー = C-D-01d-02.png(1024 x 1024・白地に黒の 2 値)。原画は読むだけで書き換えない。
# 中身(格子・影・縮小の方法)は make_icons.swift の冒頭。swiftc と iconutil だけを使う(追加依存なし)。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INBOX="${MEETCUE_HOME:-$HOME/.meeting-cue}/icon-inbox"
ART="${1:-$INBOX/D-01d-x4.png}"
MENU="${2:-$INBOX/C-D-01d-02.png}"
PREVIEW="${3:-}"
[[ -f "$ART" && -f "$MENU" ]] || { echo "原画が無い: $ART / $MENU" >&2; exit 1; }

TMP="$(mktemp -d -t meetcue-icon)"
trap 'rm -rf "$TMP"' EXIT
swiftc -O "$HERE/make_icons.swift" -o "$TMP/make_icons" 2> "$TMP/build.log" || { cat "$TMP/build.log" >&2; exit 1; }
"$TMP/make_icons" "$ART" "$MENU" "$TMP/AppIcon.iconset" "$HERE" ${PREVIEW:+"$PREVIEW"}
iconutil -c icns "$TMP/AppIcon.iconset" -o "$HERE/AppIcon.icns"
[[ -n "$PREVIEW" ]] && cp -R "$TMP/AppIcon.iconset" "$PREVIEW/"
ls -l "$HERE/AppIcon.icns" "$HERE"/MenuBarTemplate*.png

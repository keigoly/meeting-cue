#!/bin/bash
# Meeting Cue!.app を作る(swiftc・追加依存なし)。ダブルクリックでウィンドウだけが開く(Terminal は開かない)。
#
#   packaging/make_mac_app.sh [出力先ディレクトリ(既定 ~/Applications)]
#
# 本体は helpers/macos/overlay_helper/main.swift のホストモード: Info.plist の MeetcueLaunchScript(この repo の
# launch.sh の絶対パス)を `serve` で子プロセスとして起動し、その画面を出す。マイク・システム音声録音の許可は
# このアプリ(Meeting Cue!)に付く。作り直すと署名が変わり、許可をもう一度求められることがある。
# repo を動かしたら作り直す。app 自体は git に入れない。デバッグで Terminal に出したいときは packaging/launch.sh open app。
# アイコンは packaging/icon/(AppIcon.icns = アプリ・MenuBarTemplate(@2x).png = メニューバー)を Resources へ入れる。
# 原画から作り直すのは packaging/icon/make_icons.sh。
# Info.plist の MeetcueBuildCommit = 作ったときのコミット(メニューの「アップデートを確認」が作り直しの要否を決める)。
# 名前(2026-09-26): 表示は日英とも「Meeting Cue!」。bundle id(local.meetcue.app)と実行ファイル名(Meeting Cue)は
# 内部名なので据え置く。旧名「Meeting Cue.app」があれば新しい名前へ改名してから中身を作り直す(フォルダを消さないので、
# Dock に置いた項目やログイン項目が付いてきやすい)。
set -euo pipefail

# 作り直しは 1 つずつ(2026-09-27 段 2): 終了時の自動の作り直しと手の作り直しが重なり、片方の rm -rf Contents が
# もう片方の途中を消した(cp: …/Resources/AppIcon.icns: No such file or directory)
if [[ -z "${MEETCUE_APP_LOCKED:-}" ]] && command -v lockf >/dev/null; then
  export MEETCUE_APP_LOCKED=1
  exec lockf -k -t 600 "/tmp/meetcue-make-app-$(id -u).lock" "$0" "$@"
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DIR="${1:-$HOME/Applications}"
RUNTIME="$HOME/Apps/meeting-cue"   # 実行用ツリー(更新係が stable を取り込む)。ここ以外から作るとアプリは自動更新から外れる
if [[ -d "$RUNTIME/.git" && "$(cd "$REPO" && pwd -P)" != "$(cd "$RUNTIME" && pwd -P)" ]]; then
  echo "注意: 実行用ツリー($RUNTIME)ではない repo から作ります。自動更新から外れます(戻すには $RUNTIME/packaging/make_mac_app.sh)" >&2
fi
APP="$DEST_DIR/Meeting Cue!.app"
LEGACY="$DEST_DIR/Meeting Cue.app"   # 2026-09-26 の改名より前の名前
LAUNCHER="$REPO/packaging/launch.sh"
SRC="$REPO/helpers/macos/overlay_helper/main.swift"
ICON="$REPO/packaging/icon"
[[ -f "$LAUNCHER" && -f "$SRC" ]] || { echo "launch.sh / main.swift が無い" >&2; exit 1; }
for f in AppIcon.icns MenuBarTemplate.png MenuBarTemplate@2x.png; do
  [[ -f "$ICON/$f" ]] || { echo "アイコンが無い: $ICON/$f(packaging/icon/make_icons.sh で作る)" >&2; exit 1; }
done
chmod 755 "$LAUNCHER"

TMP="$(mktemp -d -t meetcue-app)"
trap 'rm -rf "$TMP"' EXIT
swiftc -parse-as-library -O "$SRC" -o "$TMP/Meeting Cue" 2> "$TMP/build.log" || { cat "$TMP/build.log" >&2; exit 1; }

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
mkdir -p "$DEST_DIR"
if [[ -d "$LEGACY" && ! -e "$APP" ]] &&
   [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$LEGACY/Contents/Info.plist" 2>/dev/null)" == local.meetcue.app ]]; then
  mv "$LEGACY" "$APP"
  echo "旧名の app を改名: $LEGACY → $APP" >&2
fi
rm -rf "$APP/Contents"   # 中身だけ作り直す(app のフォルダ自体は残す)
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$TMP/Meeting Cue" "$APP/Contents/MacOS/Meeting Cue"
cp "$ICON/AppIcon.icns" "$ICON/MenuBarTemplate.png" "$ICON/MenuBarTemplate@2x.png" "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Meeting Cue!</string>
  <key>CFBundleDisplayName</key><string>Meeting Cue!</string>
  <key>CFBundleIdentifier</key><string>local.meetcue.app</string>
  <key>CFBundleExecutable</key><string>Meeting Cue</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.2</string>
  <key>CFBundleVersion</key><string>$(date +%Y%m%d%H%M)</string>
  <key>LSMinimumSystemVersion</key><string>14.4</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>自分の発言を文字起こしするためにマイクを使います(音声はこの Mac の中だけで処理します)。</string>
  <key>NSAudioCaptureUsageDescription</key><string>会議アプリから聞こえる相手の声を文字起こしするために、この Mac の音声出力を取り込みます。</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>MeetcueLaunchScript</key><string>$(xml "$LAUNCHER")</string>
  <key>MeetcueBuildCommit</key><string>$(git -C "$REPO" rev-parse HEAD 2>/dev/null)</string>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null
codesign --force --sign - "$APP" >/dev/null 2>&1 || true   # ad-hoc 署名(許可の記録に使われる)
# Finder / Dock に新しい名前とアイコンをすぐ反映させる
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true
echo "$APP"

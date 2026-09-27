# packaging/icon/ — DEVELOPMENT.md

## 1. このディレクトリの役割

Meeting Cue! のアイコンの成果物と、それを原画から作り直す道具の置き場。`packaging/make_mac_app.sh` がここの 3 ファイルを `Meeting Cue!.app/Contents/Resources/` へ入れる。

| ファイル | 役割 |
|---|---|
| `AppIcon.icns` | アプリのアイコン(16〜1024・Info.plist の `CFBundleIconFile`)。原画 D-01d-x4.png を macOS の格子に収めたもの |
| `MenuBarTemplate.png` / `@2x.png` | メニューバーのアイコン(18pt のテンプレート画像 = 黒 + アルファ・18 / 36 px)。原画 C-D-01d-02.png の黒の外接矩形を 18pt の枠に縦横比を保って置いたもの。録音中の赤い点は `overlay_helper/main.swift` の `Host.updateIcon` が重ねる(右上・直径 6pt・周り 1.25pt を丸く抜く) |
| `make_icons.sh` | 作り直す: `packaging/icon/make_icons.sh [アプリ用の原画] [メニューバー用の原画] [確認画像の出力先]`。既定の原画は `~/.meeting-cue/icon-inbox/` の採用案。原画は読むだけ |
| `make_icons.swift` | 中身(AppKit / CoreGraphics / Accelerate / SwiftUI だけ・追加依存なし)。格子 = 1024 に 824 の角丸(連続曲率・半径 185.4)+ 影(下 10・ぼかし 10・黒 30%)。各サイズを原画から直接 Lanczos で縮める。メニューバーは面積平均で縮める |


## 2. 現在の問題点(2026-09-26)

- 16 px のアプリのアイコンは顔が読み取りにくく、メニューバーの 18 px ではマイクの細い線が消える(耳当ての白い点と指は残る)。いずれも採用時に想定どおりとした。
- 実際のメニューバーでの見え方(明/暗・録音中の点の位置)は、画面外に描いた `NSStatusItem` のボタンで確かめた。全画面表示を抜けた実機での目視はまだ。

## 3. 作り直す・直すときの手順(user CLAUDE.md の Step 1〜3)

1. **見える化**: `packaging/icon/make_icons.sh "" "" <確認画像の出力先>` で `preview-app.png`(明/暗の背景に 256〜16 px・16 / 32 px は拡大)・`preview-menubar.png`(明/暗のメニューバーに @1x / @2x)・`AppIcon.iconset/` が出る。どのサイズで何が崩れているかを先に見る。
2. **最小改修**: 形の問題は `make_icons.swift` の該当部分だけを直す(格子・影は `appIcon`、縮小は `Pixels.lanczos` / `menuBarTemplate`)。赤い点の大きさ・位置は `main.swift` の `Host.dotDiameter` / `dotGap` と制約だけ。原画を変えるときは受け取り口に新しいファイルを置き、引数で渡す(元のファイルは書き換えない)。
3. **周辺整合**: `make_icons.sh` で成果物を作り直す → `packaging/make_mac_app.sh` で app を作り直す(ad-hoc 署名が変わるため、マイク/システム音声録音の許可をもう一度求められることがある)。

## 4. 関連ドキュメント

- [packaging/DEVELOPMENT.md](../DEVELOPMENT.md)(app の組み立て)・ルートの [DEVELOPMENT.md](../../DEVELOPMENT.md)
- 設計: [docs/REQUIREMENTS.md](../../docs/REQUIREMENTS.md)(FR-7 のメニューバー・FR-13 公開)

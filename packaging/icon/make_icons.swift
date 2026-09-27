// make_icons — Meeting Cue! のアイコン一式を原画から作る(AppKit / CoreGraphics / Accelerate / SwiftUI だけ・追加依存なし)。
//
//   make_icons <アプリ用の原画> <メニューバー用の原画> <iconset の出力先> <メニューバー画像の出力先> [確認画像の出力先]
//
// 1) アプリ: 原画(正方形・角丸なし)を macOS の格子に収める = 1024 の中央に 824 の角丸(連続曲率・半径 185.4)+ 影
//    (下 10・ぼかし 10・黒 30%。いずれも 1024 基準で、各サイズへ比例)。16〜1024 の各サイズを**原画から直接**
//    Lanczos で縮める(1024 を作ってから縮めると 2 回ぼける)。小さいサイズは角丸の辺が画素の境目に乗るよう、
//    角丸の一辺を「サイズとの差が偶数」になる整数に丸める。
// 2) メニューバー: 白地に黒の原画を黒の外接矩形で切り、黒 → 不透明・白 → 透明の単色(テンプレート画像)にして、
//    18pt の枠に縦横比を保って中央に置く(@1x = 18 px / @2x = 36 px)。縮小は面積平均(2 値の形に輪郭のにじみ・
//    リンギングを出さない)。録音中の赤い点はアプリ側で重ねる(overlay_helper の Host.updateIcon)。
// 確認画像: 明/暗の背景に各サイズを並べたもの(16・32 px は 8 倍に拡大した画素も)。

import Accelerate
import AppKit
import SwiftUI   // RoundedRectangle(style: .continuous) の輪郭を借りる(macOS のアイコンと同じ連続曲率の角)

let srgb = CGColorSpace(name: CGColorSpace.sRGB)!
let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data("make_icons: \(msg)\n".utf8))
    exit(1)
}

/// RGBA 8bit(乗算済み・sRGB)の画素。行 0 が画像の上端。
struct Pixels {
    var w: Int
    var h: Int
    var data: [UInt8]

    init(w: Int, h: Int) {
        self.w = w; self.h = h
        data = [UInt8](repeating: 0, count: w * h * 4)
    }

    init(image: CGImage) {
        self.init(w: image.width, h: image.height)
        let (w, h) = (self.w, self.h)
        data.withUnsafeMutableBytes { buf in
            let ctx = CGContext(data: buf.baseAddress, width: w, height: h, bitsPerComponent: 8,
                                bytesPerRow: w * 4, space: srgb, bitmapInfo: bitmapInfo)!
            ctx.interpolationQuality = .none
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        }
    }

    func cgImage() -> CGImage {
        let provider = CGDataProvider(data: Data(data) as CFData)!
        return CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4, space: srgb,
                       bitmapInfo: CGBitmapInfo(rawValue: bitmapInfo), provider: provider, decode: nil,
                       shouldInterpolate: true, intent: .defaultIntent)!
    }

    /// Lanczos(vImage の高品質リサンプル)で dw x dh へ。
    func lanczos(_ dw: Int, _ dh: Int) -> Pixels {
        var out = Pixels(w: dw, h: dh)
        var src = data
        let err = src.withUnsafeMutableBytes { s in
            out.data.withUnsafeMutableBytes { d in
                var sb = vImage_Buffer(data: s.baseAddress, height: vImagePixelCount(h), width: vImagePixelCount(w), rowBytes: w * 4)
                var db = vImage_Buffer(data: d.baseAddress, height: vImagePixelCount(dh), width: vImagePixelCount(dw), rowBytes: dw * 4)
                return vImageScale_ARGB8888(&sb, &db, nil, vImage_Flags(kvImageHighQualityResampling))
            }
        }
        if err != kvImageNoError { fail("vImageScale error \(err)") }
        return out
    }
}

func load(_ path: String) -> CGImage {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { fail("読めない: \(path)") }
    return img
}

func writePNG(_ image: CGImage, _ path: String) {
    let rep = NSBitmapImageRep(cgImage: image)
    guard let png = rep.representation(using: .png, properties: [:]) else { fail("PNG にできない: \(path)") }
    do { try png.write(to: URL(fileURLWithPath: path)) } catch { fail("書けない: \(path): \(error)") }
}

func bitmap(_ w: Int, _ h: Int) -> CGContext {
    CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: srgb, bitmapInfo: bitmapInfo)!
}

// ---- 1) アプリのアイコン ------------------------------------------------------------------

/// 角丸の一辺(px)。1024 基準の 824 を比例させ、サイズとの差が偶数(= 余白が整数)になる最も近い整数へ。
func innerSide(_ size: Int) -> Int {
    let ideal = Double(size) * 824.0 / 1024.0
    var best = size
    for n in stride(from: size % 2, through: size, by: 2) where abs(Double(n) - ideal) < abs(Double(best) - ideal) {
        best = n
    }
    return best
}

func appIcon(_ art: Pixels, size: Int) -> CGImage {
    let n = innerSide(size)
    let off = CGFloat((size - n) / 2)
    let k = CGFloat(size) / 1024.0
    let rect = CGRect(x: off, y: off, width: CGFloat(n), height: CGFloat(n))
    let radius = 185.4 * CGFloat(n) / 824.0
    let shape = RoundedRectangle(cornerRadius: radius, style: .continuous).path(in: rect).cgPath
    let face = art.lanczos(n, n).cgImage()

    let ctx = bitmap(size, size)
    ctx.setShadow(offset: CGSize(width: 0, height: -10 * k), blur: 10 * k,
                  color: CGColor(srgbRed: 0, green: 0, blue: 0, alpha: 0.3))
    ctx.beginTransparencyLayer(auxiliaryInfo: nil)   // 影は角丸で切った絵の形に付ける
    ctx.addPath(shape)
    ctx.clip()
    ctx.interpolationQuality = .none                  // 既に n x n に縮めてある(1:1 で置く)
    ctx.draw(face, in: rect)
    ctx.endTransparencyLayer()
    return ctx.makeImage()!
}

// ---- 2) メニューバー(テンプレート画像) -----------------------------------------------------

/// 黒の濃さ(0〜1)。行 0 が上端。
struct Coverage {
    var w: Int
    var h: Int
    var a: [Double]
    subscript(x: Int, y: Int) -> Double { a[y * w + x] }
}

func coverage(_ image: CGImage) -> Coverage {
    let px = Pixels(image: image)
    var a = [Double](repeating: 0, count: px.w * px.h)
    for i in 0..<(px.w * px.h) {
        let r = Double(px.data[i * 4]), g = Double(px.data[i * 4 + 1]), b = Double(px.data[i * 4 + 2])
        a[i] = 1.0 - (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    }
    return Coverage(w: px.w, h: px.h, a: a)
}

/// 黒(濃さ 0.5 超)の外接矩形で切り出す。
func cropToInk(_ c: Coverage) -> Coverage {
    var (x0, y0, x1, y1) = (c.w, c.h, -1, -1)
    for y in 0..<c.h {
        for x in 0..<c.w where c[x, y] > 0.5 {
            x0 = min(x0, x); x1 = max(x1, x); y0 = min(y0, y); y1 = max(y1, y)
        }
    }
    if x1 < 0 { fail("メニューバーの原画に黒い部分が無い") }
    let (w, h) = (x1 - x0 + 1, y1 - y0 + 1)
    var a = [Double](repeating: 0, count: w * h)
    for y in 0..<h { for x in 0..<w { a[y * w + x] = c[x0 + x, y0 + y] } }
    return Coverage(w: w, h: h, a: a)
}

/// 出力の 1 列(行)が原画のどの列(行)をどれだけ覆うか。原画の座標 = (出力の座標 - origin) / scale。
func areaWeights(dst: Int, src: Int, origin: Double, scale: Double) -> [[(Int, Double)]] {
    (0..<dst).map { i in
        let lo = (Double(i) - origin) / scale, hi = (Double(i + 1) - origin) / scale
        var ws: [(Int, Double)] = []
        let j0 = max(0, Int(floor(lo))), j1 = min(src - 1, Int(ceil(hi)) - 1)
        if j0 <= j1 {
            for j in j0...j1 {
                let overlap = min(hi, Double(j + 1)) - max(lo, Double(j))
                if overlap > 0 { ws.append((j, overlap * scale)) }   // 出力 1 画素に占める割合
            }
        }
        return ws
    }
}

/// 18pt の枠に縦横比を保って中央に置き、scale 倍(1 = @1x / 2 = @2x)の画素で面積平均する。黒 + アルファ。
func menuBarTemplate(_ ink: Coverage, pt: Double, scale: Int) -> CGImage {
    let side = Int(pt) * scale
    let fit = min(pt / Double(ink.w), pt / Double(ink.h)) * Double(scale)
    let ox = (Double(side) - Double(ink.w) * fit) / 2, oy = (Double(side) - Double(ink.h) * fit) / 2
    let wx = areaWeights(dst: side, src: ink.w, origin: ox, scale: fit)
    let wy = areaWeights(dst: side, src: ink.h, origin: oy, scale: fit)
    var out = Pixels(w: side, h: side)
    for y in 0..<side {
        for x in 0..<side {
            var sum = 0.0
            for (sy, fy) in wy[y] { for (sx, fx) in wx[x] { sum += ink[sx, sy] * fx * fy } }
            out.data[(y * side + x) * 4 + 3] = UInt8(max(0, min(255, (sum * 255).rounded())))   // RGB は 0(黒・乗算済み)
        }
    }
    return out.cgImage()
}

// ---- 確認画像 ------------------------------------------------------------------------------

func drawNearest(_ ctx: CGContext, _ img: CGImage, _ rect: CGRect) {
    ctx.interpolationQuality = .none
    ctx.draw(img, in: rect)
}

/// 明/暗の背景にアプリのアイコンを 256・128・64・32・16 で並べ、32 と 16 は 8 倍の画素も出す。
func appPreview(_ icons: [Int: CGImage]) -> CGImage {
    let sizes = [256, 128, 64, 32, 16]
    let rowH = 300, width = sizes.reduce(0) { $0 + $1 + 24 } + 24 + 256 + 24 + 128 + 24
    let ctx = bitmap(width, rowH * 2)
    for (row, bg) in [(1, 0.93), (0, 0.12)] {
        ctx.setFillColor(CGColor(gray: bg, alpha: 1))
        ctx.fill(CGRect(x: 0, y: row * rowH, width: width, height: rowH))
        var x = 24
        for s in sizes {
            drawNearest(ctx, icons[s]!, CGRect(x: x, y: row * rowH + 22, width: s, height: s))
            x += s + 24
        }
        drawNearest(ctx, icons[32]!, CGRect(x: x, y: row * rowH + 22, width: 256, height: 256)); x += 256 + 24
        drawNearest(ctx, icons[16]!, CGRect(x: x, y: row * rowH + 22, width: 128, height: 128))
    }
    return ctx.makeImage()!
}

/// 明/暗のメニューバー(高さ 24pt 相当)にテンプレートを黒/白で置く。@1x・@2x を等倍と 8 倍で。
func menuBarPreview(_ t1: CGImage, _ t2: CGImage) -> CGImage {
    let width = 24 + 18 + 24 + 36 + 24 + 144 + 24 + 288 + 24, rowH = 312
    let ctx = bitmap(width, rowH * 2)
    for (row, bg, fg) in [(1, 0.93, 0.0), (0, 0.16, 1.0)] {
        ctx.setFillColor(CGColor(gray: bg, alpha: 1))
        ctx.fill(CGRect(x: 0, y: row * rowH, width: width, height: rowH))
        var x = 24
        for (img, side, zoom) in [(t1, 18, 1), (t2, 36, 1), (t1, 18, 8), (t2, 36, 8)] {
            let r = CGRect(x: x, y: row * rowH + 12, width: side * zoom, height: side * zoom)
            ctx.saveGState()
            ctx.clip(to: r, mask: img)           // テンプレート = アルファだけを使って前景色で塗る
            ctx.setFillColor(CGColor(gray: fg, alpha: 1))
            ctx.fill(r)
            ctx.restoreGState()
            x += side * zoom + 24
        }
    }
    return ctx.makeImage()!
}

// ---- main ----------------------------------------------------------------------------------

let args = CommandLine.arguments
guard args.count >= 5 else {
    fail("usage: make_icons <app-art.png> <menubar-art.png> <AppIcon.iconset> <menubar-out-dir> [preview-dir]")
}
let (artPath, menuPath, iconsetDir, menuDir) = (args[1], args[2], args[3], args[4])
let previewDir = args.count > 5 ? args[5] : nil
let fm = FileManager.default
for d in [iconsetDir, menuDir] + (previewDir.map { [$0] } ?? []) {
    try? fm.createDirectory(atPath: d, withIntermediateDirectories: true)
}

let art = load(artPath)
guard art.width == art.height else { fail("アプリ用の原画は正方形であること(\(art.width)x\(art.height))") }
let artPx = Pixels(image: art)
var icons: [Int: CGImage] = [:]
for s in [16, 32, 64, 128, 256, 512, 1024] {
    icons[s] = appIcon(artPx, size: s)
    print("app \(s)px: 角丸 \(innerSide(s))px")
}
for (name, s) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128),
                  ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)] {
    writePNG(icons[s]!, "\(iconsetDir)/icon_\(name).png")
}

let ink = cropToInk(coverage(load(menuPath)))
let t1 = menuBarTemplate(ink, pt: 18, scale: 1)
let t2 = menuBarTemplate(ink, pt: 18, scale: 2)
writePNG(t1, "\(menuDir)/MenuBarTemplate.png")
writePNG(t2, "\(menuDir)/MenuBarTemplate@2x.png")
print("menubar: 外接矩形 \(ink.w)x\(ink.h)px → 18pt(@1x 18px / @2x 36px)")

if let previewDir {
    writePNG(appPreview(icons), "\(previewDir)/preview-app.png")
    writePNG(menuBarPreview(t1, t2), "\(previewDir)/preview-menubar.png")
    print("preview: \(previewDir)")
}

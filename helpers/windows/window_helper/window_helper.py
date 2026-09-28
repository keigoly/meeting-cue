"""window_helper(Windows)— 本体の画面(Web UI)を pywebview(WebView2)のウィンドウに出す。Mac の overlay_helper に当たる。

起動(本体 app.py が、アプリ用の仮想環境 ~/.meeting-cue/app-venv の python で動かす):
  window_helper.py --window --url http://127.0.0.1:8765/       本体のウィンドウ(閉じるとこのプロセスが終わる → 本体も終わる)
  window_helper.py --caption --url http://127.0.0.1:8765/caption.html   ライブ字幕のウィンドウだけ(確かめる用)
  window_helper.py --stamp-shortcut <.lnk>    ショートカットにウィンドウと同じ AppUserModelID を書いて終わる(setup.ps1 が使う)
stdin(1 行 1 コマンド・本体から): "top on" / "top off" / "caption"(字幕のウィンドウを開く)/ "quit"。EOF では止めない。
stderr: 診断 JSON(phase=shown / bridge / closed / error)。

画面との橋渡し(REQUIREMENTS FR-12): 画面の HTML は Mac の WKWebView 向けに
window.webkit.messageHandlers.meetcue.postMessage({cmd, …}) を呼ぶ。同じ名前の写しを差し込み、pywebview の API へつなぐ。
cmd: top = 常に手前 / main = 本体のウィンドウを前へ / caption = ライブ字幕のウィンドウを開く / theme = 外観が変わった
(タイトルバーの色を合わせ直す)/ drag = 何もしない(枠付きのウィンドウなのでドラッグの範囲は要らない)/
pin = 字幕の固定(クリックを下へ通す)は次の版で、今は常に手前だけ。
写しは画面の読み込みの後に入るので、読み込みの途中の呼び出しは届かない。代わりに写しが入った時点で外観を測って知らせる。

見た目(2026-09-28 keigoly様「アイコンが出ない・白いメニューがダサい・ちゃんとダークモードに」):
- アイコン: AppUserModelID(keigoly.MeetingCue)を名乗ってタスクバーで python と分け、WM_SETICON で Meeting Cue! のアイコンを付ける。
  スタートメニューのショートカットにも同じ ID を書く(--stamp-shortcut)。書かないと、動いているウィンドウをタスクバーに
  ピン留めしたとき python.exe(引数なし・python のアイコン)がピン留めされ、以後のウィンドウもその python のアイコンに
  まとめられる(2026-09-28 keigoly様「再起動しても Python のアイコンが残る」。実物は ID = keigoly.MeetingCue の Python.lnk)
- 白いメニュー(WinForms の MenuStrip)をやめた。常に手前は画面の右上、終了はウィンドウの ×、ライブ字幕は画面の
  「ライブ字幕」ボタン(写しが window.meetcueHost = {caption: true} で名乗ったときだけ画面が出す。Mac はメニューバーから)
- タイトルバーを画面の外観にそろえる(DWM の immersive dark mode)。画面の背景の明るさで判断し、外観の変更・OS の変更に追従する
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(os.environ.get("MEETCUE_HOME") or (Path.home() / ".meeting-cue"))
REPO = Path(__file__).resolve().parents[3]
AUMID = "keigoly.MeetingCue"
DARK_BG, LIGHT_BG = "#0f1923", "#ffffff"   # 画面が読み込まれるまでのウィンドウの地の色(白く光らせない)

SHIM = r"""
(function () {
  const send = (m) => {
    const go = () => window.pywebview.api.post(m);
    if (window.pywebview && window.pywebview.api && window.pywebview.api.post) go();
    else window.addEventListener('pywebviewready', go, {once: true});
  };
  // 画面の背景の明るさ → タイトルバーの色。背景が透明なら html、それも無ければ OS の設定
  const bg = (el) => { const v = (getComputedStyle(el).backgroundColor.match(/[\d.]+/g) || []).map(Number);
                       return v.length >= 3 && (v.length < 4 || v[3] > 0) ? v : null; };
  const dark = () => { const v = bg(document.body) || bg(document.documentElement);
                       return v ? (0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]) < 128
                                : matchMedia('(prefers-color-scheme: dark)').matches; };
  const appearance = () => setTimeout(() => send({cmd: 'appearance', dark: dark()}), 60);
  if (!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.meetcue)) {
    window.webkit = window.webkit || {};
    window.webkit.messageHandlers = window.webkit.messageHandlers || {};
    window.webkit.messageHandlers.meetcue = {postMessage: (m) => { send(m); if (m && m.cmd === 'theme') appearance(); }};
  }
  window.meetcueHost = {caption: true};          // ライブ字幕のウィンドウを開けるホスト(画面が「ライブ字幕」ボタンを出す)
  window.dispatchEvent(new Event('meetcue-host'));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', appearance);
  appearance();
})();
"""


def diag(fields: dict) -> None:
    sys.stderr.write(json.dumps(dict(fields, t_ms=int(time.time() * 1000)), ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


# ---- Win32(ctypes・依存なし)-----------------------------------------------------------------------------
def system_dark() -> bool:
    """OS のアプリの外観(設定 → 個人用設定 → 色)がダークか。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def ensure_icon() -> Path | None:
    """~/.meeting-cue/MeetingCue.ico(setup.ps1 が作る)。無ければ docs/images/icon.png(256 px)を ICO に包んで作る。"""
    ico = APP_DIR / "MeetingCue.ico"
    if ico.exists():
        return ico
    png = REPO / "docs" / "images" / "icon.png"
    if not png.exists():
        return None
    data = png.read_bytes()
    ico.parent.mkdir(parents=True, exist_ok=True)
    ico.write_bytes(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 22) + data)
    return ico


def _hwnd(w) -> int | None:
    try:
        return int(w.native.Handle.ToInt64())
    except Exception:
        return None


def set_icon(w, ico: Path | None) -> None:
    """ウィンドウ(タイトルバー・タスクバー・Alt+Tab)のアイコンを Meeting Cue! にする。"""
    hwnd = _hwnd(w)
    if not hwnd or not ico:
        return
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    for which, metric in ((1, 11), (0, 49)):   # ICON_BIG = SM_CXICON / ICON_SMALL = SM_CXSMICON の大きさで読む
        size = user32.GetSystemMetrics(metric)
        h = user32.LoadImageW(None, str(ico), 1, size, size, 0x10)   # IMAGE_ICON・LR_LOADFROMFILE
        if h:
            user32.SendMessageW(hwnd, 0x80, which, h)                 # WM_SETICON


def set_dark_title(w, dark: bool) -> None:
    """タイトルバーをダーク / ライトにする(DWMWA_USE_IMMERSIVE_DARK_MODE。古い Windows 10 は 19)。"""
    hwnd = _hwnd(w)
    if not hwnd:
        return
    val = ctypes.c_int(1 if dark else 0)
    for attr in (20, 19):
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(val), 4) == 0:
            break
    # 枠を描き直す(SWP_NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED)
    ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)


# ---- ショートカットの AppUserModelID(COM を ctypes で呼ぶ)------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, s: str):
        super().__init__()
        ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(self))


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PROPVARIANT(ctypes.Structure):   # vt と 16 バイトの値(ここで使うのは VT_LPWSTR = 31 の文字列だけ)
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]


def _com(obj: ctypes.c_void_p, index: int, *argtypes):
    """COM の vtable の index 番目のメソッド(失敗の HRESULT は OSError になる)。"""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return lambda *a: ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])(obj, *a)


def stamp_shortcut(lnk: Path, aumid: str = AUMID) -> str:
    """ショートカット(.lnk)に AppUserModelID を書き、読み直した値を返す。ほかの項目(実行先・引数・アイコン)は変えない。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    link, pf, ps = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    key = _PROPERTYKEY(_GUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)   # PKEY_AppUserModel_ID
    hr = ole32.CoCreateInstance(ctypes.byref(_GUID("{00021401-0000-0000-C000-000000000046}")), None, 1,   # ShellLink
                                ctypes.byref(_GUID("{000214F9-0000-0000-C000-000000000046}")), ctypes.byref(link))
    if hr != 0:
        raise OSError(f"CoCreateInstance(ShellLink) 0x{hr & 0xFFFFFFFF:08x}")
    try:
        _com(link, 0, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(_GUID("{0000010b-0000-0000-C000-000000000046}")), ctypes.byref(pf))
        _com(pf, 5, ctypes.c_wchar_p, ctypes.c_uint32)(str(lnk), 2)                        # IPersistFile.Load(STGM_READWRITE)
        _com(link, 0, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(_GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")), ctypes.byref(ps))
        buf = ctypes.create_unicode_buffer(aumid)
        _com(ps, 6, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(key), ctypes.byref(_PROPVARIANT(31, 0, 0, 0, ctypes.addressof(buf), None)))
        _com(ps, 7)()                                                                          # IPropertyStore.Commit
        _com(pf, 6, ctypes.c_wchar_p, ctypes.c_int)(None, 1)                                   # IPersistFile.Save(読んだファイルへ)
        got = _PROPVARIANT()
        _com(ps, 5, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(key), ctypes.byref(got))  # IPropertyStore.GetValue
        value = ctypes.wstring_at(got.p) if got.vt == 31 and got.p else ""
        ole32.PropVariantClear(ctypes.byref(got))
        return value
    finally:
        for o in (ps, pf, link):
            if o.value:
                _com(o, 2)()                                                                   # Release


class Bridge:
    """JS に出すのは post だけ(pywebview は js_api の公開属性をたどって出すので、Host をそのまま渡さない)。"""

    def __init__(self, host: "Host"):
        self._host = host   # _ で始まる属性は JS に出ない

    def post(self, m: dict) -> None:
        self._host.post(m)


class Host:
    """ウィンドウの持ち主。画面(JS)と stdin の 2 か所から同じ操作を受ける。"""

    def __init__(self, webview, base_url: str, icon: Path | None):
        self.webview = webview
        self.base = base_url.rstrip("/")
        self.icon = icon
        self.main = None
        self.caption = None
        self.top = False
        self.dark = system_dark()
        self.bridge = Bridge(self)

    # 画面からの呼び出し(window.pywebview.api.post → Bridge.post)
    def post(self, m: dict) -> None:
        cmd = (m or {}).get("cmd")
        if cmd != "appearance":
            diag({"phase": "bridge", "cmd": cmd})
        if cmd == "top":
            self.set_top(bool(m.get("on")))
        elif cmd == "pin" and self.caption is not None:   # 字幕の固定は次の版。今は常に手前だけ合わせる
            self.caption.on_top = True
        elif cmd == "main":
            self.show_main()
        elif cmd == "caption":
            self.open_caption()
        elif cmd == "appearance" and self.main is not None:
            self.dark = bool(m.get("dark"))
            set_dark_title(self.main, self.dark)

    def set_top(self, on: bool) -> None:
        self.top = on
        if self.main is not None:
            self.main.on_top = on

    def show_main(self) -> None:
        if self.main is not None:
            self.main.restore()
            self.main.show()

    def decorate(self, w, dark: bool) -> None:
        set_icon(w, self.icon)
        set_dark_title(w, dark)

    def open_caption(self) -> None:
        if self.caption is not None:
            self.caption.restore()
            self.caption.show()
            return
        self.caption = self.webview.create_window(
            "Meeting Cue! ライブ字幕", f"{self.base}/caption.html", js_api=self.bridge, width=900, height=240,
            min_size=(360, 140), on_top=True, text_select=True, background_color=DARK_BG)
        cap = self.caption
        cap.events.shown += lambda: self.decorate(cap, True)   # 字幕の画面はいつも暗い
        cap.events.loaded += lambda: self._inject(cap)
        cap.events.closed += self._caption_closed
        diag({"phase": "shown", "mode": "caption"})

    def _caption_closed(self) -> None:
        self.caption = None
        diag({"phase": "closed", "mode": "caption"})

    def _inject(self, w) -> None:
        try:
            w.run_js(SHIM)
        except Exception as e:   # 橋渡しが無くても画面は動く(常に手前などが効かないだけ)
            diag({"phase": "error", "where": "inject", "error": f"{type(e).__name__}: {e}"})

    def quit(self) -> None:
        for w in list(self.webview.windows):
            w.destroy()


def read_commands(host: Host) -> None:
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "quit":
            host.quit()
            return
        if cmd in ("top on", "top off"):
            host.set_top(cmd == "top on")
        elif cmd == "caption":
            host.open_caption()


def main() -> int:
    for s in (sys.stdout, sys.stderr, sys.stdin):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Meeting Cue! の Windows 用ウィンドウ(pywebview)")
    ap.add_argument("--url")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--window", action="store_true", help="本体のウィンドウ(既定)")
    mode.add_argument("--caption", action="store_true", help="ライブ字幕のウィンドウだけ")
    mode.add_argument("--stamp-shortcut", metavar="LNK", help="ショートカットにウィンドウと同じ AppUserModelID を書いて終わる")
    ap.add_argument("--width", type=int, default=1180)
    ap.add_argument("--height", type=int, default=760)
    args = ap.parse_args()
    if args.stamp_shortcut:
        try:
            got = stamp_shortcut(Path(args.stamp_shortcut))
        except OSError as e:
            diag({"phase": "error", "where": "stamp_shortcut", "lnk": args.stamp_shortcut, "error": str(e)})
            return 1
        diag({"phase": "stamped", "lnk": args.stamp_shortcut, "aumid": got, "ok": got == AUMID})
        return 0 if got == AUMID else 1
    if not args.url:
        ap.error("--url が要る(--window / --caption)")
    try:
        import webview
    except ImportError as e:
        diag({"phase": "abort", "reason": "packages_missing", "error": str(e),
              "hint": "packaging\\windows\\setup.ps1 が作るアプリ用の仮想環境の python で動かす"})
        return 7

    try:   # タスクバーで python と分け、Meeting Cue! として並べる(ウィンドウを作る前に)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(AUMID))
    except (AttributeError, OSError) as e:
        diag({"phase": "error", "where": "aumid", "error": str(e)})
    url = args.url
    base = url.rsplit("/", 1)[0] if url.endswith(".html") else url
    host = Host(webview, base, ensure_icon())
    if args.caption:
        host.open_caption()
    else:
        main_w = host.main = webview.create_window(
            "Meeting Cue!", url, js_api=host.bridge, width=args.width, height=args.height, min_size=(720, 480),
            text_select=True, background_color=DARK_BG if host.dark else LIGHT_BG)
        main_w.events.shown += lambda: host.decorate(main_w, host.dark)
        main_w.events.loaded += lambda: host._inject(main_w)
        # 本体のウィンドウを閉じたら、字幕のウィンドウも閉じて終わる(本体はこのプロセスの終了でアプリを終える)
        main_w.events.closed += host.quit
        diag({"phase": "shown", "mode": "window", "url": url})
    threading.Thread(target=read_commands, args=(host,), daemon=True).start()
    storage = APP_DIR / "webview"   # 画面の localStorage(字幕の設定など)を残す
    storage.mkdir(parents=True, exist_ok=True)
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(storage))
    diag({"phase": "closed", "mode": "all"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

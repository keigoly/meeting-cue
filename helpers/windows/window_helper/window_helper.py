"""window_helper(Windows)— 本体の画面(Web UI)を pywebview(WebView2)のウィンドウに出す。Mac の overlay_helper に当たる。

起動(本体 app.py が、アプリ用の仮想環境 ~/.meeting-cue/app-venv の python で動かす):
  window_helper.py --window --url http://127.0.0.1:8765/       本体のウィンドウ(閉じるとこのプロセスが終わる → 本体も終わる)
  window_helper.py --caption --url http://127.0.0.1:8765/caption.html   ライブ字幕のウィンドウだけ(確かめる用)
stdin(1 行 1 コマンド・本体から): "top on" / "top off" / "caption"(字幕のウィンドウを開く)/ "quit"。EOF では止めない。
stderr: 診断 JSON(phase=shown / bridge / closed / error)。

画面との橋渡し(REQUIREMENTS FR-12): 画面の HTML は Mac の WKWebView 向けに
window.webkit.messageHandlers.meetcue.postMessage({cmd, …}) を呼ぶ。同じ名前の写しを差し込み、pywebview の API へつなぐ
(HTML は変えない)。cmd: top = 常に手前 / main = 本体のウィンドウを前へ / theme・drag = 何もしない(外観は OS に従い、
枠付きのウィンドウなのでドラッグの範囲も要らない)/ pin = 字幕の固定(クリックを下へ通す)は次の版で、今は常に手前だけ。
写しは画面の読み込みの後に入るので、読み込みの途中の呼び出し(外観の初期化など)は届かない(どれも何もしない cmd)。
メニュー: 表示 → ライブ字幕 / 常に手前、Meeting Cue! → 終了(Mac のメニューバーの代わり。タスクトレイは次の版)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(os.environ.get("MEETCUE_HOME") or (Path.home() / ".meeting-cue"))

SHIM = """
(function () {
  if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.meetcue) return;
  const send = (m) => {
    const go = () => window.pywebview.api.post(m);
    if (window.pywebview && window.pywebview.api && window.pywebview.api.post) go();
    else window.addEventListener('pywebviewready', go, {once: true});
  };
  window.webkit = window.webkit || {};
  window.webkit.messageHandlers = window.webkit.messageHandlers || {};
  window.webkit.messageHandlers.meetcue = {postMessage: send};
})();
"""


def diag(fields: dict) -> None:
    sys.stderr.write(json.dumps(dict(fields, t_ms=int(time.time() * 1000)), ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


class Bridge:
    """JS に出すのは post だけ(pywebview は js_api の公開属性をたどって出すので、Host をそのまま渡さない)。"""

    def __init__(self, host: "Host"):
        self._host = host   # _ で始まる属性は JS に出ない

    def post(self, m: dict) -> None:
        self._host.post(m)


class Host:
    """ウィンドウの持ち主。画面(JS)・メニュー・stdin の 3 か所から同じ操作を受ける。"""

    def __init__(self, webview, base_url: str):
        self.webview = webview
        self.base = base_url.rstrip("/")
        self.main = None
        self.caption = None
        self.top = False
        self.bridge = Bridge(self)

    # 画面からの呼び出し(window.pywebview.api.post → Bridge.post)
    def post(self, m: dict) -> None:
        cmd = (m or {}).get("cmd")
        diag({"phase": "bridge", "cmd": cmd})
        if cmd == "top":
            self.set_top(bool(m.get("on")))
        elif cmd == "pin" and self.caption is not None:   # 字幕の固定は次の版。今は常に手前だけ合わせる
            self.caption.on_top = True
        elif cmd == "main":
            self.show_main()

    def set_top(self, on: bool) -> None:
        self.top = on
        if self.main is not None:
            self.main.on_top = on

    def show_main(self) -> None:
        if self.main is not None:
            self.main.restore()
            self.main.show()

    def open_caption(self) -> None:
        if self.caption is not None:
            self.caption.restore()
            self.caption.show()
            return
        self.caption = self.webview.create_window(
            "Meeting Cue! ライブ字幕", f"{self.base}/caption.html", js_api=self.bridge, width=900, height=240,
            min_size=(360, 140), on_top=True, text_select=True, background_color="#111111")
        self.caption.events.loaded += lambda: self._inject(self.caption)
        self.caption.events.closed += self._caption_closed
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

    def menu(self):
        from webview.menu import Menu, MenuAction, MenuSeparator
        return [Menu("Meeting Cue!", [MenuAction("終了", self.quit)]),
                Menu("表示", [MenuAction("ライブ字幕", self.open_caption), MenuSeparator(),
                              MenuAction("常に手前(切り替え)", lambda: self.set_top(not self.top))])]


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
    ap.add_argument("--url", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--window", action="store_true", help="本体のウィンドウ(既定)")
    mode.add_argument("--caption", action="store_true", help="ライブ字幕のウィンドウだけ")
    ap.add_argument("--width", type=int, default=1180)
    ap.add_argument("--height", type=int, default=760)
    args = ap.parse_args()
    try:
        import webview
    except ImportError as e:
        diag({"phase": "abort", "reason": "packages_missing", "error": str(e),
              "hint": "packaging\\windows\\setup.ps1 が作るアプリ用の仮想環境の python で動かす"})
        return 7

    url = args.url
    base = url.rsplit("/", 1)[0] if url.endswith(".html") else url
    host = Host(webview, base)
    if args.caption:
        host.open_caption()
        host.main = None
    else:
        host.main = webview.create_window("Meeting Cue!", url, js_api=host.bridge, width=args.width, height=args.height,
                                          min_size=(720, 480), text_select=True, menu=host.menu())
        host.main.events.loaded += lambda: host._inject(host.main)
        # 本体のウィンドウを閉じたら、字幕のウィンドウも閉じて終わる(本体はこのプロセスの終了でアプリを終える)
        host.main.events.closed += host.quit
        diag({"phase": "shown", "mode": "window", "url": url})
    threading.Thread(target=read_commands, args=(host,), daemon=True).start()
    storage = APP_DIR / "webview"   # 画面の localStorage(字幕の設定など)を残す
    storage.mkdir(parents=True, exist_ok=True)
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(storage))
    diag({"phase": "closed", "mode": "all"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

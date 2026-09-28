"""Meeting Cue! の Windows の起動の入口(スタートメニューのショートカットが pythonw.exe で開く・Mac の launch.sh serve に当たる)。

- 本体(`python -X utf8 -m meetcue.cli app`)を、見えないコンソールの中で動かす。pythonw から直接動かすと、本体が起動する
  ヘルパー(python.exe)ごとに黒い窓が開くため。UTF-8 モードで動かす(Windows の既定の cp932 だと文字の表示で落ちる)。
- 出力は ~/.meeting-cue/logs/app-<日時>.log(20 個まで残す)。本体のウィンドウを閉じると本体も終わる。
- 既にポート 8765 で本体が動いていれば、本体は起動せずウィンドウだけ開く(そのウィンドウを閉じても本体は止めない)。
標準ライブラリだけ。python はアプリ用の仮想環境(~/.meeting-cue/app-venv・setup.ps1 が作る)。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_DIR = Path(os.environ.get("MEETCUE_HOME") or (Path.home() / ".meeting-cue"))
PY = Path(sys.executable).with_name("python.exe")   # pythonw.exe の隣の python.exe
# 本体の meetcue.config.default_port() と同じ決まり: MEETCUE_PORT があればそれ、無ければ 8765(Windows は uid が無い)
_env_port = os.environ.get("MEETCUE_PORT", "")
PORT = int(_env_port) if _env_port.isdigit() and 1024 <= int(_env_port) <= 65535 else 8765
CREATE_NO_WINDOW = 0x08000000


def running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main() -> int:
    logs = APP_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for old in sorted(logs.glob("app-*.log"))[:-19]:
        old.unlink(missing_ok=True)
    if running():
        subprocess.Popen([str(PY), str(REPO / "helpers" / "windows" / "window_helper" / "window_helper.py"),
                          "--window", "--url", f"http://127.0.0.1:{PORT}/"], cwd=REPO, creationflags=CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    log = open(logs / f"app-{time.strftime('%Y%m%d_%H%M%S')}.log", "ab")
    subprocess.Popen([str(PY), "-X", "utf8", "-m", "meetcue.cli", "app"], cwd=REPO, creationflags=CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())

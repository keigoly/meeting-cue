"""秘密の読み込み。API キーは Mac のキーチェーン → 環境変数 → `~/.secrets/meeting-cue.env`(KEY=VALUE)の順に探す。

- キーチェーン(2026-09-26・設定画面から登録): サービス名 `local.meetcue`・アカウント = 接続先(openrouter / anthropic / openai)。
  書き込みは `security -i` に標準入力で渡す(コマンドラインにキーを出さない)。読むのは `security find-generic-password -w`。
- 既存の `~/.secrets/meeting-cue.env` はそのまま読む(書き換えない)。
- ファイルやキーが無くても落とさない(鍵なし = クラウド無しで動く)。キーの中身を画面・ログへ出さない。
- Windows の保管方法は別途(ここではキーチェーンを使わず、環境変数とファイルだけ)。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_ENV = Path.home() / ".secrets" / "meeting-cue.env"
KEYCHAIN_SERVICE = os.environ.get("MEETCUE_KEYCHAIN_SERVICE") or "local.meetcue"   # 試験では別の名前にする
PROVIDERS = {"openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
KEY_RE = re.compile(r"^[A-Za-z0-9_\-.]{16,400}$")   # 各社のキーの文字(空白・引用符は入らない)


def load_env(path: Path | str | None = None) -> dict[str, str]:
    p = Path(path) if path else DEFAULT_ENV
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
        out[k] = v
    return out


def keychain_supported() -> bool:
    return sys.platform == "darwin"


def _security(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["security", *args], input=stdin, capture_output=True, text=True, timeout=15)


def keychain_get(provider: str) -> str | None:
    if provider not in PROVIDERS or not keychain_supported():
        return None
    try:
        r = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider, "-w"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def keychain_set(provider: str, key: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    if not keychain_supported():
        raise RuntimeError("キーチェーンは Mac だけ(Windows の保管方法は別途)")
    key = (key or "").strip()
    if not KEY_RE.match(key):
        raise ValueError("キーの形が正しくありません(空白や引用符を含めない)")
    cmd = f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a {provider} -l "Meeting Cue! {provider}" -w {key}\n'
    r = _security(["-i"], stdin=cmd)   # 標準入力で渡す(ps にキーが出ない)
    if r.returncode != 0 or keychain_get(provider) != key:
        raise RuntimeError("キーチェーンに保存できませんでした")


def keychain_delete(provider: str) -> bool:
    if provider not in PROVIDERS or not keychain_supported():
        return False
    try:
        return _security(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def api_key(provider: str) -> tuple[str | None, str | None]:
    """(キー, 出どころ)。出どころは "keychain" / "env"(環境変数か ~/.secrets のファイル)/ None。"""
    k = keychain_get(provider)
    if k:
        return k, "keychain"
    load_env()
    v = os.environ.get(PROVIDERS.get(provider, ""))
    return (v, "env") if v else (None, None)


def key_status() -> dict[str, dict]:
    """画面用: 接続先ごとの {"set": bool, "source": …}。キーの中身は返さない。"""
    return {p: {"set": bool(k), "source": src} for p in PROVIDERS for k, src in [api_key(p)]}


def openrouter_key() -> str | None:
    return api_key("openrouter")[0]

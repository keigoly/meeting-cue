"""秘密の読み込み。API キーは Mac のキーチェーン → 環境変数 → `~/.secrets/meeting-cue.env`(KEY=VALUE)の順に探す。

- キーチェーン(2026-09-26・設定画面から登録): サービス名 `local.meetcue`・アカウント = 接続先(openrouter / anthropic / openai)。
  書き込みは `security -i` に標準入力で渡す(コマンドラインにキーを出さない)。読むのは `security find-generic-password -w`。
- 既存の `~/.secrets/meeting-cue.env` はそのまま読む(書き換えない)。
- ファイルやキーが無くても落とさない(鍵なし = クラウド無しで動く)。キーの中身を画面・ログへ出さない。
- Windows(2026-09-27 keigoly様): 資格情報マネージャー(汎用資格情報 `local.meetcue/<接続先>`)。関数は同じ名前
  (keychain_*)で、中で OS を分ける。出どころの表示も同じ "keychain"。
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
    return sys.platform in ("darwin", "win32")


def _security(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["security", *args], input=stdin, capture_output=True, text=True, timeout=15)


# ---- Windows: 資格情報マネージャー(2026-09-27 keigoly様・Mac のキーチェーンに当たる)---------------------------
# ctypes で advapi32 の CredReadW / CredWriteW / CredDeleteW を呼ぶ(依存なし)。汎用資格情報の名前は
# `<KEYCHAIN_SERVICE>/<接続先>`・中身は UTF-8。コマンドラインを通さないので、キーが他のプロセスから見えない。
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def _wincred():
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    pcred = ctypes.POINTER(CREDENTIALW)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pcred)]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredWriteW.argtypes = [pcred, wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    return ctypes, CREDENTIALW, adv


def _wincred_name(provider: str) -> str:
    return f"{KEYCHAIN_SERVICE}/{provider}"


def _wincred_get(provider: str) -> str | None:
    ctypes, CREDENTIALW, adv = _wincred()
    p = ctypes.POINTER(CREDENTIALW)()
    if not adv.CredReadW(_wincred_name(provider), _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8") or None
    finally:
        adv.CredFree(p)


def _wincred_set(provider: str, key: str) -> None:
    ctypes, CREDENTIALW, adv = _wincred()
    data = key.encode("utf-8")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    c = CREDENTIALW()
    c.Type, c.Persist = _CRED_TYPE_GENERIC, _CRED_PERSIST_LOCAL_MACHINE
    c.TargetName, c.UserName, c.Comment = _wincred_name(provider), provider, f"Meeting Cue! {provider}"
    c.CredentialBlobSize, c.CredentialBlob = len(data), ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    if not adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError(ctypes.get_last_error(), "CredWriteW")


def _wincred_delete(provider: str) -> bool:
    _, _, adv = _wincred()
    return bool(adv.CredDeleteW(_wincred_name(provider), _CRED_TYPE_GENERIC, 0))


def keychain_get(provider: str) -> str | None:
    if provider not in PROVIDERS or not keychain_supported():
        return None
    if sys.platform == "win32":
        try:
            return _wincred_get(provider)
        except OSError:
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
        raise RuntimeError("キーの保管庫が無い OS です(Mac のキーチェーン・Windows の資格情報マネージャーだけ)")
    key = (key or "").strip()
    if not KEY_RE.match(key):
        raise ValueError("キーの形が正しくありません(空白や引用符を含めない)")
    if sys.platform == "win32":
        try:
            _wincred_set(provider, key)
        except OSError as e:
            raise RuntimeError("資格情報マネージャーに保存できませんでした") from e
        if keychain_get(provider) != key:
            raise RuntimeError("資格情報マネージャーに保存できませんでした")
        return
    cmd =f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a {provider} -l "Meeting Cue! {provider}" -w {key}\n'
    r = _security(["-i"], stdin=cmd)   # 標準入力で渡す(ps にキーが出ない)
    if r.returncode != 0 or keychain_get(provider) != key:
        raise RuntimeError("キーチェーンに保存できませんでした")


def keychain_delete(provider: str) -> bool:
    if provider not in PROVIDERS or not keychain_supported():
        return False
    if sys.platform == "win32":
        try:
            return _wincred_delete(provider)
        except OSError:
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

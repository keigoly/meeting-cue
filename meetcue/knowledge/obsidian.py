"""Obsidian 連携(FR-5b・2026-10-08)— 保管庫の一覧・Vault の場所の決め方・ノートを Obsidian で開く。stdlib のみ。

- 保管庫の一覧: Obsidian 本体が持つ `obsidian.json` の `vaults`(Mac `~/Library/Application Support/obsidian/`・
  Windows `%APPDATA%\\obsidian\\`・Linux `~/.config/obsidian/`)。中身は {id: {path, ts, open}}。読むだけで書き換えない。
- Vault の場所: 設定画面で選んだもの(`ui.json` の `vault_root`)→ `config.toml` の `vault_root` の順。
- 開く: `obsidian://open?path=<絶対パス>`(Obsidian がどの保管庫かを見つける)。Vault の外・`.md` 以外は開かない。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote


def config_path(platform: str | None = None, home: Path | None = None, appdata: str | None = None) -> Path:
    plat, home = platform or sys.platform, home or Path.home()
    if plat == "darwin":
        return home / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    if plat == "win32":
        return Path(appdata or os.environ.get("APPDATA") or (home / "AppData" / "Roaming")) / "obsidian" / "obsidian.json"
    return home / ".config" / "obsidian" / "obsidian.json"


def detect_vaults(path: Path | None = None) -> list[dict]:
    """[{name, path, open, exists}]。開いている保管庫 → 最近使った順。Obsidian が無ければ空。"""
    p = path or config_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for v in (data.get("vaults") or {}).values():
        if not isinstance(v, dict) or not isinstance(v.get("path"), str):
            continue
        vp = Path(v["path"])
        out.append({"name": vp.name, "path": str(vp), "open": bool(v.get("open")), "exists": vp.is_dir(),
                    "ts": v.get("ts") or 0})
    out.sort(key=lambda x: (not x["open"], -(x["ts"] if isinstance(x["ts"], (int, float)) else 0)))
    return [{k: x[k] for k in ("name", "path", "open", "exists")} for x in out]


def ui_vault_root(app_dir: Path) -> Path | None:
    """設定画面で選んだ Vault(`ui.json` の `vault_root`)。無ければ None。"""
    try:
        v = json.loads((Path(app_dir) / "ui.json").read_text(encoding="utf-8")).get("vault_root")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(v).expanduser() if isinstance(v, str) and v.strip() else None


def effective_vault_root(cfg) -> Path:
    """使う Vault: 設定画面(ui.json)→ config.toml の vault_root。端末の meetcue index / doctor も同じ順で読む。"""
    return ui_vault_root(cfg.app_dir) or Path(cfg.vault_root)


def note_path(vault_root: Path, rel: str) -> Path | None:
    """Vault の中の .md だけを絶対パスにする(外へ出る・.md 以外は None)。"""
    rel = str(rel or "").split("#", 1)[0].strip()
    if not rel:
        return None
    root = Path(vault_root).resolve()
    p = (root / rel).resolve()
    if p.suffix.lower() != ".md" or (root not in p.parents) or not p.is_file():
        return None
    return p


def open_uri(p: Path) -> str:
    return "obsidian://open?path=" + quote(str(p), safe="")


def open_note(vault_root: Path, rel: str, *, runner=None) -> tuple[bool, str]:
    """(開けたか, 画面に出す一言)。runner は試験用で、渡せば OS を問わずそれを呼ぶ(既定は Windows = os.startfile・
    ほかは呼んだ時点の subprocess.run)。Windows で runner を使わずに本物の Obsidian を呼び、CI と試験のたびに
    一時フォルダのノートで「Vault not found」が出ていた(2026-10-09)。"""
    given = runner is not None
    runner = runner or subprocess.run
    p = note_path(vault_root, rel)
    if p is None:
        return False, "Vault の中のノートではありません"
    uri = open_uri(p)
    try:
        if sys.platform == "win32" and not given:
            os.startfile(uri)   # 既定のアプリ(Obsidian)で開く
        else:
            runner(["open" if sys.platform == "darwin" else "xdg-open", uri], check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Obsidian で開けませんでした({type(e).__name__})"
    return True, "Obsidian で開きました"


# ---- 索引(FR-5b): 設定画面と端末の meetcue index が同じ決まりで作る ------------------------------------------
def index_includes(root: Path, include) -> tuple[str, ...]:
    """索引するフォルダ。設定の include のうち Vault にあるもの。1 つも無ければ Vault 全体("")。
    (既定の include は作者の PARA 構成。ほかの人の Vault では全体を読む・2026-10-08)"""
    have = tuple(i for i in include if i and (Path(root) / i).is_dir())
    return have or ("",)


def index_meta(cfg) -> dict:
    try:
        return json.loads((Path(cfg.index_db).parent / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_index(cfg, root: Path, *, full: bool = False) -> dict:
    """Vault の索引を作る / 更新する。前と違う Vault なら全量で作り直す。meta.json に場所と時刻を残す。"""
    from .index import VaultIndex
    root = Path(root).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Vault が見つかりません: {root}")
    prev = index_meta(cfg).get("root")
    full = full or (prev is not None and prev != str(root))
    inc = index_includes(root, cfg.index.include)
    idx = VaultIndex(cfg.index_db)
    try:
        st = idx.build(root, include=inc, exclude=tuple(cfg.index.exclude), chunk_chars=cfg.index.chunk_chars, full=full)
        stats = idx.stats()
    finally:
        idx.close()
    meta = {"root": str(root), "built_ms": int(time.time() * 1000), "include": list(inc), "full": full}
    (Path(cfg.index_db).parent / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {**st, **stats, "include": list(inc), "full": full}

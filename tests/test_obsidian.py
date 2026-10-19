"""Obsidian 連携(FR-5b・2026-10-08): 保管庫の一覧・Vault の決め方・ノートを開く・索引・設定画面の API・MCP の受け渡し。

本物の Obsidian・キーチェーン・Obsidian の設定ファイルには触れない(一時フォルダと偽の MCP サーバー)。
"""
import json
import sys
import time
from pathlib import Path

import pytest

from meetcue import app as app_mod
from meetcue import secrets
from meetcue.app import App
from meetcue.config import Config
from meetcue.knowledge import mcp, obsidian

FAKE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")]


def _vault(root: Path, para: bool = True) -> Path:
    if para:
        (root / "01_Projects" / "VPN").mkdir(parents=True)
        (root / "01_Projects" / "VPN" / "検証.md").write_text("# 結果\nSASE の検証をした。\n", encoding="utf-8")
    else:   # PARA でない Vault(ほかの人の Vault)
        (root / "仕事").mkdir(parents=True)
        (root / "仕事" / "SASE.md").write_text("# SASE\nネットワークとセキュリティをクラウドで束ねる考え方。\n", encoding="utf-8")
    (root / ".obsidian").mkdir(parents=True, exist_ok=True)
    (root / ".obsidian" / "隠し.md").write_text("索引しない", encoding="utf-8")
    return root


def test_detect_vaults_and_config_path(tmp_path):
    a, b = tmp_path / "Work", tmp_path / "Life"
    a.mkdir()
    cfgf = tmp_path / "obsidian.json"
    cfgf.write_text(json.dumps({"vaults": {"1": {"path": str(b), "ts": 9}, "2": {"path": str(a), "ts": 1, "open": True},
                                           "3": {"path": 5}}}), encoding="utf-8")
    vs = obsidian.detect_vaults(cfgf)
    assert [(v["name"], v["open"], v["exists"]) for v in vs] == [("Work", True, True), ("Life", False, False)]   # 開いているもの → 新しい順
    assert obsidian.detect_vaults(tmp_path / "none.json") == []
    assert obsidian.config_path("darwin", Path("/h")) == Path("/h/Library/Application Support/obsidian/obsidian.json")
    assert obsidian.config_path("win32", Path("/h"), appdata="/ad") == Path("/ad/obsidian/obsidian.json")
    assert obsidian.config_path("linux", Path("/h")) == Path("/h/.config/obsidian/obsidian.json")


def test_vault_root_and_open_note(tmp_path):
    cfg = Config(app_dir=tmp_path / "app", vault_root=tmp_path / "cfg-vault")
    assert obsidian.effective_vault_root(cfg) == tmp_path / "cfg-vault"
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "ui.json").write_text(json.dumps({"vault_root": str(tmp_path / "v")}), encoding="utf-8")
    assert obsidian.effective_vault_root(cfg) == tmp_path / "v"                       # 設定画面が優先
    root = _vault(tmp_path / "v")
    assert obsidian.note_path(root, "01_Projects/VPN/検証.md#結果") == (root / "01_Projects/VPN/検証.md").resolve()
    for bad in ("../outside.md", "01_Projects/VPN", "無い.md", "", "/etc/hosts"):
        assert obsidian.note_path(root, bad) is None
    ran = []
    ok, _ = obsidian.open_note(root, "01_Projects/VPN/検証.md", runner=lambda args, **kw: ran.append(args))
    # Windows でも渡した runner を呼ぶ(本物の Obsidian を呼ぶと一時フォルダのノートで「Vault not found」が出る)
    assert ok and ran[0][-1].startswith("obsidian://open?path=") and "%E6%A4%9C%E8%A8%BC.md" in ran[0][-1]
    assert obsidian.open_note(root, "../x.md", runner=lambda *a, **k: None) == (False, "Vault の中のノートではありません")


def test_build_index_whole_vault_when_not_para_and_rebuild_on_switch(tmp_path):
    cfg = Config(app_dir=tmp_path / "app")
    other = _vault(tmp_path / "other", para=False)
    st = obsidian.build_index(cfg, other)
    assert st["include"] == [""] and st["files"] == 1 and not st["full"]              # 01_Projects などが無ければ全体を読む
    from meetcue.knowledge.index import VaultIndex
    hits = VaultIndex(cfg.index_db).search("SASE", terms=["sase"])
    assert [h.path for h in hits] == ["仕事/SASE.md"]                                   # .obsidian の中は読まない
    para = _vault(tmp_path / "para")
    st2 = obsidian.build_index(cfg, para)
    assert st2["full"] and st2["include"] == ["01_Projects"] and st2["files"] == 1     # 別の Vault なら作り直す
    assert obsidian.index_meta(cfg)["root"] == str(para)
    with pytest.raises(FileNotFoundError):
        obsidian.build_index(cfg, tmp_path / "none")


@pytest.fixture
def app(tmp_path, monkeypatch):
    store = {}
    monkeypatch.setattr(secrets, "keychain_supported", lambda: True)
    monkeypatch.setattr(secrets, "keychain_get", lambda p: store.get(p))
    monkeypatch.setattr(secrets, "keychain_set", lambda p, k: store.__setitem__(p, k))
    monkeypatch.setattr(secrets, "keychain_delete", lambda p: store.pop(p, None) is not None)
    monkeypatch.setattr(secrets, "load_env", lambda path=None: {})
    for v in secrets.PROVIDERS.values():
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(obsidian, "detect_vaults", lambda path=None: [{"name": "v", "path": str(tmp_path / "v"),
                                                                        "open": True, "exists": True}])
    opened = []
    monkeypatch.setattr(obsidian.subprocess, "run", lambda args, **kw: opened.append(args))
    monkeypatch.setattr(obsidian.os, "startfile", lambda u: opened.append(["startfile", u]), raising=False)
    a = App(Config(app_dir=tmp_path / "app", vault_root=tmp_path / "missing"), sources=[], window=False)
    a._drive_accounts = lambda: []
    pushed = []
    a.web.push = pushed.append
    a.web.status = lambda *x, **k: pushed.append({"type": "status", "text": x[0]})
    return a, store, opened, pushed, tmp_path


def test_settings_vault_index_open_and_mcp(app):
    a, store, opened, pushed, tmp = app
    code, v = a._api("GET", "/api/obsidian", None)
    assert code == 200 and not v["vault_exists"] and v["vault_from"] == "config" and v["vaults"][0]["name"] == "v"
    assert v["mcp"]["enabled"] is False and v["index"]["exists"] is False
    assert a._api("POST", "/api/settings", {"vault_root": str(tmp / "nope")})[0] == 400        # 無いフォルダは断る
    _vault(tmp / "v")
    code, s = a._api("POST", "/api/settings", {"vault_root": str(tmp / "v")})
    assert code == 200 and s["obsidian"]["vault_exists"] and s["obsidian"]["vault_from"] == "settings"
    code, r = a._api("POST", "/api/obsidian/index", {})
    assert code == 200 and r["started"]
    for _ in range(100):
        if not a._idx_state["building"]:
            break
        time.sleep(0.05)
    v = a._obsidian_view()
    assert v["index"]["exists"] and v["index"]["files"] == 1 and v["index"]["last"]["ok"] and v["index"]["root"] == str(tmp / "v")
    assert any(e.get("type") == "obsidian" and not e["index"]["building"] for e in pushed)
    code, r = a._api("POST", "/api/obsidian/open", {"path": "01_Projects/VPN/検証.md"})
    assert code == 200 and opened and "obsidian://open?path=" in opened[-1][-1]
    assert a._api("POST", "/api/obsidian/open", {"path": "../x.md"})[0] == 400
    # MCP: 既定オフ → 録音に渡さない。stdio の偽サーバーにすると確認でき、録音に検索を渡す
    assert a._mcp_search_fn("private") is None
    a._api("POST", "/api/settings", {"obsidian_mcp": True, "mcp_kind": "stdio",
                                     "mcp_command": " ".join(f'"{x}"' for x in FAKE)})
    code, c = a._api("POST", "/api/obsidian/mcp/check", {})
    assert code == 200 and c["ok"] and c["search_tool"] == "obsidian_simple_search"
    assert a._obsidian_view()["mcp"]["last"]["ok"]
    assert a._api("POST", "/api/keys/check", {"provider": "obsidian"})[1]["ok"]            # キーの「確認」も MCP の確認
    fn = a._mcp_search_fn("local")                                                            # stdio は LOCAL でも使える
    assert fn and fn(["SASE"], 2)[0].path == "03_Resources/SASE.md"
    a._api("POST", "/api/settings", {"mcp_kind": "http", "mcp_url": "https://example.com/mcp"})
    assert a._mcp_search_fn("local") is None                                                  # 外の URL は LOCAL で使わない
    assert any("LOCAL" in e.get("text", "") for e in pushed if e.get("type") == "status")
    assert a._api("POST", "/api/settings", {"mcp_kind": "ftp"})[0] == 400
    assert a._api("POST", "/api/settings", {"mcp_port": 70000})[0] == 400
    a._vs and a._vs.close()


def test_mcp_obsidian_preset_needs_key_and_uv(app, monkeypatch):
    a, store, *_ = app
    monkeypatch.setattr(mcp, "find_uvx", lambda: None)
    a._api("POST", "/api/settings", {"obsidian_mcp": True})
    m = a._obsidian_view()["mcp"]
    assert not m["ready"] and "uv" in m["error"] and m["key"] == {"set": False, "source": None}
    monkeypatch.setattr(mcp, "find_uvx", lambda: "/x/uvx")
    assert "キー" in a._obsidian_view()["mcp"]["error"]
    a._api("POST", "/api/keys/save", {"provider": "obsidian", "key": "a" * 64})
    m = a._obsidian_view()["mcp"]
    assert m["ready"] and m["key"]["set"] and "a" * 64 not in json.dumps(a.settings_view())   # キーは画面へ出さない

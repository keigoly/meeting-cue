"""設定画面(FR-10b・2026-09-26): 接続先・生成 / Jev / Google Drive のオン・オフ・初回の案内・API キー(キーチェーン)。

本物のキーチェーン・~/.secrets・ネットワークには触れない(secrets と providers の入口を差し替える)。
"""
import json

import pytest

from meetcue import app as app_mod
from meetcue import providers, secrets
from meetcue.app import DEFAULT_APP, App
from meetcue.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = {}                                                   # 偽のキーチェーン
    monkeypatch.setattr(secrets, "keychain_supported", lambda: True)
    monkeypatch.setattr(secrets, "keychain_get", lambda p: store.get(p))

    def kset(p, k):
        if not secrets.KEY_RE.match(k or ""):
            raise ValueError("キーの形が正しくありません")
        store[p] = k
    monkeypatch.setattr(secrets, "keychain_set", kset)
    monkeypatch.setattr(secrets, "keychain_delete", lambda p: store.pop(p, None) is not None)
    monkeypatch.setattr(secrets, "load_env", lambda path=None: {})
    for v in secrets.PROVIDERS.values():
        monkeypatch.delenv(v, raising=False)
    opened = []
    monkeypatch.setattr(app_mod.subprocess, "run", lambda args, **kw: opened.append(args))
    a = App(Config(app_dir=tmp_path), sources=[], window=False)
    a._drive_accounts = lambda: [{"label": "a@example.com", "root": tmp_path / "drive"}]
    pushed = []
    a.web.push = pushed.append
    return a, store, opened, pushed, tmp_path


def test_defaults_and_onboarding_flag(env):
    a, store, *_ = env
    code, v = a._api("GET", "/api/settings", None)
    assert code == 200 and {k: v[k] for k in DEFAULT_APP} == DEFAULT_APP
    assert v["need_onboarding"] is True and v["keychain"] is True and v["drive_accounts"] == ["a@example.com"]
    assert v["keys"]["openrouter"] == {"set": False, "source": None}
    assert v["providers_ready"] == ["anthropic", "openai", "openrouter"]                  # 第 2 段で 3 つとも
    assert v["anthropic_models"] == {"main": "claude-sonnet-5", "fast": "claude-haiku-4-5", "deep": "claude-opus-5-5"}
    store["openrouter"] = "sk-or-v1-" + "x" * 40                 # キーがあれば案内は出さない(既存の利用者)
    assert a._api("GET", "/api/settings", None)[1]["need_onboarding"] is False
    store.clear()
    a._api("POST", "/api/settings", {"onboarded": True})          # 「録音だけ」で案内を終えた人にも二度と出さない
    assert a._api("GET", "/api/settings", None)[1]["need_onboarding"] is False


def test_toggles_validation_and_provider_gate(env):
    a, _, _, pushed, tmp = env
    code, v = a._api("POST", "/api/settings", {"generate": False, "jev": False, "drive": False})
    assert code == 200 and (v["generate"], v["jev"], v["drive"]) == (False, False, False)
    saved = json.loads((tmp / "ui.json").read_text(encoding="utf-8"))
    assert saved["generate"] is False and "keys" not in saved     # 画面用の項目は保存しない
    assert pushed[-1]["type"] == "settings"
    for bad in ({"jev": "off"}, {"generate": 1}, {"provider": 3}, {"onboarded": "yes"}):
        assert a._api("POST", "/api/settings", bad)[0] == 400
    assert a._api("POST", "/api/settings", {"provider": "anthropic"})[1]["provider"] == "anthropic"
    code, err = a._api("POST", "/api/settings", {"provider": "gemini"})
    assert code == 400 and "接続先" in err["error"]
    code, v = a._api("POST", "/api/settings", {"openai_model": "gpt-x", "openai_price_in": 2, "openai_price_out": 8.5})
    assert code == 200 and v["openai_price_in"] == 2.0 and isinstance(v["openai_price_in"], float)
    for bad in ({"openai_price_in": -1}, {"openai_price_out": True}, {"openai_model": 3}):
        assert a._api("POST", "/api/settings", bad)[0] == 400
    (tmp / "ui.json").write_text(json.dumps({"provider": "gemini", "jev": "x"}), encoding="utf-8")
    assert a.ui_settings()["provider"] == "openrouter" and a.ui_settings()["jev"] is True   # 壊れた値は既定へ


def test_keys_save_check_delete_never_echo_key(env, monkeypatch):
    a, store, *_ = env
    key = "sk-ant-api03-" + "A" * 40
    code, v = a._api("POST", "/api/keys/save", {"provider": "anthropic", "key": key})
    assert code == 200 and store["anthropic"] == key and v["keys"]["anthropic"] == {"set": True, "source": "keychain"}
    assert key not in json.dumps(v)                               # 画面へはキーを返さない
    assert a._api("POST", "/api/keys/save", {"provider": "anthropic", "key": "bad key \"x\""})[0] == 400
    assert a._api("POST", "/api/keys/save", {"provider": "gemini", "key": key})[0] == 400
    seen = []
    monkeypatch.setattr(providers, "check_key", lambda p, k: seen.append((p, k)) or {"ok": True, "status": 200, "ms": 5,
                                                                                      "message": "ok"})
    code, res = a._api("POST", "/api/keys/check", {"provider": "anthropic"})
    assert code == 200 and res["ok"] and seen == [("anthropic", key)] and key not in json.dumps(res)
    code, v = a._api("POST", "/api/keys/delete", {"provider": "anthropic"})
    assert code == 200 and "anthropic" not in store and v["keys"]["anthropic"]["set"] is False


def test_env_file_key_is_used_but_not_overwritten(env, monkeypatch):
    a, store, *_ = env
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "f" * 40)   # 既存の ~/.secrets の設定に当たる
    assert secrets.api_key("openrouter") == ("sk-or-v1-" + "f" * 40, "env")
    assert a._api("GET", "/api/settings", None)[1]["keys"]["openrouter"] == {"set": True, "source": "env"}
    store["openrouter"] = "sk-or-v1-" + "k" * 40                  # キーチェーンがあればそちらを優先
    assert secrets.api_key("openrouter")[1] == "keychain"


def test_open_only_known_pages(env):
    a, _, opened, *_ = env
    assert a._api("POST", "/api/open", {"page": "openrouter"}) == (200, {"ok": True})
    assert opened[-1] == ["open", "https://openrouter.ai/keys"]
    assert a._api("POST", "/api/open", {"page": "https://evil.example.com"})[0] == 400
    assert len(opened) == 1


def test_drive_toggle_blocks_move(env):
    a, *_ = env
    a._api("POST", "/api/settings", {"drive": False})
    assert a._api("POST", "/api/sessions/20260926_100000_aaaaaaaa/drive", {})[0] == 409


def test_check_key_without_key_or_unknown_provider():
    assert providers.check_key("openrouter", None)["ok"] is False
    with pytest.raises(ValueError):
        providers._request("gemini", "k")


def test_models_api_openai_only(env, monkeypatch):
    a, store, *_ = env
    assert a._api("POST", "/api/models", {"provider": "anthropic"})[0] == 400
    assert a._api("POST", "/api/models", {"provider": "openai"})[0] == 409          # キーが無い
    store["openai"] = "sk-proj-" + "o" * 40
    from meetcue.cues import openai_direct
    monkeypatch.setattr(openai_direct, "list_models", lambda key: ["gpt-b", "gpt-a"] if key == store["openai"] else [])
    assert a._api("POST", "/api/models", {"provider": "openai"}) == (200, {"models": ["gpt-b", "gpt-a"]})

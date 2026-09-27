"""画面の設定(色 = 基本色・相手・自分 / ナギの台詞のオン・オフ): 既定値・保存・不正な値・他画面への配信。"""
import json

import pytest

from meetcue import secrets
from meetcue.app import DEFAULT_APP, DEFAULT_COLORS, DEFAULT_NAGI, App
from meetcue.config import Config
from meetcue.ui.web import WebUI


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    """本物のキーチェーン・~/.secrets を読まない(試験を環境に左右させない)。"""
    monkeypatch.setattr(secrets, "key_status", lambda: {p: {"set": False, "source": None} for p in secrets.PROVIDERS})


def _app(tmp_path):
    app = App(Config(app_dir=tmp_path), sources=[], window=False)
    app._drive_accounts = lambda: []
    return app


def test_defaults_save_and_validation(tmp_path):
    app = _app(tmp_path)
    pushed = []
    app.web.push = pushed.append
    code, got = app._api("GET", "/api/settings", None)
    assert code == 200 and got["colors"] == DEFAULT_COLORS and got["nagi"] is True
    assert DEFAULT_COLORS == {"accent": "#3ed6c8", "other": "#62b6ff", "self": "#8f8cff"}   # ミント / 空色 / 藤紺(ナギの色)
    code, body = app._api("POST", "/api/settings", {"colors": {"accent": "#FF8800"}})
    assert code == 200 and body["colors"]["accent"] == "#ff8800" and body["colors"]["other"] == "#62b6ff"
    assert json.loads((tmp_path / "ui.json").read_text(encoding="utf-8"))["colors"]["accent"] == "#ff8800"
    assert pushed and pushed[-1]["type"] == "settings"
    for bad in ({"accent": "red"}, {"accent": "#12345"}, {"font": "#000000"}, {"self": "#12345g"}):
        assert app._api("POST", "/api/settings", {"colors": bad})[0] == 400
    assert app._api("GET", "/api/settings", None)[1]["colors"]["accent"] == "#ff8800"   # 不正な値で壊れない



def test_theme_setting(tmp_path):
    """外観(2026-09-27): 既定は auto(macOS に合わせる)・light / dark だけ受ける・壊れた値は auto に戻す。"""
    app = _app(tmp_path)
    app.web.push = lambda ev: None
    assert app._api("GET", "/api/settings", None)[1]["theme"] == "auto"
    for t in ("dark", "light", "auto"):
        code, body = app._api("POST", "/api/settings", {"theme": t})
        assert code == 200 and body["theme"] == t
    for bad in ("night", "", 1, None):
        assert app._api("POST", "/api/settings", {"theme": bad})[0] == 400
    (tmp_path / "ui.json").write_text(json.dumps({"theme": "sepia"}), encoding="utf-8")
    assert app._api("GET", "/api/settings", None)[1]["theme"] == "auto"

def test_broken_file_falls_back_to_defaults(tmp_path):
    (tmp_path / "ui.json").write_text("{not json", encoding="utf-8")
    assert _app(tmp_path).ui_settings() == {"colors": DEFAULT_COLORS, "nagi": DEFAULT_NAGI, **DEFAULT_APP}
    (tmp_path / "ui.json").write_text(json.dumps({"colors": {"accent": "javascript:x", "self": "#010203"}}), encoding="utf-8")
    assert _app(tmp_path).ui_settings()["colors"] == DEFAULT_COLORS | {"self": "#010203"}


def test_nagi_toggle(tmp_path):
    app = _app(tmp_path)
    pushed = []
    app.web.push = pushed.append
    assert DEFAULT_NAGI is True                                    # 既定はナギの台詞
    code, body = app._api("POST", "/api/settings", {"nagi": False})
    assert code == 200 and body["nagi"] is False and body["colors"] == DEFAULT_COLORS
    assert json.loads((tmp_path / "ui.json").read_text(encoding="utf-8"))["nagi"] is False
    assert pushed[-1] == {"type": "settings", **body} and "keys" in body   # 画面へはキーの有無だけ
    app._api("POST", "/api/settings", {"colors": {"accent": "#112233"}})   # 色だけ変えてもオフのまま
    assert app.ui_settings()["nagi"] is False
    for bad in ("false", 0, None, "off"):
        assert app._api("POST", "/api/settings", {"nagi": bad})[0] == 400
    assert app.ui_settings()["nagi"] is False
    (tmp_path / "ui.json").write_text(json.dumps({"nagi": "yes"}), encoding="utf-8")
    assert app.ui_settings()["nagi"] is DEFAULT_NAGI               # 壊れた値は既定へ


def test_status_code_is_pushed_only_when_given():
    ui = WebUI(port=0)
    got = []
    ui._push = lambda ev, keep=True: got.append(ev)
    ui.status("録音を停止しています…", code="stopping")
    ui.error("x")
    assert got == [{"type": "status", "text": "録音を停止しています…", "code": "stopping"}, {"type": "error", "text": "x"}]

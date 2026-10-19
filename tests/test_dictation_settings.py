"""音声入力の設定(FR-14b・2026-10-19): キーの書き方・既定値・検め・保存・節ごとの「デフォルトにリセット」。"""
import json

from meetcue import dictation_settings as ds
from meetcue.config import Config


def _cfg(tmp_path):
    cfg = Config()
    cfg.app_dir = tmp_path
    return cfg


def test_parse_and_label_keys():
    assert ds.parse_key("ctrl+opt+V") == (("ctrl", "opt"), "V", None)
    assert ds.parse_key("Option+Control+v") == (("ctrl", "opt"), "V", None)    # 別名と順はそろえる
    assert ds.parse_key("fn") == ((), None, "fn")
    assert ds.parse_key("ctrl+fn") is None and ds.parse_key("ctrl+") is None and ds.parse_key("hyper+V") is None
    assert ds.normalize("cmd+ctrl+v") == "ctrl+cmd+V"
    assert ds.label("ctrl+opt+V") == "⌃⌥V" and ds.label("ctrl+opt+shift+V") == "⌃⌥⇧V"
    assert ds.label("fn") == "Fn" and ds.label("ropt") == "右⌥" and ds.label("ESCAPE") == "Esc"
    assert ds.lock_label("ctrl+opt+V", "SPACE") == "⌃⌥V+Space" and ds.lock_label("fn", "SPACE") == "Fn+Space"


def test_defaults_keep_the_current_behavior(tmp_path):
    s = ds.load(_cfg(tmp_path))
    assert s["keys"]["activate"] == ["ctrl+opt+V"] and s["keys"]["paste_last"] == "ctrl+opt+shift+V"
    assert s["keys"]["lock"] == "SPACE" and s["keys"]["cancel"] == "ESCAPE" and s["keys"]["cancel_enabled"]
    assert s["realtime"] == "never" and s["language"] == "ja_en" and ds.language_hints(s) == ["ja", "en"]
    # クラウドへ新しく送るものと AI は既定オフ
    assert not s["deep_context"] and not s["ai_cleanup"] and not s["edit_mode"] and not s["privacy_mode"]
    assert ds.labels(s) == {"activate": ["⌃⌥V"], "handsfree": [], "lock": "⌃⌥V+Space", "paste_last": "⌃⌥⇧V",
                            "cancel": "Esc"}


def test_defaults_follow_config_toml(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.dictation.key, cfg.dictation.mods, cfg.dictation.language_hints = "D", "ctrl,cmd", ["ja"]
    s = ds.load(cfg)
    assert s["keys"]["activate"] == ["ctrl+cmd+D"] and s["language"] == "ja"


def test_validate_rejects_keys_that_steal_typing(tmp_path):
    cfg = _cfg(tmp_path)
    _, errs = ds.save(cfg, {"keys": {"activate": ["shift+A"]}})
    assert errs and "⌃・⌥・⌘" in errs[0]
    _, errs = ds.save(cfg, {"keys": {"activate": ["F13"], "handsfree": ["fn"]}})   # F13 と修飾キーだけは使える
    assert errs == []
    _, errs = ds.save(cfg, {"keys": {"activate": ["fn", "F5"], "handsfree": []}})   # 1 つのキーだけ: Fn・F1〜F20(10-19 keigoly様)
    assert errs == []
    _, errs = ds.save(cfg, {"keys": {"activate": ["A"]}})                          # 文字のキーだけは入力を奪う
    assert errs and "1 つのキーなら" in errs[0]
    _, errs = ds.save(cfg, {"keys": {"activate": ["ctrl+opt+V"], "paste_last": "ctrl+opt+V"}})
    assert errs == ["同じキーを 2 つの役に使っています"]
    _, errs = ds.save(cfg, {"keys": {"lock": "ctrl+SPACE"}})
    assert errs and "修飾なし" in errs[0]
    _, errs = ds.save(cfg, {"keys": {"activate": []}})
    assert errs and "1〜3" in errs[0]


def test_save_merges_and_reset_restores_one_section(tmp_path):
    cfg = _cfg(tmp_path)
    s, errs = ds.save(cfg, {"realtime": "handsfree", "keys": {"activate": ["ctrl+opt+V", "fn"]}, "sounds": False,
                            "auto_enter_apps": [{"bundle": "com.tinyspeck.slackmacgap", "name": "Slack"}]})
    assert errs == [] and s["realtime"] == "handsfree" and s["keys"]["activate"] == ["ctrl+opt+V", "fn"]
    assert s["keys"]["paste_last"] == "ctrl+opt+shift+V"                  # 触れていない役はそのまま
    assert ds.uses_modifier_only(s)
    saved = json.loads(ds.path(tmp_path).read_text(encoding="utf-8"))
    assert saved["sounds"] is False
    s, _ = ds.reset(cfg, "keys")
    assert s["keys"]["activate"] == ["ctrl+opt+V"] and s["realtime"] == "handsfree" and s["sounds"] is False
    s, _ = ds.reset(cfg, "system")
    assert s["sounds"] is True and s["auto_enter_apps"] == [] and s["realtime"] == "handsfree"


def test_broken_file_falls_back_per_item(tmp_path):
    cfg = _cfg(tmp_path)
    p = ds.path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"realtime": "sometimes", "sounds": False, "keys": {"activate": ["shift+A"]}}), encoding="utf-8")
    s = ds.load(cfg)
    assert s["realtime"] == "never" and s["sounds"] is False and s["keys"]["activate"] == ["ctrl+opt+V"]
    p.write_text("{not json", encoding="utf-8")
    assert ds.load(cfg)["keys"]["activate"] == ["ctrl+opt+V"]


def test_helper_bindings_drop_disabled_cancel(tmp_path):
    cfg = _cfg(tmp_path)
    s, _ = ds.save(cfg, {"keys": {"cancel_enabled": False}})
    b = ds.helper_bindings(s)
    assert b == {"activate": ["ctrl+opt+V"], "handsfree": [], "lock": "SPACE", "paste_last": "ctrl+opt+shift+V", "cancel": ""}


def test_validate_rejects_malformed_and_reserved_keys(tmp_path):
    """読み直しの指摘(10-19): 誤った形は 400 の理由に・貼り付けと送信で自分が押すキーは選べない・キャンセルは文字のキーにしない。"""
    cfg = _cfg(tmp_path)
    for patch, want in [({"keys": None}, "keys の形"), ({"keys": {"activate": 5}}, "1〜3"), ([1, 2], "設定の形"),
                        ({"keys": {"activate": ["cmd+V"]}}, "⌘V と ⌘Return"), ({"keys": {"paste_last": "cmd+RETURN"}}, "⌘V と ⌘Return"),
                        ({"keys": {"cancel": "A"}}, "Esc・F キー"), ({"keys": {"lock": 5}}, "文字で書きます")]:
        s, errs = ds.save(cfg, patch)
        assert errs and want in errs[0], (patch, errs)
    for ok in ({"keys": {"cancel": "F5"}}, {"keys": {"cancel": "ctrl+X"}}, {"keys": {"cancel": "ESCAPE"}}):
        assert ds.save(cfg, ok)[1] == []
    p = ds.path(tmp_path)
    p.write_text('{"keys": {"activate": 5, "lock": 7}, "realtime": ["x"]}', encoding="utf-8")
    s = ds.load(cfg)                                                           # 壊れたファイルでも止めない
    assert s["keys"]["activate"] == ["ctrl+opt+V"] and s["realtime"] == "never"


def test_pet_defaults_and_validation(tmp_path):
    """ナギのペット(FR-14c・10-19 keigoly様「常時表示させて」): 既定は常に出す・中。誤った値は保存しない。リセットはシステムの節。"""
    cfg = _cfg(tmp_path)
    s = ds.load(cfg)
    assert s["pet"] == "always" and s["pet_size"] == "m"
    s2, errs = ds.save(cfg, {"pet": "recording", "pet_size": "l"})
    assert not errs and s2["pet"] == "recording" and s2["pet_size"] == "l"
    _, errs = ds.save(cfg, {"pet": "sometimes"})
    assert errs and ds.load(cfg)["pet"] == "recording"
    _, errs = ds.save(cfg, {"pet_size": "xl"})
    assert errs
    s3, _ = ds.reset(cfg, "system")
    assert s3["pet"] == "always" and s3["pet_size"] == "m"

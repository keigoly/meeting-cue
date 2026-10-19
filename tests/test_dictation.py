"""音声入力(FR-14・2026-10-17 / FR-14b・2026-10-19): 文の仕上げ(決めたフィラー・空白・英字の「」・置き換え辞書)・
送信コマンド・カジュアル・リアルタイムの区切り・キーの押し方(長押し / 2 回押し / ハンズフリーへの切り替え / 取り消し)・
貼り付けと記録(プライバシーモード・最後の文字起こし)・待ち受けの錠・履歴。"""
import json
import sys
import time

import pytest

from meetcue import dictation, dictation_history, dictation_settings, soniox
from meetcue.config import Config
from meetcue.dictation import (Dictation, Take, acquire_lock, casualize, finish_text, load_terms, release_lock,
                               split_ready, strip_send)
from meetcue.replacements import Replacer


def test_finish_removes_listed_fillers_only():
    assert finish_text("えー、それでですね、あのー、入金しました。")[0] == "それでですね、入金しました。"
    assert finish_text("ちゃんとえー、注文するときに伝わるでしょうか。")[0] == "ちゃんと注文するときに伝わるでしょうか。"
    assert finish_text("まあ、いいかなと。その、確認です。")[0] == "いいかなと。確認です。"
    assert finish_text("その件はあの人に聞きます。あーいう感じで。")[0] == "その件はあの人に聞きます。あーいう感じで。"
    assert finish_text("Um, I think so.")[0] == "I think so."
    text, n = finish_text("うーん、えっと、はい。")
    assert text == "はい。" and n == 2


def test_finish_spaces_quotes_and_empty():
    assert finish_text("試しています。 RunPod と「Meeting Cue」の確認。 ")[0] == "試しています。RunPod とMeeting Cueの確認。"
    assert finish_text("「よろしく」と伝えて")[0] == "「よろしく」と伝えて"       # 日本語の「」は残す
    assert finish_text("")[0] == "" and finish_text("えー。")[0] == ""


def test_finish_applies_replacements(tmp_path):
    p = tmp_path / "replacements.txt"
    p.write_text("RunPod|ランポット\nAqua Voice|アクアボイス\n", encoding="utf-8")
    assert finish_text("ランポットに入金して、アクアボイスと比べる。", Replacer(p))[0] == "RunPodに入金して、Aqua Voiceと比べる。"


def test_dictation_only_replacements_expand_snippets(tmp_path):
    """音声入力だけの置換(定型文・FR-14b): 会議の置き換え辞書の後に掛け、\\n は改行にする。"""
    shared, own = tmp_path / "replacements.txt", tmp_path / "dict.txt"
    shared.write_text("RunPod|ランポット\n", encoding="utf-8")
    own.write_text("よろしくお願いいたします。\\n山田|署名を入れて\n", encoding="utf-8")
    out = finish_text("ランポットの件です。署名を入れて", Replacer(shared), Replacer(own, escapes=True))[0]
    assert out == "RunPodの件です。よろしくお願いいたします。\n山田"


def test_load_terms_dedupes_and_skips_comments(tmp_path):
    p = tmp_path / "terms.txt"
    p.write_text("# 注釈\nRunPod\n\nrunpod\nClaude Code\n", encoding="utf-8")
    assert load_terms(p) == ["RunPod", "Claude Code"]
    assert load_terms(tmp_path / "none.txt") == []


def test_send_command_only_at_the_end():
    assert strip_send("明日伺います。送信して。") == ("明日伺います。", True)
    assert strip_send("送信して") == ("", True)
    assert strip_send("了解です。送信。") == ("了解です。", True)
    assert strip_send("承知しました！送ってください") == ("承知しました！", True)
    # 読点の後・文の途中は本文のまま(途中までの文を送らない・2026-10-19 読み直しの指摘)
    for t in ("明日伺います、送信して", "確認したら、送ってください。", "後で、送信します。", "このメールを送信して。",
              "よろしくお願いします送信してください。", "資料はメールで送信", "送信してから確認します。"):
        assert strip_send(t) == (t, False), t
    assert strip_send("Thanks. Send it.") == ("Thanks.", True)
    assert strip_send("Please send it.") == ("Please send it.", False)          # 英語は文を分けたときだけ


def test_casual_and_realtime_split():
    assert casualize("了解です。") == "了解です" and casualize("本当ですか？") == "本当ですか？"
    assert casualize("Sounds good.") == "sounds good" and casualize("OK...") == "OK..." and casualize("API ready.") == "API ready"
    assert split_ready("今日は晴れです。明日は") == ("今日は晴れです。", "明日は")
    assert split_ready("区切りなし") == ("", "区切りなし")
    long = "あ" * 41 + "、続き"
    assert split_ready(long) == ("あ" * 41 + "、", "続き")


def _cfg(tmp_path):
    cfg = Config()
    cfg.app_dir = tmp_path
    return cfg


def _ops(lines):
    """送ったコマンドの名前(start / stop / paste …)。音・バー・消音は除く。"""
    out = []
    for x in lines:
        name = json.loads(x)["cmd"] if x.startswith("{") else x
        if name not in ("sound", "bar", "mute", "sounds", "bar_mode", "clipboard", "mic", "live", "pet"):
            out.append(name)
    return out


def _engine(tmp_path, monkeypatch, **settings):
    cfg = _cfg(tmp_path)
    s = dictation_settings.load(cfg)
    s.update(settings)
    d = Dictation(cfg, out=lambda s: None, settings=s)
    sent = []
    monkeypatch.setattr(d, "send", sent.append)
    monkeypatch.setattr(Dictation, "work", lambda self, tk: None)   # Soniox にはつながない
    return d, sent


def test_hold_ends_on_release(tmp_path, monkeypatch):
    d, sent = _engine(tmp_path, monkeypatch)
    d.on_key("down")
    tk = d.take
    tk.t_down = time.monotonic() - 1.0          # 1 秒押した
    d.on_key("up")
    assert tk.mode == "hold" and tk.stopped.is_set() and d.take is None and _ops(sent) == ["start", "stop"]
    start = json.loads(sent[0])
    assert start == {"cmd": "start", "context": "app", "selection": False}   # 既定は画面の文脈も選んだ文も読まない


def test_double_press_records_until_next_press(tmp_path, monkeypatch):
    """2026-10-18 keigoly様「Aqua Voice のように 2 回押しで」: 短く 2 回押すと、もう一度押すまで録る。"""
    d, sent = _engine(tmp_path, monkeypatch)
    d.on_key("down")
    tk = d.take
    d.on_key("up")                               # 1 回目(短い)= 2 回目を待つ
    assert tk.mode == "tap" and not tk.stopped.is_set() and _ops(sent) == ["start"]
    d.on_key("down")                             # double_ms 以内の 2 回目
    d.on_key("up")
    d.tick()
    assert tk.mode == "toggle" and not tk.stopped.is_set() and _ops(sent) == ["start"]
    d.on_key("down")                             # もう一度押すと終わり
    assert tk.stopped.is_set() and d.take is None and _ops(sent) == ["start", "stop"] and not tk.canceled
    d.on_key("up")                               # 止めたときの離しは何もしない
    assert _ops(sent) == ["start", "stop"]


def test_single_short_press_is_canceled(tmp_path, monkeypatch):
    d, sent = _engine(tmp_path, monkeypatch)
    d.on_key("down")
    tk = d.take
    d.on_key("up")
    d.tick()
    assert not tk.stopped.is_set()               # まだ 2 回目を待っている
    tk.t_tap = time.monotonic() - 1.0            # double_ms を過ぎた
    d.tick()
    assert tk.canceled == "tap" and tk.stopped.is_set() and d.take is None and _ops(sent) == ["start", "stop"]
    d.on_key("down")                             # 次の押しは新しい 1 回
    assert d.take is not None and d.take is not tk


def test_one_tap_toggle_when_configured(tmp_path, monkeypatch):
    d, sent = _engine(tmp_path, monkeypatch)
    d.d.toggle_taps = 1                          # 10-17 の初版の動き: 短く 1 回押すと、もう一度押すまで録る
    d.on_key("down")
    tk = d.take
    d.on_key("up")
    assert tk.mode == "toggle" and not tk.stopped.is_set() and _ops(sent) == ["start"]
    d.on_key("down")
    assert tk.stopped.is_set() and _ops(sent) == ["start", "stop"]


def test_lock_while_holding_switches_to_handsfree(tmp_path, monkeypatch):
    """有効化を押したまま Space(Aqua Voice の Fn+Space)= 離しても止めない。"""
    d, sent = _engine(tmp_path, monkeypatch)
    d.on_key("down")
    tk = d.take
    d.on_key("down", "lock")
    tk.t_down = time.monotonic() - 2.0
    d.on_key("up")                               # 離しても続ける
    assert tk.mode == "toggle" and tk.locked and not tk.stopped.is_set()
    d.on_key("down")
    assert tk.stopped.is_set() and _ops(sent) == ["start", "stop"]
    d.on_key("down", "lock")                     # 録っていないときの切り替えは何もしない
    assert _ops(sent) == ["start", "stop"]


def test_handsfree_key_and_bar_click_toggle(tmp_path, monkeypatch):
    d, sent = _engine(tmp_path, monkeypatch)
    d.on_key("down", "handsfree")
    tk = d.take
    assert tk.mode == "toggle" and tk.trigger == "handsfree"
    d.on_key("up")                               # 有効化の離しは関係ない
    assert not tk.stopped.is_set()
    d.on_key("down", "handsfree", via="bar")     # バーのクリックでも終わる
    assert tk.stopped.is_set() and _ops(sent) == ["start", "stop"]
    d.on_key("down", "handsfree", via="bar")
    assert d.take.trigger == "bar"


def test_user_cancel_and_modifier_abort(tmp_path, monkeypatch):
    d, sent = _engine(tmp_path, monkeypatch)
    d.on_key("down", "handsfree")
    tk = d.take
    d.on_key("down", "cancel")                   # Esc: 止めて、文字にはするが貼らない
    assert tk.canceled == "user" and tk.stopped.is_set() and d.take is None
    d.on_key("down")
    tk2 = d.take
    d.on_key("abort")                            # 修飾キーを組み合わせに使った: 録らなかったことにする
    assert tk2.canceled == "abort" and tk2.stopped.is_set()
    d.on_key("down", "cancel")                   # 録っていないときの Esc は何もしない
    assert _ops(sent) == ["start", "stop", "start", "stop"]


def test_audio_goes_to_the_take_that_started(tmp_path, monkeypatch):
    d, _ = _engine(tmp_path, monkeypatch)
    d.on_key("down")
    tk = d.take
    d.on_event({"type": "rec_start"})
    d.on_event({"type": "context", "app": "Slack", "bundle": "com.tinyspeck.slackmacgap", "ms": 3})
    d.on_event({"type": "audio", "pcm": "AAAA"})
    d.on_event({"type": "rec_stop", "audio_s": 0.1, "app": "Slack", "bundle": "com.tinyspeck.slackmacgap"})
    assert bytes(tk.pcm) == b"\x00\x00\x00" and tk.rec_stopped.is_set() and d.rec_target is None
    assert tk.context["app"] == "Slack" and tk.ctx_event.is_set() and tk.app["bundle"] == "com.tinyspeck.slackmacgap"


class FakeSession:
    def __init__(self, text="", error=None):
        self.text, self.error = text, error
        self.connect_ms, self.finalize_s, self.audio_s = 900, 0.18, 2.0
        self.cfg = {}

    def languages(self):
        return {"ja": 3}


def _deliver(d, tk, raw, app=None):
    tk.app = app or {"app": "テキストエディット", "bundle": "com.apple.TextEdit"}
    d._deliver(tk, FakeSession(raw), raw, True)
    return [json.loads(x) for x in d._sent if x.startswith("{") and json.loads(x)["cmd"] == "paste"]


def _engine2(tmp_path, monkeypatch, **settings):
    d, sent = _engine(tmp_path, monkeypatch, **settings)
    d._sent = sent
    return d


def test_deliver_pastes_and_saves(tmp_path, monkeypatch):
    d = _engine2(tmp_path, monkeypatch)
    tk = Take()
    tk.mode = "hold"
    pastes = _deliver(d, tk, "えー、明日伺います。送信して。")
    assert pastes == [{"cmd": "paste", "text": "明日伺います。", "send": "enter", "id": 1, "expect": "com.apple.TextEdit"}]
    assert d.last["text"] == "明日伺います。"
    recs = list((tmp_path / "dictation").glob("*/*.json"))
    assert len(recs) == 1
    rec = json.loads(recs[0].read_text(encoding="utf-8"))
    assert rec["text"] == "明日伺います。" and rec["sent"] == "enter" and rec["app"] == "テキストエディット"
    d._on_pasted({"type": "pasted", "id": 1, "ok": True, "sent": "enter"}, time.monotonic())   # 番号で突き合わせる
    assert d.pastes == {}


def test_send_key_paste_only_and_auto_enter(tmp_path, monkeypatch):
    d = _engine2(tmp_path, monkeypatch, send_key="paste_only",
                 auto_enter_apps=[{"bundle": "com.tinyspeck.slackmacgap", "name": "Slack"}])
    assert _deliver(d, Take(), "了解です。送信して")[-1] == {"cmd": "paste", "text": "了解です。", "send": None, "id": 1,
                                                       "expect": "com.apple.TextEdit"}   # 語だけ除く
    slack = {"app": "Slack", "bundle": "com.tinyspeck.slackmacgap"}
    p = _deliver(d, Take(), "了解です。", slack)[-1]
    assert p["text"] == "了解です。" and p["send"] == "enter"                     # 自動 Enter のアプリ
    d.s["send_key"] = "off"
    assert _deliver(d, Take(), "了解です。送信して。")[-1]["text"] == "了解です。送信して。"


def test_casual_drops_final_period_in_chat_apps(tmp_path, monkeypatch):
    d = _engine2(tmp_path, monkeypatch, casual=True)
    slack = {"app": "Slack", "bundle": "com.tinyspeck.slackmacgap"}
    assert _deliver(d, Take(), "了解です。", slack)[-1]["text"] == "了解です"
    assert _deliver(d, Take(), "了解です。")[-1]["text"] == "了解です。"          # チャット以外は付けたまま


def test_user_cancel_keeps_text_without_pasting(tmp_path, monkeypatch):
    d = _engine2(tmp_path, monkeypatch)
    tk = Take()
    tk.canceled = "user"
    assert _deliver(d, tk, "やっぱりやめます。") == []
    assert d.last["text"] == "やっぱりやめます。"
    d.paste_last()                                                              # 「最後の文字起こしを貼り付け」で戻せる
    last = [json.loads(x) for x in d._sent if x.startswith("{") and json.loads(x)["cmd"] == "paste"]
    assert last[-1]["text"] == "やっぱりやめます。" and d.pastes[last[-1]["id"]][0] == "last"


def test_privacy_mode_keeps_nothing_on_disk(tmp_path, monkeypatch):
    d = _engine2(tmp_path, monkeypatch, privacy_mode=True)
    tk = Take()
    tk.pcm += b"\x00\x00" * 160
    _deliver(d, tk, "秘密の話です。")
    assert not list((tmp_path / "dictation").glob("*/*")) and d.last["text"] == "秘密の話です。"


def test_ai_cleanup_and_edit_mode(tmp_path, monkeypatch):
    from meetcue import dictation_ai
    from meetcue.cues.openrouter import StreamResult
    monkeypatch.setattr(dictation_ai, "app_target", lambda cfg: (object(), ""))
    monkeypatch.setattr(dictation_ai, "cleanup", lambda cfg, t, text, ins: StreamResult(ok=True, text=text + "(整えた)"))
    d = _engine2(tmp_path, monkeypatch, ai_cleanup=True, custom_instructions="丁寧に")
    assert _deliver(d, Take(), "明日伺います。")[-1]["text"] == "明日伺います。(整えた)"
    monkeypatch.setattr(dictation_ai, "edit", lambda cfg, t, sel, ins: StreamResult(ok=True, text=f"{sel}→{ins}"))
    d = _engine2(tmp_path, monkeypatch, edit_mode=True)
    tk = Take()
    tk.edit, tk.context = True, {"selected": "元の文"}
    assert _deliver(d, tk, "短くして。送信して。")[-1] == {"cmd": "paste", "text": "元の文→短くして。送信して。", "send": None,
                                                     "id": 1, "expect": "com.apple.TextEdit"}   # 編集の指示に送信コマンドは効かない
    monkeypatch.setattr(dictation_ai, "edit", lambda cfg, t, sel, ins: StreamResult(ok=False, error="http_error"))
    tk = Take()
    tk.edit, tk.context = True, {"selected": "元の文"}
    assert _deliver(d, tk, "短くして") == [{"cmd": "paste", "text": "元の文→短くして。送信して。", "send": None, "id": 1,
                                         "expect": "com.apple.TextEdit"}]   # 直せなかったときは貼らない


def test_realtime_feeds_sentence_by_sentence(tmp_path, monkeypatch):
    d = _engine2(tmp_path, monkeypatch, realtime="always")
    tk = Take()

    class S:
        lock = __import__("threading").Lock()
        final_tokens = [{"text": "えー、今日は晴れです。"}, {"text": "明日は"}]
    d._feed(tk, S)
    assert tk.inserted == ["今日は晴れです。"] and tk.fed == len("えー、今日は晴れです。")
    d._on_pasted({"type": "pasted", "id": tk.inserts[0]["pid"], "ok": True}, time.monotonic())   # 貼れた
    S.final_tokens.append({"text": "雨です。送信して。"})
    d._feed(tk, S)
    assert tk.inserted == ["今日は晴れです。"]                                # 送信コマンドかもしれない文は終わりまで待つ
    pastes = _deliver(d, tk, "えー、今日は晴れです。明日は雨です。送信して。")
    assert pastes[-1]["text"] == "明日は雨です。" and pastes[-1]["send"] == "enter"
    rec = json.loads(next((tmp_path / "dictation").glob("*/*.json")).read_text(encoding="utf-8"))
    assert rec["text"] == "今日は晴れです。明日は雨です。" and rec["realtime"]
    assert not d._realtime(Take()) or d.s["realtime"] == "always"
    d.s["realtime"] = "handsfree"
    t2 = Take()
    assert not d._realtime(t2)
    t2.mode = "toggle"
    assert d._realtime(t2)


def test_soniox_context_only_when_chosen(tmp_path, monkeypatch):
    d = _engine2(tmp_path, monkeypatch)
    tk = Take()
    tk.context = {"app": "Mail", "title": "Re: 見積もり", "before": "お世話になっております。"}
    assert d._soniox_context(tk) is None                                      # 既定オフ
    d.s["deep_context"] = True
    ctx = d._soniox_context(tk)
    cfg = soniox.config(["RunPod"], context=ctx)
    assert cfg["context"] == {"general": [{"key": "アプリ", "value": "Mail"}, {"key": "ウィンドウ", "value": "Re: 見積もり"}],
                              "text": "お世話になっております。", "terms": ["RunPod"]}
    tk.context["secure"] = True
    assert d._soniox_context(tk) is None                                      # パスワードの欄では送らない
    assert soniox.config([])  .get("context") is None


@pytest.mark.skipif(sys.platform == "win32", reason="音声入力は Mac だけ(Windows には fcntl の錠が無い)")
def test_listener_lock_is_exclusive(tmp_path):
    p = tmp_path / "listener.lock"
    fd = acquire_lock(p)
    assert fd is not None and acquire_lock(p) is None and dictation.lock_holder(p) is not None
    release_lock(fd)
    assert dictation.lock_holder(p) is None


def test_history_list_search_delete_and_stats(tmp_path):
    root = tmp_path / "dictation"
    for i, (hhmmss, rid, text, app) in enumerate([("090000", "aaaaaaa1", "朝の連絡です。", "Slack"),
                                                  ("100000", "aaaaaaa2", "RunPod の請求", "Mail")]):
        day = root / "2026-10-19"
        day.mkdir(parents=True, exist_ok=True)
        (day / f"{hhmmss}_{rid}.json").write_text(json.dumps({"rid": rid, "started_at": f"2026-10-19T{hhmmss[:2]}:00:00",
                                                               "text": text, "raw": text, "app": app}), encoding="utf-8")
    (root / "2026-10-19" / "100000_aaaaaaa2.wav").write_bytes(b"RIFF")
    h = dictation_history.list_items(root)
    assert [x["rid"] for x in h["items"]] == ["aaaaaaa2", "aaaaaaa1"] and h["items"][0]["audio"]
    assert [x["rid"] for x in dictation_history.list_items(root, "runpod")["items"]] == ["aaaaaaa2"]
    assert [x["rid"] for x in dictation_history.list_items(root, "slack")["items"]] == ["aaaaaaa1"]
    assert dictation_history.latest(root)["text"] == "RunPod の請求"
    trash = tmp_path / "trash"
    assert dictation_history.delete(root, "aaaaaaa2", trash) and len(list(trash.iterdir())) == 2
    assert dictation_history.find(root, "../etc") is None
    assert dictation_history.clear(root, trash) == 1 and not list(root.glob("*/*.json"))
    m = tmp_path / "metrics.jsonl"
    now = time.time()
    m.write_text("\n".join(json.dumps(r) for r in [
        {"ts_ms": int(now * 1000), "phase": "dict_start"}, {"ts_ms": int(now * 1000), "phase": "dict_text", "chars": 12},
        {"ts_ms": int(now * 1000), "phase": "dict_final", "audio_s": 3600, "ms": 180, "ok": True},
        {"ts_ms": int(now * 1000), "phase": "dict_total", "ms": 420, "ok": True}]), encoding="utf-8")
    st = dictation_history.stats(m, now)
    assert st["today"]["takes"] == 1 and st["today"]["chars"] == 12 and st["month"]["usd"] == 0.12
    assert st["latency"]["dict_total"]["p50"] == 420


def test_local_privacy_refuses_without_sending(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.privacy = "local"
    lines = []
    assert Dictation(cfg, out=lines.append).run() == 2 and "LOCAL" in lines[0]
    assert dictation.report(cfg).startswith("音声入力の記録がまだありません")


def test_pauses_fire_once_after_speech():
    """リアルタイム: 話した後の 0.5 s の間で 1 度だけ(間ごとに finalize を送る)。床の雑音より 10 dB 上を話と見なす。"""
    import array
    import math

    from meetcue.dictation import Pauses, rms_db

    def tone(amp):
        return array.array("h", [int(amp * math.sin(i / 3)) for i in range(1600)]).tobytes()

    loud, hiss = tone(8000), tone(30)
    assert -16 < rms_db(loud) < -14 and rms_db(hiss) < -55 and rms_db(b"") == -120.0
    p = Pauses(gap_ms=500)
    fired = [p.push(x) for x in [hiss] * 5 + [loud] * 8 + [hiss] * 7 + [loud] * 3 + [hiss] * 5]
    assert [i for i, f in enumerate(fired) if f] == [17, 27]   # 話の後 5 個目(0.5 s)の静けさで 1 度ずつ


def test_service_restarts_a_crashed_helper_three_times(tmp_path, monkeypatch):
    """アプリでの待ち受け: 止めていないのにヘルパーが終わったら、10 分に 3 回まで起動し直す(REQUIREMENTS §5.4)。"""
    from meetcue import dictation_app

    class Web:
        def push(self, ev, keep=True):
            pass

    class Term:
        lines = []

        def status(self, m):
            self.lines.append(m)

    svc = dictation_app.DictationService(_cfg(tmp_path), Web(), Term())
    starts = []
    monkeypatch.setattr(svc, "start", lambda: starts.append(1))
    monkeypatch.setattr(dictation_app.time, "sleep", lambda s: None)

    class Eng:
        def __init__(self, stop=False):
            self._s = stop

        def run(self):
            return 0

        def stopped(self):
            return self._s

    for _ in range(4):
        e = Eng()
        svc.engine = e
        svc._run(e)
    assert len(starts) == 3 and "3 回止まった" in svc.error
    e = Eng(stop=True)                                    # 頼んで止めたときは起動し直さない
    svc.engine = e
    svc._run(e)
    assert len(starts) == 3


class _S:
    """リアルタイムの試験用の Soniox(確定した語だけ)。"""
    def __init__(self, *texts):
        import threading
        self.lock = threading.Lock()
        self.final_tokens = [{"text": t} for t in texts]


def test_realtime_stops_and_rewinds_when_the_app_changes(tmp_path, monkeypatch):
    """リアルタイムの途中で別のアプリへ移った: その 1 回分から後は、最後にまとめて押した時のアプリへ(読み直しの指摘 10-19)。"""
    d = _engine2(tmp_path, monkeypatch, realtime="always")
    tk = Take()
    tk.context = {"app": "テキストエディット", "bundle": "com.apple.TextEdit"}
    sess = _S("一文目です。")
    d._feed(tk, sess)
    first = [json.loads(x) for x in d._sent if x.startswith('{"cmd": "paste"')][-1]
    assert first["expect"] == "com.apple.TextEdit"                              # 押した時のアプリにだけ貼る
    d._on_pasted({"type": "pasted", "id": tk.inserts[0]["pid"], "ok": True}, time.monotonic())
    sess.final_tokens.append({"text": "二文目です。"})
    d._feed(tk, sess)
    d._on_pasted({"type": "pasted", "id": tk.inserts[1]["pid"], "ok": False, "reason": "app_changed"}, time.monotonic())
    assert tk.rt_off and not d._realtime(tk) and tk.inserted == ["一文目です。"]
    sess.final_tokens.append({"text": "三文目です。"})
    d._feed(tk, sess)                                                          # 止めた後は貼らない
    assert len(tk.inserts) == 2
    pastes = _deliver(d, tk, "一文目です。二文目です。三文目です。", app={"app": "ターミナル", "bundle": "com.apple.Terminal"})
    assert pastes[-1]["text"] == "二文目です。三文目です。" and pastes[-1]["expect"] == "com.apple.TextEdit"
    assert d.last["text"] == "一文目です。二文目です。三文目です。"


def test_app_changed_before_paste_keeps_the_text(tmp_path, monkeypatch):
    """離した後に別のアプリへ移った: 貼らず、取り戻し方を出す(取り戻しのキーが無ければクリップボードへ)。"""
    d = _engine2(tmp_path, monkeypatch)
    pid = _deliver(d, Take(), "テストです。")[-1]["id"]
    d._on_pasted({"type": "pasted", "id": pid, "ok": False, "reason": "app_changed"}, time.monotonic())
    bars = [json.loads(x) for x in d._sent if x.startswith('{"cmd": "bar"')]
    assert "⌃⌥⇧V で貼り付け" in bars[-1]["text"] and not any(x.startswith('{"cmd": "clip"') for x in d._sent)
    d.s["keys"]["paste_last"] = ""
    pid = _deliver(d, Take(), "二つ目です。")[-1]["id"]
    d._on_pasted({"type": "pasted", "id": pid, "ok": False, "reason": "app_changed"}, time.monotonic())
    clips = [json.loads(x) for x in d._sent if x.startswith('{"cmd": "clip"')]
    assert clips == [{"cmd": "clip", "text": "二つ目です。"}]


def test_guard_refuses_while_recording_a_local_meeting(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    d = Dictation(cfg, out=lambda s: None, settings=dictation_settings.load(cfg),
                  guard=lambda: "LOCAL の録音中は音声入力を使えません")
    sent = []
    monkeypatch.setattr(d, "send", sent.append)
    d.on_key("down")
    d.on_key("down", "handsfree")
    assert d.take is None and "start" not in _ops(sent)
    assert any("LOCAL の録音中" in json.loads(x).get("text", "") for x in sent if x.startswith('{"cmd": "bar"'))


def test_edit_mode_waits_for_a_late_selection(tmp_path, monkeypatch):
    """選んだ文が遅れて届いても、指示を選んだ文の上に貼らない(読み直しの指摘 10-19)。"""
    import threading
    from meetcue import dictation_ai
    from meetcue.cues.openrouter import StreamResult
    monkeypatch.setattr(dictation_ai, "app_target", lambda cfg: (object(), ""))
    monkeypatch.setattr(dictation_ai, "edit", lambda cfg, t, sel, ins: StreamResult(ok=True, text=f"{sel}→{ins}"))
    d = _engine2(tmp_path, monkeypatch, edit_mode=True, realtime="always")
    tk = Take()
    tk.edit = None
    assert not d._realtime(tk)                                                 # 分からない間はリアルタイムで貼らない

    def late():
        time.sleep(0.15)
        tk.context = {"selected": "元の文", "bundle": "com.apple.TextEdit"}
        tk.ctx_event.set()
    threading.Thread(target=late).start()
    assert _deliver(d, tk, "短くして。")[-1]["text"] == "元の文→短くして。" and tk.edit is True


def test_work_errors_do_not_leave_the_bar_busy(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    d = Dictation(cfg, out=lambda s: None, settings=dictation_settings.load(cfg))
    sent = []
    monkeypatch.setattr(d, "send", sent.append)

    def boom(*a, **k):
        raise RuntimeError("壊れた")
    monkeypatch.setattr(soniox, "Session", boom)
    d.work(Take())                                                             # 例外を外へ出さない
    bars = [json.loads(x) for x in sent if x.startswith('{"cmd": "bar"')]
    assert bars[-1]["state"] == "error"


def test_web_refuses_foreign_hosts_and_non_json_posts():
    """ローカルの API の守り: ほかのサイト(DNS の付け替え)と、JSON でない POST は断る(読み直しの指摘 10-19)。"""
    import socket
    import urllib.error
    import urllib.request

    from meetcue.ui.web import WebUI
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    got = []
    ui = WebUI(port=port, on_action=got.append)
    ui.start()
    try:
        assert urllib.request.urlopen(ui.url + "api/state", timeout=3).status == 200
        local = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"Host": f"localhost:{port}"})
        assert urllib.request.urlopen(local, timeout=3).status == 200
        for req, code in [
            (urllib.request.Request(ui.url + "api/state", headers={"Host": "evil.example"}), 403),
            (urllib.request.Request(ui.url + "api/action", data=b'{"action": "toggle_pause"}', method="POST",
                                    headers={"Content-Type": "text/plain"}), 415),
            (urllib.request.Request(ui.url + "api/action", data=b'{"action": "toggle_pause"}', method="POST",
                                    headers={"Content-Type": "application/json", "Host": "evil.example"}), 403)]:
            try:
                urllib.request.urlopen(req, timeout=3)
                raise AssertionError("通ってしまった")
            except urllib.error.HTTPError as e:
                assert e.code == code
        assert got == []
    finally:
        ui.stop()


def test_captured_key_is_relayed_to_the_page(tmp_path):
    """キーを決める間(10-19 keigoly様「Fn だけで」): ヘルパーが拾ったキーを、本体が画面(SSE の dictation)へ中継する。"""
    from meetcue import dictation_app

    pushed = []

    class Web:
        def push(self, ev, keep=True):
            pushed.append((ev, keep))

    class Term:
        def status(self, m):
            pass

    cfg = _cfg(tmp_path)
    svc = dictation_app.DictationService(cfg, Web(), Term())
    d = Dictation(cfg, out=lambda s: None, settings=dictation_settings.load(cfg), on_change=svc._changed)
    d.on_event({"type": "capture", "on": True, "ok": True, "listen": True})
    d.on_event({"type": "captured", "spec": "fn", "label": "Fn"})
    assert ({"type": "dictation", "kind": "captured", "spec": "fn", "label": "Fn"}, False) in pushed
    assert any(e["kind"] == "capture" and e["on"] for e, _ in pushed)


def test_live_text_shows_what_is_being_said(tmp_path, monkeypatch):
    """話している言葉の窓(10-19 keigoly様「字幕が出ていなかった」): 確定 + 途中の語を貼る時と同じ仕上げで出し、
    同じ文は送り直さない。止めた後は貼る文を出し、途中の文で上書きしない。設定でオフにできる。"""
    d = _engine2(tmp_path, monkeypatch)
    tk = Take()

    class S:
        lock = __import__("threading").Lock()
        final_tokens = [{"text": "えー、今日は"}]
        partial = "晴れです"
    d._live(tk, S)
    d._live(tk, S)                                                             # 同じ文は送らない
    lives = [json.loads(x)["text"] for x in d._sent if x.startswith('{"cmd": "live"')]
    assert lives == ["今日は晴れです"]
    _deliver(d, tk, "えー、今日は晴れです。")
    S.partial = "晴れです、たぶん"
    d._live(tk, S)                                                             # 貼る文を出した後は上書きしない
    lives = [json.loads(x)["text"] for x in d._sent if x.startswith('{"cmd": "live"')]
    assert lives == ["今日は晴れです", "今日は晴れです。"]
    d2 = _engine2(tmp_path, monkeypatch, live_text=False)
    d2._live(Take(), S)
    assert not any(x.startswith('{"cmd": "live"') for x in d2._sent)


def test_stop_button_ends_the_take(tmp_path, monkeypatch):
    d, sent = _engine(tmp_path, monkeypatch)
    d.on_key("down", "handsfree")
    tk = d.take
    d.on_key("down", "stop")                                                   # 窓の ■
    assert tk.stopped.is_set() and tk.mode == "toggle" and d.take is None and _ops(sent) == ["start", "stop"]
    d.on_key("down")
    held = d.take
    d.on_key("down", "stop")                                                   # 押している最中に ■ を押しても止まる
    assert held.stopped.is_set() and held.mode == "toggle"


def test_modifier_only_key_waits_before_announcing(tmp_path, monkeypatch):
    """右 ⌘ など修飾キーだけのキー: 録り始めはすぐ・音とバーは 0.3 s 待つ。その間にショートカットと分かれば黙って捨てる
    (10-19 02:30 に右 ⌘ のショートカットで、録り始めの音とバーが一瞬出た)。"""
    d, sent = _engine(tmp_path, monkeypatch)
    d.on_key("down", via="tap")
    tk = d.take
    assert _ops(sent) == ["start"] and not tk.announced
    assert not any(x.startswith('{"cmd": "sound"') or x.startswith('{"cmd": "bar"') for x in sent)
    d.on_key("abort")                                                          # ⌘C などに使った
    assert tk.canceled == "abort" and not any(x.startswith('{"cmd": "sound"') or x.startswith('{"cmd": "bar"') for x in sent)
    sent.clear()
    d.on_key("down", via="tap")
    tk2 = d.take
    tk2.grace_until = time.monotonic() - 0.01                                  # 待ち時間を過ぎた
    d.tick()
    assert tk2.announced and any('"name": "start"' in x for x in sent if x.startswith('{"cmd": "sound"'))
    d3, sent3 = _engine(tmp_path, monkeypatch)
    d3.on_key("down")                                                          # ⌃⌥V などの組み合わせ(Carbon)はすぐ知らせる
    assert d3.take.announced and any('"name": "start"' in x for x in sent3 if x.startswith('{"cmd": "sound"'))


def test_aborted_shortcut_never_connects_to_soniox(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    d = Dictation(cfg, out=lambda s: None, settings=dictation_settings.load(cfg))
    monkeypatch.setattr(d, "send", lambda line: None)
    made = []
    monkeypatch.setattr(soniox, "Session", lambda *a, **k: made.append(1) or (_ for _ in ()).throw(RuntimeError("つないだ")))
    tk = Take()
    tk.grace_until = time.monotonic() + 5
    tk.canceled = "abort"
    d.work(tk)
    assert made == []


def test_helper_resource_is_recorded_and_summarized(tmp_path, monkeypatch):
    """ナギのペット v0 の Step 1(10-19 keigoly様「メモリとか CPU とかも極力食わないように」): ヘルパーの重さを 1 分ごとに
    resource.jsonl(phase pet_resource)へ残し、ステータスの画面と report に出す。速さの記録(metrics.jsonl)には混ぜない。"""
    d, _ = _engine(tmp_path, monkeypatch)
    assert d.resource_view() is None
    d.on_event({"type": "resource", "cpu_pct": 0.9, "interval_s": 60, "footprint_mb": 11.0})   # 最初の 1 分 = 起動の分
    assert d.resource_view() is None
    d.on_event({"type": "resource", "cpu_pct": 0.006, "cpu_s": 0.05, "interval_s": 60, "footprint_mb": 7.1, "peak_mb": 7.2,
                "wakeups": 3, "idle_wakeups": 1})
    d.on_event({"type": "resource", "cpu_pct": 0.3, "interval_s": 60, "footprint_mb": 9.4, "peak_mb": 9.6, "wakeups": 600,
                "idle_wakeups": 120, "pet": {"state": "listen", "visible": True}})
    d.on_event({"type": "resource", "cpu_s": 0.0})          # 最初の 1 回(間隔の無い行)は数えない
    v = d.resource_view()
    assert v["minutes"] == 2 and v["cpu_max"] == 0.3 and v["mem_max"] == 9.4 and v["last"]["pet"]["state"] == "listen"
    d.res_metrics.close()
    rows = [json.loads(x) for x in (tmp_path / "dictation" / "resource.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["phase"] for r in rows] == ["pet_resource"] * 3 and rows[0]["startup"] and rows[1]["footprint_mb"] == 7.1
    assert "pet_resource" not in (tmp_path / "dictation" / "metrics.jsonl").read_text(encoding="utf-8")
    line = dictation.resource_summary(tmp_path / "dictation" / "resource.jsonl")
    assert line.startswith("ヘルパーの重さ(2 分)") and "最大 0.300%" in line and "最大 9.4 MB" in line
    assert dictation.resource_summary(tmp_path / "none.jsonl") == ""


def test_pet_follows_settings_and_meeting_state(tmp_path, monkeypatch):
    """ナギのペット(FR-14c): ヘルパーに絵の置き場を渡し(画面を出さない試験では渡さない)、設定と会議の状態を送る。"""
    d, sent = _engine(tmp_path, monkeypatch)
    argv = d.helper_argv()
    assert argv[argv.index("--pet-dir") + 1] == str(dictation.PET_DIR) and (dictation.PET_DIR / "idle.png").exists()
    d2 = Dictation(_cfg(tmp_path), out=lambda s: None, settings=d.s, helper_args=["--no-bar", "--no-sound"])
    assert "--pet-dir" not in d2.helper_argv()
    d2.res_metrics.close()
    d.on_event({"type": "ready", "bindings": {}})
    pets = [json.loads(x) for x in sent if x.startswith('{"cmd": "pet"')]
    assert pets == [{"cmd": "pet", "mode": "always", "size": "m", "meeting": "idle"}]
    d.set_meeting("listen")                       # アプリのスレッドから: 本体のループに積む
    d.on_event(d.events.get_nowait())
    d.meeting_event("cue")
    d.on_event(d.events.get_nowait())
    assert [json.loads(x) for x in sent[-2:]] == [{"cmd": "pet", "meeting": "listen"}, {"cmd": "pet", "event": "cue"}]
    d.s = {**d.s, "pet": "recording", "pet_size": "s"}
    sent.clear()
    d.on_event({"type": "_settings", "settings": d.s})
    assert {"cmd": "pet", "mode": "recording", "size": "s", "meeting": "listen"} in [json.loads(x) for x in sent]


def test_service_maps_meeting_events_and_saves_pet_menu(tmp_path, monkeypatch):
    """アプリの知らせ → ペットの表情(録音中 = listen・保存 = think・質問の検知 = cue・保存した = done)。
    右クリックの「隠す」「大きさ」は設定に保存し、「音声入力の画面を開く」は既定のブラウザで #dictation を開く。"""
    from meetcue import dictation_app
    from meetcue.ui.web import WebUI

    class Term:
        def status(self, m):
            pass

    cfg = _cfg(tmp_path)
    web = WebUI(port=8799)
    svc = dictation_app.DictationService(cfg, web, Term())
    assert svc._on_web in web.listeners

    class Eng:
        running = False

        def __init__(self):
            self.calls = []

        def set_meeting(self, st):
            self.calls.append(("meeting", st))

        def meeting_event(self, e):
            self.calls.append(("event", e))

        def apply_settings(self, s):
            self.calls.append(("settings", s["pet"], s["pet_size"]))

    eng = Eng()
    svc.engine = eng
    web.set_state(recording=True, paused=False)
    web.set_state(saving_note="仕上げています")          # 同じ状態は送り直さない
    web.push({"type": "judgment", "trigger": True})
    web.push({"type": "judgment", "trigger": False})
    web.set_state(recording=False, saving=True)
    web.push({"type": "session_done", "id": "x", "summary": True})
    web.set_state(saving=False)
    assert eng.calls == [("meeting", "listen"), ("event", "cue"), ("meeting", "think"), ("event", "done"), ("meeting", "idle")]
    eng.calls.clear()
    svc._changed({"kind": "pet", "action": "set", "pet": "off"})
    svc._changed({"kind": "pet", "action": "set", "pet_size": "l"})
    s = dictation_settings.load(cfg)
    assert s["pet"] == "off" and s["pet_size"] == "l" and eng.calls[-1] == ("settings", "off", "l")
    opened = []
    monkeypatch.setattr(dictation_app.subprocess, "Popen", lambda argv, **kw: opened.append(argv))
    svc._changed({"kind": "pet", "action": "open"})
    assert opened == [["open", "http://127.0.0.1:8799/#dictation"]]

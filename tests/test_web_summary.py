import json
import time
import urllib.request
from pathlib import Path

from meetcue.summary import build_summary, save_to_vault
from meetcue.ui.web import MultiUI, WebUI


def _free_port():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_web_ui_state_events_and_action():
    got = []
    ui = WebUI(port=_free_port(), on_action=got.append)
    ui.start()
    try:
        ui.set_state(mode="presenter", privacy="private", paused=False)
        ui.utterance("system", "費用はどのくらいでしょうか。", "abcd1234")
        ui.cue_line("abcd1234", "回答1: 見出し | 骨子 | 根拠: なし")
        st = json.loads(urllib.request.urlopen(ui.url + "api/state", timeout=3).read())
        assert st["mode"] == "presenter"
        import os
        assert st["uid"] == getattr(os, "getuid", lambda: None)()   # 本体の持ち主(別の利用者の本体と見分ける・2026-09-28)
        html = urllib.request.urlopen(ui.url, timeout=3).read().decode()
        assert "Meeting Cue!" in html and "EventSource" in html
        req = urllib.request.Request(ui.url + "api/action", data=json.dumps({"action": "toggle_pause"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        assert json.loads(urllib.request.urlopen(req, timeout=3).read())["ok"] and got == ["toggle_pause"]
        # SSE: 接続すると state と履歴が流れる
        resp = urllib.request.urlopen(ui.url + "events", timeout=3)
        lines = []
        t0 = time.time()
        while len(lines) < 3 and time.time() - t0 < 3:
            line = resp.readline().decode()
            if line.startswith("data:"):
                lines.append(json.loads(line[5:]))
        types = [x["type"] for x in lines]
        assert types[0] == "state" and "utterance" in types and "cue_line" in types
        resp.close()
    finally:
        ui.stop()


def test_multi_ui_skips_missing_methods():
    class A:
        def status(self, m): self.m = m
    a = A()
    ui = MultiUI(a, None)
    ui.status("x"); ui.set_state(mode="p")   # A に set_state が無くても落ちない
    assert a.m == "x"


def test_summary_without_llm_and_vault_local(tmp_path: Path):
    d = tmp_path / "s"; d.mkdir()
    (d / "meta.json").write_text(json.dumps({"id": "ab12", "started_ms": 1000, "ended_ms": 61000, "mode": "participant", "privacy": "local"}))
    (d / "transcript.jsonl").write_text('{"rid":"r1","channel":"system","text":"費用は？"}\n{"rid":"r2","channel":"mic","text":"確認します"}\n')
    (d / "judgments.jsonl").write_text('{"rid":"r1","summary":{"speech_act":"question","intent":"cost"}}\n')
    (d / "cues.jsonl").write_text(json.dumps({"rid": "r1", "kind": "cues", "part": "answers", "intent": "概算が知りたい",
        "answers": [{"title": "A", "body": "a", "source": "なし"}, {"title": "B", "body": "b", "source": "x.md#h"}],
        "counters": [], "ranking": {"answer": [{"index": 2, "total": 3.0}, {"index": 1, "total": 2.0}]}}, ensure_ascii=False) + "\n")
    out = build_summary(d, api_key=None, model="m", privacy="local")
    text = out.read_text()
    assert "LLM 要約なし" in text and "★回答: **B**" in text and text.index("**B**") < text.index("**A**")
    vault = tmp_path / "vault"
    dest = save_to_vault(out, vault, privacy="local", session_id="ab12")
    assert dest.exists() and "機微モード" in dest.read_text() and "費用" not in dest.read_text()

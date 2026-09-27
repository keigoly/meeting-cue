"""一覧の右クリック: Mac + Google Drive の一覧・書き出し・Google Drive へ移動・ゴミ箱(2026-09-26)。"""
import json
import sys
from pathlib import Path

import pytest

from meetcue import library
from meetcue.app import App
from meetcue.config import Config

SID_A = "20260926_100000_aaaaaaaa"
SID_B = "20260926_110000_bbbbbbbb"


def _session(root, sid, *, title="定例", privacy="private", audio=True, summary="## 要約\n- 決定: A"):
    d = root / sid
    (d / "audio").mkdir(parents=True)
    meta = {"title": title, "started_ms": 1790380800000, "ended_ms": 1790380860000, "mode": "participant", "privacy": privacy}
    if audio:
        (d / "audio" / "system.m4a").write_bytes(b"s")
        (d / "audio" / "mic.m4a").write_bytes(b"m")
        meta["audio"] = {"system": {"path": "audio/system.m4a", "t0_ms": 1790380800000},
                         "mic": {"path": "audio/mic.m4a", "t0_ms": 1790380800350}}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    rows = [{"rid": "r1", "channel": "system", "text": "費用はどのくらいですか", "t_ms": 1790380815000,   # 確定は遅れて来る
             "start_seconds": 5.0, "end_seconds": 7.0},
            {"rid": "r2", "channel": "mic", "text": "月 3 万円ほどです", "t_ms": 1790380820000,
             "start_seconds": 11.7, "end_seconds": 13.0}]                                          # + mic の頭のずれ 0.35 s
    (d / "transcript.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    if summary:
        (d / "summary.md").write_text(summary, encoding="utf-8")
    return d


def _fake_mix(tmp_path, ok=True):
    """mix-helper の代わり: 受け取った引数を args.json に残し、--out に 1 バイト書く。
    [python, スクリプト] の引数の並びで渡す(2026-09-27: Windows の書き出しと同じ形。#! の実行ファイルは Windows で動かないため)。"""
    p = tmp_path / "mix_helper_fake.py"
    log = tmp_path / "args.json"
    body = ('import json, sys\n'
            f'json.dump(sys.argv[1:], open({str(log)!r}, "w", encoding="utf-8"), ensure_ascii=False)\n'
            'a = sys.argv[1:]; out = a[a.index("--out") + 1]\n'
            + ('open(out, "wb").write(b"x"); print(json.dumps({"ok": True, "ms": 5, "duration_ms": 31000, "inputs": 2}))\n' if ok
               else 'print(json.dumps({"ok": False, "error": "boom"})); sys.exit(1)\n'))
    p.write_text(body, encoding="utf-8")
    return [sys.executable, str(p)], log


def test_list_merges_mac_and_drive(tmp_path):
    mac, drive = tmp_path / "sessions", tmp_path / "drive" / "Meeting Cue!"
    _session(mac, SID_A)
    _session(drive, SID_B, title="Drive の記録")
    _session(drive, SID_A, title="重複(Drive 側)")
    rows = library.list_sessions([mac, drive])
    assert [(r["id"], r["where"]) for r in rows] == [(SID_B, "drive"), (SID_A, "mac")]   # 新しい順・同じ id は Mac を優先
    assert rows[1]["title"] == "定例"
    d = library.session_detail([mac, drive], SID_B)
    assert d["where"] == "drive" and d["transcript"][0]["text"] == "費用はどのくらいですか"
    assert library.set_title([mac, drive], SID_B, "改名") and library.session_detail([mac, drive], SID_B)["title"] == "改名"
    assert library.list_sessions([mac, tmp_path / "無い"])[0]["id"] == SID_A   # Drive が無くても落ちない


def test_drive_accounts_found_in_cloud_storage(tmp_path):
    (tmp_path / "GoogleDrive-a@example.com" / "マイドライブ").mkdir(parents=True)
    (tmp_path / "GoogleDrive-b@example.com" / "My Drive").mkdir(parents=True)
    (tmp_path / "GoogleDrive-c@example.com").mkdir()                     # マイドライブが無いものは除く
    (tmp_path / "Dropbox").mkdir()
    accts = library.drive_accounts(tmp_path)
    assert [a["label"] for a in accts] == ["a@example.com", "b@example.com"]
    assert accts[0]["root"] == tmp_path / "GoogleDrive-a@example.com" / "マイドライブ" / "Meeting Cue!"
    assert library.drive_accounts(tmp_path / "無い") == []


def test_move_to_drive_and_local_is_refused(tmp_path):
    mac, drive = tmp_path / "sessions", tmp_path / "drive" / "Meeting Cue!"
    _session(mac, SID_A)
    _session(mac, SID_B, privacy="local")
    dest = library.move_to_drive(mac, SID_A, drive)
    assert dest == drive / SID_A and (dest / "audio" / "mic.m4a").exists() and not (mac / SID_A).exists()
    with pytest.raises(PermissionError):
        library.move_to_drive(mac, SID_B, drive)                        # LOCAL は Mac の外に出さない
    assert (mac / SID_B).exists() and not (drive / SID_B).exists()
    _session(mac, SID_A)                                                # 同じ id が Drive にあれば上書きしない
    with pytest.raises(FileExistsError):
        library.move_to_drive(mac, SID_A, drive)


def test_trash_moves_folder_and_names_are_unique(tmp_path):
    mac, trash = tmp_path / "sessions", tmp_path / "Trash"
    trash.mkdir()
    _session(mac, SID_A, title="定例/週次")
    first = library.trash_session(mac, SID_A, trash)
    assert first.parent == trash and (first / "meta.json").exists() and not (mac / SID_A).exists()
    assert first.name.startswith("Meeting Cue! 定例 週次 ")                 # / は名前に使わない
    _session(mac, SID_A, title="定例/週次")
    assert library.trash_session(mac, SID_A, trash).name == first.name + " 2"
    with pytest.raises(FileNotFoundError):
        library.trash_session(mac, SID_A, trash)


def test_export_writes_audio_transcript_summary(tmp_path):
    mac, out = tmp_path / "sessions", tmp_path / "out"
    _session(mac, SID_A, title="定例: 9 月")
    mix, log = _fake_mix(tmp_path)
    res = library.export_session(mac, SID_A, out, mix)
    folder = out / Path(res["path"]).name
    assert folder.parent == out and folder.name.startswith("定例 9 月 ")          # : は使わない
    assert res["files"] == ["音声.m4a", "文字起こし.md", "サマリ.md"]
    args = json.loads(log.read_text(encoding="utf-8"))
    assert Path(args[args.index("--out") + 1]) == folder / "音声.m4a"
    ins = [args[i + 1] for i, a in enumerate(args) if a == "--in"]
    assert any(x.endswith("system.m4a@0.000") for x in ins) and any(x.endswith("mic.m4a@0.350") for x in ins)   # 頭合わせ
    md = (folder / "文字起こし.md").read_text(encoding="utf-8")
    assert md.startswith("# 定例: 9 月") and "- [00:05] 相手: 費用はどのくらいですか" in md and "- [00:12] 自分: 月 3 万円ほどです" in md
    assert "2026-09-26 " in md.splitlines()[2] and ":" in md.splitlines()[2]              # 見出しの時刻は 13:40 の形
    assert (folder / "サマリ.md").read_text(encoding="utf-8").startswith("## 要約")
    again = library.export_session(mac, SID_A, out, mix)                      # 2 回目は別のフォルダ
    assert again["path"] == res["path"] + " 2"


def test_export_without_audio_or_summary_and_mix_failure(tmp_path):
    mac, out = tmp_path / "sessions", tmp_path / "out"
    _session(mac, SID_A, audio=False, summary=None)
    mix, log = _fake_mix(tmp_path)
    res = library.export_session(mac, SID_A, out, mix)
    assert res["files"] == ["文字起こし.md"] and not log.exists()             # 音声が無ければ mix を呼ばない
    _session(mac, SID_B)
    bad, _ = _fake_mix(tmp_path, ok=False)
    with pytest.raises(RuntimeError, match="boom"):
        library.export_session(mac, SID_B, out, bad)


def test_api_session_ops(tmp_path, monkeypatch):
    app = App(Config(app_dir=tmp_path), sources=[], window=False)
    drive_root = tmp_path / "cloud" / "Meeting Cue!"
    monkeypatch.setattr(app, "_drive_accounts", lambda: [{"label": "a@example.com", "root": drive_root}])
    monkeypatch.setattr(library, "trash_session", lambda root, sid, trash=None: tmp_path / "Trash" / sid)
    pushed = []
    app.web.push = pushed.append
    mac = app.cfg.sessions_root
    _session(mac, SID_A)
    _session(mac, SID_B, privacy="local")
    assert app._api("GET", "/api/storage", None) == (200, {"drive": [{"label": "a@example.com"}]})
    assert app._api("POST", f"/api/sessions/{SID_B}/drive", {})[0] == 403        # LOCAL は移せない
    code, body = app._api("POST", f"/api/sessions/{SID_A}/drive", {})
    assert code == 200 and body["path"] == str(drive_root / SID_A)
    assert pushed[-1] == {"type": "sessions_changed", "op": "drive", "id": SID_A}
    rows = app._api("GET", "/api/sessions", None)[1]["sessions"]
    assert {(r["id"], r["where"]) for r in rows} == {(SID_A, "drive"), (SID_B, "mac")}
    assert app._api("POST", f"/api/sessions/{SID_A}/drive", {"account": 3})[0] == 400
    assert app._api("POST", f"/api/sessions/{SID_B}/trash", {})[0] == 200
    monkeypatch.setattr(app, "_choose_folder", lambda: None)
    assert app._api("POST", f"/api/sessions/{SID_A}/export", {}) == (200, {"ok": False, "cancelled": True})   # キャンセル
    app.session = type("S", (), {"dir": mac / SID_B})()                         # 録音中の記録は操作しない
    assert app._api("POST", f"/api/sessions/{SID_B}/trash", {})[0] == 409

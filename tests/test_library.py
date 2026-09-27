"""セッションの一覧・詳細・題名(meetcue app の一覧と録音後の画面)。"""
import json

from meetcue import library


def _make(root, sid, *, title=None, ended=True):
    d = root / sid
    d.mkdir(parents=True)
    meta = {"id": sid[-8:], "started_ms": 1_000_000, "ended_ms": 1_060_000 if ended else None,
            "mode": "participant", "privacy": "private", "sources": ["mic:mic"], "model": "m"}
    if title:
        meta["title"] = title
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "transcript.jsonl").write_text(
        json.dumps({"rid": "r1", "channel": "system", "text": "費用はどのくらいですか", "start_seconds": 1.0,
                    "end_seconds": 2.0, "t_ms": 1_002_500}) + "\n", encoding="utf-8")
    (d / "judgments.jsonl").write_text(
        json.dumps({"rid": "r1", "source": "jev", "trigger": True,
                    "summary": {"speech_act": "question", "to_me": 0.6, "intent": "cost"}}) + "\n", encoding="utf-8")
    cues = [{"rid": "r1", "kind": "knowledge", "items": [{"path": "a.md", "heading": "h", "snippet": "s"}]},
            {"rid": "r1", "kind": "cues", "part": "answers", "intent": "費用", "counters": [],
             "answers": [{"title": f"A{i}", "body": "b", "source": "なし"} for i in range(1, 6)],
             "ranking": {"answer": [{"index": 4, "total": 9}, {"index": 2, "total": 8}, {"index": 5, "total": 7},
                                    {"index": 1, "total": 6}]}}]
    (d / "cues.jsonl").write_text("\n".join(json.dumps(c) for c in cues) + "\n", encoding="utf-8")
    return d


def test_list_and_detail(tmp_path):
    _make(tmp_path, "20260926_010000_aaaaaaaa")
    _make(tmp_path, "20260926_020000_bbbbbbbb", title="定例", ended=False)
    (tmp_path / "junk").mkdir()
    rows = library.list_sessions(tmp_path)
    assert [r["id"] for r in rows] == ["20260926_020000_bbbbbbbb", "20260926_010000_aaaaaaaa"]   # 新しい順
    assert rows[0]["title"] == "定例" and rows[1]["title"] == "無題"
    assert rows[1]["duration_s"] == 60.0 and rows[1]["questions"] == 1 and rows[1]["utterances"] == 1
    d = library.session_detail(tmp_path, "20260926_010000_aaaaaaaa")
    assert d["transcript"][0]["at_s"] == 2.5 and d["judgments"]["r1"]["trigger"]
    ans = d["cues"]["r1"]["answers"]
    assert [a["title"] for a in ans] == ["A4", "A2", "A5", "A1", "A3"]      # 採点順、未採点は後ろ
    assert [a["star"] for a in ans] == [True, True, True, False, False]   # 上位 3 に ★
    assert d["cues"]["r1"]["knowledge"][0]["path"] == "a.md"


def test_title_and_path_safety(tmp_path):
    _make(tmp_path, "20260926_010000_aaaaaaaa")
    assert library.set_title(tmp_path, "20260926_010000_aaaaaaaa", "  顧客定例 ")
    assert library.list_sessions(tmp_path)[0]["title"] == "顧客定例"
    assert library.set_title(tmp_path, "20260926_010000_aaaaaaaa", "")
    assert library.list_sessions(tmp_path)[0]["title"] == "無題"
    for bad in ("../x", "20260926_010000_aaaaaaaa/../..", "", "20260926_010000_zzzzzzzz"):
        assert library.session_detail(tmp_path, bad) is None and not library.set_title(tmp_path, bad, "x")


def test_audio_and_levels(tmp_path):
    d = _make(tmp_path, "20260926_010000_aaaaaaaa")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    meta["audio"] = {"system": {"path": "audio/system.m4a", "t0_ms": 1_000_300},
                     "mic": {"path": "audio/mic.m4a", "t0_ms": 1_000_100}}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "audio").mkdir()
    (d / "audio" / "system.m4a").write_bytes(b"x" * 10)            # mic は meta にあるがファイルが無い → 出さない
    (d / "levels.json").write_text(json.dumps({"step_s": 0.1, "channels": {"system": [-30, -90]}}), encoding="utf-8")
    row = library.list_sessions(tmp_path)[0]
    assert row["audio"] == {"system": {"url": "/api/sessions/20260926_010000_aaaaaaaa/audio/system.m4a", "t0_ms": 1_000_300}}
    assert library.audio_file(tmp_path, d.name, "system.m4a") == d / "audio" / "system.m4a"
    for bad in ("../meta.json", "mic.m4a", "system.wav", "x/system.m4a"):
        assert library.audio_file(tmp_path, d.name, bad) is None
    assert library.levels(tmp_path, d.name)["channels"]["system"] == [-30, -90]
    assert library.levels(tmp_path, "20260926_010000_zzzzzzzz") is None

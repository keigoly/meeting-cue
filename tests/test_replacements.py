"""置き換え辞書(2026-09-27): 誤りを正しい語へ・空白と大文字小文字を区別しない・長いものから 1 回だけ・1 文字の誤りは使わない。"""
import asyncio
import time

from meetcue.replacements import Replacer, parse

TEXT = """# 注釈
Palo Alto Networks|パラワートネットワークス|パウアートネットワークス|パロアルト
Meeting Cue|ミーティング級|ミーティングQ
PoC|POC|PYC
新井と申します|洗いと申します
SASE|サージー|サ
だけ正しい語
"""


def test_parse_and_ignored_lines():
    rules, ignored = parse(TEXT)
    assert rules[0] == ("Palo Alto Networks", ["パラワートネットワークス", "パウアートネットワークス", "パロアルト"])
    assert ("SASE", ["サージー"]) in rules                     # 1 文字の誤り「サ」は使わない
    assert ignored == ["だけ正しい語"]


def test_apply_spaces_case_longest_first(tmp_path):
    p = tmp_path / "replacements.txt"
    p.write_text(TEXT, encoding="utf-8")
    r = Replacer(p)
    assert r.apply("パラワートネットワークスのファイアウォール") == "Palo Alto Networksのファイアウォール"
    assert r.apply("今はミーティング Qという") == "今はMeeting Cueという"          # 空白の有無を区別しない
    assert r.apply("PoC と poc と PYC") == "PoC と PoC と PoC"                  # 大文字・小文字を区別しない
    assert r.apply("洗いと申します。洗いました") == "新井と申します。洗いました"   # 文脈ごと登録した誤りだけ
    assert r.apply("パロアルトネットワークス") == "Palo Alto Networksネットワークス"   # 登録どおり(長い誤りを先に書けば防げる)
    assert r.apply("サンプル") == "サンプル"
    assert r.apply("pocket の POC") == "pocket の PoC"                          # 英字だけの誤りは英単語の一部に当てない
    assert r.apply("POC進めています") == "PoC進めています"                        # 日本語が続くのはよい


def test_api_get_and_save(tmp_path):
    from meetcue.app import App
    from meetcue.config import Config
    a = App(Config(app_dir=tmp_path), sources=[], window=False)
    assert a._api("GET", "/api/replacements", None) == (200, {"text": "", "rules": 0, "wrongs": 0, "ignored": [],
                                                              "path": str(tmp_path / "replacements.txt")})
    code, v = a._api("POST", "/api/replacements", {"text": "Jev|ジェブ\nだけ"})
    assert code == 200 and v["rules"] == 1 and v["ignored"] == ["だけ"]
    assert (tmp_path / "replacements.txt").read_text(encoding="utf-8") == "Jev|ジェブ\nだけ\n"
    assert a._api("POST", "/api/replacements", {"text": "x" * 200_001})[0] == 400


def test_reload_on_file_change_and_save(tmp_path):
    p = tmp_path / "replacements.txt"
    r = Replacer(p)
    assert r.apply("オブシャン") == "オブシャン" and r.view()["rules"] == 0   # ファイルが無くても動く
    r.save("Obsidian|オブシャン|オブシジャン")
    assert r.apply("オブシャンに") == "Obsidianに"
    time.sleep(0.01)
    p.write_text("Obsidian|オブシジャン\n", encoding="utf-8")                    # 手で直しても次から効く
    import os
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert r.apply("オブシャン") == "オブシャン" and r.apply("オブシジャン") == "Obsidian"


def test_pipeline_applies_to_transcript_and_keeps_raw(tmp_path):
    import json

    import meetcue.pipeline as pl
    from meetcue.config import Config
    from meetcue.segmenter import Utterance
    from meetcue.session import Session

    (tmp_path / "replacements.txt").write_text("Jev|ジェブ\n", encoding="utf-8")

    class UI:
        def __init__(self):
            self.utts = []

        def utterance(self, ch, text, rid):
            self.utts.append(text)

        def __getattr__(self, name):
            return lambda *a, **kw: None
    cfg = Config(app_dir=tmp_path)
    s = Session(tmp_path / "s", mode="participant", privacy="local", sources=[], model="m")
    ui = UI()
    p = pl.Pipeline(cfg, s, ui, [], api_key=None, llm_enabled=False, jev_enabled=False)
    asyncio.run(p._handle(Utterance(rid="r1", channel="mic", text="相手の質問をジェブで判定", start_s=0, end_s=1,
                                    t_final_ms=0)))
    rec = json.loads((s.dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["text"] == "相手の質問をJevで判定" and rec["raw"] == "相手の質問をジェブで判定"
    assert ui.utts == ["相手の質問をJevで判定"]

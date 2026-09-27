from meetcue.cues.prompts import build_messages, parse_cue_lines
from meetcue.segmenter import split_sentences, tail_kind, tail_punct


def test_parse_cue_lines_partial_and_full():
    text = ("意図: 設定方法を知りたい\n回答1: 見出しA | 骨子A | 根拠: 01_Projects/x.md#h\n\n"
            "逆質問1: 対象は全ユーザーですか | 範囲を確認\n回答2: 見出しB | 骨子B | 根拠: なし")
    p = parse_cue_lines(text)
    assert p["intent"] == "設定方法を知りたい"
    assert p["answers"][0] == {"title": "見出しA", "body": "骨子A", "source": "01_Projects/x.md#h"}
    assert p["answers"][1]["source"] == "なし"
    assert p["counters"][0]["question"].startswith("対象は") and p["counters"][0]["aim"] == "範囲を確認"
    assert parse_cue_lines("回答1: だけ") == {"intent": "", "answers": [{"title": "だけ", "body": "", "source": ""}], "counters": []}


def test_build_messages_includes_knowledge_and_mode():
    class H:
        path, heading, body = "01_Projects/a.md", "節", "本文"
    msgs = build_messages(mode="presenter", profile="SE", context=["[system] 前の話"], utterance="質問？",
                          judgment={"speech_act": "question", "intent": "how"}, hits=[H()])
    assert msgs[0]["role"] == "system" and "登壇者" in msgs[1]["content"]
    assert "01_Projects/a.md#節" in msgs[1]["content"] and "前の話" in msgs[1]["content"]


def test_tail_kind_and_split():
    assert tail_kind("扱いますか") == "ja_end"
    assert tail_kind("費用は") == "particle"
    assert tail_kind("ブラックオフツーというゲー") == "mid_word"
    assert tail_kind("承知しました。") == "ja_end"
    assert tail_punct("以上です。") and not tail_punct("以上です")
    assert split_sentences("はい。次に費用ですが 4.5 万円です。いつ") == ["はい。", "次に費用ですが 4.5 万円です。", "いつ"]


def test_short_discourse_marker_is_carried(tmp_path):
    import asyncio
    from meetcue.metrics import Metrics
    from meetcue.segmenter import Segmenter, SegmenterConfig

    class FakeHelper:
        async def finalize(self): pass

    m = Metrics(tmp_path / "m.jsonl")
    seg = Segmenter(FakeHelper(), "system", SegmenterConfig(), m)
    out = asyncio.Queue()

    async def run():
        await seg._emit("まず", {}, out, forced=True)
        assert out.empty() and seg._carry == "まず"
        await seg._emit("確認ですが、この構成はどう扱いますか。", {}, out, forced=True)
        u = await out.get()
        assert u.text.startswith("まず確認ですが")
    asyncio.run(run())

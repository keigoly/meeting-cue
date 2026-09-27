from meetcue.judge import heuristic, jev


def test_heuristic_question_and_request():
    q = heuristic.judge("導入にかかる費用はどのくらいでしょうか。")
    assert q["speech_act"] == "question" and q["intent"] == "cost" and q["to_me"] >= 0.6
    r = heuristic.judge("事例があれば教えてください。")
    assert r["speech_act"] in ("question", "request")
    s = heuristic.judge("今日の議題は三つあります。")
    assert s["speech_act"] == "statement"
    a = heuristic.judge("なるほど、承知しました。")
    assert a["speech_act"] == "agreement"


def test_jev_summarize_and_trigger():
    answers = {
        "speech_act": {"type": "choice", "choice": "question", "probabilities": {"question": 0.92, "statement": 0.05},
                       "confidence": 0.9},
        "to_me": {"type": "noul", "noul": 0.57},
        "intent": {"type": "choice", "choice": "cost", "probabilities": {"cost": 0.8}},
        "urgency": {"type": "score", "score": 0.9, "legend": {"0.0": "low", "0.5": "medium", "1.0": "high"}},
        "answer_length": {"type": "choice", "choice": "short", "probabilities": {"short": 0.6}},
        "broken": "x",
    }
    s = jev.summarize(answers)
    assert s["speech_act"] == "question" and s["speech_act_p"] == 0.92 and s["to_me"] == 0.57
    assert s["urgency_label"] == "high" and "broken" not in s
    assert jev.should_trigger(s, to_me_min=0.5)
    assert not jev.should_trigger(s, to_me_min=0.6)
    s2 = dict(s, speech_act="statement")
    assert not jev.should_trigger(s2)


def test_jev_state_is_bounded():
    st = jev.build_state("あ" * 5000, ["b" * 1000, "c" * 1000, "d" * 1000], "presenter", "room")
    assert len(st["utterance"]) == jev.MAX_STATE_CHARS
    assert len(st["prev"]) == 2 and all(len(p) <= 300 for p in st["prev"])


def test_jev_no_key_fails_closed():
    r = jev.decide({"utterance": "x"}, api_key=None)
    assert not r.ok and r.error == "no_key"

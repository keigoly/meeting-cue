from meetcue.judge import selector


def test_weights_override_by_mode_and_intent():
    base = selector.weights_for("answer", "participant", None)
    assert base["on_point"] == 1.2 and base["grounded"] == 1.0
    risky = selector.weights_for("answer", "presenter", "risk")
    assert risky["safe"] == 1.0 and risky["grounded"] == 1.2 and risky["persuasive"] == 1.0
    aud = selector.weights_for("counter", "audience", "clarify")
    assert aud["reveals_intent"] == 1.2 and aud["polite"] == 0.7


def test_rank_puts_unscored_last_and_is_stable():
    a = selector.Scored("answer", 1, "a", scores={"on_point": 0.5}, total=1.0)
    b = selector.Scored("answer", 2, "b", scores={"on_point": 1.0}, total=2.0)
    c = selector.Scored("answer", 3, "c", error="connect_error")
    d = selector.Scored("answer", 4, "d", scores={"on_point": 1.0}, total=2.0)
    assert [x.index for x in selector.rank([a, c, d, b])] == [2, 4, 1, 3]


def test_weighted_total():
    assert selector.weighted_total({"on_point": 1.0, "grounded": 0.5}, {"on_point": 1.0, "grounded": 2.0}) == 2.0
    assert selector.weighted_total({}, {"on_point": 1.0}) is None


def test_score_candidate_without_key_fails_open():
    c = selector.Scored("counter", 1, "対象は全ユーザーですか")
    out = selector.score_candidate(c, utterance="q", context=[], mode="participant", intent=None,
                                   api_key=None, model="x", timeout=1)
    assert out.total is None and out.error == "no_key"

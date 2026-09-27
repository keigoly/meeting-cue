"""話し続ける音声の打ち切り(long_utterance_ms / long_utterance_hard_ms)。2026-09-26 の一人語りで確定が 59 s 遅れた件。"""
import asyncio
import json
import time

from meetcue.metrics import Metrics
from meetcue.segmenter import Segmenter, SegmenterConfig

TAP = dict(pause_ms=1200, pause_incomplete_ms=1600, pause_sentence_ms=1100, poll_ms=10)


class FakeHelper:
    def __init__(self):
        self.finalized = 0

    async def finalize(self):
        self.finalized += 1


def _speaking(seg: Segmenter, text: str, age_s: float) -> None:
    """partial が今も更新され続けている(間なし)状態を作る。"""
    now = time.perf_counter()
    seg._pending, seg._current, seg._last_change, seg._utt_t0 = True, text, now, now - age_s
    seg._finalize_sent = False


def _watch_once(seg: Segmenter) -> None:
    async def run():
        t = asyncio.create_task(seg._watch(asyncio.Queue()))
        await asyncio.sleep(0.05)
        t.cancel()
    asyncio.run(run())


def _phases(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_long_utterance_finalizes_at_sentence_end_once(tmp_path):
    h = FakeHelper()
    seg = Segmenter(h, "system", SegmenterConfig(**TAP), Metrics(tmp_path / "m.jsonl"))
    _speaking(seg, "判断に特化したAIです。で、これが", age_s=9)
    _watch_once(seg)
    assert h.finalized == 1 and seg._long_cut
    # final が来る前に次の partial が来ても(_finalize_sent が戻る)再送しない
    _speaking(seg, "判断に特化したAIです。で、これが面白い", age_s=9.5)
    _watch_once(seg)
    assert h.finalized == 1
    ev = [r for r in _phases(tmp_path / "m.jsonl") if r["phase"] == "long_finalize"]
    assert len(ev) == 1 and ev[0]["reason"] == "sentence" and ev[0]["age_ms"] >= 9000


def test_long_utterance_needs_sentence_end_until_hard_limit(tmp_path):
    h = FakeHelper()
    seg = Segmenter(h, "system", SegmenterConfig(**TAP), Metrics(tmp_path / "m.jsonl"))
    _speaking(seg, "判断に特化したAIを作っている新しいスタートアップで", age_s=9)
    _watch_once(seg)
    assert h.finalized == 0
    _speaking(seg, "判断に特化したAIを作っている新しいスタートアップで創業者の一人は", age_s=21)
    _watch_once(seg)
    assert h.finalized == 1
    assert [r["reason"] for r in _phases(tmp_path / "m.jsonl") if r["phase"] == "long_finalize"] == ["hard"]


def test_long_utterance_disabled_keeps_old_behavior(tmp_path):
    h = FakeHelper()
    seg = Segmenter(h, "system", SegmenterConfig(**TAP, long_utterance_ms=0, long_utterance_hard_ms=0),
                    Metrics(tmp_path / "m.jsonl"))
    _speaking(seg, "判断に特化したAIです。で、これが", age_s=60)
    _watch_once(seg)
    assert h.finalized == 0


def test_long_cut_carries_unfinished_tail_to_next_utterance(tmp_path):
    seg = Segmenter(FakeHelper(), "system", SegmenterConfig(**TAP), Metrics(tmp_path / "m.jsonl"))
    out = asyncio.Queue()

    async def run():
        # 語の途中で切れた言いかけ(mid_word)は、長さで切ったときだけ繰り越す(助詞終わりは既存の規則で繰り越される)
        seg._long_cut = True
        await seg._emit("判断に特化したAIです。で、これが面白いとこ", {}, out, forced=True)
        assert out.get_nowait().text == "判断に特化したAIです。"
        assert out.empty() and seg._carry == "で、これが面白いとこ" and not seg._long_cut
        await seg._emit("ろは文字のデータなら読めるんです。", {}, out, forced=False)
        assert out.get_nowait().text == "で、これが面白いところは文字のデータなら読めるんです。"
        # 間で切ったとき(従来の経路)は言いかけもそのまま発話として出す
        await seg._emit("判断に特化したAIです。で、これが面白いとこ", {}, out, forced=True)
        assert out.get_nowait().text == "判断に特化したAIです。"
        assert out.get_nowait().text == "で、これが面白いとこ" and seg._carry == ""
        # 文末のない 1 片だけ(hard で切った)は繰り越さずに出す
        seg._long_cut = True
        await seg._emit("創業者の一人はオープンAIの出身で指示通りに答える研究に関わっていた人なんですけれど", {}, out, forced=True)
        assert out.get_nowait().text.startswith("創業者の一人は") and seg._carry == ""
    asyncio.run(run())
    ev = _phases(tmp_path / "m.jsonl")
    assert [r.get("reason") for r in ev if r["phase"] == "carry_tail"] == ["long_cut"]
    assert all(isinstance(r["age_ms"], int) for r in ev if r["phase"] == "segment")


def test_split_final_after_long_cut_is_carried(tmp_path):
    """finalize 1 回に final が 2 つ返る(2026-09-26 実測 'ほぼ業界' | '初の AI…')。2 つ目の言いかけは繰り越す。"""
    seg = Segmenter(FakeHelper(), "system", SegmenterConfig(**TAP), Metrics(tmp_path / "m.jsonl"))
    out = asyncio.Queue()

    async def run():
        seg._long_cut = True
        await seg._emit("このジェルという AIはだいぶ特殊な AIで判断に特化している", {}, out, forced=True)
        assert out.get_nowait().text.startswith("このジェル")        # hard の 1 片はそのまま出す
        await seg._emit("ほぼ業界", {}, out, forced=True)              # 直後の続きの final
        assert out.empty() and seg._carry == "ほぼ業界"
        await seg._emit("初の AIということになります。", {}, out, forced=False)
        assert out.get_nowait().text == "ほぼ業界初の AIということになります。"
    asyncio.run(run())
    assert [r.get("reason") for r in _phases(tmp_path / "m.jsonl") if r["phase"] == "carry_tail"] == ["long_cut_cont"]


def test_self_voice_uses_long_pauses_and_room_stays_short(tmp_path):
    """自分の声(mic)は相手側と同じ長めの区切り(2026-09-27 読み上げ: CER 平均 28.6% → 26.4%)。対面の相手(room)は短いまま。"""
    from meetcue.config import Config, load_config
    from meetcue.pipeline import Pipeline, Source
    from meetcue.session import Session

    cfg = Config(app_dir=tmp_path)
    p = Pipeline(cfg, Session(tmp_path / "s", mode="participant", privacy="local", sources=[], model="m"),
                 object(), [], api_key=None, llm_enabled=False)
    assert p._segmenter_config(Source.parse("mic")) is cfg.segmenter_self
    assert p._segmenter_config(Source.parse("file:x.m4a:mic")) is cfg.segmenter_self
    assert p._segmenter_config(Source.parse("room")) is cfg.segmenter
    assert p._segmenter_config(Source.parse("tap-all")) is cfg.segmenter_tap
    assert (cfg.segmenter_self.pause_ms, cfg.segmenter_self.pause_sentence_ms) == (1200, 1100)
    assert cfg.segmenter.pause_ms == 500
    toml = tmp_path / "config.toml"
    toml.write_text("[segmenter_self]\npause_ms = 900\n", encoding="utf-8")
    assert load_config(toml).segmenter_self.pause_ms == 900


def test_carried_tail_keeps_its_own_time_not_the_partial_range(tmp_path):
    """2026-09-27 テスト3: 長さで切った final の切れ端(質問)を単独で出すとき、最後の partial の範囲(前の確定の終わり =
    60 s 前から始まる)を使っていた。切り出した final の範囲と位置から見積もる。頭に付けたときも切れ端の始まりから。"""
    seg = Segmenter(FakeHelper(), "system", SegmenterConfig(**TAP), Metrics(tmp_path / "m.jsonl"))
    out = asyncio.Queue()
    seg._last_ev = {"start_s": 37.93, "end_s": 105.55}              # partial の範囲(長い無音を含む)
    final = {"start_s": 97.09, "end_s": 105.73}
    text = "ありがとうございます。〇〇さんは〇〇の勉強をなさっているんですね。その分野へ進もうと思ったきっかけは"

    async def run():
        seg._long_cut = True
        await seg._emit(text, final, out, forced=True)
        first = [out.get_nowait(), out.get_nowait()]
        assert [u.start_s for u in first] == [97.09, 97.09] and seg._carry == "その分野へ進もうと思ったきっかけは"
        seg._carry_t = time.perf_counter() - 5                       # 続きが来ないまま → 単独で出す
        t = asyncio.create_task(seg._watch(out))
        await asyncio.sleep(0.05)
        t.cancel()
        u = out.get_nowait()
        assert u.text == "その分野へ進もうと思ったきっかけは" and 102.0 < u.start_s < 103.5 and u.end_s == 105.73
        # 続きが来た場合: 頭に付けた発話の始まりは切れ端の始まり
        seg._long_cut = True
        await seg._emit(text, final, out, forced=True)
        out.get_nowait(), out.get_nowait()
        await seg._emit("何ですか。", {"start_s": 106.1, "end_s": 107.4}, out, forced=False)
        u2 = out.get_nowait()
        assert u2.text == "その分野へ進もうと思ったきっかけは何ですか。" and 102.0 < u2.start_s < 103.5 and u2.end_s == 107.4
    asyncio.run(run())

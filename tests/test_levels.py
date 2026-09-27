"""音量(helper の stderr・phase=level)の振り分け: 画面へ流し、記録は 5 s 集計、相手側の無音 30 s で警告。"""
import json
from types import SimpleNamespace

import meetcue.pipeline as pl
from meetcue.config import Config
from meetcue.metrics import Metrics


class FakeUI:
    def __init__(self):
        self.levels, self.errors, self.statuses, self.codes = [], [], [], []

    def level(self, ch, db, peak):
        self.levels.append((ch, db))

    def error(self, msg, code=None):
        self.errors.append(msg)
        self.codes.append(code)

    def status(self, msg, code=None):
        self.statuses.append(msg)
        self.codes.append(code)


def _pipeline(tmp_path, ui):
    cfg = Config(app_dir=tmp_path)   # 索引・台帳を実環境から切り離す
    session = SimpleNamespace(metrics=Metrics(tmp_path / "m.jsonl"))
    return pl.Pipeline(cfg, session, ui, [], api_key=None)


def _feed(p, clock, channel, db, seconds):
    for _ in range(int(seconds * 10)):
        clock[0] += 0.1
        p._diag(channel, {"phase": "level", "db": db, "peak_db": db})


def test_silence_on_system_warns_once_and_recovers(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pl.time, "monotonic", lambda: clock[0])
    ui = FakeUI()
    p = _pipeline(tmp_path, ui)
    _feed(p, clock, "system", -90, 40)
    assert len(ui.errors) == 1 and "30 秒" in ui.errors[0]
    _feed(p, clock, "system", -30, 6)
    assert any("戻りました" in s for s in ui.statuses)
    assert ui.codes == ["silence", "silence_end"]   # 画面がナギの台詞に差し替える目印
    _feed(p, clock, "mic", -90, 40)            # 自分側の無音は警告しない
    assert len(ui.errors) == 1
    rows = [json.loads(line) for line in (tmp_path / "m.jsonl").read_text(encoding="utf-8").splitlines()]
    phases = [r["phase"] for r in rows]
    assert phases.count("silence_warning") == 1 and phases.count("silence_end") == 1
    assert "level" not in phases                # 0.1 s ごとの生の値は記録しない
    l5 = [r for r in rows if r["phase"] == "level_5s"]
    assert l5 and all(45 <= r["n"] <= 52 for r in l5)   # 約 5 s(0.1 s × 50)ごと
    assert len(ui.levels) == 860                # 画面へは全部流す


def test_level_average_is_energy_mean(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(pl.time, "monotonic", lambda: clock[0])
    p = _pipeline(tmp_path, FakeUI())
    _feed(p, clock, "system", -20, 2.5)
    _feed(p, clock, "system", -90, 2.7)
    row = next(json.loads(line) for line in (tmp_path / "m.jsonl").read_text(encoding="utf-8").splitlines()
               if '"level_5s"' in line)
    assert -24 <= row["db_avg"] <= -22 and row["db_max"] == -20   # 半分が -20 dB なら平均は約 -23 dB

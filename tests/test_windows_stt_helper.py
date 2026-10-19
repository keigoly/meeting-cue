"""Windows の STT ヘルパー(helpers/windows/stt_helper)と、本体からの起動の仕方(REQUIREMENTS FR-12・契約は FR-2)。

- 起動の仕方(Source.argv の Windows の分岐)と、取り込み未実装の abort は標準ライブラリだけで試す(どの OS でも走る)。
- 実際の認識は、専用の仮想環境・モデル・合成音声(tests/fixtures/wav・開発用 repo だけ)がそろう Windows でだけ走る。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from meetcue.config import Config
from meetcue.pipeline import Source, helper_hint

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "helpers" / "windows" / "stt_helper" / "stt_helper.py"
WAV = REPO / "tests" / "fixtures" / "wav" / "statement_ja.wav"
LONG_WAV = REPO / "tests" / "fixtures" / "wav" / "long_ja.wav"


def test_argv_windows_runs_helper_with_its_own_python(tmp_path):
    cfg = Config(helpers_dir=REPO / "helpers" / "windows", stt_python=tmp_path / "venv" / "Scripts" / "python.exe")
    argv = Source.parse("file:x.wav:system").argv(cfg, platform="win32")
    assert argv[:2] == [str(cfg.stt_python), str(HELPER)]
    assert argv[2:] == ["--locale", "ja-JP", "--channel", "system", "--file", "x.wav", "--pace", str(cfg.file_pace)]
    assert Source.parse("mic").argv(cfg, platform="win32")[2:] == ["--locale", "ja-JP", "--channel", "mic"]
    assert "--loopback" in Source.parse("tap-all").argv(cfg, platform="win32")
    cfg.loopback_device, cfg.mic_device = "Headset", "USB Mic"   # 取り込み機器を名前の一部で選ぶ(config.toml)
    assert Source.parse("tap-all").argv(cfg, platform="win32")[-2:] == ["--loopback-device", "Headset"]
    assert Source.parse("mic").argv(cfg, platform="win32")[-2:] == ["--mic-device", "USB Mic"]


def test_argv_mac_unchanged():
    cfg = Config(helpers_dir=REPO / "helpers" / "macos")
    assert Source.parse("mic").argv(cfg, platform="darwin") == [
        str(REPO / "helpers" / "macos" / "stt_helper" / "stt-helper"), "--locale", "ja-JP", "--channel", "mic"]
    assert "setup.ps1" in helper_hint("win32") and "make" in helper_hint("darwin")


@pytest.mark.skipif(importlib.util.find_spec("faster_whisper") is not None, reason="この python には専用環境のパッケージが入っている")
def test_packages_missing_aborts_with_a_reason():
    """本体の python(専用環境の外)で動かされたら、理由(packages_missing・終了コード 7)を返して終わる。"""
    p = subprocess.run([sys.executable, str(HELPER), "--channel", "mic"], capture_output=True, timeout=30)
    assert p.returncode == 7
    d = json.loads(p.stderr.decode("utf-8").strip().splitlines()[-1])
    assert d["phase"] == "abort" and d["reason"] == "packages_missing" and "setup.ps1" in d["hint"]
    assert p.stdout == b""   # stdout は契約のイベントだけ(abort は stderr)


def test_pcm_out_emits_s16le_chunks_of_100ms(monkeypatch):
    """--pcm-out(FR-2b・2026-10-18): 認識に渡すのと同じ音を s16le にして 0.1 s(1,600 標本)ずつ出し、止めるときに残りを出す。"""
    np = pytest.importorskip("numpy")   # 専用環境の numpy(本体の試験の python には無いことが多い)
    import base64
    spec = importlib.util.spec_from_file_location("win_stt_helper", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    got = []
    monkeypatch.setattr(mod, "emit", got.append)
    out = mod.PcmOut()
    out.add(np.full(1000, 0.5, dtype=np.float32))
    assert got == []                                   # 0.1 s に満たない間は出さない
    out.add(np.concatenate([np.full(1500, -1.2, dtype=np.float32), np.zeros(1000, dtype=np.float32)]))
    assert [g["seq"] for g in got] == [1, 2] and all(g["type"] == "audio" for g in got)
    first = np.frombuffer(base64.b64decode(got[0]["pcm"]), dtype="<i2")
    assert len(first) == 1600 and first[0] == 16383 and first[1000] == -32767   # 0.5 と、はみ出した -1.2 は -1.0 に
    out.flush()
    assert len(np.frombuffer(base64.b64decode(got[-1]["pcm"]), dtype="<i2")) == 3500 - 3200 and got[-1]["seq"] == 3
    out.flush()
    assert len(got) == 3                                # 残りが無ければ何も出さない


def _real_helper_ready() -> bool:
    cfg = Config()
    model = cfg.app_dir / "models" / "faster-whisper-large-v3-turbo" / "model.bin"
    return sys.platform == "win32" and cfg.stt_python.exists() and model.exists() and WAV.exists()


@pytest.mark.skipif(not _real_helper_ready(), reason="専用の仮想環境・モデル・合成音声がそろう Windows でだけ")
def test_real_helper_speaks_the_contract():
    cfg = Config()
    p = subprocess.run([str(cfg.stt_python), str(HELPER), "--channel", "system", "--file", str(WAV), "--pace", "0"],
                       capture_output=True, timeout=180)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")[-2000:]
    evs = [json.loads(l) for l in p.stdout.decode("utf-8").splitlines() if l.strip()]
    types = [e["type"] for e in evs]
    assert types[0] == "ready" and types[-1] == "bye"
    assert set(types[1:-1]) <= {"partial", "final"} and "final" in types
    for e in evs[1:-1]:
        assert e["channel"] == "system" and isinstance(e["text"], str) and e["start_s"] <= e["end_s"] <= e["fed_s"] + 0.2
    text = unicodedata.normalize("NFKC", "".join(e["text"] for e in evs if e["type"] == "final"))
    for word in ("議題", "来期の体制", "セキュリティ監視の改善", "採用計画"):
        assert word in text
    diags = [json.loads(l) for l in p.stderr.decode("utf-8").splitlines() if l.startswith("{")]
    assert any(d.get("phase") == "level" for d in diags)
    assert any(d.get("phase") == "asset_ready" for d in diags)


@pytest.mark.skipif(not _real_helper_ready(), reason="専用の仮想環境・モデル・合成音声がそろう Windows でだけ")
def test_real_helper_closes_the_recording_before_the_remaining_recognition(tmp_path, monkeypatch):
    """止めたときは録音を認識より先に閉じる(2026-10-08: 認識が溜まったまま止めて強制終了され、m4a に目次が書かれず
    録音が丸ごと聞けなくなった)。60 s を一度に渡して認識の途中で止め、待ちを 0.5 s に縮めても m4a が閉じていること。"""
    import asyncio
    from meetcue.stt_reader import STTHelper
    monkeypatch.setattr(STTHelper, "QUIT_WAIT_S", 0.5)
    cfg = Config()
    out = tmp_path / "rec.m4a"
    diags: list[dict] = []

    async def main() -> None:
        h = STTHelper([str(cfg.stt_python), str(HELPER), "--channel", "system", "--file", str(LONG_WAV), "--pace", "0",
                       "--record", str(out)], channel="system", on_diag=diags.append)
        await h.start()
        async for ev in h.events():
            if ev.get("type") == "ready":
                break
        await asyncio.sleep(1.0)   # 60 s が一度に届き、残りの区間を認識している最中に止める
        await h.stop()

    asyncio.run(main())
    assert any(d.get("phase") == "record_done" for d in diags)
    assert b"moov" in out.read_bytes()

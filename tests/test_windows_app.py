"""Windows のアプリ(REQUIREMENTS FR-12・2026-09-27): API キーの保管(資格情報マネージャー)と、書き出しの音声を重ねる mix_helper。

- 資格情報マネージャーは Windows でだけ走る。試験用の名前(local.meetcue.pytest)で保存し、終わったら必ず消す。
- mix_helper は STT ヘルパー専用の仮想環境(PyAV)と合成音声(tests/fixtures/wav・開発用 repo だけ)がそろう Windows でだけ走る。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from meetcue import secrets
from meetcue.config import Config

REPO = Path(__file__).resolve().parents[1]
WAV = REPO / "tests" / "fixtures" / "wav" / "statement_ja.wav"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows だけ")


@windows_only
def test_credential_manager_roundtrip(monkeypatch):
    monkeypatch.setattr(secrets, "KEYCHAIN_SERVICE", "local.meetcue.pytest")
    key = "sk-test-" + "w" * 40
    try:
        assert secrets.keychain_supported()
        secrets.keychain_set("openai", key)
        assert secrets.keychain_get("openai") == key
        assert secrets.api_key("openai") == (key, "keychain")
        with pytest.raises(ValueError):
            secrets.keychain_set("openai", "has space " + "x" * 20)   # キーの形でないものは保存しない
    finally:
        secrets.keychain_delete("openai")
    assert secrets.keychain_get("openai") is None
    assert secrets.keychain_delete("openai") is False


def _mix_ready() -> bool:
    return sys.platform == "win32" and Config().stt_python.exists() and WAV.exists()


@pytest.mark.skipif(not _mix_ready(), reason="STT ヘルパー専用の仮想環境と合成音声がそろう Windows でだけ")
def test_mix_helper_overlays_two_tracks_with_offset(tmp_path):
    out = tmp_path / "音声.m4a"
    helper = REPO / "helpers" / "windows" / "mix_helper" / "mix_helper.py"
    p = subprocess.run([str(Config().stt_python), str(helper), "--out", str(out), "--in", f"{WAV}@0", "--in", f"{WAV}@0.5"],
                       capture_output=True, timeout=120)
    res = json.loads(p.stdout.decode("utf-8").strip().splitlines()[-1])
    assert p.returncode == 0 and res["ok"] and res["inputs"] == 2, p.stderr.decode("utf-8", "replace")[-1000:]
    assert abs(res["duration_ms"] - (10386 + 500)) < 50 and out.stat().st_size > 1000   # 長さ = 長い方 + ずれ

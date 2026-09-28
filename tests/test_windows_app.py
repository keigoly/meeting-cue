"""Windows のアプリ(REQUIREMENTS FR-12・2026-09-27): API キーの保管(資格情報マネージャー)と、書き出しの音声を重ねる mix_helper、
スタートメニューのショートカットの AppUserModelID(2026-09-28)。

- 資格情報マネージャーは Windows でだけ走る。試験用の名前(local.meetcue.pytest)で保存し、終わったら必ず消す。
- ショートカットは Windows でだけ走る。試験用の .lnk を一時フォルダに作る(スタートメニューには触れない)。
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


def _powershell(script: str) -> str:
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    return p.stdout.decode("utf-8", "replace").strip()


@windows_only
def test_stamp_shortcut_sets_window_aumid_and_keeps_target(tmp_path):
    """スタートメニューのショートカットにウィンドウと同じ ID を書く(無いとピン留めが python.exe を指す・2026-09-28)。"""
    helper = REPO / "helpers" / "windows" / "window_helper" / "window_helper.py"
    lnk = tmp_path / "MeetingCue-test.lnk"
    _powershell(f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
                f"$s.TargetPath = '{sys.executable}'; $s.Arguments = '\"launch.pyw\"'; $s.Save()")
    p = subprocess.run([sys.executable, str(helper), "--stamp-shortcut", str(lnk)], capture_output=True, timeout=60)
    d = json.loads(p.stderr.decode("utf-8").strip().splitlines()[-1])
    assert p.returncode == 0 and d["phase"] == "stamped" and d["aumid"] == "keigoly.MeetingCue", d
    got = _powershell(f"$f = (New-Object -ComObject Shell.Application).Namespace('{tmp_path}').ParseName('{lnk.name}'); "
                      f"$f.ExtendedProperty('System.AppUserModel.ID'); "
                      f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); $s.TargetPath; $s.Arguments")
    assert got.splitlines() == ["keigoly.MeetingCue", sys.executable, '"launch.pyw"']   # ID は Shell からも読め、実行先と引数は変わらない
    p = subprocess.run([sys.executable, str(helper), "--stamp-shortcut", str(tmp_path / "missing.lnk")], capture_output=True, timeout=60)
    assert p.returncode == 1 and json.loads(p.stderr.decode("utf-8").strip().splitlines()[-1])["where"] == "stamp_shortcut"


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

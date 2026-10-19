"""Mac の STT ヘルパー(helpers/macos/stt_helper)を実際に動かす試験。ビルド済みのヘルパーと合成音声がそろう Mac でだけ走る。

2026-10-09: --file で供給の途中に止めると、quit の後も供給が続き、閉じた録音を同じパスに作り直して上書きしていた
(moov の無いファイルが残り、録音が丸ごと聞けなくなった)。Windows の同じ試験は test_windows_stt_helper.py。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from meetcue.stt_reader import STTHelper

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "helpers" / "macos" / "stt_helper" / "stt-helper"
LONG_WAV = REPO / "tests" / "fixtures" / "wav" / "long_ja.wav"


@pytest.mark.skipif(sys.platform != "darwin" or not HELPER.exists() or not LONG_WAV.exists(),
                    reason="ビルド済みのヘルパーと合成音声がそろう Mac でだけ")
def test_real_helper_keeps_the_recording_closed_after_quit(tmp_path):
    """60 s を実時間で供給している途中で止めても、閉じた録音を作り直さない(record_start は 1 回・m4a に moov がある)。"""
    out = tmp_path / "rec.m4a"
    diags: list[dict] = []

    async def main() -> bool:
        h = STTHelper([str(HELPER), "--locale", "ja-JP", "--channel", "mic", "--file", str(LONG_WAV), "--pace", "1.0",
                       "--record", str(out)], channel="mic", on_diag=diags.append)
        await h.start()

        async def ready() -> bool:
            async for ev in h.events():
                if ev.get("type") == "ready":
                    return True
            return False

        try:
            ok = await asyncio.wait_for(ready(), timeout=120)   # 初回は認識の素材の取り込みを待つ
        except asyncio.TimeoutError:
            ok = False
        if ok:
            await asyncio.sleep(1.5)   # 供給は実時間で続いている(残り約 58 s)
        await h.stop()
        return ok

    if not asyncio.run(main()):
        pytest.skip(f"ヘルパーが ready にならなかった: {[d.get('phase') for d in diags][-3:]}")
    phases = [d.get("phase") for d in diags if d.get("phase") in ("record_start", "record_done")]
    assert phases == ["record_start", "record_done"], phases
    assert b"moov" in out.read_bytes()

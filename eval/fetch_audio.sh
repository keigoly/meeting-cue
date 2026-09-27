#!/bin/bash
# 評価用の音声を YouTube から取り直す(私的な試験用。repo には入れない)。置き場は ~/.meeting-cue/eval/audio/。
#
#   eval/fetch_audio.sh              # 既定の 2 本(iroots 模擬面接・Jev 解説の冒頭 5 分)
#   eval/fetch_audio.sh <video_id>   # 任意の 1 本(<id>.m4a)
#
# yt-dlp は uvx で一時的に使う(依存に入れない)。切り出しは ffmpeg(無ければ全体のまま)。
set -euo pipefail
OUT="${MEETCUE_HOME:-$HOME/.meeting-cue}/eval/audio"
mkdir -p "$OUT"
get() { uvx --quiet yt-dlp --no-warnings -f bestaudio -x --audio-format m4a -o "$OUT/%(id)s.%(ext)s" "https://www.youtube.com/watch?v=$1"; }

if [[ $# -ge 1 ]]; then
  get "$1"
else
  get qp8xasjyQig   # 【模擬面接】一人で面接の練習ができます(iroots)14:08・視聴者宛ての質問 17 問
  get KNNroW1xS04   # 判断特化型AI・Jev:知っておいたほうがいいこと全部共有 18:16・一人語り(区切りと誤起動の確認)
  if command -v ffmpeg >/dev/null; then
    ffmpeg -loglevel error -y -i "$OUT/KNNroW1xS04.m4a" -t 300 -c copy "$OUT/jev_5min.m4a"
  fi
fi
ls -la "$OUT"

#!/bin/bash
# 合成音声のテスト素材を作り直す(macOS の say・Kyoko)。[[slnc ms]] は無音。
# 2026-09-27: Windows 版の計測用に、16 kHz・モノラルの WAV と正解の文面(.txt)を wav/ に作る(long_ja = 1 分ほど続けて話す業務連絡)。
# macOS の読み上げの音声は Apple の使用許諾で個人の非商用に限られるため、wav/ は開発用リポジトリだけに置き、公開版には入れない。
set -euo pipefail
cd "$(dirname "$0")"

MEETING="本日はご説明ありがとうございます。全体像はよく分かりました。 [[slnc 1400]] まず確認ですが、この構成でグローバルプロテクトの分割トンネルはどう扱いますか。 [[slnc 2500]] なるほど、承知しました。 [[slnc 1200]] 次に、導入にかかる費用はどのくらいでしょうか。 [[slnc 2500]] ありがとうございます。 [[slnc 1200]] 最後に、既存のシームと競合するリスクはありませんか。 [[slnc 2500]] 以上です。"
STATEMENT="今日の議題は三つあります。一つ目は来期の体制について、二つ目はセキュリティ監視の改善、三つ目は採用計画です。"
LONG="それでは今週の進捗を共有します。まず、社内向けの問い合わせ窓口の件です。先週から試験運用を始めていて、問い合わせの多くは、パスワードの再設定と、共有フォルダの権限に関するものでした。次に、来月の移行作業についてです。対象の端末を三つの組に分けて、週末ごとに作業します。各回のあとには、動作確認の担当者を置く予定です。 [[slnc 1500]] 最後に、課題が二つあります。一つ目は、古い端末の一部で更新が失敗することです。原因は調査中ですが、空き容量の不足が疑われています。二つ目は、手順書の更新が遅れていることです。今週中に下書きを出しますので、確認をお願いします。 [[slnc 1500]] 私からは以上です。何か質問はありますか。"

say -v Kyoko -o meeting_ja.aiff "$MEETING"
say -v Kyoko -o statement_ja.aiff "$STATEMENT"
say -v Kyoko -o long_ja.aiff "$LONG"

# Windows 版の計測用: 16 kHz・モノラル・16 bit の WAV と、無音の印を除いた正解の文面(1 文 1 行)
mkdir -p wav
for name in meeting_ja statement_ja long_ja; do
  afconvert -f WAVE -d LEI16@16000 -c 1 "$name.aiff" "wav/$name.wav"
done
for pair in "meeting_ja:$MEETING" "statement_ja:$STATEMENT" "long_ja:$LONG"; do
  name="${pair%%:*}"; text="${pair#*:}"
  printf '%s\n' "$text" | perl -CSD -Mutf8 -pe 's/\s*\[\[slnc \d+\]\]\s*/\n/g; s/。/。\n/g' | perl -CSD -ne 'print if /\S/' > "wav/$name.txt"
done
ls -la *.aiff wav/

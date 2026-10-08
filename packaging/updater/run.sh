#!/bin/bash
# 更新係の入口(launchd の check と packaging/update_app.sh の apply から)。uv の python 3.12 で updater.py を動かす
# (依存は stdlib のみ)。python を毎回ここで探すので、uv が python を入れ直しても launchd の設定は変えなくてよい。
#
#   packaging/updater/run.sh check | apply [...] | install [--interval 300] | uninstall | status
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TREE="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$(uv python find 3.12 2>/dev/null)" || { echo "python 3.12 が見つかりません(uv python install 3.12)" >&2; exit 1; }
exec "$PY" "$TREE/packaging/updater/updater.py" --tree "$TREE" "$@"

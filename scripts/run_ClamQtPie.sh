#!/bin/sh
# ClamQtPie を開発環境から起動する。
# Python は CLAMQTPIE_PYTHON（例: venv の bin/python）、clamav_frontend/.venv、PATH の python3 の順に探す。
# 引数はそのまま app.py に渡す。
cd "$(dirname "$0")/../clamav_frontend" || exit 1

if [ -n "$CLAMQTPIE_PYTHON" ]; then
    PYTHON=$CLAMQTPIE_PYTHON
elif [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
else
    echo ".venv が見つからないため、PATH の python3 で起動します。" >&2
    PYTHON=python3
fi

exec "$PYTHON" app.py "$@"

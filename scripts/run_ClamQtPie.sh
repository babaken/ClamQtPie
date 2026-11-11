#!/bin/sh
# ClamQtPie のトレイGUI を開発環境から起動する（root は要らない）。
# 先にサービスを動かしておく（sudo scripts/install_service.sh、または service_main.py --console）。
# Python は CLAMQTPIE_PYTHON（例: venv の bin/python）、clamav_frontend/.venv、PATH の python3 の順に探す。
# 引数はそのまま tray_main.py に渡す。
cd "$(dirname "$0")/../clamav_frontend" || exit 1

if [ -n "$CLAMQTPIE_PYTHON" ]; then
    PYTHON=$CLAMQTPIE_PYTHON
elif [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
else
    echo ".venv が見つからないため、PATH の python3 で起動します。" >&2
    PYTHON=python3
fi

exec "$PYTHON" tray_main.py "$@"

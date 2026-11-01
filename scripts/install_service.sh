#!/bin/sh
# ClamQtPie サービスを、開発中のソースから systemd に登録して開始する（開発中の確認用。配布物には入れない）。
# 正式な導入方法（install.sh）は段階 12-4 で作る。
#
# 注意: サービスは root で動き、このフォルダのソースをそのまま実行する。一般の利用者が書き換えられる場所の
# ソースを root で動かすことになるため、確認が終わったら uninstall_service.sh で必ず削除すること。
#
# Python は CLAMQTPIE_PYTHON、clamav_frontend/.venv、PATH の python3 の順に探す（run_ClamQtPie.sh と同じ）。
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo CLAMQTPIE_PYTHON="${CLAMQTPIE_PYTHON:-}" sh "$0" "$@"
fi

APP_DIR=$(cd "$(dirname "$0")/../clamav_frontend" && pwd)
if [ -n "${CLAMQTPIE_PYTHON:-}" ]; then
    PYTHON=$CLAMQTPIE_PYTHON
elif [ -x "$APP_DIR/.venv/bin/python" ]; then
    PYTHON="$APP_DIR/.venv/bin/python"
else
    PYTHON=$(command -v python3)
fi
UNIT=/etc/systemd/system/clamqtpie.service

echo "登録します: $UNIT（$PYTHON $APP_DIR/service_main.py --systemd）"
cat > "$UNIT" <<EOF
# 開発中の確認用（scripts/install_service.sh が作成）
[Unit]
Description=ClamQtPie service (development)
After=network.target clamav-daemon.service
Wants=clamav-daemon.service
StartLimitIntervalSec=1d
StartLimitBurst=3

[Service]
Type=notify
NotifyAccess=main
ExecStart=$PYTHON $APP_DIR/service_main.py --systemd
WorkingDirectory=$APP_DIR
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable --now clamqtpie.service
systemctl --no-pager status clamqtpie.service | head -5

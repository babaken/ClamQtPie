#!/bin/sh
# install_service.sh で登録した ClamQtPie サービスを止めて、登録を削除する（開発中の確認用）。
# --purge を付けると、サービスの設定・データ・ログ（/etc/clamqtpie、/var/lib/clamqtpie、/var/log/clamqtpie）も消す
# （隔離したファイルも消える）。
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo sh "$0" "$@"
fi

UNIT=/etc/systemd/system/clamqtpie.service
systemctl disable --now clamqtpie.service 2>/dev/null || true
rm -f "$UNIT"
# スケジュールのためにスリープから起こすタイマー（サービスが止まるときにも消すが、残っていれば消す）
systemctl stop clamqtpie-wake.timer 2>/dev/null || true
rm -f /etc/systemd/system/clamqtpie-wake.timer /etc/systemd/system/clamqtpie-wake.service
systemctl daemon-reload
echo "登録を削除しました: $UNIT"

if [ "${1:-}" = "--purge" ]; then
    rm -rf /etc/clamqtpie /var/lib/clamqtpie /var/log/clamqtpie
    echo "設定・データ・ログを削除しました"
fi

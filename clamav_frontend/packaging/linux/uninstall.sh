#!/bin/sh
# ClamQtPie を削除する（Linux。段階 12-4d）
#
#   sudo sh /opt/clamqtpie/uninstall.sh            設定・データ・ログを消すかを尋ねる（既定は残す）
#   sudo sh /opt/clamqtpie/uninstall.sh --purge    尋ねずに、設定・データ・ログ（隔離ファイルを含む）とグループも消す
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo sh "$0" "$@"
fi

DEST=/opt/clamqtpie
GROUP=clamqtpie
PURGE=no
[ "${1:-}" = "--purge" ] && PURGE=yes

systemctl disable --now clamqtpie.service 2>/dev/null || true
# スケジュールのためにスリープから起こすタイマー（サービスが止まるときにも消すが、残っていれば消す）
systemctl stop clamqtpie-wake.timer 2>/dev/null || true
rm -f /etc/systemd/system/clamqtpie.service /etc/systemd/system/clamqtpie-wake.timer \
    /etc/systemd/system/clamqtpie-wake.service
systemctl daemon-reload 2>/dev/null || true
rm -f /usr/share/polkit-1/actions/io.github.babaken.clamqtpie.policy /usr/share/applications/clamqtpie.desktop
if [ -L /usr/local/bin/clamqtpie-ctl ]; then
    rm -f /usr/local/bin/clamqtpie-ctl
fi
if [ -d "$DEST" ] && [ ! -L "$DEST" ]; then
    rm -rf "$DEST"
fi
echo "ClamQtPie を削除しました"

if [ "$PURGE" = "no" ] && [ -t 0 ]; then
    printf "設定・データ・ログ（/etc/clamqtpie・/var/lib/clamqtpie・/var/log/clamqtpie。隔離したファイルを含む）も削除しますか？ [y/N] "
    read -r answer
    case "$answer" in
        y|Y|yes|YES) PURGE=yes ;;
    esac
fi
if [ "$PURGE" = "yes" ]; then
    rm -rf /etc/clamqtpie /var/lib/clamqtpie /var/log/clamqtpie
    if getent group "$GROUP" >/dev/null 2>&1; then
        groupdel "$GROUP" || true
    fi
    echo "設定・データ・ログとグループ $GROUP を削除しました"
else
    echo "設定・データ・ログは残しました（/etc/clamqtpie・/var/lib/clamqtpie・/var/log/clamqtpie）"
fi

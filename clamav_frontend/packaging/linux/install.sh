#!/bin/sh
# ClamQtPie を導入する（Linux。サービス化・Web API設計書 11章。段階 12-4d）
#
#   sudo sh install.sh
#
# - /opt/clamqtpie に置き、root の持ち物・一般の利用者が書き込めない権限にする（サービスは root で動くため）
# - clamqtpie グループを作り、sudo した利用者を入れる（トレイGUI を使えるのは、このグループの利用者だけ）
# - systemd の unit を置いて、サービスを開始する。clamqtpie-ctl を /usr/local/bin に置く
# - 昇格ヘルパーの polkit のポリシー（毎回、管理者の認証を求める）と、アプリのメニューの項目を置く
#
# 更新も同じコマンドで行う（サービスを止めて差し替え、また開始する。設定・データはそのまま）。
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo sh "$0" "$@"
fi

SRC=$(cd "$(dirname "$0")" && pwd)
DEST=/opt/clamqtpie
GROUP=clamqtpie
UNIT=/etc/systemd/system/clamqtpie.service
POLICY=/usr/share/polkit-1/actions/io.github.babaken.clamqtpie.policy
DESKTOP=/usr/share/applications/clamqtpie.desktop

fail() {
    echo "エラー: $*" >&2
    exit 1
}

[ -x "$SRC/app/clamqtpie-service" ] || fail "$SRC/app/clamqtpie-service がありません（配布物を展開したフォルダで実行してください）"
command -v systemctl >/dev/null 2>&1 || fail "systemd が見つかりません"
# リンクを置かれていたら、その先（ほかの場所）を root の持ち物に変えたり、そこへ書き込んだりしない
for path in "$DEST" "$DEST.new" "$DEST.old" /opt; do
    [ ! -L "$path" ] || fail "$path がリンクになっています"
done

echo "== ClamQtPie を $DEST に導入します =="
if systemctl is-active --quiet clamqtpie.service; then
    echo "サービスを止めます"
    systemctl stop clamqtpie.service
fi

# 写してから入れ替える（途中で失敗しても、動いていたものを壊さない）
rm -rf "$DEST.new" "$DEST.old"
mkdir -p "$DEST.new"
cp -R "$SRC/app/." "$DEST.new/"
mkdir -p "$DEST.new/share"
cp "$SRC/share/clamqtpie.service" "$SRC/share/clamqtpie.desktop" "$SRC/share/io.github.babaken.clamqtpie.policy" \
    "$DEST.new/share/"
cp "$SRC/uninstall.sh" "$DEST.new/uninstall.sh"
chown -R root:root "$DEST.new"
chmod -R go-w "$DEST.new"
chmod 755 "$DEST.new"
if [ -d "$DEST" ]; then
    mv "$DEST" "$DEST.old"
fi
mv "$DEST.new" "$DEST"
rm -rf "$DEST.old"

# トレイGUI を使える利用者のグループ（サービスがトークンをこのグループに読ませる）
if ! getent group "$GROUP" >/dev/null 2>&1; then
    groupadd --system "$GROUP"
    echo "グループ $GROUP を作りました"
fi
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    if id -nG "$SUDO_USER" | tr ' ' '\n' | grep -qx "$GROUP"; then
        echo "$SUDO_USER はすでにグループ $GROUP に入っています"
    else
        usermod -aG "$GROUP" "$SUDO_USER"
        # デスクトップによっては、サインアウトしても利用者の systemd --user が残り、アプリに新しいグループが
        # 付かない（Plasma 6 で確認）。そのときは再起動で効く
        echo "$SUDO_USER をグループ $GROUP に入れました（サインアウトしてサインインし直すと、トレイGUI を使えます。"
        echo "使えなければ PC を再起動してください）"
    fi
fi

install -m 644 "$DEST/share/clamqtpie.service" "$UNIT"
install -d -m 755 "$(dirname "$POLICY")"
install -m 644 "$DEST/share/io.github.babaken.clamqtpie.policy" "$POLICY"
install -m 644 "$DEST/share/clamqtpie.desktop" "$DESKTOP"
install -d -m 755 /usr/local/bin
ln -sfn "$DEST/clamqtpie-ctl" /usr/local/bin/clamqtpie-ctl

systemctl daemon-reload
systemctl enable --now clamqtpie.service
echo "== 導入しました =="
systemctl --no-pager --lines=0 status clamqtpie.service || true
echo
echo "トレイGUI: アプリのメニューの「ClamQtPie」、または $DEST/clamqtpie"
echo "旧版（ポータブル）のデータを移すには: sudo clamqtpie-ctl migrate --from <旧版のフォルダ>"
echo "（旧版のログの日付を保つため、移行は導入した日のうちに行ってください）"
echo "ほかの利用者にトレイGUI を使わせるには: sudo clamqtpie-ctl users add <利用者名>"

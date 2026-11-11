"""
トレイの「WebUI」サブメニュー（サービス化・Web API設計書 3.3節・10章。段階 12-5c）

- 状態の行（「WebUI: ON（21:54 に OFF）」など。押せない）、ブラウザで開く、ON にする（1 時間・8 時間・24 時間・
  時間を指定...・OFF にするまで）、OFF にする時刻を変更...、OFF にする、ログイン設定...
- 状態は GET /web（user）で読む。メニューを開くたびと、ON/OFF を変えた後に読み直す
- PUT /web・PUT /web/credentials は Bearer の admin だけなので、昇格ヘルパーを通す（api_client/elevation.py。
  UAC・pkexec と、ヘルパーの確認画面で確かめてから実行する）
- ON にするときにパスワードが未設定なら、先にログイン設定のダイアログを開き、ログインの設定と ON を 1 回の昇格に
  まとめる
- 二段階認証の設定...（段階 12-5d）: 秘密鍵はダイアログで作って QR コードを見せ、利用者がコードを入れた後で、秘密鍵と
  コードを 1 回の昇格で POST /web/totp に送る。設定済みなら「設定し直す」「解除する」を選ぶ
- ログインの停止を解く: 確認コードの間違いが続いて WebUI のログインを止めているときだけ使える（POST /web/unlock）
- LAN からの接続を許可しているときは、状態の行の下に「LAN からの接続: 許可（試験的）」を出し（隠し設定によらず）、
  ON にするときは本文に lan: true を付けて、昇格の確認画面に「LAN からも接続できます」と出す（段階 12-5e2）。
  LAN・HTTPS の設定の項目はトレイには出さない（v0.10.0 では clamqtpie-ctl web lan・web tls だけ）
"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QMenu, QMessageBox, QWidget

from api_client.admin_ops import Operation
from core.duration_text import local_time_text
from core.exceptions import AppError, ConfigValidationError
from core.version import APP_NAME
from gui.dialogs.webui_dialogs import (
    GeneratedPasswordDialog,
    RecoveryCodesDialog,
    TotpSetupDialog,
    WebUiLoginDialog,
    WebUiTimerDialog,
)
from gui.error_dialog import show_error_dialog

logger = logging.getLogger("clamav_frontend")

#: 「ON にする」の長さ（表示名, 分。None は OFF にするまで）
DURATIONS: tuple[tuple[str, int | None], ...] = (
    ("1 時間", 60),
    ("8 時間", 8 * 60),
    ("24 時間", 24 * 60),
)


def _is_wrong_code(error: AppError) -> bool:
    """POST /web/totp が確認コードを断った（api.web_control.enable_totp。detail が "code"）"""
    return error.code == ConfigValidationError.default_code and error.detail == "code"


def state_text(status: dict | None) -> str:
    """状態の行の文言"""
    if status is None:
        return "WebUI: 状態を読めません"
    if not status.get("enabled"):
        return "WebUI: OFF"
    off_at = status.get("off_at")
    when = f"（{local_time_text(off_at)} に OFF）" if off_at else "（OFF にするまで）"
    if not status.get("running"):
        return f"WebUI: ON{when}・いまは待ち受けていません"
    return f"WebUI: ON{when}"


class WebUiMenu(QObject):
    def __init__(self, menu: QMenu, api, parent_widget: Callable[[], QWidget | None] = lambda: None) -> None:
        """menu: トレイのメニューに足したサブメニュー。api: ElevatedCaller（admin の操作は昇格ヘルパーに回す）"""
        super().__init__(menu)
        self.menu = menu
        self._api = api
        self._parent = parent_widget
        self.status: dict | None = None

        self.state_action = QAction("WebUI: -", menu)
        self.state_action.setEnabled(False)
        self.lan_action = QAction("LAN からの接続: 許可（試験的）", menu)
        self.lan_action.setEnabled(False)
        self.open_action = QAction("ブラウザで開く", menu)
        self.on_menu = QMenu("ON にする", menu)
        self.duration_actions: list[QAction] = []
        for label, minutes in DURATIONS:
            action = QAction(label, self.on_menu)
            action.triggered.connect(lambda _checked=False, m=minutes: self.enable({"off_after_min": m}))
            self.on_menu.addAction(action)
            self.duration_actions.append(action)
        self.custom_action = QAction("時間を指定...", self.on_menu)
        self.custom_action.triggered.connect(self.enable_custom)
        self.forever_action = QAction("OFF にするまで", self.on_menu)
        self.forever_action.triggered.connect(lambda: self.enable({"off_after_min": None}))
        self.on_menu.addAction(self.custom_action)
        self.on_menu.addAction(self.forever_action)
        self.timer_action = QAction("OFF にする時刻を変更...", menu)
        self.off_action = QAction("OFF にする", menu)
        self.login_action = QAction("ログイン設定...", menu)
        self.totp_action = QAction("二段階認証の設定...", menu)
        self.unlock_action = QAction("ログインの停止を解く", menu)

        menu.addAction(self.state_action)
        menu.addAction(self.lan_action)
        menu.addAction(self.open_action)
        menu.addSeparator()
        menu.addMenu(self.on_menu)
        menu.addAction(self.timer_action)
        menu.addAction(self.off_action)
        menu.addSeparator()
        menu.addAction(self.login_action)
        menu.addAction(self.totp_action)
        menu.addAction(self.unlock_action)

        self.open_action.triggered.connect(self.open_browser)
        self.timer_action.triggered.connect(self.change_timer)
        self.off_action.triggered.connect(self.disable)
        self.login_action.triggered.connect(self.edit_login)
        self.totp_action.triggered.connect(self.edit_totp)
        self.unlock_action.triggered.connect(self.unlock_login)
        menu.aboutToShow.connect(self.refresh)
        self._apply()

    # -- 状態 -------------------------------------------------------------------------------------
    def refresh(self) -> None:
        try:
            self.status = self._api.call("GET", "/web")
        except AppError as exc:
            logger.debug(f"WebUI の状態を読めませんでした ({exc.code})")
            self.status = None
        self._apply()

    def _apply(self) -> None:
        status = self.status
        enabled = bool(status and status.get("enabled"))
        self.state_action.setText(state_text(status))
        self.lan_action.setVisible(bool(status and status.get("lan")))
        self.open_action.setEnabled(enabled and bool(status.get("running")) and bool(status.get("urls")))
        self.on_menu.setEnabled(status is not None and not enabled)  # ON の間は「OFF にする時刻を変更...」
        self.timer_action.setEnabled(enabled)
        self.off_action.setEnabled(enabled)
        self.login_action.setEnabled(status is not None)
        self.totp_action.setEnabled(status is not None)
        self.totp_action.setText("二段階認証の設定...（設定済み）" if status and status.get("totp_set")
                                 else "二段階認証の設定...")
        stopped = bool(status and status.get("login_stopped"))
        self.unlock_action.setVisible(stopped)
        self.unlock_action.setEnabled(stopped)

    # -- 操作 -------------------------------------------------------------------------------------
    def open_browser(self) -> None:
        urls = (self.status or {}).get("urls") or []
        if urls:
            QDesktopServices.openUrl(QUrl(urls[0]))

    def enable_custom(self) -> None:
        dialog = WebUiTimerDialog(self._parent(), "WebUI を ON にする")
        if dialog.exec() == WebUiTimerDialog.DialogCode.Accepted and dialog.body():
            self.enable(dialog.body())

    def enable(self, timer: dict) -> None:
        """ON にする（timer は off_after_min か off_at）。パスワードが未設定なら、先にログインを設定する"""
        operations = []
        login = None
        if not (self.status or {}).get("password_set"):
            login = self._ask_login(first_time=True)
            if login is None:
                return
            operations.append(Operation("PUT", "/web/credentials", {"username": login[0], "password": login[1]}))
        # LAN からの接続を許可しているときは、確認画面で分かるように lan: true も送る（許可の設定は変わらない）
        lan = {"lan": True} if (self.status or {}).get("lan") else {}
        operations.append(Operation("PUT", "/web", {"enabled": True, **lan, **timer}))
        if self._run(operations) and login and login[2]:
            GeneratedPasswordDialog(self._parent(), login[0], login[1]).exec()

    def change_timer(self) -> None:
        dialog = WebUiTimerDialog(self._parent())
        if dialog.exec() == WebUiTimerDialog.DialogCode.Accepted and dialog.body():
            self._run([Operation("PUT", "/web", dialog.body())])

    def disable(self) -> None:
        self._run([Operation("PUT", "/web", {"enabled": False})])

    def edit_login(self) -> None:
        login = self._ask_login(first_time=False)
        if login is None:
            return
        if self._run([Operation("PUT", "/web/credentials", {"username": login[0], "password": login[1]})]) \
                and login[2]:
            GeneratedPasswordDialog(self._parent(), login[0], login[1]).exec()

    def edit_totp(self) -> None:
        if (self.status or {}).get("totp_set"):
            choice = self._ask_totp_action()
            if choice == "disable":
                self._run([Operation("DELETE", "/web/totp")])
                return
            if choice != "setup":
                return
        secret, error = None, None
        while True:
            dialog = TotpSetupDialog(self._parent(), secret=secret, error=error)
            if dialog.exec() != TotpSetupDialog.DialogCode.Accepted or not dialog.code():
                return
            try:
                answers = self._api.batch([Operation("POST", "/web/totp",
                                                     {"secret": dialog.secret, "code": dialog.code()})])
            except AppError as exc:
                self.refresh()
                if not _is_wrong_code(exc):
                    show_error_dialog(self._parent(), exc)  # 取り消し（E8111）は何も出さない
                    return
                # 確認コードは昇格の確認の後で確かめるので、時間をかけると切れる。同じ秘密鍵のまま開き直し、
                # 新しいコードを入れてもらう（作り直すと、認証アプリに登録し直すことになる）
                secret, error = dialog.secret, exc.message
                continue
            self.refresh()
            RecoveryCodesDialog(self._parent(), list((answers or [{}])[0].get("recovery_codes") or [])).exec()
            return

    def unlock_login(self) -> None:
        self._run([Operation("POST", "/web/unlock")])

    def _ask_totp_action(self) -> str | None:
        """設定済みのとき: "setup"（設定し直す）・"disable"（解除する）・None（キャンセル）"""
        left = (self.status or {}).get("recovery_codes_left", 0)
        box = QMessageBox(self._parent())
        box.setWindowTitle(f"{APP_NAME}: WebUI の二段階認証")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"二段階認証は設定済みです（予備のコードの残り: {left} 個）。")
        box.setInformativeText("設定し直すと、今の認証アプリの登録と予備のコードは使えなくなります。")
        setup = box.addButton("設定し直す", QMessageBox.ButtonRole.AcceptRole)
        disable = box.addButton("解除する", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        return "setup" if clicked is setup else "disable" if clicked is disable else None

    def _ask_login(self, first_time: bool) -> tuple[str, str, bool] | None:
        username = (self.status or {}).get("username") or "admin"
        dialog = WebUiLoginDialog(self._parent(), username, first_time=first_time)
        if dialog.exec() != WebUiLoginDialog.DialogCode.Accepted:
            return None
        return dialog.result_value()

    def _run(self, operations: list[Operation], results: bool = False):
        """昇格ヘルパーで実行する。取り消し・失敗なら False（失敗はエラーのダイアログ）。results なら、成功したときに
        各操作の応答の一覧を返す"""
        try:
            answers = self._api.batch(operations)
        except AppError as exc:
            show_error_dialog(self._parent(), exc)  # 取り消し（E8111）は何も出さない
            self.refresh()
            return False
        self.refresh()
        return list(answers or [{}]) if results else True

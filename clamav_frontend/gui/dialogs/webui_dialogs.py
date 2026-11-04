"""
トレイの「WebUI」サブメニューのダイアログ（サービス化・Web API設計書 3.3節・10章。段階 12-5c）

- WebUiTimerDialog: OFF にする時刻を、長さ（90m・8h・2d など）か日時で決める
- WebUiLoginDialog: ログインのユーザー名・パスワード（自動で作ることもできる）
- GeneratedPasswordDialog: 自動で作ったパスワードを 1 回だけ見せる（コピーできる）

入力の検査はサービスと同じ関数（core/duration_text.py・core/web_password.py）で行い、通らなければダイアログを
閉じずに赤字で知らせる。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from core import duration_text, web_password
from core.version import APP_NAME

_ERROR_STYLE = "color: #e5484d;"


def _ok_cancel_buttons(dialog: QDialog) -> QDialogButtonBox:
    """「OK」「キャンセル」（Qt の翻訳は配布物に入れていないため、文言を自分で付ける）"""
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
    buttons.accepted.connect(dialog._try_accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


def _error_label() -> QLabel:
    label = QLabel("")
    label.setStyleSheet(_ERROR_STYLE)
    label.setWordWrap(True)
    return label


class WebUiTimerDialog(QDialog):
    """OFF にする時刻を決める。body() は PUT /web に渡す {"off_after_min": n} か {"off_at": "..."}"""

    def __init__(self, parent=None, title: str = "WebUI を OFF にする時刻", now: datetime | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: {title}")
        self._now = now
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("WebUI を自動で OFF にする時刻を決めてください。"))

        self.duration_radio = QRadioButton("長さで決める")
        self.duration_radio.setChecked(True)
        self.duration_edit = QLineEdit()
        self.duration_edit.setPlaceholderText("例: 90m、8h、2d")
        self.duration_edit.setToolTip("m 分・h 時間・d 日・w 週（1d12h のように組み合わせられます）")
        self.at_radio = QRadioButton("日時で決める")
        self.at_edit = QDateTimeEdit()
        self.at_edit.setCalendarPopup(True)
        self.at_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        start = (now or datetime.now()).astimezone() + timedelta(hours=1)
        self.at_edit.setDateTime(QDateTime(start.year, start.month, start.day, start.hour, 0, 0))
        form = QFormLayout()
        form.addRow(self.duration_radio, self.duration_edit)
        hint = QLabel("m 分・h 時間・d 日・w 週（1d12h のように組み合わせられます）")
        hint.setStyleSheet("color: gray;")
        form.addRow("", hint)
        form.addRow(self.at_radio, self.at_edit)
        layout.addLayout(form)
        self.duration_radio.toggled.connect(self._sync)
        self.error_label = _error_label()
        layout.addWidget(self.error_label)
        layout.addWidget(_ok_cancel_buttons(self))
        self._body: dict | None = None
        self._sync()

    def _sync(self) -> None:
        self.duration_edit.setEnabled(self.duration_radio.isChecked())
        self.at_edit.setEnabled(not self.duration_radio.isChecked())

    def _try_accept(self) -> None:
        try:
            if self.duration_radio.isChecked():
                self._body = {"off_after_min": duration_text.parse_duration_minutes(self.duration_edit.text())}
            else:
                moment = self.at_edit.dateTime().toPython().astimezone()  # ローカル時刻として読む
                self._body = {"off_at": duration_text.check_off_at(
                    moment.astimezone(timezone.utc).isoformat(timespec="seconds"), self._now)}
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()

    def body(self) -> dict | None:
        return self._body


class WebUiLoginDialog(QDialog):
    """ログインの設定。result() は (ユーザー名, パスワード, 自動で作ったか)"""

    def __init__(self, parent=None, username: str = "admin", first_time: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: WebUI のログイン設定")
        layout = QVBoxLayout(self)
        intro = ("WebUI を使う前に、ログインのユーザー名とパスワードを決めてください。" if first_time
                 else "WebUI のログインのユーザー名とパスワードを変えます。ログイン中の WebUI はすべてログアウトします。")
        label = QLabel(intro)
        label.setWordWrap(True)
        layout.addWidget(label)
        form = QFormLayout()
        self.username_edit = QLineEdit(username)
        self.username_edit.setMaxLength(32)
        form.addRow("ユーザー名", self.username_edit)
        self.generate_check = QCheckBox("パスワードを自動で作る（20 文字。作ったパスワードは 1 回だけ表示します）")
        form.addRow("", self.generate_check)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText(f"{web_password.MIN_LENGTH} 文字以上")
        form.addRow("パスワード", self.password_edit)
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("もう一度", self.confirm_edit)
        layout.addLayout(form)
        self.generate_check.toggled.connect(self._sync)
        self.error_label = _error_label()
        layout.addWidget(self.error_label)
        layout.addWidget(_ok_cancel_buttons(self))
        self._result: tuple[str, str, bool] | None = None

    def _sync(self) -> None:
        manual = not self.generate_check.isChecked()
        self.password_edit.setEnabled(manual)
        self.confirm_edit.setEnabled(manual)

    def _try_accept(self) -> None:
        username = self.username_edit.text().strip()
        problem = web_password.username_problem(username)
        if problem:
            self.error_label.setText(problem)
            return
        if self.generate_check.isChecked():
            self._result = (username, web_password.generate_password(), True)
            self.accept()
            return
        password = self.password_edit.text()
        problem = web_password.password_problem(password)
        if problem:
            self.error_label.setText(problem)
            return
        if password != self.confirm_edit.text():
            self.error_label.setText("パスワードが一致しません")
            return
        self._result = (username, password, False)
        self.accept()

    def result_value(self) -> tuple[str, str, bool] | None:
        return self._result


class GeneratedPasswordDialog(QDialog):
    """自動で作ったパスワードを 1 回だけ見せる"""

    def __init__(self, parent, username: str, password: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: WebUI のパスワード")
        layout = QVBoxLayout(self)
        label = QLabel(f"WebUI のログインを設定しました（ユーザー名: {username}）。\n"
                       "次のパスワードを控えてください。この画面を閉じると、もう表示できません。")
        label.setWordWrap(True)
        layout.addWidget(label)
        row = QHBoxLayout()
        self.password_edit = QLineEdit(password)
        self.password_edit.setReadOnly(True)
        row.addWidget(self.password_edit)
        copy_button = QPushButton("コピー")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(password))
        row.addWidget(copy_button)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("閉じる")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

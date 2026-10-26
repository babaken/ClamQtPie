"""
共通エラーダイアログ

参照: エラーコード一覧・例外設計書5.1節
- [アイコン] {message} / エラーコード: {code} / [詳細を見る] / [OK]
- 「詳細を見る」はログ画面へ遷移し、該当エラーコードで検索欄を自動入力する
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QMessageBox, QPushButton, QWidget

from core.exceptions import AppError

#: 「詳細を見る」の既定動作（MainWindowがログ画面への遷移を登録する）。各画面はコールバックを
#: 個別に渡さなくても、画面設計書「共通仕様」のログ画面へのリンク付きダイアログになる
_default_detail_handler: Callable[[str], None] | None = None


def set_default_detail_handler(handler: Callable[[str], None] | None) -> None:
    global _default_detail_handler
    _default_detail_handler = handler


def show_error_dialog(
    parent: QWidget | None,
    error: AppError,
    on_show_detail: Callable[[str], None] | None = None,
) -> None:
    """エラーコード一覧・例外設計書5.1節の共通フォーマットでダイアログを表示する

    on_show_detail: 「詳細を見る」押下時に呼ばれるコールバック。省略時はset_default_detail_handler()
                     で登録された既定動作（ログ画面へ遷移し、エラーコードで絞り込む）を使う。
    """
    if on_show_detail is None:
        on_show_detail = _default_detail_handler
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("エラー")
    box.setText(error.message)
    box.setInformativeText(f"エラーコード: {error.code}")

    detail_button: QPushButton | None = None
    if on_show_detail is not None:
        detail_button = box.addButton("詳細を見る", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Ok)

    box.exec()

    if detail_button is not None and box.clickedButton() is detail_button:
        on_show_detail(error.code)


def show_confirm_dialog(parent: QWidget | None, title: str, message: str) -> bool:
    """破壊的操作の確認ダイアログ（画面設計書「共通仕様」）"""
    result = QMessageBox.question(
        parent,
        title,
        message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return result == QMessageBox.StandardButton.Yes

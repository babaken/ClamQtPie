"""
ジョブ編集ダイアログ

参照: 画面設計書4章「ジョブ編集ダイアログ 項目定義」
Phase 11: 新規作成時に既定プリセットのCron式が空のまま（保存するとE4002）だった不具合と、
          対象パス未入力のままスキャンジョブを保存できた不具合を修正。入力エラーはダイアログを
          閉じずに赤字で表示する。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
)

from core.scheduler_service import cron_trigger
from gui.plain_text import plain

#: プリセット周期とcron式の対応（画面設計書4章「実行周期」）。曜日は標準のcron式の数え方（0=日曜）
#: （APSchedulerへはcore.scheduler_service.cron_triggerで変換して渡す）
_CRON_PRESETS = {
    "毎日 3:00": "0 3 * * *",
    "毎週日曜 3:00": "0 3 * * 0",
    "毎月1日 3:00": "0 3 1 * *",
}
_CUSTOM_LABEL = "カスタム（Cron式を直接入力）"

#: 種別の表示名と内部値（schedule.scan_type）の対応
SCAN_TYPE_LABELS = {"scan": "スキャン", "update": "定義更新"}


class JobEditDialog(QDialog):
    def __init__(self, parent=None, initial: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ジョブ編集")
        layout = QFormLayout(self)

        self.name_edit = QLineEdit()
        layout.addRow("ジョブ名", self.name_edit)

        self.type_combo = QComboBox()
        for value, label in SCAN_TYPE_LABELS.items():
            self.type_combo.addItem(label, value)
        self.type_combo.currentIndexChanged.connect(lambda _i: self._on_type_changed(self.scan_type()))
        layout.addRow("種別", self.type_combo)

        target_layout = QHBoxLayout()
        self.target_edit = QLineEdit()
        self.browse_button = QPushButton("参照")
        self.browse_button.clicked.connect(self._on_browse_clicked)
        target_layout.addWidget(self.target_edit)
        target_layout.addWidget(self.browse_button)
        self.target_row_label = "対象パス"
        layout.addRow(self.target_row_label, _wrap(target_layout))

        self.cron_preset_combo = QComboBox()
        self.cron_preset_combo.addItems(list(_CRON_PRESETS.keys()) + [_CUSTOM_LABEL])
        self.cron_preset_combo.currentTextChanged.connect(self._on_preset_changed)
        layout.addRow("実行周期", self.cron_preset_combo)

        self.cron_edit = QLineEdit()
        layout.addRow("Cron式", self.cron_edit)

        self.error_label = plain(QLabel(""))
        self.error_label.setStyleSheet("color: #e53935;")
        self.error_label.setVisible(False)
        layout.addRow(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        if initial:
            self.name_edit.setText(initial.get("name", ""))
            self.set_scan_type(initial.get("scan_type", "scan"))
            self.target_edit.setText(initial.get("target", ""))
            cron = initial.get("cron", "")
            preset = next((label for label, value in _CRON_PRESETS.items() if value == cron), None)
            self.cron_preset_combo.setCurrentText(preset or _CUSTOM_LABEL)
            self.cron_edit.setText(cron)
        # 初期選択のプリセットはcurrentTextChangedが発火しないため明示的に反映する
        self._on_preset_changed(self.cron_preset_combo.currentText())
        self._on_type_changed(self.scan_type())

    def scan_type(self) -> str:
        return self.type_combo.currentData()

    def set_scan_type(self, value: str) -> None:
        index = self.type_combo.findData(value)
        if index >= 0:
            self.type_combo.setCurrentIndex(index)

    def _on_type_changed(self, type_value: str) -> None:
        is_scan = type_value == "scan"
        self.target_edit.setEnabled(is_scan)
        self.browse_button.setEnabled(is_scan)

    def _on_preset_changed(self, label: str) -> None:
        if label in _CRON_PRESETS:
            self.cron_edit.setText(_CRON_PRESETS[label])
            self.cron_edit.setEnabled(False)
        else:
            self.cron_edit.setEnabled(True)

    def _on_browse_clicked(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "対象フォルダを選択")
        if path:
            self.target_edit.setText(path)

    def validate(self) -> str:
        """入力エラーのメッセージを返す（問題なければ空文字）。重複名の確認はSchedulerService側（E4001）"""
        values = self.result_values()
        if not values["name"]:
            return "ジョブ名を入力してください"
        if values["scan_type"] == "scan":
            if not values["target"]:
                return "スキャン対象のパスを指定してください"
            if not Path(values["target"]).exists():
                return f"スキャン対象のパスが見つかりません: {values['target']}"
        try:
            cron_trigger(values["cron"])
        except ValueError:
            return "実行周期（Cron式）の指定が正しくありません"
        return ""

    def accept(self) -> None:
        message = self.validate()
        if message:
            self.error_label.setText(message)
            self.error_label.setVisible(True)
            return
        super().accept()

    def result_values(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "scan_type": self.scan_type(),
            "target": self.target_edit.text().strip() if self.scan_type() == "scan" else "",
            "cron": self.cron_edit.text().strip(),
        }


def _wrap(layout: QHBoxLayout):
    from PySide6.QtWidgets import QWidget

    w = QWidget()
    w.setLayout(layout)
    return w

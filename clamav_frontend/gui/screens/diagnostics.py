"""
システム診断画面

画面レイアウト刷新で追加。ClamAVの実行ファイルの場所・設定ファイル・定義DBフォルダ・clamdの接続先を確認し、
診断レポート（core/diagnostics_service.py）を表示・コピーする。
- 「レポートをコピー（パス伏せ字）」はホームフォルダ・ユーザー名・コンピューター名に加え、ソケット・
  設定ファイル・clamonaccのパス・アプリのフォルダ・clamdのホスト名とグローバルIPを伏せ字にする
  （問い合わせ等に貼り付ける場合はこちらを使う）
- 診断はclamdへの接続・sc / systemctlの実行を含むため、BackgroundTaskでバックグラウンド実行する
"""
from __future__ import annotations

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QTextEdit

from core.background_task import BackgroundTask
from core.diagnostics_service import LEVEL_NG, LEVEL_WARN, mask_report
from gui.screens.base import BaseScreen, wrap_layout
from gui.theme import COLOR_NG, COLOR_OK, COLOR_WARN, set_variant
from gui.plain_text import plain


class DiagnosticsScreen(BaseScreen):
    screen_title = "システムとエンジンの診断"
    screen_subtitle = "ClamAV の実行ファイルの場所や clamd の接続先を確認し、診断レポートを作成します。"

    def build_content(self) -> None:
        self.begin_card()
        buttons = QHBoxLayout()
        self.run_button = set_variant(QPushButton("ヘルスチェックを実行"), "primary")
        self.copy_masked_button = QPushButton("レポートをコピー（パス伏せ字）")
        self.copy_masked_button.setToolTip(
            "ホームフォルダ・ユーザー名・コンピューター名、ソケット・設定ファイルのパス、"
            "clamdのホスト名などを伏せ字にしてコピーします"
        )
        self.copy_full_button = QPushButton("完全なレポートをコピー")
        self.copy_full_button.setToolTip("パス等を伏せ字にせずにコピーします（社外へ送る場合は伏せ字版を使ってください）")
        self.run_button.clicked.connect(self.run_diagnostics)
        self.copy_masked_button.clicked.connect(lambda: self._copy(masked=True))
        self.copy_full_button.clicked.connect(lambda: self._copy(masked=False))
        for button in (self.run_button, self.copy_masked_button, self.copy_full_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.add_layout_widget(wrap_layout(buttons))
        self.summary_label = plain(QLabel("「ヘルスチェックを実行」をクリックすると診断レポートを作成します。"))
        self.summary_label.setObjectName("cardText")
        self.summary_label.setWordWrap(True)
        self.add_layout_widget(self.summary_label)

        self.begin_card("診断レポートの詳細")
        self.report_area = QTextEdit()
        self.report_area.setReadOnly(True)
        self.report_area.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.report_area.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.report_area.setPlaceholderText("診断レポートはまだ作成されていません。")
        self.add_layout_widget(self.report_area)

        self._diagnostics_service = None
        self._report_text = ""
        self._report_sensitive: dict[str, str] = {}
        self._task = BackgroundTask()
        self._task.finished.connect(self._on_finished)
        self._update_buttons()

    def bind_services(self, diagnostics_service) -> None:
        self._diagnostics_service = diagnostics_service
        self._update_buttons()

    @property
    def report_text(self) -> str:
        return self._report_text

    def run_diagnostics(self) -> None:
        if self._diagnostics_service is None:
            return
        if self._task.run("diagnostics", self._diagnostics_service.run):
            self.summary_label.setText("診断しています...")
            self.summary_label.setStyleSheet("")
            self._update_buttons()

    def _on_finished(self, _name: str, report, error) -> None:
        if error is not None:
            self.summary_label.setText(f"診断を完了できませんでした: {error}")
            self.summary_label.setStyleSheet(f"color: {COLOR_NG};")
        else:
            self._report_text = report.to_text()
            self._report_sensitive = dict(report.sensitive)
            self.report_area.setPlainText(self._report_text)
            ng = sum(1 for c in report.checks if c.level == LEVEL_NG)
            warn = sum(1 for c in report.checks if c.level == LEVEL_WARN)
            if ng:
                text, color = f"問題が {ng} 件見つかりました（注意 {warn} 件）。レポートの [NG] の行を確認してください。", COLOR_NG
            elif warn:
                text, color = f"動作に必要な項目は揃っています（注意 {warn} 件）。", COLOR_WARN
            else:
                text, color = "問題は見つかりませんでした。", COLOR_OK
            self.summary_label.setText(text)
            self.summary_label.setStyleSheet(f"color: {color};")
        self._update_buttons()

    def _copy(self, masked: bool) -> None:
        if not self._report_text:
            return
        text = mask_report(self._report_text, extra=self._report_sensitive) if masked else self._report_text
        QApplication.clipboard().setText(text)
        self.summary_label.setText(
            "伏せ字にしたレポートをクリップボードにコピーしました。" if masked
            else "完全なレポートをクリップボードにコピーしました。"
        )
        self.summary_label.setStyleSheet("")

    def _update_buttons(self) -> None:
        running = self._task.is_running
        self.run_button.setEnabled(self._diagnostics_service is not None and not running)
        has_report = bool(self._report_text) and not running
        self.copy_masked_button.setEnabled(has_report)
        self.copy_full_button.setEnabled(has_report)

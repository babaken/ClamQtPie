"""
MainWindow — ヘッダー＋左ナビゲーション（10画面）＋ステータスバー

参照: 画面設計書「共通仕様」
Phase8: ×ボタンのトレイ格納挙動（closeEvent）を実装
画面レイアウト刷新: docs/screenshots_i18n.zip のサンプルに合わせ、
  - 上部ヘッダー（≡でナビを折りたたみ、アプリのアイコン・製品名ClamQtPie・稼働状態の表示）
  - アイコン付きの左ナビ（下部にPowered by ClamAVと定義DBの版）
  - 「ClamAV エンジン サービス」「システム診断」の2画面を追加して10画面
  に変更した。製品名・アイコンはClamQtPie独自のもの（ClamAVのロゴは使わない）。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from core.exceptions import AppError
from core.version import APP_NAME, APP_POWERED_BY, APP_SUBTITLE, APP_VERSION
from gui import icons
from gui.error_dialog import show_error_dialog
from gui.screens.diagnostics import DiagnosticsScreen
from gui.screens.engine import EngineScreen
from gui.screens.home import HomeScreen
from gui.screens.log import LogScreen
from gui.screens.quarantine import QuarantineScreen
from gui.screens.realtime import RealtimeScreen
from gui.screens.scan import ScanScreen
from gui.screens.schedule import ScheduleScreen
from gui.screens.settings import SettingsScreen
from gui.screens.update import UpdateScreen
from gui.theme import COLOR_NG, COLOR_OK, COLOR_TEXT_MUTED

_NAV_KEY_ROLE = Qt.ItemDataRole.UserRole
_NAV_LABEL_ROLE = Qt.ItemDataRole.UserRole + 1

#: 左ナビゲーション10項目（表示順）。ラベルはナビの表示名で、画面上部のタイトルは各画面のscreen_title
NAV_ITEMS = [
    ("home", "ホーム", HomeScreen),
    ("scan", "ウイルスと脅威の防止", ScanScreen),
    ("update", "セキュリティ インテリジェンス", UpdateScreen),
    ("log", "保護の履歴", LogScreen),
    ("quarantine", "隔離されたアイテム", QuarantineScreen),
    ("engine", "ClamAV エンジン サービス", EngineScreen),
    ("realtime", "リアルタイム保護", RealtimeScreen),
    ("schedule", "スキャン スケジューラー", ScheduleScreen),
    ("diagnostics", "システム診断", DiagnosticsScreen),
    ("settings", "設定", SettingsScreen),
]

_SIDEBAR_WIDTH = 250
_SIDEBAR_COLLAPSED_WIDTH = 64


class MainWindow(QMainWindow):
    #: 表示中の画面が切り替わった（画面キー: "home" / "quarantine" 等）
    screen_changed = Signal(str)

    def __init__(self, tray_icon: QSystemTrayIcon | None = None, config_service=None) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 840)
        self.setMinimumSize(900, 600)

        self._tray_icon = tray_icon
        self._config_service = config_service

        self._screens: dict[str, QWidget] = {}
        self._sidebar_collapsed = False
        self._build_central_widget()
        self._build_status_bar()

    # -- 画面構築 -----------------------------------------------------
    def _build_central_widget(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        self.stacked_widget = QStackedWidget()
        for key, _label, screen_cls in NAV_ITEMS:
            screen = screen_cls()
            self._screens[key] = screen
            self.stacked_widget.addWidget(screen)
        body.addWidget(self.stacked_widget, stretch=1)
        root.addLayout(body, stretch=1)

        self.nav_list.currentRowChanged.connect(self.stacked_widget.setCurrentIndex)
        self.nav_list.currentRowChanged.connect(
            lambda row: self.screen_changed.emit(self.nav_list.item(row).data(_NAV_KEY_ROLE))
        )
        self.nav_list.setCurrentRow(0)
        self.setCentralWidget(central)

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("appHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)

        self.hamburger_button = QPushButton("≡")
        self.hamburger_button.setObjectName("hamburger")
        self.hamburger_button.setToolTip("メニューを折りたたむ／展開する")
        self.hamburger_button.clicked.connect(self.toggle_sidebar)
        layout.addWidget(self.hamburger_button)

        logo = QLabel()
        app_icon = QApplication.windowIcon()
        if not app_icon.isNull():
            logo.setPixmap(app_icon.pixmap(28, 28))
        layout.addWidget(logo)
        name_label = QLabel(APP_NAME)
        name_label.setObjectName("appName")
        subtitle_label = QLabel(APP_SUBTITLE)
        subtitle_label.setObjectName("appSubtitle")
        layout.addWidget(name_label)
        layout.addWidget(subtitle_label)
        layout.addStretch(1)

        self.status_pill = QLabel()
        self.status_pill.setObjectName("statusPill")
        self._set_pill("確認中...", COLOR_TEXT_MUTED)
        layout.addWidget(self.status_pill)
        return header

    def _build_sidebar(self) -> QFrame:
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(_SIDEBAR_WIDTH)
        layout = QVBoxLayout(self.sidebar)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(0)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("navList")
        self.nav_list.setIconSize(QSize(22, 22))
        self.nav_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for key, label, _screen_cls in NAV_ITEMS:
            item = QListWidgetItem(icons.icon(key), label)
            item.setData(_NAV_KEY_ROLE, key)
            item.setData(_NAV_LABEL_ROLE, label)
            item.setToolTip(label)
            self.nav_list.addItem(item)
        layout.addWidget(self.nav_list, stretch=1)

        self.sidebar_footer = QFrame()
        self.sidebar_footer.setObjectName("sidebarFooter")
        footer_layout = QVBoxLayout(self.sidebar_footer)
        footer_layout.setContentsMargins(14, 8, 14, 8)
        footer_layout.setSpacing(2)
        footer_title = QLabel(APP_POWERED_BY)
        footer_title.setObjectName("sidebarFooterTitle")
        self.footer_db_label = QLabel("定義DB: -")
        self.footer_db_label.setObjectName("sidebarFooterDetail")
        footer_layout.addWidget(footer_title)
        footer_layout.addWidget(self.footer_db_label)
        layout.addWidget(self.sidebar_footer)
        return self.sidebar

    def _build_status_bar(self) -> None:
        status_bar = self.statusBar()
        self.app_version_label = QLabel(f"{APP_NAME} {APP_VERSION}")
        status_bar.addWidget(self.app_version_label)
        self.clamd_status_label = QLabel("clamd: 確認中...")
        self.db_update_label = QLabel("定義DB更新: -")
        self.realtime_status_label = QLabel("リアルタイム監視: OFF")
        for label in (self.clamd_status_label, self.db_update_label, self.realtime_status_label):
            status_bar.addPermanentWidget(label)

    # -- 左ナビの折りたたみ -------------------------------------------------
    @property
    def sidebar_collapsed(self) -> bool:
        # 実際の幅（DPIやスタイルで変わり得る）からは判定せず、状態を持つ
        return self._sidebar_collapsed

    def toggle_sidebar(self) -> None:
        """≡ボタン: 左ナビをアイコンだけの幅に折りたたむ／元に戻す（項目名はツールチップで確認できる）"""
        collapse = not self._sidebar_collapsed
        self._sidebar_collapsed = collapse
        self.sidebar.setFixedWidth(_SIDEBAR_COLLAPSED_WIDTH if collapse else _SIDEBAR_WIDTH)
        self.sidebar_footer.setVisible(not collapse)
        for row in range(self.nav_list.count()):
            item = self.nav_list.item(row)
            item.setText("" if collapse else item.data(_NAV_LABEL_ROLE))

    # -- 稼働状態の表示 -------------------------------------------------
    def _set_pill(self, text: str, color: str) -> None:
        self.status_pill.setText(f"<span style='color:{color};'>●</span>&nbsp; {text}")

    def update_status_bar(self, status) -> None:
        """ヘッダー右上・左ナビ下部・ステータスバー3項目を更新する（画面設計書「共通仕様」）。statusはhome.AppStatus"""
        if status.clamd_running is None:
            clamd_text = "確認中..."
            self._set_pill("確認中...", COLOR_TEXT_MUTED)
        else:
            clamd_text = "稼働中" if status.clamd_running else "停止"
            if status.clamd_running:
                self._set_pill("ClamAV はスキャンの準備ができています", COLOR_OK)
            else:
                self._set_pill("ClamAV エンジン サービスが停止しています", COLOR_NG)
        self.clamd_status_label.setText(f"clamd: {clamd_text}")
        db_text = status.db_date if status.clamd_running and status.db_date else "-"
        self.db_update_label.setText(f"定義DB更新: {db_text}")
        self.realtime_status_label.setText(
            f"リアルタイム監視: {'ON' if status.realtime_running else 'OFF'}"
        )
        self.footer_db_label.setText(
            f"定義DB: {status.db_version}" if status.clamd_running and status.db_version else "定義DB: -"
        )

    # -- 画面遷移 -----------------------------------------------------
    def navigate_to(self, key: str) -> None:
        for row in range(self.nav_list.count()):
            item = self.nav_list.item(row)
            if item.data(_NAV_KEY_ROLE) == key:
                self.nav_list.setCurrentRow(row)
                return

    def get_screen(self, key: str) -> QWidget:
        return self._screens[key]

    # -- エラーダイアログ（エラーコード一覧・例外設計書5.1節） -----------
    def show_error(self, error: AppError) -> None:
        show_error_dialog(self, error, on_show_detail=self.show_log_for_error_code)

    def show_log_for_error_code(self, code: str) -> None:
        """エラーダイアログの「詳細を見る」: ログ画面へ遷移し、エラーコードで絞り込む"""
        self.show_from_tray()
        self.navigate_to("log")
        log_screen = self.get_screen("log")
        if hasattr(log_screen, "filter_by_error_code"):
            log_screen.filter_by_error_code(code)

    # -- ×ボタンのトレイ格納挙動（タスクトレイ常駐アプリプロセス設計書4.1節） -------
    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()

        if self._tray_icon is not None and self._config_service is not None:
            config = self._config_service.current
            if not config.general.show_tray_hint_shown:
                self._tray_icon.showMessage(
                    APP_NAME,
                    "アプリはタスクトレイに常駐しています",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
                config.general.show_tray_hint_shown = True
                self._config_service.save(config)

    def show_from_tray(self) -> None:
        """トレイアイコンのダブルクリック／「メイン画面を開く」から呼ばれる（4.2節）"""
        self.show()
        self.raise_()
        self.activateWindow()
        self.screen_changed.emit(self.current_screen_key())

    def current_screen_key(self) -> str:
        item = self.nav_list.currentItem()
        return item.data(_NAV_KEY_ROLE) if item else ""

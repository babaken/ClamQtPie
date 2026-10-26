"""
ホーム画面

参照: 画面設計書1章
Phase8: クイックアクション・ステータス表示の結線を追加
Phase 11: 直近アクティビティ（ログから最大10件）、カード／行クリックでの画面遷移、
          clamd停止時の「起動」ボタン、ステータスの文字色を追加
画面レイアウト刷新: 「保護の概要」として、状態を示すヒーローカード（clamd・定義DB・リアルタイム監視）と
          4つの操作カード（スキャン・定義更新・リアルタイム監視・エンジン サービス）に組み替えた。
          clamdの「起動」ボタンは「ClamAV エンジン サービス」画面（gui/screens/engine.py）へ移した。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.logger import CATEGORY_QUARANTINE, CATEGORY_REALTIME, CATEGORY_SCAN, CATEGORY_UPDATE
from gui import icons
from gui.screens.base import BaseScreen, make_card, wrap_layout
from gui.theme import COLOR_NG, COLOR_OK, COLOR_TEXT_MUTED, set_accent, set_variant

#: 直近アクティビティの最大件数（画面設計書1章）
ACTIVITY_LIMIT = 10
#: 直近アクティビティで読む過去のログの日数
ACTIVITY_DAYS = 7
#: アクティビティとして表示するログのカテゴリと、行クリック時の遷移先画面
_ACTIVITY_SCREENS = {
    CATEGORY_SCAN: "scan",
    CATEGORY_REALTIME: "realtime",
    CATEGORY_UPDATE: "update",
    CATEGORY_QUARANTINE: "quarantine",
}


@dataclass
class AppStatus:
    """ホーム画面・ステータスバー・タスクトレイで共有する稼働状態（画面設計書「共通仕様」）"""

    clamd_running: bool | None = None  # None = 未確認
    db_version: str = ""
    db_date: str = ""
    realtime_running: bool = False
    engine_version: str = ""  # 例: "ClamAV 1.5.4"（エンジン サービス画面で表示）


class _StatusCard(QFrame):
    """ヒーローカード内の「項目名: 値」表示。クリックで対応画面へ遷移できる"""

    clicked = Signal()

    def __init__(self, title: str, value: str) -> None:
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        title_label = QLabel(f"{title}:")
        title_label.setObjectName("cardText")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("value_label")
        self.set_value(value)
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)

    def set_value(self, text: str, color: str | None = None) -> None:
        self.value_label.setText(text)
        self.value_label.setStyleSheet("font-weight: bold;" + (f" color: {color};" if color else ""))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qtのオーバーライド
        self.clicked.emit()
        super().mousePressEvent(event)


class HomeScreen(BaseScreen):
    screen_title = "保護の概要"
    screen_subtitle = "システムの保護状態、使用中のスキャン エンジン、ウイルス定義の情報を表示します。"

    #: refresh_status()の結果（AppStatus）。ステータスバー・トレイアイコンの更新に使う
    status_refreshed = Signal(object)

    def build_content(self) -> None:
        # 1. 状態（ヒーローカード）: clamd・定義DB・リアルタイム監視
        self.hero_card = self.begin_card()
        hero = QHBoxLayout()
        hero.setSpacing(16)
        hero_icon = QLabel()
        app_icon = QApplication.windowIcon()
        hero_icon.setPixmap(
            app_icon.pixmap(56, 56) if not app_icon.isNull() else icons.pixmap("home", 56)
        )
        hero_icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        hero.addWidget(hero_icon)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(4)
        self.hero_title_label = QLabel("状態を確認しています...")
        self.hero_title_label.setObjectName("heroTitle")
        self.hero_detail_label = QLabel("")
        self.hero_detail_label.setObjectName("cardText")
        self.hero_detail_label.setWordWrap(True)
        hero_text.addWidget(self.hero_title_label)
        hero_text.addWidget(self.hero_detail_label)
        meta = QHBoxLayout()
        meta.setSpacing(18)
        self.clamd_status_card = _StatusCard("エンジン", "確認中...")
        self.db_version_card = _StatusCard("定義DB", "-")
        self.realtime_status_card = _StatusCard("リアルタイム監視", "OFF")
        for card in (self.clamd_status_card, self.db_version_card, self.realtime_status_card):
            meta.addWidget(card)
        meta.addStretch(1)
        hero_text.addLayout(meta)
        hero.addLayout(hero_text, stretch=1)
        self.add_layout_widget(wrap_layout(hero))
        # 画面設計書1章「遷移: 各カード／ボタンから対応画面へ」
        self.clamd_status_card.clicked.connect(lambda: self._navigate("engine"))
        self.db_version_card.clicked.connect(lambda: self._navigate("update"))
        self.realtime_status_card.clicked.connect(lambda: self._navigate("realtime"))
        self.end_card()

        # 2. 操作カード（2列）
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        self.scan_now_button = set_variant(QPushButton("クイック スキャン"), "primary")
        self.custom_scan_button = QPushButton("カスタム スキャン")
        self.update_now_button = QPushButton("定義を更新")
        self.toggle_realtime_button = QPushButton("監視を開始")
        self.realtime_settings_button = QPushButton("監視を設定")
        self.engine_button = QPushButton("エンジンの操作")
        self.scan_now_button.clicked.connect(self._on_scan_now_clicked)
        self.custom_scan_button.clicked.connect(lambda: self._navigate("scan"))
        self.update_now_button.clicked.connect(self._on_update_now_clicked)
        self.toggle_realtime_button.clicked.connect(self._on_toggle_realtime_clicked)
        self.realtime_settings_button.clicked.connect(lambda: self._navigate("realtime"))
        self.engine_button.clicked.connect(lambda: self._navigate("engine"))
        cards = [
            ("scan", "ウイルス スキャン", "ファイル・フォルダーを ClamAV で検査します。クイック スキャンは"
             "デスクトップ・ダウンロード・ドキュメント等を対象にします。",
             (self.scan_now_button, self.custom_scan_button)),
            ("update", "ウイルス定義", "freshclam で最新のウイルス定義データベースを取得します。",
             (self.update_now_button,)),
            ("realtime", "リアルタイム ファイル監視", "監視対象のフォルダーで作成・変更されたファイルを自動でスキャンします。",
             (self.toggle_realtime_button, self.realtime_settings_button)),
            ("engine", "ClamAV エンジン サービス", "検査を行う常駐サービス clamd の状態確認と起動・停止を行います。",
             (self.engine_button,)),
        ]
        for index, (icon_name, title, text, buttons) in enumerate(cards):
            grid.addWidget(_action_card(icon_name, title, text, buttons), index // 2, index % 2)
        self.add_layout_widget(wrap_layout(grid))

        # 3. 直近アクティビティ一覧（ログから最大10件、クリックで対応画面へ）
        self.begin_card("直近のアクティビティ")
        self.activity_table = QTableWidget(0, 3)
        self.activity_table.setHorizontalHeaderLabels(["日時", "種別", "概要"])
        self.activity_table.cellDoubleClicked.connect(self._on_activity_clicked)
        self.activity_table.cellClicked.connect(self._on_activity_clicked)
        self.add_layout_widget(self.activity_table)

        self._clamav_service = None
        self._realtime_manager = None
        self._log_reader_service = None
        self._status = AppStatus()
        self._on_quick_scan = None
        self._on_update_now = None
        self._on_toggle_realtime = None
        self._on_navigate = None

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(
        self,
        clamav_service,
        realtime_manager,
        on_quick_scan,
        on_update_now,
        on_toggle_realtime,
        log_reader_service=None,
        on_navigate=None,
    ) -> None:
        """
        on_quick_scan / on_update_now / on_toggle_realtime / on_navigate:
            他画面への遷移と実処理起動はMainWindow（app.py）側のコールバックに委譲する
            （画面間の直接依存を避けるため）。
        """
        self._clamav_service = clamav_service
        self._realtime_manager = realtime_manager
        self._log_reader_service = log_reader_service
        self._on_quick_scan = on_quick_scan
        self._on_update_now = on_update_now
        self._on_toggle_realtime = on_toggle_realtime
        self._on_navigate = on_navigate
        self.refresh_status()

    @property
    def status(self) -> AppStatus:
        return self._status

    def refresh_status(self, check_clamd: bool = True) -> None:
        """稼働状態を更新してstatus_refreshedを発行する

        check_clamd=Falseの場合はclamdへの問い合わせとアクティビティの再読込を省略し、
        リアルタイム監視の状態のみ更新する（短い間隔での定期更新用。clamdの状態は前回の結果を引き継ぐ）。
        """
        if self._clamav_service is None:
            return

        if check_clamd:
            try:
                if self._clamav_service.ping(retry=False):
                    engine, db_version, db_date = self._clamav_service.get_version()
                    self._status.clamd_running = True
                    self._status.engine_version = engine
                    self._status.db_version, self._status.db_date = db_version, db_date
                else:
                    self._status.clamd_running = False
            except Exception:  # noqa: BLE001
                self._status.clamd_running = False
            if self._status.clamd_running:
                self.clamd_status_card.set_value("稼働中", COLOR_OK)
                self.db_version_card.set_value(f"{self._status.db_version} / {self._status.db_date}")
            else:
                self.clamd_status_card.set_value("停止", COLOR_NG)
                self.db_version_card.set_value("-")
            self.refresh_activity()

        if self._realtime_manager is not None:
            self._status.realtime_running = self._realtime_manager.is_running
            self.realtime_status_card.set_value(
                "ON" if self._status.realtime_running else "OFF",
                COLOR_OK if self._status.realtime_running else None,
            )
            self.toggle_realtime_button.setText("監視を停止" if self._status.realtime_running else "監視を開始")

        self._update_hero()
        self.status_refreshed.emit(self._status)

    def _update_hero(self) -> None:
        status = self._status
        if status.clamd_running is None:
            title, detail, color = "状態を確認しています...", "", COLOR_TEXT_MUTED
        elif not status.clamd_running:
            title = "ClamAV エンジン サービスが停止しています"
            detail = "スキャンとリアルタイム監視の検査ができません。[エンジンの操作] から clamd を起動してください。"
            color = COLOR_NG
        elif status.realtime_running:
            title = "保護されています"
            detail = "clamd が稼働中で、リアルタイム監視も有効です。"
            color = None
        else:
            title = "スキャンの準備ができています"
            detail = "clamd が稼働中です。リアルタイム監視はオフです。"
            color = None
        self.hero_title_label.setText(title)
        self.hero_title_label.setStyleSheet(f"color: {color};" if color else "")
        self.hero_detail_label.setText(detail)
        self.hero_detail_label.setVisible(bool(detail))
        set_accent(self.hero_card, status.clamd_running is False)

    def refresh_activity(self) -> None:
        """直近アクティビティ（スキャン・検知・更新・隔離のログ）を新しい順に最大10件表示する"""
        if self._log_reader_service is None:
            return
        # 日をまたいでも直近のアクティビティが消えないよう、過去のファイルも直近の分だけ読む
        since = date.today() - timedelta(days=ACTIVITY_DAYS)
        entries = [
            e for e in self._log_reader_service.read_entries(since=since)
            if e.category in _ACTIVITY_SCREENS and e.level != "DEBUG"
        ][-ACTIVITY_LIMIT:]
        self.activity_table.setRowCount(0)
        for entry in reversed(entries):
            row = self.activity_table.rowCount()
            self.activity_table.insertRow(row)
            values = [entry.datetime.strftime("%Y-%m-%d %H:%M:%S"), entry.category, entry.message]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(entry.message)
                self.activity_table.setItem(row, col, item)

    def _on_activity_clicked(self, row: int, _column: int) -> None:
        item = self.activity_table.item(row, 1)
        if item is not None:
            self._navigate(_ACTIVITY_SCREENS.get(item.text(), "log"))

    def _navigate(self, key: str) -> None:
        if self._on_navigate is not None:
            self._on_navigate(key)

    # -- クイックアクション -----------------------------------------------------
    def _on_scan_now_clicked(self) -> None:
        if self._on_quick_scan is not None:
            self._on_quick_scan()

    def _on_update_now_clicked(self) -> None:
        if self._on_update_now is not None:
            self._on_update_now()

    def _on_toggle_realtime_clicked(self) -> None:
        if self._on_toggle_realtime is not None:
            self._on_toggle_realtime()
        self.refresh_status(check_clamd=False)


def _action_card(icon_name: str, title: str, text: str, buttons) -> QFrame:
    """アイコン＋見出し＋説明＋ボタン列の操作カード"""
    card, layout = make_card()
    header = QHBoxLayout()
    header.setSpacing(10)
    icon_label = QLabel()
    icon_label.setPixmap(icons.pixmap(icon_name, 26))
    title_label = QLabel(title)
    title_label.setObjectName("cardTitle")
    header.addWidget(icon_label)
    header.addWidget(title_label, stretch=1)
    layout.addLayout(header)
    text_label = QLabel(text)
    text_label.setObjectName("cardText")
    text_label.setWordWrap(True)
    layout.addWidget(text_label)
    layout.addStretch(1)
    row = QHBoxLayout()
    for button in buttons:
        row.addWidget(button)
    row.addStretch(1)
    layout.addLayout(row)
    return card

"""
TrayIcon — タスクトレイ常駐アイコン・メニュー

参照: タスクトレイ常駐アプリプロセス設計書5章、画面設計書9章
- メニュー: メイン画面を開く／今すぐスキャン／定義更新／リアルタイム監視ON-OFF／終了
- アイコン状態: 通常／監視中／検知あり(未対応)／エラー（優先順位: エラー > 検知あり > 監視中 > 通常）
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon, QWidget

from core.version import APP_NAME


class TrayIconState(Enum):
    NORMAL = "normal"
    MONITORING = "monitoring"
    DETECTION_PENDING = "detection_pending"
    ERROR = "error"


#: 状態の優先順位（5.2節）: エラー > 検知あり(未対応) > 監視中 > 通常
_STATE_PRIORITY = {
    TrayIconState.ERROR: 3,
    TrayIconState.DETECTION_PENDING: 2,
    TrayIconState.MONITORING: 1,
    TrayIconState.NORMAL: 0,
}

_ICON_FILENAMES = {
    TrayIconState.NORMAL: "tray_normal.png",
    TrayIconState.MONITORING: "tray_monitoring.png",
    TrayIconState.DETECTION_PENDING: "tray_detection.png",
    TrayIconState.ERROR: "tray_error.png",
}

#: アイコン素材未配置時に使うQt標準アイコン。空のQIconだとWindowsではトレイに何も表示されず、
#: ×ボタンでトレイ格納した後にメイン画面へ戻ることも終了することもできなくなる（Windows実機テストで発見）
_FALLBACK_STANDARD_ICONS = {
    TrayIconState.NORMAL: QStyle.StandardPixmap.SP_ComputerIcon,
    TrayIconState.MONITORING: QStyle.StandardPixmap.SP_DialogApplyButton,
    TrayIconState.DETECTION_PENDING: QStyle.StandardPixmap.SP_MessageBoxWarning,
    TrayIconState.ERROR: QStyle.StandardPixmap.SP_MessageBoxCritical,
}


class TrayIcon(QSystemTrayIcon):
    def __init__(self, icons_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icons_dir = icons_dir
        self._states: set[TrayIconState] = {TrayIconState.NORMAL}

        self.setIcon(self._icon_for(TrayIconState.NORMAL))
        self.setToolTip(APP_NAME)

        self._menu = QMenu(parent)
        self.open_action = QAction("メイン画面を開く", self._menu)
        self.scan_now_action = QAction("今すぐスキャン", self._menu)
        self.update_now_action = QAction("定義更新", self._menu)
        self.toggle_realtime_action = QAction("リアルタイム監視 ON/OFF", self._menu)
        self.toggle_realtime_action.setCheckable(True)
        self.quit_action = QAction("終了", self._menu)

        self._menu.addAction(self.open_action)
        self._menu.addAction(self.scan_now_action)
        self._menu.addAction(self.update_now_action)
        self._menu.addAction(self.toggle_realtime_action)
        self._menu.addSeparator()
        self._menu.addAction(self.quit_action)
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)

    def _icon_for(self, state: TrayIconState) -> QIcon:
        path = self._icons_dir / _ICON_FILENAMES[state]
        if path.exists():
            return QIcon(str(path))
        # アイコン素材未配置時はQt標準アイコンで代替する（起動を妨げず、かつトレイに必ず表示する）
        style = QApplication.style()
        icon = style.standardIcon(_FALLBACK_STANDARD_ICONS[state])
        if icon.isNull():
            icon = style.standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        return icon

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # ダブルクリックでメイン画面を開く（4.2節）
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_action.trigger()

    # -- 状態管理（5.2節: 優先順位に基づき最も優先度の高い状態を表示） -------------
    def set_state(self, state: TrayIconState, active: bool) -> None:
        if active:
            self._states.add(state)
        else:
            self._states.discard(state)
        if not self._states:
            self._states.add(TrayIconState.NORMAL)

        current = max(self._states, key=lambda s: _STATE_PRIORITY[s])
        self.setIcon(self._icon_for(current))
        self.toggle_realtime_action.setChecked(TrayIconState.MONITORING in self._states)

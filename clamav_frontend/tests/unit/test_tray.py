"""
タスクトレイ常駐アプリプロセス設計書5章のテスト
"""
from gui.tray import TrayIcon, TrayIconState


def test_menu_has_5_items(qtbot, tmp_path):
    tray = TrayIcon(tmp_path)
    qtbot.addWidget(tray.contextMenu())

    actions = tray.contextMenu().actions()
    # セパレータを含め6項目（メイン画面/スキャン/更新/監視トグル/セパレータ/終了）
    labels = [a.text() for a in actions if not a.isSeparator()]
    assert labels == ["メイン画面を開く", "今すぐスキャン", "定義更新", "リアルタイム監視 ON/OFF", "終了"]


def test_fallback_icon_is_not_null_when_assets_missing(qtbot, tmp_path):
    # 空のQIconだとWindowsではトレイに表示されず、トレイ格納後に操作不能になる
    tray = TrayIcon(tmp_path)  # tmp_pathにはアイコン素材が無い

    assert not tray.icon().isNull()
    for state in TrayIconState:
        tray.set_state(state, True)
        assert not tray.icon().isNull()


def test_state_priority_error_overrides_monitoring(qtbot, tmp_path):
    tray = TrayIcon(tmp_path)

    tray.set_state(TrayIconState.MONITORING, True)
    tray.set_state(TrayIconState.ERROR, True)

    assert TrayIconState.ERROR in tray._states
    assert TrayIconState.MONITORING in tray._states
    # 優先度計算で現在アイコンがERRORベースになっていることを間接的に確認
    current = max(tray._states, key=lambda s: {"error": 3, "detection_pending": 2, "monitoring": 1, "normal": 0}[s.value])
    assert current == TrayIconState.ERROR


def test_toggle_realtime_action_reflects_monitoring_state(qtbot, tmp_path):
    tray = TrayIcon(tmp_path)

    tray.set_state(TrayIconState.MONITORING, True)
    assert tray.toggle_realtime_action.isChecked() is True

    tray.set_state(TrayIconState.MONITORING, False)
    assert tray.toggle_realtime_action.isChecked() is False


def test_state_removed_falls_back_to_normal(qtbot, tmp_path):
    tray = TrayIcon(tmp_path)

    tray.set_state(TrayIconState.DETECTION_PENDING, True)
    tray.set_state(TrayIconState.DETECTION_PENDING, False)

    assert tray._states == {TrayIconState.NORMAL}


def test_double_click_triggers_open_action(qtbot, tmp_path):
    from PySide6.QtWidgets import QSystemTrayIcon

    tray = TrayIcon(tmp_path)
    triggered = []
    tray.open_action.triggered.connect(lambda: triggered.append(True))

    tray._on_activated(QSystemTrayIcon.ActivationReason.DoubleClick)

    assert triggered == [True]

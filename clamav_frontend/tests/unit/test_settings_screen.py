"""
gui/screens/settings.py のサービス層結線テスト
"""
from unittest.mock import MagicMock

from core.clamd_client import check_connection
from core.config_service import ConfigService
from core.exceptions import ClamdConnectionError
from gui.screens.settings import SettingsScreen


def test_settings_screen_loads_config_values(qtbot, tmp_path):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    model = config_service.load()
    model.smtp.host = "smtp.example.com"
    model.smtp.port = 465
    config_service.save(model)

    notification_service = MagicMock()
    screen.bind_services(config_service, notification_service)

    assert screen.smtp_host_edit.text() == "smtp.example.com"
    assert screen.smtp_port_spin.value() == 465


def test_settings_screen_save_persists_changes(qtbot, tmp_path):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    notification_service = MagicMock()
    screen.bind_services(config_service, notification_service)

    screen.smtp_host_edit.setText("smtp.new-host.example.com")
    screen.on_detect_mail_check.setChecked(False)
    screen.save_button.click()

    reloaded = config_service.load()
    assert reloaded.smtp.host == "smtp.new-host.example.com"
    assert reloaded.notification.on_detect_mail is False


def test_freshclam_path_is_loaded_and_saved(qtbot, tmp_path):
    # レビュー指摘: PATHに無いfreshclamを使えるよう、定義更新タブで場所を指定できるようにした
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    screen.bind_services(config_service, MagicMock())
    assert screen.freshclam_path_edit.text() == "freshclam"

    screen.freshclam_path_edit.setText("  C:\\Program Files\\ClamAV\\freshclam.exe  ")
    screen.save_button.click()
    assert config_service.load().update.freshclam_path == "C:\\Program Files\\ClamAV\\freshclam.exe"


def test_empty_freshclam_path_falls_back_to_path_lookup(qtbot, tmp_path):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    screen.bind_services(config_service, MagicMock())

    screen.freshclam_path_edit.setText("   ")
    screen.save_button.click()
    assert config_service.load().update.freshclam_path == "freshclam"
    assert screen.freshclam_path_edit.text() == "freshclam"


def test_freshclam_browse_sets_selected_path(qtbot, tmp_path, monkeypatch):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    monkeypatch.setattr(
        "gui.screens.settings.QFileDialog.getOpenFileName", lambda *a, **k: ("/usr/local/bin/freshclam", "")
    )
    screen.freshclam_browse_button.click()
    assert screen.freshclam_path_edit.text().replace("\\", "/") == "/usr/local/bin/freshclam"


def test_settings_screen_save_sets_smtp_password_via_keyring(qtbot, tmp_path, monkeypatch):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    notification_service = MagicMock()
    screen.bind_services(config_service, notification_service)

    captured = {}
    monkeypatch.setattr(
        "keyring.set_password", lambda service, account, password: captured.update(pw=password)
    )

    screen.smtp_password_edit.setText("hunter2")
    screen.save_button.click()

    assert captured["pw"] == "hunter2"
    assert screen.smtp_password_edit.text() == ""  # 保存後はクリアされる


def test_settings_screen_save_applies_autostart_immediately(qtbot, tmp_path):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    autostart_service = MagicMock()
    autostart_service.is_enabled.return_value = True
    screen.bind_services(config_service, MagicMock(), autostart_service)

    screen.autostart_check.setChecked(False)
    screen.save_button.click()

    autostart_service.disable.assert_called_once()
    assert config_service.load().general.autostart is False

    autostart_service.is_enabled.return_value = False
    screen.autostart_check.setChecked(True)
    screen.save_button.click()

    autostart_service.enable.assert_called_once()


def test_settings_screen_shows_error_when_autostart_apply_fails(qtbot, tmp_path, monkeypatch):
    from core.exceptions import ProcessError

    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    autostart_service = MagicMock()
    autostart_service.is_enabled.return_value = False
    autostart_service.enable.side_effect = ProcessError("自動起動の設定に失敗しました", code="E8002")
    screen.bind_services(config_service, MagicMock(), autostart_service)

    shown = []
    import gui.screens.settings as settings_module

    monkeypatch.setattr(settings_module, "show_error_dialog", lambda *a, **k: shown.append(True))

    screen.autostart_check.setChecked(True)
    screen.save_button.click()

    assert shown == [True]


def test_settings_screen_test_send_calls_notification_service(qtbot, tmp_path, monkeypatch):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    notification_service = MagicMock()
    screen.bind_services(config_service, notification_service)

    import gui.screens.settings as settings_module

    monkeypatch.setattr(settings_module.QMessageBox, "information", lambda *a, **k: None)

    screen.smtp_test_button.click()

    notification_service.send_test_mail.assert_called_once()


def test_settings_screen_test_send_shows_error_on_failure(qtbot, tmp_path, monkeypatch):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    notification_service = MagicMock()
    from core.exceptions import MailSendError

    notification_service.send_test_mail.side_effect = MailSendError(detail="smtp down")
    screen.bind_services(config_service, notification_service)

    shown = []
    import gui.screens.settings as settings_module

    monkeypatch.setattr(settings_module, "show_error_dialog", lambda *a, **k: shown.append(True))

    screen.smtp_test_button.click()

    assert shown == [True]


# -- Phase 11: 保存時の即時反映・E8002時のチェック戻し・トレイ案内 --------------------------
def test_save_calls_on_saved_for_live_apply(qtbot, tmp_path):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    saved = []
    screen.bind_services(config_service, MagicMock(), on_saved=saved.append)

    screen.log_level_combo.setCurrentText("DEBUG")
    screen.update_interval_spin.setValue(8)
    screen.realtime_default_action_combo.setCurrentIndex(1)
    screen.save_button.click()

    assert saved[0].logging.level == "DEBUG"
    assert saved[0].update.interval_hours == 8
    assert saved[0].realtime.default_action == "notify_only"


def test_autostart_failure_reverts_checkbox_and_config(qtbot, tmp_path, monkeypatch):
    from core.exceptions import ProcessError

    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    model = config_service.load()
    model.general.autostart = False
    config_service.save(model)
    autostart_service = MagicMock()
    autostart_service.is_enabled.return_value = False
    autostart_service.enable.side_effect = ProcessError("自動起動の設定に失敗しました", code="E8002")
    screen.bind_services(config_service, MagicMock(), autostart_service)
    import gui.screens.settings as settings_module

    monkeypatch.setattr(settings_module, "show_error_dialog", lambda *a, **k: None)

    screen.autostart_check.setChecked(True)
    screen.save_button.click()

    assert screen.autostart_check.isChecked() is False  # エラーコード一覧E8002: チェックを元に戻す
    assert config_service.load().general.autostart is False


def test_tray_hint_setting_resets_shown_flag(qtbot, tmp_path):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    model = config_service.load()
    model.general.show_tray_hint_shown = True
    config_service.save(model)
    screen.bind_services(config_service, MagicMock())
    assert screen.tray_hint_check.isChecked() is False

    screen.tray_hint_check.setChecked(True)
    screen.save_button.click()

    assert config_service.load().general.show_tray_hint_shown is False


def _bound_screen(qtbot, tmp_path, **kwargs):
    screen = SettingsScreen()
    qtbot.addWidget(screen)
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    screen.bind_services(config_service, MagicMock(), **kwargs)
    return screen, config_service


def test_engine_tab_loads_and_saves_clamd_settings(qtbot, tmp_path):
    # 「ClamAV エンジン」タブ（コードレビュー後に追加）
    screen, config_service = _bound_screen(qtbot, tmp_path)
    assert screen.tcp_host_edit.text() == "127.0.0.1"
    assert screen.tcp_port_spin.value() == 3310
    assert screen.clamd_auto_start_check.isChecked() is True

    screen.connection_type_combo.setCurrentIndex(screen.connection_type_combo.findData("tcp"))
    screen.tcp_host_edit.setText(" 192.168.1.10 ")
    screen.tcp_port_spin.setValue(3311)
    screen.service_name_edit.setText(" ClamAV-Custom ")
    screen.clamd_auto_start_check.setChecked(False)
    screen.clamonacc_path_edit.setText("/opt/clamav/sbin/clamonacc")
    screen.save_button.click()

    saved = config_service.load()
    assert saved.clamd.connection_type == "tcp"
    assert saved.clamd.tcp_host == "192.168.1.10"
    assert saved.clamd.tcp_port == 3311
    assert saved.clamd.service_name == "ClamAV-Custom"
    assert saved.clamd.auto_start is False
    assert saved.realtime.clamonacc_path == "/opt/clamav/sbin/clamonacc"


def test_engine_tab_empty_fields_fall_back_to_defaults(qtbot, tmp_path):
    screen, config_service = _bound_screen(qtbot, tmp_path)
    screen.tcp_host_edit.setText("")
    screen.unix_socket_edit.setText("  ")
    screen.service_name_edit.setText("  ")
    screen.clamonacc_path_edit.setText("")
    screen.save_button.click()

    saved = config_service.load()
    assert saved.clamd.tcp_host == "127.0.0.1"
    assert saved.clamd.unix_socket_path == "/var/run/clamav/clamd.ctl"
    assert saved.clamd.service_name == ""  # 空欄はOSの既定のサービス名
    assert saved.realtime.clamonacc_path == "clamonacc"
    assert screen.tcp_host_edit.text() == "127.0.0.1"


def test_engine_tab_enables_fields_for_selected_connection_type(qtbot, tmp_path):
    screen, _ = _bound_screen(qtbot, tmp_path)
    screen.connection_type_combo.setCurrentIndex(screen.connection_type_combo.findData("tcp"))
    assert screen.tcp_host_edit.isEnabled() and screen.tcp_port_spin.isEnabled()
    assert not screen.unix_socket_edit.isEnabled()


def test_connection_test_uses_entered_values_before_saving(qtbot, tmp_path):
    received = []

    def tester(clamd_config):
        received.append(clamd_config)
        return "ClamAV 1.5.4 / 定義 28157"

    screen, config_service = _bound_screen(qtbot, tmp_path, connection_tester=tester)
    screen.connection_type_combo.setCurrentIndex(screen.connection_type_combo.findData("tcp"))
    screen.tcp_host_edit.setText("192.168.1.10")
    screen.tcp_port_spin.setValue(3311)

    with qtbot.waitSignal(screen._connection_task.finished, timeout=3000):
        screen.connection_test_button.click()

    assert (received[0].connection_type, received[0].tcp_host, received[0].tcp_port) == ("tcp", "192.168.1.10", 3311)
    assert config_service.load().clamd.tcp_port == 3310  # 保存はしない
    assert "接続できました: ClamAV 1.5.4" in screen.connection_test_label.text()
    assert screen.connection_test_button.isEnabled()


def test_connection_test_shows_failure(qtbot, tmp_path):
    def tester(_clamd_config):
        raise ClamdConnectionError(detail="TCP 127.0.0.1:1 に接続できません")

    screen, _ = _bound_screen(qtbot, tmp_path, connection_tester=tester)

    with qtbot.waitSignal(screen._connection_task.finished, timeout=3000):
        screen.connection_test_button.click()

    assert "接続できません: TCP 127.0.0.1:1" in screen.connection_test_label.text()
    assert screen.connection_test_button.isEnabled()


def test_connection_test_with_check_connection_like_app(qtbot, tmp_path, fake_clamd_server):
    # app.pyと同じくconnection_tester=check_connectionで結線し、偽のclamdへ接続する
    fake_clamd_server.response_map["VERSION"] = "ClamAV 1.5.4/28157/Sun Oct 18 08:00:00 2026"
    screen, _ = _bound_screen(qtbot, tmp_path, connection_tester=check_connection)
    screen.connection_type_combo.setCurrentIndex(screen.connection_type_combo.findData("tcp"))
    screen.tcp_host_edit.setText(fake_clamd_server.host)
    screen.tcp_port_spin.setValue(fake_clamd_server.port)

    with qtbot.waitSignal(screen._connection_task.finished, timeout=5000):
        screen.connection_test_button.click()

    assert screen.connection_test_label.text() == "接続できました: ClamAV 1.5.4 / 定義 28157"

def test_reload_on_show_keeps_settings_changed_elsewhere(qtbot, tmp_path):
    # レビュー指摘: 設定画面が起動時の入力値のまま保存し、リアルタイム画面で「通知のみ」にした
    # 検知時アクションを「自動隔離」に戻していた。app.pyは設定画面を開くたびにreload_from_config()を呼ぶ
    screen, config_service = _bound_screen(qtbot, tmp_path)
    config_service.current.realtime.default_action = "notify_only"  # リアルタイム画面での変更（メモリ上）
    config_service.current.general.show_tray_hint_shown = True  # ×ボタンでの案内の表示済み

    screen.reload_from_config()  # 設定画面を開いた
    screen.log_level_combo.setCurrentText("WARNING")  # 別の項目を変えて保存
    screen.save_button.click()

    saved = config_service.load()
    assert saved.realtime.default_action == "notify_only"
    assert saved.general.show_tray_hint_shown is True
    assert saved.logging.level == "WARNING"

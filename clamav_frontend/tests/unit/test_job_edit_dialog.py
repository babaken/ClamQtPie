"""
JobEditDialog単体テスト、およびScheduleScreenの追加/編集フロー
"""
from dataclasses import dataclass
from unittest.mock import MagicMock

from PySide6.QtWidgets import QDialog

from gui.dialogs.job_edit_dialog import JobEditDialog
from gui.screens.schedule import ScheduleScreen


@dataclass
class _FakeSchedule:
    id: int
    name: str
    enabled: bool
    cron: str
    target: str
    scan_type: str
    last_run: str
    next_run: str


def test_dialog_preset_sets_cron_and_disables_edit(qtbot):
    dialog = JobEditDialog()
    qtbot.addWidget(dialog)

    dialog.cron_preset_combo.setCurrentText("カスタム（Cron式を直接入力）")  # 初期値から変更発火させる
    dialog.cron_preset_combo.setCurrentText("毎日 3:00")

    assert dialog.cron_edit.text() == "0 3 * * *"
    assert dialog.cron_edit.isEnabled() is False


def test_new_dialog_has_cron_of_default_preset(qtbot):
    # Phase 11で発見: 新規作成時に既定プリセット「毎日 3:00」が選ばれているのにCron式が空で、
    # そのまま保存するとE4002になっていた
    dialog = JobEditDialog()
    qtbot.addWidget(dialog)

    assert dialog.cron_preset_combo.currentText() == "毎日 3:00"
    assert dialog.cron_edit.text() == "0 3 * * *"


def test_edit_dialog_selects_matching_preset(qtbot):
    dialog = JobEditDialog(
        initial={"name": "w", "scan_type": "update", "target": "", "cron": "0 3 * * 0"}
    )
    qtbot.addWidget(dialog)

    assert dialog.cron_preset_combo.currentText() == "毎週日曜 3:00"
    assert dialog.cron_edit.text() == "0 3 * * 0"
    assert dialog.type_combo.currentText() == "定義更新"


def test_dialog_validation_keeps_dialog_open_and_shows_message(qtbot, tmp_path):
    dialog = JobEditDialog()
    qtbot.addWidget(dialog)

    dialog.accept()  # ジョブ名なし
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "ジョブ名" in dialog.error_label.text()

    dialog.name_edit.setText("daily-scan")
    dialog.accept()  # スキャンで対象パスなし
    assert "スキャン対象" in dialog.error_label.text()

    dialog.target_edit.setText(str(tmp_path / "missing"))
    dialog.accept()
    assert "見つかりません" in dialog.error_label.text()

    dialog.target_edit.setText(str(tmp_path))
    dialog.cron_preset_combo.setCurrentText("カスタム（Cron式を直接入力）")
    dialog.cron_edit.setText("not a cron")
    dialog.accept()
    assert "Cron式" in dialog.error_label.text()

    dialog.cron_edit.setText("0 5 * * *")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_update_job_does_not_require_target(qtbot):
    dialog = JobEditDialog()
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("daily-update")
    dialog.set_scan_type("update")

    assert dialog.validate() == ""


def test_dialog_custom_preset_enables_manual_cron_input(qtbot):
    dialog = JobEditDialog()
    qtbot.addWidget(dialog)

    dialog.cron_preset_combo.setCurrentText("カスタム（Cron式を直接入力）")

    assert dialog.cron_edit.isEnabled() is True


def test_dialog_type_change_disables_target_for_update(qtbot):
    dialog = JobEditDialog()
    qtbot.addWidget(dialog)

    dialog.set_scan_type("update")
    assert dialog.target_edit.isEnabled() is False

    dialog.set_scan_type("scan")
    assert dialog.target_edit.isEnabled() is True


def test_dialog_initial_values_populate_fields(qtbot):
    dialog = JobEditDialog(
        initial={"name": "daily-scan", "scan_type": "scan", "target": "/home", "cron": "0 3 * * *"}
    )
    qtbot.addWidget(dialog)

    assert dialog.name_edit.text() == "daily-scan"
    assert dialog.target_edit.text() == "/home"
    assert dialog.cron_edit.text() == "0 3 * * *"


def test_dialog_result_values(qtbot):
    dialog = JobEditDialog()
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("weekly-update")
    dialog.set_scan_type("update")
    dialog.cron_preset_combo.setCurrentText("毎週日曜 3:00")

    values = dialog.result_values()

    assert values == {
        "name": "weekly-update",
        "scan_type": "update",
        "target": "",
        "cron": "0 3 * * 0",
    }


def test_schedule_screen_add_job_calls_service(qtbot, monkeypatch):
    screen = ScheduleScreen()
    qtbot.addWidget(screen)
    service = MagicMock()
    service.list_all.return_value = []
    screen.bind_services(service)

    fake_dialog = MagicMock()
    fake_dialog.exec.return_value = QDialog.DialogCode.Accepted
    fake_dialog.result_values.return_value = {
        "name": "daily-scan",
        "scan_type": "scan",
        "target": "/home",
        "cron": "0 3 * * *",
    }

    import gui.screens.schedule as schedule_module

    monkeypatch.setattr(schedule_module, "JobEditDialog", lambda *a, **k: fake_dialog)

    screen._on_add_job_clicked()

    service.add_job.assert_called_once_with("daily-scan", "scan", "0 3 * * *", target="/home")


def test_schedule_screen_edit_job_calls_service(qtbot, monkeypatch):
    screen = ScheduleScreen()
    qtbot.addWidget(screen)
    service = MagicMock()
    service.list_all.return_value = [
        _FakeSchedule(1, "daily-scan", True, "0 3 * * *", "/home", "scan", "", "")
    ]
    screen.bind_services(service)
    screen.job_table.selectRow(0)

    fake_dialog = MagicMock()
    fake_dialog.exec.return_value = QDialog.DialogCode.Accepted
    fake_dialog.result_values.return_value = {
        "name": "daily-scan-renamed",
        "scan_type": "scan",
        "target": "/home",
        "cron": "0 4 * * *",
    }

    import gui.screens.schedule as schedule_module

    monkeypatch.setattr(schedule_module, "JobEditDialog", lambda *a, **k: fake_dialog)

    screen._on_edit_job_clicked()

    service.update_job.assert_called_once_with(1, "daily-scan-renamed", "scan", "0 4 * * *", target="/home")


def test_schedule_screen_add_job_cancelled_does_not_call_service(qtbot, monkeypatch):
    screen = ScheduleScreen()
    qtbot.addWidget(screen)
    service = MagicMock()
    service.list_all.return_value = []
    screen.bind_services(service)

    fake_dialog = MagicMock()
    fake_dialog.exec.return_value = QDialog.DialogCode.Rejected

    import gui.screens.schedule as schedule_module

    monkeypatch.setattr(schedule_module, "JobEditDialog", lambda *a, **k: fake_dialog)

    screen._on_add_job_clicked()

    service.add_job.assert_not_called()

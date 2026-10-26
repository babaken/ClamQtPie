"""
gui/screens/schedule.py と gui/screens/update.py のサービス層結線テスト
"""
from dataclasses import dataclass
from unittest.mock import MagicMock

from gui.screens.schedule import ScheduleScreen
from gui.screens.update import UpdateScreen


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


@dataclass
class _FakeHistory:
    id: int
    executed_at: str
    result: str
    before_version: str
    after_version: str


def test_schedule_screen_loads_jobs(qtbot):
    screen = ScheduleScreen()
    qtbot.addWidget(screen)
    service = MagicMock()
    service.list_all.return_value = [
        _FakeSchedule(1, "daily-scan", True, "0 3 * * *", "/home", "scan", "", "2026-07-18T03:00:00")
    ]

    screen.bind_services(service)

    assert screen.job_table.rowCount() == 1
    assert screen.job_table.item(0, 0).text() == "daily-scan"


def test_schedule_screen_delete_requires_confirmation(qtbot, monkeypatch):
    screen = ScheduleScreen()
    qtbot.addWidget(screen)
    service = MagicMock()
    service.list_all.return_value = [
        _FakeSchedule(1, "daily-scan", True, "0 3 * * *", "/home", "scan", "", "")
    ]
    screen.bind_services(service)
    screen.job_table.selectRow(0)

    import gui.screens.schedule as schedule_module

    monkeypatch.setattr(schedule_module, "show_confirm_dialog", lambda *a, **k: False)
    screen.delete_job_button.click()

    service.remove_job.assert_not_called()


def test_schedule_screen_run_now_calls_service(qtbot):
    screen = ScheduleScreen()
    qtbot.addWidget(screen)
    service = MagicMock()
    service.list_all.return_value = [
        _FakeSchedule(1, "daily-scan", True, "0 3 * * *", "/home", "scan", "", "")
    ]
    screen.bind_services(service)
    screen.job_table.selectRow(0)

    screen.run_now_button.click()

    service.run_now.assert_called_once_with(1)


def test_update_screen_loads_history(qtbot):
    screen = UpdateScreen()
    qtbot.addWidget(screen)
    update_service = MagicMock()
    update_service.list_history.return_value = [
        _FakeHistory(1, "2026-07-18T03:00:00", "success", "27100", "27200")
    ]
    dispatcher = MagicMock()
    clamav_service = MagicMock()
    clamav_service.get_version.return_value = ("ClamAV 1.3.1", "27200", "Mon Jan 6 2026")

    screen.bind_services(update_service, dispatcher, clamav_service)

    assert screen.history_table.rowCount() == 1
    assert screen.history_table.item(0, 1).text() == "成功"
    assert "27200" in screen.version_label.text()


def test_update_screen_button_starts_dispatcher(qtbot):
    screen = UpdateScreen()
    qtbot.addWidget(screen)
    update_service = MagicMock()
    update_service.list_history.return_value = []
    dispatcher = MagicMock()
    dispatcher.is_running = False
    clamav_service = MagicMock()
    clamav_service.get_version.return_value = ("ClamAV 1.3.1", "27200", "date")

    screen.bind_services(update_service, dispatcher, clamav_service)
    screen.update_now_button.click()

    dispatcher.start.assert_called_once()
    assert screen.update_now_button.isEnabled() is False


def test_update_screen_log_line_appends_to_log_area(qtbot):
    screen = UpdateScreen()
    qtbot.addWidget(screen)
    update_service = MagicMock()
    update_service.list_history.return_value = []
    dispatcher = MagicMock()
    clamav_service = MagicMock()
    clamav_service.get_version.return_value = ("ClamAV 1.3.1", "27200", "date")
    screen.bind_services(update_service, dispatcher, clamav_service)

    screen._on_log_line("Downloading main.cvd")

    assert "Downloading main.cvd" in screen.log_area.toPlainText()

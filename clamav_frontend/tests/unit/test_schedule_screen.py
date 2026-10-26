"""
スケジュール画面のPhase 11追加分（人間可読表示・有効/無効・次回実行日時・バックグラウンド実行）のテスト
"""
import threading

import pytest

from core.scheduler_service import SchedulerService
from db.database import DatabaseService
from gui.screens.schedule import ScheduleScreen, describe_cron, describe_last_run


@pytest.mark.parametrize(
    "cron, expected",
    [
        ("0 3 * * *", "毎日 3:00"),
        ("30 21 * * 1", "毎週月曜 21:30"),
        ("0 3 * * 0", "毎週日曜 3:00"),
        ("5 4 15 * *", "毎月15日 4:05"),
        ("*/10 * * * *", "*/10 * * * *"),  # 該当しない形はそのまま
    ],
)
def test_describe_cron(cron, expected):
    assert describe_cron(cron) == expected


def test_describe_last_run():
    assert describe_last_run("") == "未実行"
    assert describe_last_run("2026-10-19T03:00:00+00:00 (検知あり 2件)").endswith("検知あり 2件")
    assert describe_last_run("2026-10-19T03:00:00+00:00 (失敗)").endswith("失敗")
    assert describe_last_run("2026-10-19T03:00:00+00:00").endswith("成功")


@pytest.fixture
def env(qtbot, tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    release = threading.Event()
    calls = []

    def run_scan(target, name):
        calls.append(target)
        release.wait(timeout=5)

    service = SchedulerService(db, run_scan, lambda: None)
    service.start()
    screen = ScheduleScreen()
    qtbot.addWidget(screen)
    shown = []
    screen.bind_services(service, on_show_log=shown.append)
    yield screen, service, release, calls, shown, tmp_path
    release.set()
    service.shutdown()
    db.close()


def test_list_shows_readable_values_and_next_run(env):
    screen, service, _, _, _, tmp_path = env
    service.add_job("日次スキャン", "scan", "0 3 * * *", target=str(tmp_path))
    screen.reload()

    assert screen.job_table.item(0, 1).text() == "スキャン"
    assert screen.job_table.item(0, 2).text() == "毎日 3:00"
    assert screen.job_table.item(0, 4).text() != "-"  # APSchedulerの次回実行日時
    assert screen.job_table.item(0, 5).text() == "未実行"


def test_toggle_enabled_clears_next_run(env):
    screen, service, _, _, _, tmp_path = env
    service.add_job("日次スキャン", "scan", "0 3 * * *", target=str(tmp_path))
    screen.reload()
    screen.job_table.selectRow(0)

    screen.toggle_job_button.click()

    assert screen.job_table.item(0, 3).text() == "無効"
    assert screen.job_table.item(0, 4).text() == "-"


def test_run_now_runs_in_background_without_blocking(env, qtbot):
    screen, service, release, calls, _, tmp_path = env
    service.add_job("日次スキャン", "scan", "0 3 * * *", target=str(tmp_path))
    screen.reload()
    screen.job_table.selectRow(0)

    screen.run_now_button.click()  # スキャンが終わるまで待たずに戻る
    assert screen.job_table.item(0, 5).text() == "実行中..."
    qtbot.waitUntil(lambda: calls == [str(tmp_path)], timeout=3000)

    release.set()
    qtbot.waitUntil(lambda: screen.job_table.item(0, 5).text().endswith("成功"), timeout=3000)


def test_double_click_last_run_shows_log_for_job(env):
    screen, service, _, _, shown, tmp_path = env
    service.add_job("日次スキャン", "scan", "0 3 * * *", target=str(tmp_path))
    screen.reload()

    screen._on_cell_double_clicked(0, 5)

    assert shown == ["日次スキャン"]

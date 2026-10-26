"""
テスト仕様書3.4節: SC-U01〜U04 に対応
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.exceptions import JobExecutionError, JobValidationError
from core.scheduler_service import SchedulerService, cron_trigger
from db.database import DatabaseService


@pytest.fixture
def scheduler_service(tmp_path):
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()

    scan_calls = []
    update_calls = []

    def run_scan(target, job_name=None):
        scan_calls.append(target)

    def run_update():
        update_calls.append(True)

    service = SchedulerService(db_service, run_scan, run_update)
    service.scan_calls = scan_calls
    service.update_calls = update_calls
    yield service
    service.shutdown()
    db_service.close()


def test_duplicate_job_name_raises_validation_error(scheduler_service):
    # SC-U01
    scheduler_service.add_job("daily-scan", "scan", "0 3 * * *", target="/home")

    with pytest.raises(JobValidationError) as exc_info:
        scheduler_service.add_job("daily-scan", "scan", "0 4 * * *", target="/tmp")

    assert exc_info.value.code == "E4001"


def test_invalid_cron_raises_validation_error(scheduler_service):
    # SC-U02
    with pytest.raises(JobValidationError) as exc_info:
        scheduler_service.add_job("bad-cron", "scan", "not a cron", target="/home")

    assert exc_info.value.code == "E4002"


def test_run_now_executes_immediately_and_updates_last_run(scheduler_service):
    # SC-U03
    record = scheduler_service.add_job("manual-scan", "scan", "0 3 * * *", target="/home")

    scheduler_service.run_now(record.id)

    assert scheduler_service.scan_calls == ["/home"]
    updated = [r for r in scheduler_service.list_all() if r.id == record.id][0]
    assert updated.last_run != ""


def test_job_execution_error_updates_last_run_with_failure_marker(scheduler_service):
    # SC-U04
    def failing_scan(target, job_name=None):
        raise RuntimeError("scan failed")

    scheduler_service._run_scan = failing_scan
    record = scheduler_service.add_job("failing-scan", "scan", "0 3 * * *", target="/home")

    with pytest.raises(JobExecutionError):
        scheduler_service.run_now(record.id)

    updated = [r for r in scheduler_service.list_all() if r.id == record.id][0]
    assert "失敗" in updated.last_run


def _block_scan_until_released(scheduler_service):
    """run_scanを「呼ばれたら解放されるまで待つ」実装に差し替え、実行中状態を作る"""
    import threading

    started = threading.Event()
    release = threading.Event()

    def blocking_scan(target, job_name=None):
        scheduler_service.scan_calls.append(target)
        started.set()
        release.wait(timeout=5)

    scheduler_service._run_scan = blocking_scan
    return started, release


def test_run_now_while_same_job_running_raises_already_running(scheduler_service):
    # 重複実行防止: 実行中の同一ジョブを「今すぐ実行」するとE4004
    import threading

    from core.exceptions import JobAlreadyRunningError

    started, release = _block_scan_until_released(scheduler_service)
    record = scheduler_service.add_job("long-scan", "scan", "0 3 * * *", target="/home")

    first = threading.Thread(target=scheduler_service.run_now, args=(record.id,))
    first.start()
    assert started.wait(timeout=5)

    with pytest.raises(JobAlreadyRunningError) as exc_info:
        scheduler_service.run_now(record.id)
    assert exc_info.value.code == "E4004"

    release.set()
    first.join(timeout=5)
    assert scheduler_service.scan_calls == ["/home"]  # 2回目は実行されていない

    # 実行完了後は再度実行できる
    release.set()
    scheduler_service.run_now(record.id)
    assert scheduler_service.scan_calls == ["/home", "/home"]


def test_scheduled_fire_while_same_job_running_is_skipped_silently(scheduler_service):
    # スケジュール発火側（raise_if_running=False）は例外を出さずスキップする
    import threading

    started, release = _block_scan_until_released(scheduler_service)
    record = scheduler_service.add_job("long-scan", "scan", "0 3 * * *", target="/home")

    first = threading.Thread(target=scheduler_service.run_now, args=(record.id,))
    first.start()
    assert started.wait(timeout=5)

    scheduler_service._execute(record)  # スケジュール発火と同じ呼び出し方

    release.set()
    first.join(timeout=5)
    assert scheduler_service.scan_calls == ["/home"]


def test_different_jobs_can_run_concurrently(scheduler_service):
    import threading

    started, release = _block_scan_until_released(scheduler_service)
    long_job = scheduler_service.add_job("long-scan", "scan", "0 3 * * *", target="/home")
    update_job = scheduler_service.add_job("daily-update", "update", "0 4 * * *")

    first = threading.Thread(target=scheduler_service.run_now, args=(long_job.id,))
    first.start()
    assert started.wait(timeout=5)

    scheduler_service.run_now(update_job.id)  # 別ジョブは排他されない
    assert scheduler_service.update_calls == [True]

    release.set()
    first.join(timeout=5)


def test_running_flag_is_cleared_after_failure(scheduler_service):
    def failing_scan(target, job_name=None):
        raise RuntimeError("scan failed")

    scheduler_service._run_scan = failing_scan
    record = scheduler_service.add_job("failing-scan", "scan", "0 3 * * *", target="/home")

    for _ in range(2):  # 失敗後も実行中扱いが残らず、E4004ではなくE4003になる
        with pytest.raises(JobExecutionError):
            scheduler_service.run_now(record.id)


def test_update_job_type_calls_run_update(scheduler_service):
    record = scheduler_service.add_job("daily-update", "update", "0 4 * * *")

    scheduler_service.run_now(record.id)

    assert scheduler_service.update_calls == [True]


def test_remove_job_deletes_from_db(scheduler_service):
    record = scheduler_service.add_job("to-remove", "scan", "0 3 * * *", target="/home")

    scheduler_service.remove_job(record.id)

    assert scheduler_service.list_all() == []


def test_scheduled_job_runs_via_apscheduler(scheduler_service, qtbot):
    # 結合的な確認: APScheduler経由での実行（1秒後の1回限りcron相当をシミュレート）
    import time

    scheduler_service.start()
    scheduler_service.add_job("near-future", "scan", "* * * * *", target="/home")

    # 実際のcron発火を待つのは非現実的なため、_scheduler内にジョブが登録されたことのみ確認
    job = scheduler_service._scheduler.get_job(
        f"schedule_{scheduler_service.list_all()[0].id}"
    )
    assert job is not None


def test_set_auto_update_adds_and_removes_interval_job(scheduler_service):
    scheduler_service.start()

    scheduler_service.set_auto_update(lambda: None, True, 6)
    job = scheduler_service._scheduler.get_job("auto_update")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 6 * 3600

    scheduler_service.set_auto_update(lambda: None, False, 6)
    assert scheduler_service._scheduler.get_job("auto_update") is None


def _next_fire(cron, after):
    return cron_trigger(cron).get_next_fire_time(None, after)


@pytest.mark.parametrize(
    "cron, weekday",
    [
        ("0 3 * * 0", 6),  # 日曜（datetime.weekday: 月=0…日=6）
        ("0 3 * * 7", 6),  # 7も日曜
        ("0 3 * * 1", 0),  # 月曜
        ("0 3 * * 6", 5),  # 土曜
        ("0 3 * * sun", 6),
    ],
)
def test_cron_trigger_uses_standard_weekday_numbers(cron, weekday):
    # レビュー指摘: APSchedulerのfrom_crontabは0を月曜とするため、「毎週日曜 3:00」が月曜に実行されていた
    after = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)  # 金曜
    assert _next_fire(cron, after).weekday() == weekday


def test_cron_trigger_weekday_ranges_and_steps():
    after = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)  # 金曜
    # 平日（1-5）: 金曜12時の次は月曜
    assert _next_fire("0 3 * * 1-5", after).weekday() == 0
    # 週末（6,0）: 次は土曜
    assert _next_fire("0 3 * * 6,0", after).weekday() == 5
    # 5-7（金〜日）: 次は土曜
    assert _next_fire("0 3 * * 5-7", after).weekday() == 5
    # 0-6 は毎日
    assert _next_fire("0 3 * * 0-6", after).day == 24


@pytest.mark.parametrize("cron", ["0 3 * * 8", "0 3 * * 5-1", "0 3 * * x", "0 3 * *", "0 3 * * */0"])
def test_cron_trigger_rejects_invalid_weekday(cron):
    with pytest.raises(ValueError):
        cron_trigger(cron)


def test_weekly_sunday_preset_job_is_scheduled_on_sunday(scheduler_service):
    scheduler_service.start()
    scheduler_service.add_job("weekly", "scan", "0 3 * * 0", target="/home")
    job = scheduler_service._scheduler.get_job(f"schedule_{scheduler_service.list_all()[0].id}")
    assert job.next_run_time.weekday() == 6


def test_auto_update_first_run_is_based_on_last_update(scheduler_service):
    # レビュー指摘: 初回が登録から interval 後だったため、起動から4時間以内に終了すると一度も更新されなかった
    scheduler_service.start()
    now = datetime.now(timezone.utc)

    scheduler_service.set_auto_update(lambda: None, True, 4, last_run=now - timedelta(hours=1))
    next_run = scheduler_service._scheduler.get_job("auto_update").next_run_time
    assert abs((next_run - (now + timedelta(hours=3))).total_seconds()) < 60

    scheduler_service.set_auto_update(lambda: None, False, 4)
    scheduler_service.set_auto_update(lambda: None, True, 4, last_run=now - timedelta(days=2))
    next_run = scheduler_service._scheduler.get_job("auto_update").next_run_time
    assert next_run - now < timedelta(minutes=5)  # 期限切れなら起動の少し後に実行する


def test_auto_update_without_history_runs_soon(scheduler_service):
    scheduler_service.start()
    scheduler_service.set_auto_update(lambda: None, True, 4, last_run=None)
    next_run = scheduler_service._scheduler.get_job("auto_update").next_run_time
    assert next_run - datetime.now(timezone.utc) < timedelta(minutes=5)


def test_auto_update_reregistration_with_same_interval_keeps_next_run(scheduler_service):
    # 自動更新と関係のない設定の保存で、次回の実行時刻がやり直しにならない
    scheduler_service.start()
    now = datetime.now(timezone.utc)
    scheduler_service.set_auto_update(lambda: None, True, 4, last_run=now - timedelta(hours=1))
    first = scheduler_service._scheduler.get_job("auto_update").next_run_time

    scheduler_service.set_auto_update(lambda: None, True, 4, last_run=now)
    assert scheduler_service._scheduler.get_job("auto_update").next_run_time == first

    scheduler_service.set_auto_update(lambda: None, True, 6, last_run=now - timedelta(hours=1))
    changed = scheduler_service._scheduler.get_job("auto_update")
    assert changed.trigger.interval.total_seconds() == 6 * 3600
    assert changed.next_run_time != first

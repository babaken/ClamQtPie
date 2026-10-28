"""
スケジュールスキャンの検知時処理（core/scheduled_scan.py）と最終実行結果のテスト

Phase 11で発見した不具合: スケジュールスキャンが検知結果を捨てており、隔離・通知・
最終実行結果（検知あり）への反映が無かった。
"""
from unittest.mock import MagicMock

import pytest

from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig, RealtimeConfig
from core.exceptions import ClamdConnectionError
from core.exclude_service import ExcludeService
from core.quarantine_service import QuarantineService
from core.scheduled_scan import run_scheduled_scan
from core.scheduler_service import SchedulerService
from db.database import DatabaseService
from tests.conftest import FAKE_INFECTED_CONTENT


@pytest.fixture
def env(tmp_path, fake_clamd_server):
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    target = tmp_path / "target"
    target.mkdir()
    infected = target / "infected.txt"
    infected.write_text(FAKE_INFECTED_CONTENT)
    fake_clamd_server.response_map["SCAN"] = f"{infected}: Eicar-Test-Signature FOUND"
    clamav_service = ClamAVService(
        ClamdConfig(connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port)
    )
    yield {
        "db_service": db_service,
        "target": target,
        "infected": infected,
        "clamav_service": clamav_service,
        "exclude_service": ExcludeService(db_service),
        "quarantine_service": QuarantineService(db_service, tmp_path / "quarantine"),
        "notification_service": MagicMock(),
    }
    db_service.close()


def _run(env, action="quarantine", target=None, clamav_service=None):
    # app.pyの_run_scheduled_scan()と同じ呼び出し方
    return run_scheduled_scan(
        str(target if target is not None else env["target"]),
        "日次スキャン",
        clamav_service or env["clamav_service"],
        env["exclude_service"],
        env["quarantine_service"],
        env["notification_service"],
        RealtimeConfig(default_action=action),
    )


def test_detection_is_quarantined_and_notified_when_action_is_quarantine(env):
    result = _run(env, action="quarantine")

    assert result.detected == 1
    assert result.detections == [(str(env["infected"]), "Eicar-Test-Signature")]
    assert not env["infected"].exists()
    assert len(env["quarantine_service"].list_all()) == 1
    env["notification_service"].notify_detection.assert_called_once_with(
        str(env["infected"]), "Eicar-Test-Signature", quarantined=True
    )


def test_detection_is_only_notified_when_action_is_notify_only(env):
    result = _run(env, action="notify_only")

    assert result.detected == 1
    assert env["infected"].exists()
    assert env["quarantine_service"].list_all() == []
    env["notification_service"].notify_detection.assert_called_once_with(
        str(env["infected"]), "Eicar-Test-Signature", quarantined=False
    )


def test_missing_target_raises(env, tmp_path):
    with pytest.raises(FileNotFoundError):
        _run(env, target=tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        _run(env, target="")


def test_clamd_unreachable_raises_connection_error(env):
    unreachable = ClamAVService(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1))
    with pytest.raises(ClamdConnectionError):
        _run(env, clamav_service=unreachable)
    env["notification_service"].notify_detection.assert_not_called()


# -- 最終実行結果（画面設計書4章: 成功／失敗／検知あり） ----------------------------
def _scheduler(env, run_scan=None, run_update=None, on_job_fail=None):
    return SchedulerService(
        env["db_service"],
        run_scan or (lambda target, name: None),
        run_update or (lambda: None),
        on_job_fail=on_job_fail,
    )


def test_last_run_records_detection_count(env):
    scheduler = _scheduler(env, run_scan=lambda target, name: _run(env))
    record = scheduler.add_job("日次スキャン", "scan", "0 3 * * *", target=str(env["target"]))

    scheduler.run_now(record.id)

    last_run = scheduler.list_all()[0].last_run
    assert last_run.endswith("(検知あり 1件)")


def test_last_run_records_update_failure_without_job_fail_notification(env):
    on_job_fail = MagicMock()
    failure = MagicMock(result="failure")
    scheduler = _scheduler(env, run_update=lambda: failure, on_job_fail=on_job_fail)
    record = scheduler.add_job("日次更新", "update", "0 4 * * *")

    scheduler.run_now(record.id)

    assert scheduler.list_all()[0].last_run.endswith("(失敗)")
    on_job_fail.assert_not_called()  # 定義更新失敗の通知はrun_update側で送信済み（二重通知防止）


def test_last_run_without_detection_has_no_note(env, fake_clamd_server):
    fake_clamd_server.response_map["SCAN"] = f"{env['infected']}: OK"
    scheduler = _scheduler(env, run_scan=lambda target, name: _run(env))
    record = scheduler.add_job("日次スキャン", "scan", "0 3 * * *", target=str(env["target"]))

    scheduler.run_now(record.id)

    assert "(" not in scheduler.list_all()[0].last_run


def test_last_run_records_skipped_update(env):
    skipped = MagicMock(result="skipped", detected=0)
    scheduler = _scheduler(env, run_update=lambda: skipped)
    record = scheduler.add_job("日次更新", "update", "0 4 * * *")

    scheduler.run_now(record.id)

    assert scheduler.list_all()[0].last_run.endswith("(スキップ（OSのサービスが更新）)")


def test_only_scanned_file_is_quarantined_when_response_names_another_file(env, fake_clamd_server, tmp_path):
    # 隔離するのは検査したファイルだけ。応答に別のファイルのパスが書かれていても、そのファイルには触れない
    bystander = tmp_path / "bystander.txt"
    bystander.write_text("keep")
    fake_clamd_server.response_map["SCAN"] = f"{bystander}: Eicar-Test-Signature FOUND"

    result = _run(env, action="quarantine")

    assert result.detections == [(str(env["infected"]), "Eicar-Test-Signature")]
    assert bystander.exists()
    assert [row.original_path for row in env["quarantine_service"].list_all()] == [str(env["infected"])]


def test_multiline_response_quarantines_nothing(env, fake_clamd_server, tmp_path):
    # 応答が複数の結果になった場合はERRORとして扱い、どのファイルも隔離しない
    bystander = tmp_path / "bystander.txt"
    bystander.write_text("keep")
    fake_clamd_server.response_map["SCAN"] = f"{env['infected']}\n{bystander}: Eicar-Test-Signature FOUND"

    result = _run(env, action="quarantine")

    assert result.detected == 0
    assert result.errors == 1
    assert bystander.exists()
    assert env["infected"].exists()
    assert env["quarantine_service"].list_all() == []


def test_limits_exceeded_file_is_not_quarantined(env, fake_clamd_server):
    # 上限を超えたという報告（検知ではない）で、大きいだけのファイルを隔離しない
    fake_clamd_server.response_map["SCAN"] = f"{env['infected']}: Heuristics.Limits.Exceeded.MaxScanSize FOUND"

    result = _run(env, action="quarantine")

    assert result.detected == 0
    assert result.errors == 1
    assert env["infected"].exists()
    assert env["quarantine_service"].list_all() == []


def test_last_run_records_unscanned_file_count(env, fake_clamd_server):
    fake_clamd_server.response_map["SCAN"] = f"{env['infected']}: Heuristics.Limits.Exceeded.MaxFileSize FOUND"
    scheduler = _scheduler(env, run_scan=lambda target, name: _run(env))
    record = scheduler.add_job("日次スキャン", "scan", "0 3 * * *", target=str(env["target"]))

    scheduler.run_now(record.id)

    assert scheduler.list_all()[0].last_run.endswith("(未検査 1件)")

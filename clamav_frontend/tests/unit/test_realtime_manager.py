"""
テスト仕様書3.2節: RT-U01〜U06 に対応
"""
import time

import pytest

from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig, RealtimeConfig
from core.exclude_service import ExcludeService
from core.quarantine_service import QuarantineService
from core.realtime_manager import RealtimeManager
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService


def _build_manager(
    tmp_path, server, extra_exclude_dirs=None, **config_overrides
) -> tuple[RealtimeManager, DatabaseService]:
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()

    clamd_config = ClamdConfig(connection_type="tcp", tcp_host=server.host, tcp_port=server.port)
    clamav_service = ClamAVService(clamd_config)
    watch_target_service = WatchTargetService(db_service)
    exclude_service = ExcludeService(db_service)
    quarantine_service = QuarantineService(db_service, tmp_path / "quarantine")

    defaults = dict(windows_debounce_sec=0.2, max_pending_timers=3, scan_worker_threads=1)
    defaults.update(config_overrides)
    realtime_config = RealtimeConfig(**defaults)

    manager = RealtimeManager(
        clamav_service,
        watch_target_service,
        exclude_service,
        quarantine_service,
        realtime_config,
        extra_exclude_dirs=extra_exclude_dirs,
    )
    return manager, db_service, watch_target_service


def test_debounce_collapses_rapid_events_into_single_scan(fake_clamd_server, tmp_path, qtbot):
    # RT-U01
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    manager, db_service, watch_target_service = _build_manager(tmp_path, fake_clamd_server)

    scan_calls = []
    original_scan_file = manager._clamav_service.scan_file

    def spy_scan_file(path, **kwargs):
        scan_calls.append(path)
        return original_scan_file(path)

    manager._clamav_service.scan_file = spy_scan_file

    watch_target_service.add(str(tmp_path))
    assert manager.start() is True

    target_file = tmp_path / "a.txt"
    target_file.write_text("1")
    time.sleep(0.05)
    target_file.write_text("12")  # 連続変更（デバウンス期間内）

    time.sleep(0.6)  # デバウンス(0.2s)後に発火するまで待つ

    manager.stop()
    db_service.close()

    assert len(scan_calls) == 1  # 2回の変更が1回のスキャンにまとまる


def test_excluded_path_is_not_queued(fake_clamd_server, tmp_path, qtbot):
    # RT-U02
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    manager, db_service, watch_target_service = _build_manager(tmp_path, fake_clamd_server)

    exclude_service = manager._exclude_service
    exclude_service.add("extension", ".log")

    scan_calls = []
    original_scan_file = manager._clamav_service.scan_file

    def spy_scan_file(path, **kwargs):
        scan_calls.append(path)
        return original_scan_file(path)

    manager._clamav_service.scan_file = spy_scan_file

    watch_target_service.add(str(tmp_path))
    manager.start()

    (tmp_path / "excluded.log").write_text("x")
    time.sleep(0.6)

    manager.stop()
    db_service.close()

    assert scan_calls == []


def test_directory_events_are_ignored(fake_clamd_server, tmp_path, qtbot):
    # RT-U03（サブディレクトリ作成自体はスキャン対象にしない）
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    manager, db_service, watch_target_service = _build_manager(tmp_path, fake_clamd_server)

    scan_calls = []
    original_scan_file = manager._clamav_service.scan_file

    def spy_scan_file(path, **kwargs):
        scan_calls.append(path)
        return original_scan_file(path)

    manager._clamav_service.scan_file = spy_scan_file

    watch_target_service.add(str(tmp_path))
    manager.start()

    (tmp_path / "subdir").mkdir()
    time.sleep(0.6)

    manager.stop()
    db_service.close()

    assert scan_calls == []


def test_max_pending_timers_evicts_oldest(fake_clamd_server, tmp_path, qtbot):
    # RT-U04
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    manager, db_service, watch_target_service = _build_manager(
        tmp_path, fake_clamd_server, windows_debounce_sec=5.0, max_pending_timers=2
    )

    scanned_paths = []
    original_scan_file = manager._clamav_service.scan_file

    def spy_scan_file(path, **kwargs):
        scanned_paths.append(path)
        return original_scan_file(path)

    manager._clamav_service.scan_file = spy_scan_file

    watch_target_service.add(str(tmp_path))
    manager.start()

    paths = []
    for i in range(3):
        p = tmp_path / f"f{i}.txt"
        p.write_text(str(i))
        paths.append(str(p))
        time.sleep(0.05)

    # 3件目追加（max_pending_timers=2超過）で1件目（最古）が即時発火されるはず
    time.sleep(0.5)

    manager.stop()
    db_service.close()

    assert paths[0] in scanned_paths  # 最古のタイマーは5秒を待たず即時発火済み


def test_lock_error_is_retried_once(fake_clamd_server, tmp_path, qtbot):
    # RT-U05
    call_count = {"n": 0}

    def dynamic_scan(path, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            from core.clamd_client import ScanResult

            return [ScanResult(path=path, status="ERROR", detail="Access denied")]
        from core.clamd_client import ScanResult

        return [ScanResult(path=path, status="OK")]

    manager, db_service, watch_target_service = _build_manager(
        tmp_path, fake_clamd_server, lock_retry_wait_sec=0.05
    )
    manager._clamav_service.scan_file = dynamic_scan
    locked = tmp_path / "locked.txt"
    locked.write_text("x")

    manager._scan_one(str(locked))

    assert call_count["n"] == 2
    db_service.close()


def test_start_with_no_targets_returns_false(fake_clamd_server, tmp_path, qtbot):
    # RT-U06
    manager, db_service, _ = _build_manager(tmp_path, fake_clamd_server)

    result = manager.start()

    assert result is False
    assert manager.is_running is False
    db_service.close()


@pytest.fixture
def app_log():
    """アプリのロガー（propagate=Falseのためcaplogでは拾えない）の記録を集める"""
    import logging

    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    app_logger = logging.getLogger("clamav_frontend")
    app_logger.addHandler(handler)
    yield records
    app_logger.removeHandler(handler)


def _spy_scans(manager):
    scanned = []
    original_scan_file = manager._clamav_service.scan_file

    def spy_scan_file(path, **kwargs):
        scanned.append(path)
        return original_scan_file(path)

    manager._clamav_service.scan_file = spy_scan_file
    return scanned


def test_renamed_file_is_scanned_at_destination(fake_clamd_server, tmp_path, qtbot):
    # Phase 11改訂: 一時ファイルに書いてから名前を変える保存方式（ブラウザのダウンロード等）。
    # 以前は移動イベントを無視しており、完成したファイルを検査していなかった（Linux実機で発見）
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    manager, db_service, watch_target_service = _build_manager(tmp_path, fake_clamd_server)
    scanned = _spy_scans(manager)
    watch_target_service.add(str(tmp_path))
    manager.start()

    part = tmp_path / "download.zip.part"
    part.write_text("x")
    final = tmp_path / "download.zip"
    part.rename(final)
    time.sleep(0.8)

    manager.stop()
    db_service.close()

    assert str(final) in scanned


def test_moved_in_directory_files_are_scanned(fake_clamd_server, tmp_path, qtbot):
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    watched = tmp_path / "watched"
    watched.mkdir()
    manager, db_service, watch_target_service = _build_manager(tmp_path, fake_clamd_server)
    scanned = _spy_scans(manager)
    watch_target_service.add(str(watched))
    manager.start()

    staging = watched / "extracting.tmp"
    (staging / "inner").mkdir(parents=True)
    (staging / "inner" / "a.exe").write_text("x")
    time.sleep(0.05)
    staging.rename(watched / "extracted")  # 展開先フォルダの名前を最後に変える
    time.sleep(0.8)

    manager.stop()
    db_service.close()

    assert str(watched / "extracted" / "inner" / "a.exe") in scanned


def test_moved_to_excluded_path_is_not_scanned(fake_clamd_server, tmp_path, qtbot):
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    manager, db_service, watch_target_service = _build_manager(tmp_path, fake_clamd_server)
    manager._exclude_service.add("extension", ".log")
    scanned = _spy_scans(manager)
    watch_target_service.add(str(tmp_path))
    manager.start()

    tmp_file = tmp_path / "app.tmp"
    tmp_file.write_text("x")
    tmp_file.rename(tmp_path / "app.log")
    time.sleep(0.8)

    manager.stop()
    db_service.close()

    assert str(tmp_path / "app.log") not in scanned


def test_vanished_file_is_not_warned_or_retried(fake_clamd_server, tmp_path, app_log):
    # 一時ファイルがスキャン前に削除・移動された場合はE2004の警告にしない
    from core.clamd_client import ScanResult

    calls = []

    def scan(path, **kwargs):
        calls.append(path)
        return [ScanResult(path=path, status="ERROR", detail="Permission denied.")]

    manager, db_service, _ = _build_manager(tmp_path, fake_clamd_server, lock_retry_wait_sec=0.05)
    manager._clamav_service.scan_file = scan

    manager._scan_one(str(tmp_path / "gone.tmp"))

    assert calls == [str(tmp_path / "gone.tmp")]  # リトライしない
    assert [r for r in app_log if r.levelname == "WARNING"] == []
    db_service.close()


def test_existing_unscannable_file_is_still_warned(fake_clamd_server, tmp_path, app_log):
    from core.clamd_client import ScanResult

    manager, db_service, _ = _build_manager(tmp_path, fake_clamd_server, lock_retry_wait_sec=0.05)
    manager._clamav_service.scan_file = lambda path, **kw: [ScanResult(path=path, status="ERROR", detail="Access denied")]
    target = tmp_path / "locked.bin"
    target.write_text("x")

    manager._scan_one(str(target))

    warnings = [r for r in app_log if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert getattr(warnings[0], "error_code", "") == "E2004"
    db_service.close()


def _quarantine_loop_scenario(fake_clamd_server, tmp_path, extra_exclude_dirs):
    """監視対象（tmp_path）の中に隔離フォルダ（tmp_path/quarantine）がある状態で、感染ファイルを隔離する"""
    fake_clamd_server.response_map["SCAN"] = "file: Eicar-Test-Signature FOUND"  # 何を検査しても検知
    manager, db_service, watch_target_service = _build_manager(
        tmp_path, fake_clamd_server, extra_exclude_dirs=extra_exclude_dirs
    )
    scanned = _spy_scans(manager)
    watch_target_service.add(str(tmp_path))
    manager.start()

    (tmp_path / "download.bin").write_text("x")
    time.sleep(2.0)  # デバウンス(0.2s)を何周もできる時間

    manager.stop()
    records = manager._quarantine_service.list_all()
    db_service.close()
    return scanned, records


def test_app_dirs_are_not_scanned_so_quarantine_does_not_loop(fake_clamd_server, tmp_path, qtbot):
    # Linux実機で発見: 隔離フォルダへの移動（移動イベント）が検査され、隔離したファイルが再び検知されて
    # 隔離が無限に繰り返された（約2時間で7,500回）。アプリ自身のフォルダは検査しない
    quarantine_dir = tmp_path / "quarantine"
    scanned, records = _quarantine_loop_scenario(fake_clamd_server, tmp_path, [quarantine_dir])

    assert len(records) == 1
    assert [p for p in scanned if str(quarantine_dir) in p] == []


def test_quarantine_does_not_loop_even_without_app_dir_exclusion(fake_clamd_server, tmp_path, qtbot):
    # 二重の防止策: 除外の指定が無くても、隔離フォルダ内のファイルは隔離し直さない
    scanned, records = _quarantine_loop_scenario(fake_clamd_server, tmp_path, None)

    assert len(records) == 1

def test_clamd_connection_error_is_reported_once_until_recovered():
    # レビュー指摘: clamd停止中はファイルのイベントごとにscan_errorが出て、エラーダイアログが積み重なっていた
    from unittest.mock import MagicMock

    from core.clamd_client import ScanResult
    from core.exceptions import ClamdConnectionError

    clamav = MagicMock()
    manager = RealtimeManager(clamav, MagicMock(), MagicMock(), MagicMock(), RealtimeConfig())
    errors = []
    manager.scan_error.connect(errors.append)

    clamav.scan_file.side_effect = ClamdConnectionError(detail="down")
    for i in range(5):
        manager._scan_one(f"/tmp/file{i}")
    assert len(errors) == 1

    # 検査できたら解除され、次に接続できなくなったときはまた1回通知する
    clamav.scan_file.side_effect = None
    clamav.scan_file.return_value = [ScanResult(path="/tmp/ok", status="OK")]
    manager._scan_one("/tmp/ok")
    clamav.scan_file.side_effect = ClamdConnectionError(detail="down again")
    manager._scan_one("/tmp/file5")
    manager._scan_one("/tmp/file6")
    assert len(errors) == 2

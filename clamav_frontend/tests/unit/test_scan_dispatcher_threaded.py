"""
ScanDispatcher が実際にQThread上でScanWorkerを実行し、GUIスレッドをブロックせずに
シグナルが届くことを確認する（画面設計書2章の進捗表示の土台）。
"""
from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from core.scan_dispatcher import ScanDispatcher


def _service_for(server) -> ClamAVService:
    config = ClamdConfig(connection_type="tcp", tcp_host=server.host, tcp_port=server.port)
    return ClamAVService(config)


def test_scan_dispatcher_runs_in_background_thread(fake_clamd_server, tmp_path, qtbot):
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello")
    fake_clamd_server.response_map["SCAN"] = f"{file_path}: OK"

    service = _service_for(fake_clamd_server)
    dispatcher = ScanDispatcher(service)

    with qtbot.waitSignal(dispatcher.finished, timeout=5000) as blocker:
        dispatcher.start([str(file_path)])
        assert dispatcher.is_running is True

    summary = blocker.args[0]
    assert summary["scanned"] == 1
    assert dispatcher.is_running is False


def test_scan_dispatcher_double_start_is_ignored(fake_clamd_server, tmp_path, qtbot):
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello")
    fake_clamd_server.response_map["SCAN"] = f"{file_path}: OK"
    fake_clamd_server.delay_sec = 0.3  # 完了前に2回目のstart()を呼ぶ猶予を作る

    service = _service_for(fake_clamd_server)
    dispatcher = ScanDispatcher(service)

    with qtbot.waitSignal(dispatcher.finished, timeout=5000):
        dispatcher.start([str(file_path)])
        first_thread = dispatcher._thread
        dispatcher.start([str(file_path)])  # 排他制御により無視される
        assert dispatcher._thread is first_thread


def test_shutdown_stops_running_scan_and_waits_for_thread(fake_clamd_server, tmp_path, qtbot):
    # サービスの終了時（イベントループが止まった後）にスキャンが動いていても、スレッドを止めて待ち、
    # 動いたままの QThread を破棄しない（プロセスが異常終了しない）
    import time

    file_path = tmp_path / "a.txt"
    file_path.write_text("hello")
    fake_clamd_server.response_map["SCAN"] = f"{file_path}: OK"
    fake_clamd_server.delay_sec = 30  # clamd の応答を待っている途中で止める

    dispatcher = ScanDispatcher(_service_for(fake_clamd_server))
    dispatcher.start([str(file_path)])
    qtbot.waitUntil(lambda: dispatcher._worker is not None and dispatcher._worker._current is not None, timeout=5000)
    thread = dispatcher._thread

    started = time.monotonic()
    assert dispatcher.shutdown() is True
    assert time.monotonic() - started < 5
    assert not thread.isRunning()
    assert dispatcher.is_running is False


def test_shutdown_without_scan_does_nothing():
    from types import SimpleNamespace

    assert ScanDispatcher(SimpleNamespace()).shutdown() is True

"""
ScanDispatcher / ScanWorker のユニットテスト

ScanWorker.run()は同期的に直接呼び出して検証する（QThread経由の結合的な
挙動はtest_scan_dispatcher_threaded.pyで別途qtbotのwaitSignalを使い確認する）。
"""
from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from core.exclude_service import ExcludeService
from core.scan_dispatcher import ScanWorker
from db.database import DatabaseService


def _service_for(server, **overrides) -> ClamAVService:
    config = ClamdConfig(connection_type="tcp", tcp_host=server.host, tcp_port=server.port, **overrides)
    return ClamAVService(config)


def test_scan_single_file_emits_expected_signals(fake_clamd_server, tmp_path, qtbot):
    file_path = tmp_path / "a.txt"
    file_path.write_text("hello")
    fake_clamd_server.response_map["SCAN"] = f"{file_path}: OK"

    service = _service_for(fake_clamd_server)
    worker = ScanWorker(service, [str(file_path)])

    progress_calls = []
    file_scanned_calls = []
    finished_calls = []

    worker.progress.connect(lambda a, b, c: progress_calls.append((a, b, c)))
    worker.file_scanned.connect(lambda a, b, c, d: file_scanned_calls.append((a, b, c, d)))
    worker.finished.connect(lambda summary: finished_calls.append(summary))

    worker.run()

    # 先頭は対象列挙中の不確定表示（total=0）、続いてファイルごとの進捗
    assert progress_calls == [(0, 0, "スキャン対象のファイルを確認しています..."), (0, 1, str(file_path))]
    assert len(file_scanned_calls) == 1
    assert file_scanned_calls[0][1] == "OK"
    assert finished_calls[0] == {"scanned": 1, "detected": 0, "errors": 0, "cancelled": False}


def test_scan_directory_enumerates_recursively(fake_clamd_server, tmp_path, qtbot):
    (tmp_path / "sub").mkdir()
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "sub" / "b.txt"
    f1.write_text("1")
    f2.write_text("2")

    # サーバは常にOKを返す想定（応答内のパスの厳密一致はクライアント側では
    # 要求しない実装のため、固定応答でも成立する）
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    service = _service_for(fake_clamd_server)
    worker = ScanWorker(service, [str(tmp_path)], include_subfolders=True)

    file_scanned_calls = []
    worker.file_scanned.connect(lambda *args: file_scanned_calls.append(args))

    worker.run()

    assert len(file_scanned_calls) == 2


def test_scan_excludes_files_via_exclude_service(fake_clamd_server, tmp_path):
    scan_dir = tmp_path / "scan_target"
    scan_dir.mkdir()
    f1 = scan_dir / "a.txt"
    f2 = scan_dir / "b.log"
    f1.write_text("1")
    f2.write_text("2")

    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    db_service = DatabaseService(tmp_path / "dbdir" / "app.db")
    db_service.connect()
    exclude_service = ExcludeService(db_service)
    exclude_service.add("extension", ".log")

    service = _service_for(fake_clamd_server)
    worker = ScanWorker(
        service,
        [str(scan_dir)],
        apply_exclude=True,
        exclude_service=exclude_service,
    )

    file_scanned_calls = []
    worker.file_scanned.connect(lambda *args: file_scanned_calls.append(args))
    worker.run()

    assert len(file_scanned_calls) == 1  # b.logは除外される
    db_service.close()


def test_scan_stop_flag_halts_before_completion(fake_clamd_server, tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(str(i))

    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    service = _service_for(fake_clamd_server)
    worker = ScanWorker(service, [str(tmp_path)])

    scanned_paths = []

    def on_progress(scanned, total, path):
        scanned_paths.append(path)
        if len(scanned_paths) >= 2:
            worker.stop()

    worker.progress.connect(on_progress)

    finished_summary = {}
    worker.finished.connect(lambda s: finished_summary.update(s))

    worker.run()

    assert finished_summary["cancelled"] is True
    assert finished_summary["scanned"] < 5


def test_skip_dirs_are_not_enumerated(fake_clamd_server, tmp_path):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc" / "status").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    worker = ScanWorker(_service_for(fake_clamd_server), [str(tmp_path)], skip_dirs=(str(tmp_path / "proc"),))
    scanned = []
    worker.progress.connect(lambda a, b, path: scanned.append(path))
    worker.run()

    assert str(tmp_path / "keep.txt") in scanned
    assert str(tmp_path / "proc" / "status") not in scanned


def test_clamd_unreachable_aborts_scan_with_single_error(tmp_path):
    from core.clamav_service import ClamAVService
    from core.config_model import ClamdConfig

    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text(str(i))
    service = ClamAVService(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1))
    worker = ScanWorker(service, [str(tmp_path)])
    errors, summary = [], {}
    worker.error.connect(errors.append)
    worker.finished.connect(summary.update)

    worker.run()

    assert len(errors) == 1  # ファイル数分のエラーダイアログを出さない
    assert summary["cancelled"] is True


# -- 応答待ち・待機中の表示と停止操作による打ち切り（プロトコル設計書4章・5章） ------------------
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402

from PySide6.QtCore import Qt as _Qt  # noqa: E402


def _run_in_thread(worker):
    events, summary = [], {}
    direct = _Qt.ConnectionType.DirectConnection
    worker.activity.connect(lambda s, p, e: events.append((s, p, e)), direct)
    worker.finished.connect(summary.update, direct)
    errors = []
    worker.error.connect(errors.append, direct)
    thread = _threading.Thread(target=worker.run, daemon=True)
    thread.start()
    return thread, events, summary, errors


def test_stalled_scan_is_reported_and_stop_aborts_immediately(fake_clamd_server, tmp_path, monkeypatch):
    target = tmp_path / "big.iso"
    target.write_text("x")
    fake_clamd_server.response_map["SCAN"] = f"{target}: OK"
    fake_clamd_server.delay_sec = 30  # clamdが応答しない状態
    monkeypatch.setattr(ScanWorker, "STALL_NOTICE_SEC", 0.3)
    monkeypatch.setattr(ScanWorker, "_MONITOR_INTERVAL_SEC", 0.1)
    worker = ScanWorker(_service_for(fake_clamd_server), [str(target)])

    thread, events, summary, errors = _run_in_thread(worker)
    deadline = _time.monotonic() + 5
    while not any(e[0] == "stalled" for e in events) and _time.monotonic() < deadline:
        _time.sleep(0.05)
    stalled = [e for e in events if e[0] == "stalled"]
    assert stalled and stalled[0][1] == str(target)

    started = _time.monotonic()
    worker.stop()  # 応答待ちの通信を打ち切ってすぐに止まる（30秒待たない）
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert _time.monotonic() - started < 3
    assert summary["cancelled"] is True
    assert errors == []  # 停止操作による打ち切りはエラーとして表示しない


def test_queued_while_connection_slots_are_busy(fake_clamd_server, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    fake_clamd_server.response_map["SCAN"] = f"{target}: OK"
    service = _service_for(fake_clamd_server, max_concurrent_connections=2)  # オンデマンドは1接続まで
    service._ondemand_semaphore.acquire()  # 他のオンデマンドスキャンが使用中
    worker = ScanWorker(service, [str(target)])

    thread, events, summary, _ = _run_in_thread(worker)
    deadline = _time.monotonic() + 5
    while not events and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert events[0][0] == "queued"

    service._ondemand_semaphore.release()
    thread.join(timeout=5)
    assert summary["scanned"] == 1
    assert events[-1][0] == "scanning"  # 待機が終わったら通常表示に戻す


def test_stop_while_queued(fake_clamd_server, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    service = _service_for(fake_clamd_server, max_concurrent_connections=2)
    service._ondemand_semaphore.acquire()
    worker = ScanWorker(service, [str(target)])

    thread, events, summary, _ = _run_in_thread(worker)
    deadline = _time.monotonic() + 5
    while not events and _time.monotonic() < deadline:
        _time.sleep(0.05)
    worker.stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert summary["cancelled"] is True
    service._ondemand_semaphore.release()

"""
テスト仕様書3.1節: CLM-I02、およびClamAVService固有の
リトライ・自動再起動ロジックのテスト（clamdソケット通信プロトコル設計書4章）
"""
import threading
import time

import pytest

from core.clamav_service import FILE_VANISHED_DETAIL, ClamAVService
from core.config_model import ClamdConfig


def _service_for(server, **overrides) -> ClamAVService:
    config = ClamdConfig(connection_type="tcp", tcp_host=server.host, tcp_port=server.port, **overrides)
    return ClamAVService(config)


def test_scan_file_falls_back_to_instream_on_permission_denied(fake_clamd_server, tmp_path):
    # clamd（clamavユーザー）が読めないファイルはSCANがPermission deniedになるため、
    # アプリが自分で開いてINSTREAMで送り直す（Linux実機テストで発見）
    target = tmp_path / "private.txt"
    target.write_text("content")
    fake_clamd_server.response_map["SCAN"] = f"{target}: lstat() failed: Permission denied. ERROR"
    fake_clamd_server.response_map["INSTREAM"] = "stream: Eicar-Test-Signature FOUND"

    results = _service_for(fake_clamd_server).scan_file(str(target))

    assert len(results) == 1
    assert results[0].path == str(target)
    assert results[0].status == "FOUND"
    assert results[0].detail == "Eicar-Test-Signature"


def test_scan_file_keeps_error_when_app_cannot_read_either(fake_clamd_server, tmp_path, monkeypatch):
    target = tmp_path / "unreadable.txt"
    target.write_text("x")
    fake_clamd_server.response_map["SCAN"] = f"{target}: lstat() failed: Permission denied. ERROR"

    def deny(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("core.clamav_service.open", deny, raising=False)
    results = _service_for(fake_clamd_server).scan_file(str(target))

    assert results[0].status == "ERROR"
    assert "Permission denied" in results[0].detail


def test_scan_file_reports_vanished_file_instead_of_permission_denied(fake_clamd_server, tmp_path):
    # 一時ファイルがスキャン前に消えた場合、clamdは読めないためPermission deniedを返すが、
    # 原因が分かるよう「ファイルが見つかりません」とする（Linux実機で発見）
    missing = tmp_path / "missing.txt"
    fake_clamd_server.response_map["SCAN"] = f"{missing}: lstat() failed: Permission denied. ERROR"

    results = _service_for(fake_clamd_server).scan_file(str(missing))

    assert results[0].status == "ERROR"
    assert results[0].detail == FILE_VANISHED_DETAIL


def test_scan_file_stream_size_over_returns_error_result(fake_clamd_server, tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    fake_clamd_server.response_map["SCAN"] = f"{big}: Access denied. ERROR"

    results = _service_for(fake_clamd_server, max_stream_size_mb=1).scan_file(str(big))

    assert results[0].status == "ERROR"
    assert results[0].path == str(big)


def test_scan_file_other_errors_do_not_fall_back(fake_clamd_server, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    fake_clamd_server.response_map["SCAN"] = f"{target}: Can't open file or directory ERROR"
    fake_clamd_server.response_map["INSTREAM"] = "stream: Should-Not-Be-Used FOUND"

    results = _service_for(fake_clamd_server).scan_file(str(target))

    assert results[0].status == "ERROR"


def test_ping_success_no_retry_needed(fake_clamd_server):
    fake_clamd_server.response_map["PING"] = "PONG"
    service = _service_for(fake_clamd_server)

    assert service.ping() is True


def test_ping_failure_returns_false_after_retries():
    # 接続不可ポートに対して3回リトライ後Falseを返すこと（4章）
    config = ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1, connect_timeout_sec=0.2)
    service = ClamAVService(config)

    start = time.time()
    result = service.ping()
    elapsed = time.time() - start

    assert result is False
    # 3回のリトライで最低2秒（1秒間隔×2回分）はかかるはず
    assert elapsed >= 1.5


def test_ensure_available_calls_restart_callback_once_on_failure():
    restart_called = threading.Event()

    def fake_restart() -> bool:
        restart_called.set()
        return True

    config = ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1, connect_timeout_sec=0.1)
    service = ClamAVService(config, restart_clamd=fake_restart)

    # time.sleep(5)による遅延を避けるため、内部スリープをモンキーパッチ
    import core.clamav_service as svc_module

    original_sleep = svc_module.time.sleep
    svc_module.time.sleep = lambda _: None
    try:
        result = service.ensure_available()
    finally:
        svc_module.time.sleep = original_sleep

    assert restart_called.is_set()
    assert result is False  # 再起動後もダミーポートには繋がらないためFalse


def test_max_concurrent_connections_limits_parallel_scans(fake_clamd_server):
    # CLM-I02: 同時接続数がmax_concurrent_connectionsを超えないことを確認
    fake_clamd_server.response_map["SCAN"] = "/tmp/x.txt: OK"
    fake_clamd_server.delay_sec = 0.2  # 各スキャンに意図的な遅延を入れる

    service = _service_for(fake_clamd_server, max_concurrent_connections=2)

    concurrent_count = 0
    max_observed = 0
    lock = threading.Lock()

    def scan_task():
        nonlocal concurrent_count, max_observed
        with service._semaphore:
            with lock:
                concurrent_count += 1
                max_observed = max(max_observed, concurrent_count)
            time.sleep(0.05)
            with lock:
                concurrent_count -= 1

    threads = [threading.Thread(target=scan_task) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_observed <= 2


def test_realtime_scan_has_reserved_slot_over_ondemand(fake_clamd_server, monkeypatch):
    # プロトコル設計書5章: キュー超過時はリアルタイム検知を優先（オンデマンドは上限-1まで）
    service = _service_for(fake_clamd_server, max_concurrent_connections=2)
    release = threading.Event()
    running = {"ondemand": 0, "max_ondemand": 0}
    lock = threading.Lock()

    def fake_scan(path):
        if path == "realtime":
            return ["realtime-done"]
        with lock:
            running["ondemand"] += 1
            running["max_ondemand"] = max(running["max_ondemand"], running["ondemand"])
        release.wait(timeout=5)
        with lock:
            running["ondemand"] -= 1
        return []

    monkeypatch.setattr(service, "_scan_file", fake_scan)
    ondemand = [threading.Thread(target=service.scan_file, args=(f"/f{i}",)) for i in range(3)]
    for t in ondemand:
        t.start()
    time.sleep(0.2)

    # オンデマンドが詰まっていてもリアルタイムは待たずに実行できる
    assert service.scan_file("realtime", priority=True) == ["realtime-done"]
    assert running["max_ondemand"] == 1

    release.set()
    for t in ondemand:
        t.join(timeout=5)


# -- FILDES（ファイルディスクリプタ渡し、UNIXソケット接続） -----------------------------
from tests.conftest import FAKE_INFECTED_CONTENT, requires_fdpass  # noqa: E402


def _unix_service_for(server) -> ClamAVService:
    return ClamAVService(ClamdConfig(connection_type="unix", unix_socket_path=server.unix_socket_path))


@requires_fdpass
def test_unix_socket_scans_by_fdpass_without_scan_command(fake_clamd_unix_server, tmp_path):
    # SCANはclamd（clamavユーザー）が読めないファイルで権限エラーになり、INSTREAMで送り直していた
    # （通信2回・clamd.logに警告）。UNIXソケットではアプリが開いたファイルをFILDESで渡す
    infected = tmp_path / "eicar.txt"
    infected.write_text(FAKE_INFECTED_CONTENT)
    clean = tmp_path / "clean.txt"
    clean.write_text("hello")
    service = _unix_service_for(fake_clamd_unix_server)

    found = service.scan_file(str(infected))
    ok = service.scan_file(str(clean))

    assert (found[0].path, found[0].status, found[0].detail) == (str(infected), "FOUND", "Eicar-Test-Signature")
    assert (ok[0].path, ok[0].status) == (str(clean), "OK")
    assert fake_clamd_unix_server.commands == ["FILDES", "FILDES"]  # SCAN・INSTREAMを送らない


@requires_fdpass
def test_fdpass_reports_vanished_file(fake_clamd_unix_server, tmp_path):
    missing = tmp_path / "gone.tmp"

    results = _unix_service_for(fake_clamd_unix_server).scan_file(str(missing))

    assert results[0].detail == FILE_VANISHED_DETAIL
    assert fake_clamd_unix_server.commands == []


@requires_fdpass
def test_fdpass_falls_back_to_scan_when_app_cannot_open(fake_clamd_unix_server, tmp_path, monkeypatch):
    # アプリ自身が開けないファイル（root所有等）は、clamdなら読める場合があるためSCANを送る
    target = tmp_path / "root-only.txt"
    target.write_text("x")
    fake_clamd_unix_server.response_map["SCAN"] = f"{target}: OK"
    real_open = __import__("os").open

    def deny(path, *args, **kwargs):
        if str(path) == str(target):
            raise PermissionError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("core.clamav_service.os.open", deny)
    results = _unix_service_for(fake_clamd_unix_server).scan_file(str(target))

    assert results[0].status == "OK"
    assert fake_clamd_unix_server.commands == ["SCAN"]


@requires_fdpass
def test_fdpass_unsupported_switches_to_scan_for_later_files(fake_clamd_unix_server, tmp_path):
    fake_clamd_unix_server.response_map["FILDES"] = "UNKNOWN COMMAND"
    target = tmp_path / "a.txt"
    target.write_text("x")
    fake_clamd_unix_server.response_map["SCAN"] = f"{target}: OK"
    service = _unix_service_for(fake_clamd_unix_server)

    first = service.scan_file(str(target))
    second = service.scan_file(str(target))

    assert first[0].status == second[0].status == "OK"
    assert fake_clamd_unix_server.commands == ["FILDES", "SCAN", "SCAN"]  # 非対応と分かったら以降は試さない


@requires_fdpass
def test_fdpass_empty_response_is_connection_error_and_keeps_fdpass(fake_clamd_unix_server, tmp_path):
    # レビュー指摘: スキャンの停止操作やclamdの再起動で応答が空になると「非対応」と判断し、
    # アプリを再起動するまでFILDESを使わなくなっていた（SCANの送り直しで停止もすぐに効かなかった）
    from core.exceptions import ClamdConnectionError

    target = tmp_path / "a.txt"
    target.write_text("x")
    service = _unix_service_for(fake_clamd_unix_server)

    fake_clamd_unix_server.response_map["FILDES"] = ""  # 応答なしで閉じる
    with pytest.raises(ClamdConnectionError):
        service.scan_file(str(target))
    assert fake_clamd_unix_server.commands == ["FILDES"]  # SCANで送り直さない

    del fake_clamd_unix_server.response_map["FILDES"]
    assert service.scan_file(str(target))[0].status == "OK"
    assert fake_clamd_unix_server.commands == ["FILDES", "FILDES"]  # 次のファイルもFILDESで検査する


@requires_fdpass
def test_fdpass_is_not_used_for_special_files(fake_clamd_unix_server, tmp_path):
    # 名前付きパイプは開くと書き手を待って止まるため、FILDESを使わずSCAN（clamdの設定に従う）
    import os

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    fake_clamd_unix_server.response_map["SCAN"] = f"{fifo}: OK"

    results = _unix_service_for(fake_clamd_unix_server).scan_file(str(fifo))

    assert results[0].status == "OK"
    assert fake_clamd_unix_server.commands == ["SCAN"]


def test_tcp_connection_does_not_use_fdpass(fake_clamd_server, tmp_path):
    # ファイルディスクリプタ渡しはUNIXソケットでしか使えない（Windows・TCP接続は従来どおりSCAN）
    target = tmp_path / "a.txt"
    target.write_text("x")
    fake_clamd_server.response_map["SCAN"] = f"{target}: OK"

    results = _service_for(fake_clamd_server).scan_file(str(target))

    assert results[0].status == "OK"
    assert fake_clamd_server.commands == ["SCAN"]

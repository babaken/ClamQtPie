"""
テスト仕様書3.1節: CLM-U01〜U09 に対応
"""
import io

import pytest

from core.clamd_client import ClamdClient, check_connection
from core.config_model import ClamdConfig
from core.exceptions import ClamdConnectionError, ClamdStreamSizeError


def _client_for(server) -> ClamdClient:
    config = ClamdConfig(connection_type="tcp", tcp_host=server.host, tcp_port=server.port)
    return ClamdClient(config)


def test_ping_pong_returns_true(fake_clamd_server):
    # CLM-U01
    fake_clamd_server.response_map["PING"] = "PONG"
    client = _client_for(fake_clamd_server)

    assert client.ping() is True


def test_ping_connection_refused_raises_connection_error():
    # CLM-U02
    config = ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1)  # 未使用ポート想定
    client = ClamdClient(config)

    with pytest.raises(ClamdConnectionError):
        client.ping()


def test_scan_response_ok_found_error_parsed_correctly(fake_clamd_server):
    # CLM-U03
    fake_clamd_server.response_map["SCAN"] = "/tmp/clean.txt: OK"
    client = _client_for(fake_clamd_server)

    results = client.scan_path("/tmp/clean.txt")
    assert results[0].path == "/tmp/clean.txt"
    assert results[0].status == "OK"

    fake_clamd_server.response_map["SCAN"] = "/tmp/eicar.txt: Eicar-Test-Signature FOUND"
    results = client.scan_path("/tmp/eicar.txt")
    assert results[0].status == "FOUND"
    assert results[0].detail == "Eicar-Test-Signature"

    fake_clamd_server.response_map["SCAN"] = "/tmp/locked.txt: Access denied ERROR"
    results = client.scan_path("/tmp/locked.txt")
    assert results[0].status == "ERROR"
    assert results[0].detail == "Access denied"


def test_multiscan_multiple_lines_parsed(fake_clamd_server):
    # CLM-U04
    fake_clamd_server.response_map["MULTISCAN"] = (
        "/tmp/a.txt: OK\n/tmp/b.txt: Eicar-Test-Signature FOUND\n/tmp/c.txt: OK"
    )
    client = _client_for(fake_clamd_server)

    results = client.scan_path("/tmp", multi=True)
    assert len(results) == 3
    assert [r.status for r in results] == ["OK", "FOUND", "OK"]


def test_version_response_parsed(fake_clamd_server):
    # CLM-U05
    fake_clamd_server.response_map["VERSION"] = "ClamAV 1.3.1/27200/Mon Jan  6 08:00:00 2026"
    client = _client_for(fake_clamd_server)

    engine, db_version, db_date = client.get_version()
    assert engine == "ClamAV 1.3.1"
    assert db_version == "27200"
    assert "2026" in db_date


def test_read_timeout_raises_timeout_error(fake_clamd_server):
    # CLM-U06
    fake_clamd_server.delay_sec = 0.5
    config = ClamdConfig(
        connection_type="tcp",
        tcp_host=fake_clamd_server.host,
        tcp_port=fake_clamd_server.port,
        read_timeout_sec=0.1,
    )
    client = ClamdClient(config)

    from core.exceptions import ClamdTimeoutError

    with pytest.raises(ClamdTimeoutError):
        client.ping()


def test_scan_is_not_cut_off_by_read_timeout(fake_clamd_server):
    # プロトコル設計書4章: スキャン系の読み取りタイムアウトは無制限（大容量ファイルの検査で打ち切らない）
    fake_clamd_server.delay_sec = 0.5
    fake_clamd_server.response_map["SCAN"] = "/big.iso: OK"
    config = ClamdConfig(
        connection_type="tcp",
        tcp_host=fake_clamd_server.host,
        tcp_port=fake_clamd_server.port,
        read_timeout_sec=0.1,
    )

    results = ClamdClient(config).scan_path("/big.iso")

    assert results[0].status == "OK"


def test_instream_sends_length_prefixed_chunks(fake_clamd_server):
    # CLM-U07
    fake_clamd_server.response_map["INSTREAM"] = "stream: OK"
    client = _client_for(fake_clamd_server)

    result = client.scan_stream(io.BytesIO(b"hello world"))
    assert result.status == "OK"


def test_instream_size_exceeded_raises_stream_size_error(fake_clamd_server):
    # CLM-U08
    config = ClamdConfig(
        connection_type="tcp",
        tcp_host=fake_clamd_server.host,
        tcp_port=fake_clamd_server.port,
        max_stream_size_mb=0,  # 実質0バイトで即超過させる
        instream_chunk_size=8,
    )
    client = ClamdClient(config)

    with pytest.raises(ClamdStreamSizeError):
        client.scan_stream(io.BytesIO(b"x" * 100))


def test_malformed_scan_response_does_not_raise(fake_clamd_server):
    # CLM-U09: 応答フォーマット不正時は例外を送出せず ERROR として返す
    fake_clamd_server.response_map["SCAN"] = "malformed line without expected suffix"
    client = _client_for(fake_clamd_server)

    results = client.scan_path("/tmp/whatever")
    assert results[0].status == "ERROR"
    assert results[0].detail == "応答フォーマット不正"


def test_abort_thread_interrupts_waiting_scan(fake_clamd_server):
    import threading
    import time

    fake_clamd_server.delay_sec = 30
    fake_clamd_server.response_map["SCAN"] = "/big.iso: OK"
    client = ClamdClient(
        ClamdConfig(connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port)
    )
    outcome = {}

    def scan():
        outcome["ident"] = threading.get_ident()
        try:
            outcome["result"] = client.scan_path("/big.iso")
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=scan, daemon=True)
    thread.start()
    time.sleep(0.3)

    assert client.abort_thread(outcome["ident"]) is True
    thread.join(timeout=3)
    assert not thread.is_alive()  # 30秒の応答待ちを打ち切れた
    assert client.abort_thread(outcome["ident"]) is False  # 通信中でなければ何もしない


def test_check_connection_returns_engine_and_db_version(fake_clamd_server):
    # 設定画面「接続テスト」（app.pyがconnection_testerとして渡す）
    fake_clamd_server.response_map["VERSION"] = "ClamAV 1.5.4/28157/Sun Oct 18 08:00:00 2026"
    config = ClamdConfig(connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port)

    assert check_connection(config) == "ClamAV 1.5.4 / 定義 28157"


def test_check_connection_raises_when_unreachable():
    config = ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1)  # 未使用ポート想定

    with pytest.raises(ClamdConnectionError):
        check_connection(config)


def test_check_connection_raises_when_ping_is_not_pong(fake_clamd_server):
    fake_clamd_server.response_map["PING"] = "UNKNOWN COMMAND"
    config = ClamdConfig(connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port)

    with pytest.raises(ClamdConnectionError):
        check_connection(config)

"""接続先が本物の clamd かを確かめる処理のテスト（サービス化・Web API設計書 9章。段階 12-3d）"""
from __future__ import annotations

import logging
import os
import socket
import sys
from types import SimpleNamespace

import pytest

from core import peer_verify
from core.clamd_client import ClamdClient
from core.config_model import ClamdConfig
from core.exceptions import ClamdConnectionError
from core.peer_verify import ClamdEndpointVerifier, VerifyError, check_unix_socket, tcp_listener_uids

_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def _row(local: str, state: str, uid: int) -> str:
    return f"   0: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000 {uid:5d}        0 1 1\n"


def test_listeners_on_loopback_are_read_from_proc(tmp_path):
    (tmp_path / "tcp").write_text(_HEADER
                                  + _row("0100007F:0CEE", "0A", 110)      # 127.0.0.1:3310
                                  + _row("00000000:0CEE", "0A", 1000)     # 0.0.0.0:3310（ループバックにも届く）
                                  + _row("0101A8C0:0CEE", "0A", 2000)     # 192.168.1.1:3310（ループバックには届かない）
                                  + _row("0100007F:0CEF", "0A", 3000)     # 別のポート
                                  + _row("0100007F:0CEE", "01", 4000))    # 待ち受けではない
    (tmp_path / "tcp6").write_text(_HEADER
                                   + _row("00000000000000000000000001000000:0CEE", "0A", 5000)   # ::1
                                   + _row("0000000000000000FFFF00000100007F:0CEE", "0A", 6000))  # ::ffff:127.0.0.1
    assert sorted(tcp_listener_uids(3310, tmp_path)) == [110, 1000, 5000, 6000]
    assert tcp_listener_uids(3310, tmp_path / "missing") == []


def _verifier(monkeypatch, system, **config):
    monkeypatch.setattr(peer_verify, "trusted_ids", lambda user: ({0, 110}, {0, 120}))
    clock = [0.0]
    verifier = ClamdEndpointVerifier(ClamdConfig(**config), system=system, clock=lambda: clock[0])
    return verifier, clock


def test_linux_tcp_listener_must_belong_to_clamd(monkeypatch):
    verifier, _ = _verifier(monkeypatch, "Linux", connection_type="tcp")
    monkeypatch.setattr(peer_verify, "tcp_listener_uids", lambda port: [110])
    verifier.check()
    assert verifier.verified is True

    verifier.invalidate()
    monkeypatch.setattr(peer_verify, "tcp_listener_uids", lambda port: [110, 1000])  # ほかの利用者も待ち受けている
    with pytest.raises(VerifyError, match="clamd の利用者ではありません"):
        verifier.check()
    assert verifier.verified is False

    verifier.invalidate()
    monkeypatch.setattr(peer_verify, "tcp_listener_uids", lambda port: [])
    with pytest.raises(VerifyError, match="待ち受けているプログラムがありません"):
        verifier.check()


def test_windows_tcp_listener_must_be_the_clamd_service(monkeypatch):
    verifier, _ = _verifier(monkeypatch, "Windows", service_name="clamd")
    monkeypatch.setattr(peer_verify, "tcp_listener_pids", lambda port: [4242])
    monkeypatch.setattr(peer_verify, "windows_service_pid", lambda name: 4242)
    verifier.check()
    assert verifier.verified is True

    verifier.invalidate()
    monkeypatch.setattr(peer_verify, "tcp_listener_pids", lambda port: [9999])
    with pytest.raises(VerifyError, match="clamd のサービス"):
        verifier.check()

    verifier.invalidate()
    monkeypatch.setattr(peer_verify, "windows_service_pid", lambda name: None)  # サービスが止まっている
    with pytest.raises(VerifyError, match="動いていません"):
        verifier.check()


def test_result_is_reused_for_a_few_seconds(monkeypatch):
    verifier, clock = _verifier(monkeypatch, "Linux", connection_type="tcp")
    calls = []
    monkeypatch.setattr(peer_verify, "tcp_listener_uids", lambda port: calls.append(port) or [110])
    verifier.check()
    clock[0] = peer_verify.CACHE_SEC - 1
    verifier.check()
    assert len(calls) == 1
    clock[0] = peer_verify.CACHE_SEC + 1
    verifier.check()
    assert len(calls) == 2


def test_remote_clamd_is_used_without_verification(monkeypatch, caplog):
    verifier, _ = _verifier(monkeypatch, "Linux", connection_type="tcp", tcp_host="192.0.2.10")
    with caplog.at_level(logging.WARNING, logger="clamav_frontend"):
        verifier.check()
        verifier.invalidate()
        verifier.check()
    assert verifier.verified is False
    assert caplog.text.count("相手を確かめずに接続します") == 1


def test_failure_of_the_check_itself_blocks_the_connection(monkeypatch):
    verifier, _ = _verifier(monkeypatch, "Windows")

    def broken(port):
        raise RuntimeError("access denied")

    monkeypatch.setattr(peer_verify, "tcp_listener_pids", broken)
    with pytest.raises(VerifyError, match="確かめられません"):
        verifier.check()


# -- ClamdClient ---------------------------------------------------------------------------------
class _Refusing:
    verified = False

    def __init__(self):
        self.invalidated = 0

    def check(self):
        raise VerifyError("偽物")

    def invalidate(self):
        self.invalidated += 1


def test_client_does_not_talk_to_an_unverified_endpoint(fake_clamd_server):
    client = ClamdClient(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=fake_clamd_server.port))
    client.verifier = _Refusing()
    with pytest.raises(ClamdConnectionError) as error:
        client.ping()
    assert "確かめられません" in error.value.detail
    assert fake_clamd_server.commands == []  # 何も送っていない


def test_reason_for_refusing_is_logged_once(fake_clamd_server, caplog):
    # 管理者が理由を知れるよう、確かめられなかった理由をログに残す（状態の確認のたびには残さない）
    client = ClamdClient(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=fake_clamd_server.port))
    client.verifier = _Refusing()
    with caplog.at_level("INFO", logger="clamav_frontend"):
        for _ in range(3):
            with pytest.raises(ClamdConnectionError):
                client.ping()
        assert caplog.text.count("接続先が clamd であることを確かめられないため") == 1
        assert "偽物" in caplog.text
        client.verifier = SimpleNamespace(check=lambda: None, invalidate=lambda: None)
        assert client.ping() is True
        assert "確かめられました" in caplog.text


def test_connection_failure_checks_again_next_time():
    class Passing(_Refusing):
        def check(self):
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = ClamdClient(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=port))
    client.verifier = Passing()
    with pytest.raises(ClamdConnectionError):
        client.ping()
    assert client.verifier.invalidated == 1


# -- 実際の OS で -------------------------------------------------------------------------------
@pytest.fixture
def listening_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    yield sock.getsockname()[1]
    sock.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows の TCP の表")
def test_windows_listener_pid_is_found(listening_port):
    assert os.getpid() in peer_verify.tcp_listener_pids(listening_port)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux の /proc/net/tcp")
def test_linux_listener_uid_is_found(listening_port):
    assert os.getuid() in tcp_listener_uids(listening_port)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32", reason="UNIX ソケットの所有者")
def test_unix_socket_owner_and_folder_are_checked(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir(mode=0o755)
    os.chmod(folder, 0o755)
    path = str(folder / "clamd.ctl")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    try:
        me = {os.getuid()}
        check_unix_socket(path, me, {os.getgid()})
        with pytest.raises(VerifyError, match="所有者"):
            check_unix_socket(path, {0}, {0})
        os.chmod(folder, 0o777)  # ほかの利用者がソケットを作り直せる
        with pytest.raises(VerifyError, match="書き込めます"):
            check_unix_socket(path, me, {os.getgid()})
        os.chmod(folder, 0o755)
        regular = folder / "file"
        regular.write_text("x")
        with pytest.raises(VerifyError, match="ソケットではありません"):
            check_unix_socket(str(regular), me, set())
    finally:
        server.close()


def test_windows_tcp_table_is_parsed():
    import struct

    v4 = struct.pack("<I", 2) + struct.pack("<6I", 2, struct.unpack("<I", bytes([127, 0, 0, 1]))[0],
                                            socket.htons(3310), 0, 0, 4242)
    v4 += struct.pack("<6I", 2, 0, socket.htons(8765), 0, 0, 77)
    assert peer_verify.parse_tcp_table(v4, socket.AF_INET) == [
        (peer_verify.ipaddress.ip_address("127.0.0.1"), 3310, 4242),
        (peer_verify.ipaddress.ip_address("0.0.0.0"), 8765, 77),
    ]
    row6 = bytes(15) + b"\x01" + struct.pack("<II", 0, socket.htons(3310)) + bytes(16) + struct.pack("<IIII", 0, 0, 2, 99)
    assert peer_verify.parse_tcp_table(struct.pack("<I", 1) + row6, socket.AF_INET6) == [
        (peer_verify.ipaddress.ip_address("::1"), 3310, 99)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のサービス")
def test_missing_windows_service_has_no_pid():
    assert peer_verify.windows_service_pid("ClamQtPieNoSuchService") is None


@pytest.mark.skipif(sys.platform != "win32" or not socket.has_ipv6, reason="Windows の TCP の表（IPv6）")
def test_windows_ipv6_listener_pid_is_found():
    with socket.socket(socket.AF_INET6) as sock:
        sock.bind(("::1", 0))
        sock.listen(1)
        assert os.getpid() in peer_verify.tcp_listener_pids(sock.getsockname()[1])

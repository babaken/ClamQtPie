"""内部 API の接続先（endpoint.json と、接続先がサービスかの確認）のテスト（サービス化・Web API設計書 3.1節・9.4節。段階 12-3d）"""
from __future__ import annotations

import io
import json
import os
import socket

import pytest

import clamqtpie_ctl
from core import service_endpoint
from core.exceptions import ServiceUnavailableError
from core.service_endpoint import ENDPOINT_FILE, read_endpoint, verify_service_listener, write_endpoint


def test_endpoint_is_written_and_read(tmp_path):
    write_endpoint(tmp_path, 18765)
    data = json.loads((tmp_path / ENDPOINT_FILE).read_text(encoding="ascii"))
    assert data == {"port": 18765, "pid": os.getpid()}
    assert read_endpoint(tmp_path) == 18765


@pytest.mark.parametrize("content", ['{"port": true}', '{"port": 0}', '{"port": 70000}', '{"port": "8765"}',
                                     '[8765]', 'not json', '{"port": 8765, "x": "' + "a" * 5000 + '"}'])
def test_broken_endpoint_is_ignored(tmp_path, content):
    (tmp_path / ENDPOINT_FILE).write_text(content, encoding="ascii")
    assert read_endpoint(tmp_path) is None


def test_missing_endpoint_is_ignored(tmp_path):
    assert read_endpoint(tmp_path) is None


def test_windows_listener_must_be_the_service(monkeypatch):
    monkeypatch.setattr(service_endpoint, "tcp_listener_pids", lambda port: [500])
    monkeypatch.setattr(service_endpoint, "windows_service_pid", lambda name: 500)
    verify_service_listener(8765, system="Windows")
    monkeypatch.setattr(service_endpoint, "tcp_listener_pids", lambda port: [500, 9000])  # ほかのプロセスも待ち受けている
    with pytest.raises(ServiceUnavailableError) as error:
        verify_service_listener(8765, system="Windows")
    assert error.value.code == "E8105"
    monkeypatch.setattr(service_endpoint, "tcp_listener_pids", lambda port: [500])
    monkeypatch.setattr(service_endpoint, "windows_service_pid", lambda name: None)  # サービスが止まっている
    with pytest.raises(ServiceUnavailableError):
        verify_service_listener(8765, system="Windows")


def test_linux_listener_must_be_root(monkeypatch):
    monkeypatch.setattr(service_endpoint, "tcp_listener_uids", lambda port: [0])
    verify_service_listener(8765, system="Linux")
    monkeypatch.setattr(service_endpoint, "tcp_listener_uids", lambda port: [1000])
    with pytest.raises(ServiceUnavailableError, match="サービスではありません"):
        verify_service_listener(8765, system="Linux")
    monkeypatch.setattr(service_endpoint, "tcp_listener_uids", lambda port: [])
    with pytest.raises(ServiceUnavailableError):
        verify_service_listener(8765, system="Linux")


def test_failure_of_the_check_means_do_not_connect(monkeypatch):
    def broken(port):
        raise OSError("denied")

    monkeypatch.setattr(service_endpoint, "tcp_listener_uids", broken)
    with pytest.raises(ServiceUnavailableError) as error:
        verify_service_listener(8765, system="Linux")
    assert "確かめられません" in error.value.detail


def test_a_program_of_this_user_is_not_the_service():
    # テストを動かす利用者が待ち受けても、サービス（Windows のサービス、Linux の root）とはみなさない
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root では待ち受けの所有者が root になる")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        with pytest.raises(ServiceUnavailableError):
            verify_service_listener(sock.getsockname()[1])


# -- clamqtpie-ctl status ------------------------------------------------------------------------
@pytest.fixture
def status(tmp_path, monkeypatch):
    monkeypatch.setattr(clamqtpie_ctl, "service_running", lambda *a, **k: True)
    monkeypatch.setattr("core.wake_timer.wake_timer_policy", lambda: "")
    asked = []
    monkeypatch.setattr(clamqtpie_ctl, "_api_version", lambda port: asked.append(port) or "アプリ 0.9.3、API 1")

    def run():
        out = io.StringIO()
        assert clamqtpie_ctl.main(["--data-dir", str(tmp_path), "status"], out=out) == 0
        return out.getvalue()

    return tmp_path, run, asked


def test_ctl_status_uses_the_port_the_service_listens_on(status, monkeypatch):
    data, run, asked = status
    (data / "api").mkdir()
    write_endpoint(data / "api", 18999)
    monkeypatch.setattr(clamqtpie_ctl, "verify_service_listener", lambda port: None)
    text = run()
    assert "127.0.0.1:18999" in text and asked == [18999]


def test_ctl_status_does_not_ask_an_unverified_server(status, monkeypatch):
    _, run, asked = status

    def refuse(port):
        raise ServiceUnavailableError("接続先が ClamQtPie のサービスではありません", detail="違うプロセス")

    monkeypatch.setattr(clamqtpie_ctl, "verify_service_listener", refuse)
    text = run()
    assert asked == [] and "問い合わせませんでした" in text and "E8105" in text

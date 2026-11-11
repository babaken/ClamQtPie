"""サービスの本体のテスト（サービス化・Web API設計書 9章。段階 12-3a）"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.service_paths import ServicePaths, UnsafeDataDirectoryError, prepare_directories, writable_by_others
from core.service_status import service_running
from core.systemd_notify import notify

_PROJECT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- データの置き場所 -------------------------------------------------------------------------
def test_default_locations():
    paths = ServicePaths.default()
    if sys.platform == "win32":
        assert paths.config_file.parent == paths.data_dir and paths.data_dir.name == "ClamQtPie"
    else:
        assert paths.config_file == Path("/etc/clamqtpie/config.json")
        assert paths.data_dir == Path("/var/lib/clamqtpie") and paths.log_dir == Path("/var/log/clamqtpie")
    assert paths.api_dir == paths.data_dir / "api" and paths.db_file == paths.data_dir / "app.db"


def test_created_directories_are_not_writable_by_others(tmp_path):
    paths = ServicePaths.under(tmp_path / "svc")
    prepare_directories(paths, strict=True)
    for directory in paths.directories():
        assert directory.is_dir()
        assert writable_by_others(directory) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の権限")
def test_directory_writable_by_others_blocks_the_service(tmp_path):
    # 一般の利用者が書き換えられる置き場所では、サービスとして起動しない（開発中の確認では警告だけ）
    paths = ServicePaths.under(tmp_path / "svc")
    prepare_directories(paths, strict=True)
    os.chmod(paths.api_dir, 0o777)
    with pytest.raises(UnsafeDataDirectoryError):
        prepare_directories(paths, strict=True)
    prepare_directories(paths, strict=False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のアクセス権")
def test_folder_with_inherited_user_write_access_is_detected(tmp_path):
    # 一時フォルダは本人以外（OWNER RIGHTS など）にも書き込みを許しているため、サービスの置き場所としては使わない
    assert writable_by_others(tmp_path) is not None


def test_symlinked_directory_blocks_the_service(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    base = tmp_path / "svc"
    base.mkdir()
    try:
        (base / "logs").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("この環境ではリンクを作れない")
    with pytest.raises(UnsafeDataDirectoryError):
        prepare_directories(ServicePaths.under(base), strict=True)


# -- OS への通知・状態 ------------------------------------------------------------------------
def test_notify_does_nothing_outside_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify("READY=1") is False


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32", reason="UNIX ソケット")
def test_notify_sends_the_state_to_systemd(tmp_path, monkeypatch):
    address = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(address)
        monkeypatch.setenv("NOTIFY_SOCKET", address)
        assert notify("READY=1") is True
        assert server.recv(100) == b"READY=1"


@pytest.mark.parametrize("system,returncode,stdout,expected", [
    ("Windows", 0, b"STATE              : 4  RUNNING", True),
    ("Windows", 0, b"STATE              : 1  STOPPED", False),
    ("Windows", 1060, b"", False),  # 登録されていない
    ("Linux", 0, b"", True),
    ("Linux", 3, b"", False),
])
def test_service_running(monkeypatch, system, returncode, stdout, expected):
    monkeypatch.setattr("core.service_status.find_system_tool", lambda name, system=None: f"/usr/bin/{name}")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    assert service_running(system, run=run) is expected
    assert calls[0][-1] in ("ClamQtPie", "clamqtpie.service")


# -- 実際に動かす -------------------------------------------------------------------------------
def _start(data_dir: Path, port: int) -> subprocess.Popen:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(json.dumps({"api": {"port": port}}), encoding="utf-8")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    return subprocess.Popen([sys.executable, "service_main.py", "--console", "--data-dir", str(data_dir)],
                            cwd=_PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _wait_for_log(data_dir: Path, text: str, timeout: float = 30) -> bool:
    log = data_dir / "logs" / "app.log"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists() and text in log.read_text(encoding="utf-8", errors="replace"):
            return True
        time.sleep(0.2)
    return False


def test_service_answers_on_the_api_and_refuses_a_second_instance(tmp_path):
    import httpx2 as httpx

    data_dir, port = tmp_path / "svc", _free_port()
    process = _start(data_dir, port)
    try:
        assert _wait_for_log(data_dir, "サービスを開始しました"), process.stdout.read() if process.poll() else ""
        token = (data_dir / "api" / "user.token").read_text(encoding="ascii")
        # 待ち受けたポートを endpoint.json で知らせる（段階 12-3d）
        assert json.loads((data_dir / "api" / "endpoint.json").read_text(encoding="ascii"))["port"] == port
        response = httpx.get(f"http://127.0.0.1:{port}/api/v1/status",
                             headers={"Authorization": f"Bearer {token}"}, timeout=10)
        assert response.status_code == 200
        # サービスでは通知を保存し、未読を API で読める（段階 12-3c）
        response = httpx.get(f"http://127.0.0.1:{port}/api/v1/notifications/pending",
                             headers={"Authorization": f"Bearer {token}"}, timeout=10)
        assert response.status_code == 200 and response.json() == {"notifications": [], "total": 0}
        # 起動の完了の前に API が待ち受けを始めていること（ログの順番）
        log = (data_dir / "logs" / "app.log").read_text(encoding="utf-8")
        assert log.index("Web API の待ち受けを開始しました") < log.index("サービスを開始しました")
        from core.version import APP_VERSION

        assert f"ClamQtPie {APP_VERSION}" in log  # 起動の行に版を出す
        second = subprocess.run([sys.executable, "service_main.py", "--console", "--data-dir", str(data_dir)],
                                cwd=_PROJECT, capture_output=True, text=True, timeout=60)
        assert second.returncode == 4
    finally:
        if sys.platform == "win32":
            process.kill()
        else:
            process.terminate()  # SIGTERM で終了の処理を通る
        process.wait(timeout=30)
    if sys.platform != "win32":
        assert process.returncode == 0
        assert list((data_dir / "api").glob("*.token")) == []
        assert _wait_for_log(data_dir, "サービスを停止しました", timeout=5)


def test_service_fails_to_start_when_the_api_port_is_taken(tmp_path):
    # API がなければ画面も WebUI も使えないため、待ち受けられなければ起動を失敗にする（9.4節）
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        data_dir = tmp_path / "svc"
        process = _start(data_dir, blocker.getsockname()[1])
        process.wait(timeout=60)
        assert process.returncode == 3
        assert list((data_dir / "api").glob("*.token")) == []
    finally:
        blocker.close()

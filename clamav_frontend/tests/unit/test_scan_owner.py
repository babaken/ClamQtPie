"""サービスで user のトークンのスキャンを、頼んだ利用者のフォルダの中に限るテスト（12章 R-9。段階 12-3d）"""
from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import API_PREFIX, create_app
from api.auth import ROLE_ADMIN, ROLE_USER, ApiTokens
from api.bridge import EventHub
from api.context import ApiContext
from api.jobs import JobTracker
from core import peer_identity
from core.peer_identity import is_inside, linux_connection_uid, parse_connection_table
from core.scan_dispatcher import ScanWorker

_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


# -- 接続元の利用者 ------------------------------------------------------------------------------
def test_linux_client_socket_owner_is_found(tmp_path):
    # 127.0.0.1:50000 → 127.0.0.1:8765 の接続（クライアント側の行の所有者が頼んだ利用者）
    (tmp_path / "tcp").write_text(
        _HEADER
        + "   0: 0100007F:C350 0100007F:223D 01 00000000:00000000 00:00000000 00000000  1000 0 1 1\n"
        + "   1: 0100007F:223D 0100007F:C350 01 00000000:00000000 00:00000000 00000000     0 0 2 1\n"
        + "   2: 0100007F:C351 0100007F:223D 01 00000000:00000000 00:00000000 00000000  2000 0 3 1\n")
    assert linux_connection_uid(("127.0.0.1", 50000), ("127.0.0.1", 8765), tmp_path) == 1000
    assert linux_connection_uid(("127.0.0.1", 50002), ("127.0.0.1", 8765), tmp_path) is None


def test_windows_connection_table_is_parsed():
    lo = struct.unpack("<I", bytes([127, 0, 0, 1]))[0]
    raw = struct.pack("<I", 1) + struct.pack("<6I", 5, lo, socket.htons(50000), lo, socket.htons(8765), 4321)
    assert parse_connection_table(raw, socket.AF_INET) == [
        (peer_identity.ipaddress.ip_address("127.0.0.1"), 50000,
         peer_identity.ipaddress.ip_address("127.0.0.1"), 8765, 4321)]


def test_real_connection_is_traced_to_this_user():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname())
        conn, _ = server.accept()
        try:
            home = peer_identity.requester_home(client.getsockname(), server.getsockname())
        finally:
            conn.close()
            client.close()
    assert home is not None
    assert os.path.normcase(os.path.realpath(home)) == os.path.normcase(os.path.realpath(os.path.expanduser("~")))


def test_inside_check_follows_links_to_the_real_place(tmp_path):
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()
    assert is_inside(str(home / "docs"), str(home)) and is_inside(str(home), str(home))
    assert not is_inside(str(outside), str(home))
    assert not is_inside(str(tmp_path / "home2"), str(home))  # 名前の前の部分が同じだけの別のフォルダ
    link = home / "link"
    if _make_dir_link(link, outside):
        assert not is_inside(str(link), str(home))  # リンクで外を指していれば中ではない


def _make_dir_link(link, target) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


# -- 列挙でリンク・ジャンクションをたどらない -------------------------------------------------
def test_scan_does_not_walk_into_links(fake_clamd_server, tmp_path):
    from core.clamav_service import ClamAVService
    from core.config_model import ClamdConfig

    home = tmp_path / "home"
    home.mkdir()
    (home / "mine.txt").write_text("x")
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    if not _make_dir_link(home / "link", outside):
        pytest.skip("リンクを作れない")
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"
    service = ClamAVService(ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=fake_clamd_server.port))
    worker = ScanWorker(service, [str(home)])
    scanned = []
    worker.progress.connect(lambda a, b, path: scanned.append(str(path)))
    worker.run()
    assert str(home / "mine.txt") in scanned
    assert not any("secret.txt" in path for path in scanned)


# -- API -----------------------------------------------------------------------------------------
class _DirectInvoker:
    def call(self, fn, *args):
        import concurrent.futures

        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    async def run(self, fn, *args):
        import asyncio

        return await asyncio.wrap_future(self.call(fn, *args))


class _FakeScan:
    def __init__(self, jobs):
        self.jobs, self.is_running, self.started = jobs, False, []

    def start(self, targets, include_subfolders=True, apply_exclude=True, label="", skip_dirs=(), **kwargs):
        self.started.append(targets)
        self.jobs.on_scan_started(label)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr("core.scan_targets.full_scan_targets", lambda: [str(tmp_path)])
    monkeypatch.setattr("core.scan_targets.full_scan_skip_dirs", lambda: ())
    hub = EventHub()
    jobs = JobTracker(hub)
    scan = _FakeScan(jobs)
    asked = []
    config = SimpleNamespace(current=SimpleNamespace(scan=SimpleNamespace(quick_targets=[str(other)])))

    def make(service_mode=True, requester=str(home)):
        context = ApiContext(
            config_service=config, clamav_service=None, realtime_manager=None, scan_dispatcher=scan,
            update_service=None, update_dispatcher=None, scheduler_service=None, quarantine_service=None,
            exclude_service=None, watch_target_service=None, log_reader_service=None,
            invoker=_DirectInvoker(), events=hub, jobs=jobs, service_mode=service_mode,
            requester_home=lambda client, server: asked.append((client, server)) or requester,
        )
        tokens = ApiTokens(tmp_path / "tokens")
        client = TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765",
                            client=("127.0.0.1", 50000))

        def post(body, role=ROLE_USER):
            scan.is_running = False
            return client.post(f"{API_PREFIX}/scans", json=body,
                               headers={"Authorization": f"Bearer {tokens.token_for(role)}"})

        return post

    return SimpleNamespace(make=make, home=home, other=other, scan=scan, asked=asked)


def test_user_can_scan_inside_their_own_folder(env):
    post = env.make()
    assert post({"type": "custom", "targets": [str(env.home / "Downloads")]}).status_code == 202
    assert env.asked == [(("127.0.0.1", 50000), ("127.0.0.1", 8765))]  # 接続元と待ち受けから割り出す
    assert post({"type": "quick", "targets": [str(env.home)]}).status_code == 202


def test_user_cannot_scan_outside_their_folder(env):
    post = env.make()
    for body in ({"type": "custom", "targets": [str(env.other)]},
                 {"type": "custom", "targets": [str(env.home), str(env.other)]},
                 {"type": "full"}):
        response = post(body)
        assert response.status_code == 403 and response.json()["error"]["code"] == "E8102"
    assert env.scan.started == []


def test_admin_and_configured_quick_scan_are_not_limited(env):
    post = env.make()
    assert post({"type": "quick"}).status_code == 202  # 管理者が決めた場所（scan.quick_targets）
    assert post({"type": "custom", "targets": [str(env.other)]}, role=ROLE_ADMIN).status_code == 202
    assert post({"type": "full"}, role=ROLE_ADMIN).status_code == 202


def test_unknown_requester_cannot_choose_places(env):
    post = env.make(requester=None)
    assert post({"type": "custom", "targets": [str(env.home)]}).status_code == 403


def test_desktop_app_api_is_not_limited(env):
    # 従来のアプリは利用者の権限で動くため、もともとその利用者が読める場所しか検査できない
    post = env.make(service_mode=False)
    assert post({"type": "custom", "targets": [str(env.other)]}).status_code == 202
    assert env.asked == []

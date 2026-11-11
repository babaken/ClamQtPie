"""昇格ヘルパー（elevate_main.py）と、トレイGUI からの頼み方（api_client/elevation.py）のテスト（段階 12-4c）

UAC・pkexec の確認と、昇格したプロセスの確認画面は自動のテストでは出せないため、確認画面は関数で差し替え、
起動は同じプロセスのスレッド・子プロセスで代わりに行う（実機で確かめる）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest

from api_client.admin_ops import Lookup, Operation, describe, is_admin, parse_operations
from api_client.elevation import ElevatedCaller, ElevationRunner, Launched, exchange_linux
from api_client.sync import SyncCaller
from api_client.client import ApiClient
from core.exceptions import (
    ApiForbiddenError,
    ApiNotFoundError,
    ElevationCancelledError,
    ElevationError,
)
from elevate_main import EXIT_CANCELLED, EXIT_DONE, EXIT_FAILED, EXIT_INVALID, frame, read_frame, run_request
from tests.unit.test_api_proxies import service  # noqa: F401 - 本物の API を待ち受けるフィクスチャ

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows の名前付きパイプ")


def _request(*operations) -> bytes:
    return json.dumps({"operations": [op.to_json() for op in operations]}).encode("utf-8")


# -- 操作の一覧と説明 ----------------------------------------------------------------------------
def test_only_listed_admin_operations_are_accepted():
    ops = parse_operations({"operations": [{"method": "DELETE", "path": "/quarantine/3"},
                                           {"method": "PUT", "path": "/realtime", "body": {"enabled": True}}]})
    assert [(o.method, o.path) for o in ops] == [("DELETE", "/quarantine/3"), ("PUT", "/realtime")]
    for bad in ({"operations": [{"method": "GET", "path": "/config"}]},  # 一覧に無い（読み取り）
                {"operations": [{"method": "DELETE", "path": "/quarantine/3/../../logs"}]},
                {"operations": [{"method": "POST", "path": "/clamd/start", "extra": 1}]},
                {"operations": [{"method": "PUT", "path": "/realtime", "body": [1]}]},
                {"operations": []}, {"operations": [{"method": "POST", "path": "/clamd/start"}] * 201}, []):
        with pytest.raises(ValueError):
            parse_operations(bad)


def test_user_first_operations_are_not_elevated_up_front():
    assert is_admin("DELETE", "/quarantine/3") and is_admin("PUT", "/config")
    assert not is_admin("POST", "/scans") and not is_admin("GET", "/quarantine")


def test_descriptions_come_from_the_operation_and_the_helpers_lookup():
    lookup = Lookup(quarantine={3: {"original_path": "C:\\Users\\a\\x.exe", "virus_name": "Eicar"}},
                    excludes={4: {"type": "extension", "value": ".tmp"}},
                    config={"update": {"stale_days": 3}, "protected": ["api.port"]})
    assert describe(Operation("POST", "/quarantine/3/restore", {"overwrite": False}), lookup) == \
        ["隔離ファイル「C:\\Users\\a\\x.exe」（Eicar）を元の場所に復元します"]
    assert describe(Operation("DELETE", "/quarantine/9"), lookup) == ["隔離ファイル（ID 9）を完全に削除します（元に戻せません）"]
    assert describe(Operation("PATCH", "/excludes/4", {"enabled": False}), lookup) == ["除外「拡張子: .tmp」を無効にします"]
    assert describe(Operation("PUT", "/config", {"update": {"stale_days": 9}}), lookup) == \
        ["設定を変更します", "  update.stale_days: 3 → 9"]
    assert describe(Operation("POST", "/schedules", {"name": "毎晩", "scan_type": "scan", "cron": "0 3 * * *",
                                                     "target": "/home", "wake": True})) == \
        ["スケジュールを追加します: 「毎晩」（スキャン、cron「0 3 * * *」、対象「/home」、スリープ中でも起こして実行する）"]
    assert describe(Operation("POST", "/scans", {"type": "full"})) == ["フルスキャンを開始します"]


# -- 昇格ヘルパー（本物の API に admin のトークンで） ----------------------------------------------
def test_helper_runs_operations_after_confirmation(qapp, service):  # noqa: F811
    shown = []
    code, result = run_request(
        _request(Operation("POST", "/excludes", {"type": "extension", "value": ".tmp"}),
                 Operation("PUT", "/realtime", {"enabled": True})),
        service.api_dir, confirm=lambda lines: shown.extend(lines) or True, verify=lambda port: None)
    assert code == EXIT_DONE and result["status"] == "done" and result["completed"] == 2
    assert result["results"][0]["value"] == ".tmp"
    assert shown == ["除外を追加します: 拡張子「.tmp」", "リアルタイム監視を開始します"]
    assert service.context.realtime_manager.is_running is True


def test_helper_does_nothing_when_cancelled(qapp, service):  # noqa: F811
    code, result = run_request(_request(Operation("PUT", "/realtime", {"enabled": True})), service.api_dir,
                               confirm=lambda lines: False, verify=lambda port: None)
    assert (code, result["status"]) == (EXIT_CANCELLED, "cancelled")
    assert service.context.realtime_manager.is_running is False


def test_helper_stops_at_the_first_failure(qapp, service):  # noqa: F811
    code, result = run_request(
        _request(Operation("DELETE", "/excludes/999"), Operation("PUT", "/realtime", {"enabled": True})),
        service.api_dir, confirm=lambda lines: True, verify=lambda port: None)
    assert code == EXIT_FAILED and result["completed"] == 0 and result["error"]["code"] == "E8109"
    assert service.context.realtime_manager.is_running is False  # 残りは実行しない


def test_helper_refuses_unlisted_operations_before_asking(qapp, service):  # noqa: F811
    asked = []
    raw = json.dumps({"operations": [{"method": "GET", "path": "/config"}]}).encode()
    code, result = run_request(raw, service.api_dir, confirm=lambda lines: asked.append(lines) or True,
                               verify=lambda port: None)
    assert code == EXIT_INVALID and asked == []


def test_helper_checks_the_listener_before_sending_the_token(qapp, service):  # noqa: F811
    from core.exceptions import ServiceUnavailableError

    def refuse(port):
        raise ServiceUnavailableError("接続先が ClamQtPie のサービスではありません")

    code, result = run_request(_request(Operation("POST", "/clamd/start")), service.api_dir,
                               confirm=lambda lines: True, verify=refuse)
    assert code == EXIT_FAILED and result["error"]["code"] == "E8105"


# -- トレイGUI からの頼み方 ----------------------------------------------------------------------
def _in_process_exchange(api_dir, confirm=lambda lines: True):
    def exchange(payload: bytes) -> bytes:
        _code, result = run_request(payload, api_dir, confirm, verify=lambda port: None)
        return json.dumps(result).encode("utf-8")
    return exchange


def _caller(service, confirm=lambda lines: True):  # noqa: F811
    client = ApiClient(service.api_dir, verify=lambda port: None)
    client.prepare()
    return ElevatedCaller(SyncCaller(client), ElevationRunner(_in_process_exchange(service.api_dir, confirm)))


def test_admin_operations_go_through_the_helper(qapp, service):  # noqa: F811
    caller = _caller(service)
    item = caller.call("POST", "/excludes", {"type": "extension", "value": ".log"})
    assert item["value"] == ".log"
    assert caller.call("GET", "/excludes")["total"] == 1  # 読み取りは user のトークンのまま
    caller.batch([Operation("PATCH", f"/excludes/{item['id']}", {"enabled": False}),
                  Operation("DELETE", f"/excludes/{item['id']}")])
    assert caller.call("GET", "/excludes")["total"] == 0


def test_cancel_and_failure_are_reported(qapp, service):  # noqa: F811
    with pytest.raises(ElevationCancelledError):
        _caller(service, confirm=lambda lines: False).call("PUT", "/realtime", {"enabled": True})
    with pytest.raises(ApiNotFoundError) as error:
        _caller(service).batch([Operation("DELETE", "/excludes/1"), Operation("DELETE", "/excludes/2")])
    assert error.value.completed == 0


def test_forbidden_user_operation_is_retried_once_with_elevation(qapp, service):  # noqa: F811
    service.context.service_mode = True  # user のトークンのフルスキャンは 403
    caller = _caller(service)
    caller.call("POST", "/scans", {"type": "full"})
    assert service.scan.started  # 昇格してやり直した


def test_forbidden_unlisted_operation_is_not_elevated(qapp):
    class Api:
        def call(self, method, path, body=None, params=None):
            raise ApiForbiddenError()

    class Runner:
        def run(self, operations):
            raise AssertionError("昇格しない")

    with pytest.raises(ApiForbiddenError):
        ElevatedCaller(Api(), Runner()).call("GET", "/config")


def test_cancelled_elevation_shows_no_error_dialog(qapp):
    from gui.error_dialog import show_error_dialog

    show_error_dialog(None, ElevationCancelledError())  # 予期しない QMessageBox はテストが失敗にする


# -- 受け渡し --------------------------------------------------------------------------------------
_ECHO = ("import sys,struct,json;h=sys.stdin.buffer.read(4);n=struct.unpack('>I',h)[0];sys.stdin.buffer.read(n);"
         "d=json.dumps({'status':'done','completed':1,'results':[{'ok':1}]}).encode();"
         "sys.stdout.buffer.write(struct.pack('>I',len(d))+d)")


def test_linux_exchange_uses_standard_io():
    raw = exchange_linux(b'{"operations": []}', command=[sys.executable, "-c", _ECHO])
    assert json.loads(raw)["results"] == [{"ok": 1}]


def test_linux_exchange_treats_dismissed_pkexec_as_cancel():
    with pytest.raises(ElevationCancelledError):
        exchange_linux(b"{}", command=[sys.executable, "-c", "import sys; sys.exit(126)"])
    with pytest.raises(ElevationError):
        exchange_linux(b"{}", command=[sys.executable, "-c", "import sys; sys.exit(1)"])


@windows_only
def test_windows_exchange_over_a_named_pipe():
    import win32event
    import win32file

    from api_client.elevation import exchange_windows
    from elevate_main import open_pipe_client

    done = win32event.CreateEvent(None, True, False, None)
    received = []

    def helper(name):
        try:
            handle = open_pipe_client(name)
            received.append(read_frame(lambda n: win32file.ReadFile(handle, n)[1]))
            win32file.WriteFile(handle, frame(b'{"status": "done", "results": [1]}'))
            handle.Close()
        finally:
            win32event.SetEvent(done)  # 失敗しても、待っている側を止めない

    def launch(name):
        threading.Thread(target=helper, args=(name,), daemon=True).start()
        return Launched(done, os.getpid())

    assert exchange_windows(b"request", launch=launch) == b'{"status": "done", "results": [1]}'
    assert received == [b"request"]


@windows_only
def test_windows_exchange_rejects_another_client_and_a_silent_exit():
    import win32event

    from api_client.elevation import exchange_windows
    from elevate_main import open_pipe_client

    handles = []  # つないだままにする（閉じると、確かめる前にパイプが閉じる）

    def launch_other(name):
        threading.Thread(target=lambda: handles.append(open_pipe_client(name)), daemon=True).start()
        return Launched(win32event.CreateEvent(None, True, False, None), 1)  # 起動したヘルパーの ID と違う

    with pytest.raises(ElevationError) as error:
        exchange_windows(b"x", launch=launch_other)
    assert "以外" in error.value.detail

    exited = win32event.CreateEvent(None, True, True, None)  # つながずに終わった
    with pytest.raises(ElevationError) as error:
        exchange_windows(b"x", launch=lambda name: Launched(exited, 1))
    assert "終了しました" in error.value.detail


def test_pipe_name_is_checked():
    if sys.platform != "win32":
        pytest.skip("Windows の名前付きパイプ")
    from elevate_main import open_pipe_client

    with pytest.raises(ValueError):
        open_pipe_client("..\\other")


@windows_only
def test_windows_helper_end_to_end_over_the_pipe(qapp, service):  # noqa: F811
    import win32event

    from api_client.elevation import exchange_windows
    from elevate_main import serve_pipe

    done = win32event.CreateEvent(None, True, False, None)
    codes = []

    def helper(name):
        try:
            codes.append(serve_pipe(name, service.api_dir, confirm=lambda lines: True, verify=lambda port: None))
        finally:
            win32event.SetEvent(done)

    def launch(name):
        threading.Thread(target=helper, args=(name,), daemon=True).start()
        return Launched(done, os.getpid())

    runner = ElevationRunner(lambda payload: exchange_windows(payload, launch=launch))
    [result] = runner.run([Operation("POST", "/realtime/targets", {"path": str(service.tmp)})])
    assert result["path"] == str(service.tmp) and codes == [EXIT_DONE]


def test_confirm_dialog_opens_without_selection_and_focuses_cancel(qapp):
    # 開いたときに本文の全体が選択されていない・Enter だけでは実行しない（Linux 実機の確認での指摘）
    from elevate_main import build_confirm_dialog

    dialog = build_confirm_dialog(["監視対象を追加します: /home/u/a", "スケジュール: 0 3 1 * *"])
    try:
        assert not dialog.text.textCursor().hasSelection()
        assert dialog.cancel_button.isDefault() and dialog.focusWidget() is dialog.cancel_button
    finally:
        dialog.deleteLater()

"""
clamdサービスの起動制御（core/clamd_process.py、Phase 11）のテスト
"""
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from core.clamd_process import ClamdServiceController, ClamdStarter
from core.config_model import ClamdConfig
from core.exceptions import ClamdProcessError
from core.exec_path import system_tool


def _runner(returncode=0, stdout="", stderr=""):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    run.calls = calls
    return run


def test_windows_starts_clamd_service_with_sc():
    run = _runner()
    assert ClamdServiceController(ClamdConfig(), system="Windows", runner=run).start() is True
    assert run.calls == [[system_tool("sc", "Windows"), "start", "clamd"]]


def test_windows_already_running_is_success():
    run = _runner(returncode=1056, stdout="[SC] StartService FAILED 1056:")
    assert ClamdServiceController(ClamdConfig(), system="Windows", runner=run).start() is True


def test_windows_failure_raises_e8003():
    run = _runner(returncode=1060, stdout="[SC] OpenService FAILED 1060")
    with pytest.raises(ClamdProcessError) as exc_info:
        ClamdServiceController(ClamdConfig(), system="Windows", runner=run).start()
    assert exc_info.value.code == "E8003"
    assert "1060" in exc_info.value.detail


def test_linux_uses_pkexec_systemctl_and_custom_service_name(monkeypatch):
    monkeypatch.setattr("core.clamd_process.os.geteuid", lambda: 1000, raising=False)
    run = _runner()
    controller = ClamdServiceController(
        ClamdConfig(service_name="clamd@scan"), system="Linux", runner=run,
        which=lambda name: "/usr/bin/pkexec",
    )
    controller.start()
    assert run.calls == [["/usr/bin/pkexec", system_tool("systemctl", "Linux"), "start", "clamd@scan"]]


def test_starter_reports_failure_in_background(qtbot):
    clamav_service = MagicMock()
    clamav_service.ensure_available.return_value = False
    starter = ClamdStarter(clamav_service)

    with qtbot.waitSignal(starter.finished, timeout=3000) as blocker:
        assert starter.start() is True

    ok, error = blocker.args
    assert ok is False
    assert error.code == "E1006"


def test_starter_passes_through_process_error(qtbot):
    clamav_service = MagicMock()
    clamav_service.ensure_available.side_effect = ClamdProcessError(detail="sc failed")
    starter = ClamdStarter(clamav_service)

    with qtbot.waitSignal(starter.finished, timeout=3000) as blocker:
        starter.start()

    assert blocker.args[1].code == "E8003"


# -- 画面レイアウト刷新: エンジン サービス画面からの停止・バックグラウンド実行 -------------
def test_windows_stops_clamd_service_with_sc():
    run = _runner()
    assert ClamdServiceController(ClamdConfig(), system="Windows", runner=run).stop() is True
    assert run.calls == [[system_tool("sc", "Windows"), "stop", "clamd"]]


def test_windows_stop_when_not_active_is_success():
    run = _runner(returncode=1062, stdout="[SC] ControlService FAILED 1062:")
    assert ClamdServiceController(ClamdConfig(), system="Windows", runner=run).stop() is True


def test_linux_stop_uses_pkexec_systemctl(monkeypatch):
    monkeypatch.setattr("core.clamd_process.os.geteuid", lambda: 1000, raising=False)
    run = _runner()
    controller = ClamdServiceController(
        ClamdConfig(), system="Linux", runner=run, which=lambda name: f"/usr/bin/{name}"
    )
    assert controller.stop() is True
    assert run.calls == [["/usr/bin/pkexec", system_tool("systemctl", "Linux"), "stop", "clamav-daemon"]]


def test_stop_failure_raises_e8003():
    run = _runner(returncode=5, stderr="Access is denied")
    with pytest.raises(ClamdProcessError) as exc_info:
        ClamdServiceController(ClamdConfig(), system="Windows", runner=run).stop()
    assert exc_info.value.code == "E8003"


def test_background_task_reports_result_and_error(qtbot):
    from core.background_task import BackgroundTask

    task = BackgroundTask()
    with qtbot.waitSignal(task.finished, timeout=3000) as blocker:
        assert task.run("ok", lambda: 42) is True
    assert blocker.args == ["ok", 42, None]

    def fail():
        raise ClamdProcessError(detail="x")

    with qtbot.waitSignal(task.finished, timeout=3000) as blocker:
        task.run("ng", fail)
    name, result, error = blocker.args
    assert (name, result) == ("ng", None)
    assert isinstance(error, ClamdProcessError)


def test_background_task_is_not_running_when_finished_arrives(qtbot):
    # レビュー指摘: finishedを受け取った時点でis_runningがTrueのままだとボタンが戻らなかった
    from core.background_task import BackgroundTask

    task = BackgroundTask()
    seen = []
    task.finished.connect(lambda *_: seen.append(task.is_running))
    with qtbot.waitSignal(task.finished, timeout=3000):
        task.run("x", lambda: None)
    assert seen == [False]
    assert task.run("again", lambda: None) is True  # 直後に次の実行を受け付ける


def test_starter_rejects_second_start_while_running(qtbot):
    import threading

    gate = threading.Event()
    clamav_service = MagicMock()
    clamav_service.ensure_available.side_effect = lambda: gate.wait(3) or True
    starter = ClamdStarter(clamav_service)

    with qtbot.waitSignal(starter.finished, timeout=5000) as blocker:
        assert starter.start() is True
        assert starter.start() is False
        gate.set()
    assert blocker.args == [True, None]


def test_windows_sc_runs_without_console_window():
    run = _runner()
    seen = {}

    def capture(command, **kwargs):
        seen.update(kwargs)
        return run(command, **kwargs)

    ClamdServiceController(ClamdConfig(), system="Windows", runner=capture).stop()
    assert "creationflags" in seen


def test_windows_sc_output_is_read_as_oem_code_page():
    # Python 3.15はUTF-8モードが既定で、日本語Windowsのscの出力（cp932）をtext=Trueだけでは読めなかった
    run = _runner()
    seen = {}

    def capture(command, **kwargs):
        seen.update(kwargs)
        return run(command, **kwargs)

    ClamdServiceController(ClamdConfig(), system="Windows", runner=capture).start()
    assert seen["encoding"] == "oem"
    assert seen["errors"] == "replace"


@pytest.mark.skipif(sys.platform != "win32", reason="OEMコードページはWindowsのみ")
@pytest.mark.parametrize("action, code", [("start", "1056"), ("stop", "1062")])
def test_windows_sc_japanese_output_still_detects_already_state(action, code):
    # 実際の子プロセスにcp932の日本語メッセージを出させ、UTF-8モードでも番号を判別できることを確かめる
    message = f"[SC] StartService FAILED {code}:\r\n\r\nサービスのインスタンスは既に実行されています。\r\n"
    script = f"import sys; sys.stdout.buffer.write({message.encode('cp932')!r}); sys.exit(1)"

    def run(command, **kwargs):
        return subprocess.run([sys.executable, "-c", script], **kwargs)

    controller = ClamdServiceController(ClamdConfig(), system="Windows", runner=run)
    assert getattr(controller, action)() is True


@pytest.mark.parametrize("name", ["-h", "clamd stop", "", "a;b"])
def test_invalid_service_name_is_not_passed_to_command(name):
    # サービス名はscやsystemctlの引数になるため、オプションとして解釈され得る名前等は使わない
    calls = []
    controller = ClamdServiceController(
        ClamdConfig(service_name=name or " "), system="Windows", runner=lambda *a, **k: calls.append(a)
    )
    with pytest.raises(ClamdProcessError):
        controller.start()
    assert calls == []

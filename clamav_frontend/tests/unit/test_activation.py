"""
Toastクリックで隔離画面を開く仕組み（core/activation.py、Phase 11）のテスト
"""
from pathlib import Path
from unittest.mock import MagicMock

from core.activation import (
    REQUEST_FILE_NAME,
    SCREEN_QUARANTINE,
    ActivationWatcher,
    helper_command,
    protocol_command,
    register_protocol,
    toast_launch_uri,
    user_state_dir,
)


def test_watcher_emits_allowed_screen_and_deletes_request(qtbot, tmp_path):
    watcher = ActivationWatcher(tmp_path)

    with qtbot.waitSignal(watcher.show_requested, timeout=3000) as blocker:
        watcher.request_file.write_text("quarantine\r\n", encoding="ascii")

    assert blocker.args == ["quarantine"]
    qtbot.waitUntil(lambda: not watcher.request_file.exists(), timeout=1000)


def test_watcher_ignores_unknown_screen(qtbot, tmp_path):
    watcher = ActivationWatcher(tmp_path)
    requested = []
    watcher.show_requested.connect(requested.append)

    watcher.request_file.write_text("settings; rm -rf /", encoding="ascii")
    watcher.check_request()

    assert requested == []
    assert not watcher.request_file.exists()


def test_stale_request_from_before_startup_is_discarded(qtbot, tmp_path):
    (tmp_path / "show_request").write_text("quarantine", encoding="ascii")
    requested = []

    watcher = ActivationWatcher(tmp_path)  # アプリ停止中のクリックで起動直後に勝手に開かない
    watcher.show_requested.connect(requested.append)
    watcher.check_request()

    assert requested == []


def test_helper_command_uses_windowless_helper():
    # PowerShellはコンソールが一瞬表示されるため、ウィンドウを持たない補助プログラムを使う（Windows実機テストで発見）
    # パスはOSの区切り文字で組み立てる（Linuxで実行しても同じ確認ができるように）
    app_dir = Path("Tools") / "ClamAVFrontend"
    assert helper_command(app_dir, frozen=True, python_executable=str(app_dir / "ClamAVFrontend.exe")) == [
        str(app_dir / "ClamQtPieActivate.exe")
    ]
    scripts = Path("venv") / "Scripts"
    assert helper_command(app_dir, frozen=False, python_executable=str(scripts / "python.exe")) == [
        str(scripts / "pythonw.exe"),
        str(app_dir / "notify_activate.py"),
    ]


def test_protocol_command_does_not_pass_url():
    command = protocol_command([r"C:\Program Files\App\ClamQtPieActivate.exe"])

    assert command == r'"C:\Program Files\App\ClamQtPieActivate.exe"'
    assert "%1" not in command  # URLの内容はコマンドに渡さない（インジェクション防止）


def test_register_protocol_writes_hkcu_classes():
    winreg = MagicMock()
    winreg.HKEY_CURRENT_USER = "HKCU"
    winreg.REG_SZ = 1

    assert register_protocol([r"C:\x\ClamQtPieActivate.exe"], winreg_module=winreg) is True

    keys = [c.args[1] for c in winreg.CreateKey.call_args_list]
    assert keys == [r"Software\Classes\clamqtpie", r"Software\Classes\clamqtpie\shell\open\command"]
    values = [(c.args[1], c.args[4]) for c in winreg.SetValueEx.call_args_list]
    assert ("URL Protocol", "") in values
    assert ("", "URL:ClamQtPie") in values


def test_toast_launch_uri():
    assert toast_launch_uri("quarantine") == "clamqtpie:quarantine"


def test_request_locked_by_writer_is_retried(qtbot, tmp_path, monkeypatch):
    # Windows実機で発見: PowerShellが書き込み中のファイルはPermission deniedで読めない。少し待って読み直す
    watcher = ActivationWatcher(tmp_path)
    watcher.request_file.write_text("quarantine", encoding="ascii")
    original_read_text = type(watcher.request_file).read_text
    calls = {"n": 0}

    def flaky_read_text(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "Permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(watcher.request_file), "read_text", flaky_read_text)

    requested = []
    watcher.show_requested.connect(requested.append)
    with qtbot.waitSignal(watcher.show_requested, timeout=3000) as blocker:
        watcher.check_request()
    qtbot.wait(300)

    assert blocker.args == ["quarantine"]
    # 失敗2回の後に読めている（Linuxではファイル作成時のフォルダ変更通知でも読みに来るため回数は3回以上）
    assert calls["n"] >= 3
    assert requested == ["quarantine"]  # 依頼1件につき通知は1回だけ


# -- 補助プログラム notify_activate.py（Windowsでのみ意味を持つが、ロジックはOS非依存で確認する） -----
def test_helper_writes_request_that_watcher_accepts(qtbot, tmp_path, monkeypatch):
    import notify_activate

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    watcher = ActivationWatcher(tmp_path / "clamqtpie")

    with qtbot.waitSignal(watcher.show_requested, timeout=3000) as blocker:
        assert notify_activate.main() == 0

    assert blocker.args == ["quarantine"]
    assert not (tmp_path / "clamqtpie" / "show_request.tmp").exists()


def test_helper_constants_match_activation(monkeypatch, tmp_path):
    import platform

    import notify_activate

    # 補助プログラムはPySide6等をimportしないため値を複製している。本体側と食い違わないこと
    assert notify_activate.REQUEST_FILE_NAME == REQUEST_FILE_NAME
    assert notify_activate.SCREEN_QUARANTINE == SCREEN_QUARANTINE
    from core.version import APP_SLUG

    assert notify_activate.APP_SLUG == APP_SLUG
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert notify_activate.request_file_path() == user_state_dir() / REQUEST_FILE_NAME


def test_helper_does_not_import_heavy_modules():
    import subprocess
    import sys

    # クリックから画面表示までを速くするため、補助プログラムはPySide6を読み込まない
    code = "import sys, notify_activate; print('PySide6' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"

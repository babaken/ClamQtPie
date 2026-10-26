"""
タスクトレイ常駐アプリプロセス設計書6章のテスト
"""
from unittest.mock import MagicMock, patch

from core.autostart_service import AutostartService
from core.exceptions import ProcessError


def _linux_service() -> AutostartService:
    # Windows上で実行しても実レジストリに触れないよう、Linux経路を明示する
    service = AutostartService("/opt/ClamAVFrontend/ClamAVFrontend")
    service._is_windows = False
    return service


def test_linux_enable_creates_desktop_file(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    service = _linux_service()

    service.enable()

    desktop_file = tmp_path / ".config" / "autostart" / "clamqtpie.desktop"
    assert desktop_file.exists()
    content = desktop_file.read_text()
    assert "Exec=/opt/ClamAVFrontend/ClamAVFrontend" in content
    assert service.is_enabled() is True


def test_linux_disable_removes_desktop_file(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    service = _linux_service()
    service.enable()

    service.disable()

    assert service.is_enabled() is False


def test_linux_disable_when_not_enabled_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    service = _linux_service()

    service.disable()  # 例外を送出しないこと

    assert service.is_enabled() is False


def test_windows_enable_uses_winreg(monkeypatch):
    service = AutostartService("C:\\Program Files\\ClamAVFrontend\\ClamAVFrontend.exe")
    service._is_windows = True

    fake_winreg = MagicMock()
    fake_winreg.HKEY_CURRENT_USER = "HKCU"
    fake_winreg.KEY_SET_VALUE = 1
    fake_winreg.REG_SZ = 1
    fake_winreg.OpenKey.return_value = MagicMock()

    with patch.dict("sys.modules", {"winreg": fake_winreg}):
        service.enable()

    fake_winreg.SetValueEx.assert_called_once()


def test_windows_enable_failure_raises_process_error(monkeypatch):
    service = AutostartService("C:\\ClamAVFrontend.exe")
    service._is_windows = True

    fake_winreg = MagicMock()
    fake_winreg.OpenKey.side_effect = OSError("registry access denied")

    with patch.dict("sys.modules", {"winreg": fake_winreg}):
        try:
            service.enable()
            assert False, "should have raised"
        except ProcessError as exc:
            assert exc.code == "E8002"

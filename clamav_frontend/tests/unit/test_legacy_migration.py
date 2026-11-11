"""
製品名の変更（ClamAVフロントエンド → ClamQtPie）で、旧名の登録を引き継ぐ処理（core/legacy_migration.py）のテスト

実レジストリには触れない（winregはモックに差し替える）。
"""
from unittest.mock import MagicMock, patch

from core.autostart_service import AutostartService
from core.legacy_migration import remove_legacy_protocol, remove_legacy_state_dir


# -- 自動起動 ------------------------------------------------------------------
def _linux_autostart(tmp_path, monkeypatch) -> AutostartService:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    service = AutostartService("/opt/ClamQtPie/ClamQtPie")
    service._is_windows = False
    return service


def test_linux_legacy_desktop_file_is_replaced(tmp_path, monkeypatch):
    service = _linux_autostart(tmp_path, monkeypatch)
    autostart_dir = tmp_path / ".config" / "autostart"
    autostart_dir.mkdir(parents=True)
    legacy = autostart_dir / "clamav-frontend.desktop"
    legacy.write_text("[Desktop Entry]\nName=ClamAV Frontend\nExec=/opt/ClamAVFrontend/ClamAVFrontend\n")

    assert service.migrate_legacy() is True

    assert not legacy.exists()
    content = (autostart_dir / "clamqtpie.desktop").read_text()
    assert "Name=ClamQtPie" in content
    assert "Exec=/opt/ClamQtPie/ClamQtPie" in content


def test_linux_without_legacy_desktop_file_does_not_enable(tmp_path, monkeypatch):
    service = _linux_autostart(tmp_path, monkeypatch)

    assert service.migrate_legacy() is False
    assert service.is_enabled() is False


def _fake_run_key(values: dict):
    """Runキーを模したwinreg（DeleteValue・SetValueExでvaluesを書き換える）"""
    winreg = MagicMock()
    winreg.HKEY_CURRENT_USER = "HKCU"
    winreg.REG_SZ = 1

    def delete_value(_key, name):
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    winreg.DeleteValue.side_effect = delete_value
    winreg.SetValueEx.side_effect = lambda _key, name, _r, _t, value: values.__setitem__(name, value)
    return winreg


def test_windows_legacy_run_value_is_replaced_with_current_exe():
    values = {"ClamAVFrontend": r"C:\Tools\ClamAVFrontend\ClamAVFrontend.exe"}
    service = AutostartService(r"C:\Tools\ClamQtPie\ClamQtPie.exe")
    service._is_windows = True

    with patch.dict("sys.modules", {"winreg": _fake_run_key(values)}):
        assert service.migrate_legacy() is True

    assert values == {"ClamQtPie": r"C:\Tools\ClamQtPie\ClamQtPie.exe"}


def test_windows_without_legacy_run_value_does_not_enable():
    values = {}
    service = AutostartService(r"C:\Tools\ClamQtPie\ClamQtPie.exe")
    service._is_windows = True

    with patch.dict("sys.modules", {"winreg": _fake_run_key(values)}):
        assert service.migrate_legacy() is False

    assert values == {}


# -- URLスキーム ---------------------------------------------------------------
def _fake_classes(command: str | None):
    winreg = MagicMock()
    winreg.HKEY_CURRENT_USER = "HKCU"
    if command is None:
        winreg.OpenKey.side_effect = FileNotFoundError("no key")
    winreg.QueryValueEx.return_value = (command, 1)
    return winreg


def test_legacy_protocol_registered_by_app_is_removed():
    winreg = _fake_classes(r'"C:\Tools\ClamAVFrontend\ClamAVFrontendActivate.exe"')

    assert remove_legacy_protocol(winreg) is True

    deleted = [c.args[1] for c in winreg.DeleteKey.call_args_list]
    # 子のキーから順に消す（winreg.DeleteKeyはサブキーのあるキーを消せない）
    assert deleted == [
        r"Software\Classes\clamqt\shell\open\command",
        r"Software\Classes\clamqt\shell\open",
        r"Software\Classes\clamqt\shell",
        r"Software\Classes\clamqt",
    ]


def test_legacy_protocol_of_other_app_is_kept():
    winreg = _fake_classes(r'"C:\Other\other.exe" "%1"')

    assert remove_legacy_protocol(winreg) is False
    winreg.DeleteKey.assert_not_called()


def test_missing_legacy_protocol_does_nothing():
    winreg = _fake_classes(None)

    assert remove_legacy_protocol(winreg) is False
    winreg.DeleteKey.assert_not_called()


# -- ユーザー単位のフォルダ ----------------------------------------------------
def test_legacy_state_dir_with_only_app_files_is_removed(tmp_path):
    legacy = tmp_path / "clamav-frontend"
    legacy.mkdir()
    (legacy / "show_request").write_text("quarantine")
    (legacy / "app.lock").write_text("")

    assert remove_legacy_state_dir(legacy) is True
    assert not legacy.exists()


def test_legacy_state_dir_with_other_files_is_kept(tmp_path):
    legacy = tmp_path / "clamav-frontend"
    legacy.mkdir()
    (legacy / "user-file.txt").write_text("keep")

    assert remove_legacy_state_dir(legacy) is False
    assert (legacy / "user-file.txt").exists()

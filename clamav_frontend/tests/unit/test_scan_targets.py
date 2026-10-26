"""
クイックスキャン／フルスキャンの対象（core/scan_targets.py、Phase 11で確定）のテスト
"""
from core import scan_targets


def test_quick_scan_targets_are_existing_unique_standard_folders(qapp, tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    locations = {"DesktopLocation": str(desktop), "DownloadLocation": str(tmp_path / "missing"),
                 "DocumentsLocation": str(desktop), "TempLocation": str(tmp_path)}
    monkeypatch.setattr(
        scan_targets.QStandardPaths, "writableLocation", lambda loc: locations[loc.name]
    )
    monkeypatch.setattr(scan_targets.platform, "system", lambda: "Linux")

    # 存在しないフォルダは除外、同じフォルダは1回だけ
    assert scan_targets.quick_scan_targets() == [str(desktop), str(tmp_path)]


def test_quick_scan_includes_startup_folder_on_windows(qapp, tmp_path, monkeypatch):
    startup = tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True)
    monkeypatch.setattr(scan_targets.QStandardPaths, "writableLocation", lambda loc: "")
    monkeypatch.setattr(scan_targets.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert scan_targets.quick_scan_targets() == [str(startup)]


def test_full_scan_is_system_drive(monkeypatch):
    monkeypatch.setattr(scan_targets.platform, "system", lambda: "Windows")
    monkeypatch.setenv("SystemDrive", "D:")
    assert scan_targets.full_scan_targets() == ["D:\\"]
    assert scan_targets.full_scan_skip_dirs() == ()

    monkeypatch.setattr(scan_targets.platform, "system", lambda: "Linux")
    assert scan_targets.full_scan_targets() == ["/"]
    assert "/proc" in scan_targets.full_scan_skip_dirs()

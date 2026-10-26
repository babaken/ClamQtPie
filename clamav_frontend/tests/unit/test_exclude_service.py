"""
ExcludeService（画面設計書3章の除外設定タブに対応）のユニットテスト
"""
import sys

import pytest

from core.exclude_service import ExcludeService
from db.database import DatabaseService


@pytest.fixture
def exclude_service(tmp_path):
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    service = ExcludeService(db_service)
    yield service
    db_service.close()


def test_add_and_list(exclude_service):
    exclude_service.add("extension", ".log")
    exclude_service.add("folder", "/tmp/ignored")

    rows = exclude_service.list_all()
    assert len(rows) == 2


def test_is_excluded_by_file(exclude_service):
    exclude_service.add("file", "/tmp/a.txt")
    assert exclude_service.is_excluded("/tmp/a.txt") is True
    assert exclude_service.is_excluded("/tmp/b.txt") is False


def test_is_excluded_by_folder(exclude_service):
    exclude_service.add("folder", "/tmp/ignored")
    assert exclude_service.is_excluded("/tmp/ignored/sub/file.txt") is True
    assert exclude_service.is_excluded("/tmp/other/file.txt") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows固有のパス比較仕様")
def test_is_excluded_ignores_case_and_separator_on_windows(exclude_service):
    exclude_service.add("file", r"C:\Users\Test\a.txt")
    exclude_service.add("folder", r"D:\Ignored")
    assert exclude_service.is_excluded("c:/users/test/A.TXT") is True
    assert exclude_service.is_excluded(r"d:\ignored\sub\file.txt") is True
    assert exclude_service.is_excluded(r"D:\Other\file.txt") is False


def test_is_excluded_by_extension(exclude_service):
    exclude_service.add("extension", ".log")
    assert exclude_service.is_excluded("/tmp/app.log") is True
    assert exclude_service.is_excluded("/tmp/app.txt") is False


def test_disabled_rule_is_not_applied(exclude_service):
    row = exclude_service.add("extension", ".log")
    exclude_service.set_enabled(row.id, False)
    assert exclude_service.is_excluded("/tmp/app.log") is False


def test_remove_rule(exclude_service):
    row = exclude_service.add("file", "/tmp/a.txt")
    exclude_service.remove(row.id)
    assert exclude_service.list_all() == []


def test_process_type_not_used_in_is_excluded(exclude_service):
    # Windowsリアルタイム監視詳細設計書4.2節: プロセス除外はis_excluded()の対象外
    exclude_service.add("process", "notepad.exe")
    assert exclude_service.is_excluded("/tmp/anything.txt") is False


def test_make_matcher_reads_rules_once(exclude_service, monkeypatch):
    exclude_service.add("extension", ".log")
    calls = {"n": 0}
    original = exclude_service.list_all

    def counting_list_all():
        calls["n"] += 1
        return original()

    monkeypatch.setattr(exclude_service, "list_all", counting_list_all)
    matcher = exclude_service.make_matcher()
    results = [matcher(f"/tmp/{i}.log") for i in range(100)]

    assert all(results)
    assert calls["n"] == 1


def test_is_under_any_folder(tmp_path):
    from core.exclude_service import is_under_any_folder

    app = tmp_path / "app"
    (app / "quarantine").mkdir(parents=True)
    assert is_under_any_folder(app / "quarantine" / "x", [app / "quarantine"]) is True
    assert is_under_any_folder(app / "quarantine", [app / "quarantine"]) is True
    assert is_under_any_folder(app / "quarantine-old" / "x", [app / "quarantine"]) is False
    assert is_under_any_folder(tmp_path / "other.txt", [app / "quarantine", app / "logs"]) is False


def test_is_under_any_folder_resolves_symlinks(tmp_path):
    import os

    import pytest

    from core.exclude_service import is_under_any_folder

    real = tmp_path / "real"
    (real / "quarantine").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("シンボリックリンクを作れない環境")
    # 監視対象をリンク経由で指定していても、アプリのフォルダと判定できる
    assert is_under_any_folder(link / "quarantine" / "x", [real / "quarantine"]) is True

"""
リンクをたどらないファイル操作（core/safe_fs.py）と、それを使う隔離・復元のテスト

途中のフォルダやファイル自体がリンク（Windows: ジャンクション、Linux: シンボリックリンク）に
置き換えられていても、リンク先のファイルを移動・上書きしないことを確かめる。
"""
import hashlib
import os
import platform

import pytest

from core.exceptions import QuarantineRestoreError, QuarantineWriteError
from core.quarantine_service import QuarantineService
from core.safe_fs import UnsafePathError, quarantine_move, restore_move
from db.database import DatabaseService

_IS_WINDOWS = platform.system() == "Windows"


def _link_dir(link, target):
    """フォルダへのリンクを作る（Windowsはジャンクション: 管理者権限なしで作れる）"""
    if _IS_WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _link_file(link, target):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("ファイルのシンボリックリンクを作れない環境（Windowsの権限など）")


@pytest.fixture
def area(tmp_path):
    quarantine = tmp_path / "moved"  # quarantine_move・restore_moveを直接試す移動先（QuarantineServiceとは別）
    quarantine.mkdir()
    protected = tmp_path / "protected"  # リンク先（操作されてはいけない場所）
    protected.mkdir()
    (protected / "important.dll").write_bytes(b"system file")
    return {"tmp": tmp_path, "quarantine": quarantine, "protected": protected}


# -- quarantine_move -----------------------------------------------------------
def test_quarantine_move_moves_file_and_returns_hash(area):
    src = area["tmp"] / "infected.exe"
    src.write_bytes(b"malware")
    dest = area["quarantine"] / "uuid-1"

    sha256, size = quarantine_move(src, dest)

    assert (sha256, size) == (hashlib.sha256(b"malware").hexdigest(), 7)
    assert not src.exists()
    assert dest.read_bytes() == b"malware"


def test_quarantine_move_refuses_path_through_linked_folder(area):
    link = area["tmp"] / "downloads"
    _link_dir(link, area["protected"])

    with pytest.raises(UnsafePathError):
        quarantine_move(link / "important.dll", area["quarantine"] / "uuid-1")

    assert (area["protected"] / "important.dll").read_bytes() == b"system file"
    assert list(area["quarantine"].iterdir()) == []


def test_quarantine_move_refuses_symlink_file(area):
    link = area["tmp"] / "infected.exe"
    _link_file(link, area["protected"] / "important.dll")

    with pytest.raises(UnsafePathError):
        quarantine_move(link, area["quarantine"] / "uuid-1")

    assert (area["protected"] / "important.dll").exists()
    assert os.path.lexists(link)


def test_quarantine_move_refuses_directory(area):
    # Windowsではフォルダを通常のファイルとして開けず、アクセス拒否（PermissionError）になる
    with pytest.raises(OSError):
        quarantine_move(area["protected"], area["quarantine"] / "uuid-1")
    assert (area["protected"] / "important.dll").exists()


def test_quarantine_move_missing_file_is_file_not_found(area):
    with pytest.raises(FileNotFoundError):
        quarantine_move(area["tmp"] / "missing.exe", area["quarantine"] / "uuid-1")


# -- restore_move --------------------------------------------------------------
def _quarantined(area, content=b"restored"):
    src = area["quarantine"] / "uuid-1"
    src.write_bytes(content)
    return src


def test_restore_move_creates_missing_folders(area):
    src = _quarantined(area)
    target = area["tmp"] / "a" / "b" / "file.txt"

    restore_move(src, target, overwrite=False)

    assert target.read_bytes() == b"restored"
    assert not src.exists()


def test_restore_move_refuses_linked_parent_folder(area):
    src = _quarantined(area, b"quarantined")
    link = area["tmp"] / "original"
    _link_dir(link, area["protected"])

    with pytest.raises(UnsafePathError):
        restore_move(src, link / "important.dll", overwrite=True)

    assert (area["protected"] / "important.dll").read_bytes() == b"system file"
    assert src.read_bytes() == b"quarantined"  # 隔離フォルダのファイルはそのまま残る


def test_restore_move_refuses_linked_ancestor_folder(area):
    src = _quarantined(area, b"quarantined")
    link = area["tmp"] / "original"
    _link_dir(link, area["protected"])

    with pytest.raises(UnsafePathError):
        restore_move(src, link / "new" / "file.dll", overwrite=False)

    assert not (area["protected"] / "new").exists()  # リンク先にフォルダも作らない


def test_restore_move_does_not_replace_directory_or_link(area):
    src = _quarantined(area)
    (area["tmp"] / "existing_dir").mkdir()
    link = area["tmp"] / "existing_link"
    _link_dir(link, area["protected"])

    for target in (area["tmp"] / "existing_dir", link):
        with pytest.raises(UnsafePathError):
            restore_move(src, target, overwrite=True)

    assert (area["protected"] / "important.dll").exists()
    assert src.exists()


def test_restore_move_overwrite_and_exists(area):
    target = area["tmp"] / "file.txt"
    target.write_bytes(b"old")
    src = _quarantined(area)

    with pytest.raises(FileExistsError):
        restore_move(src, target, overwrite=False)
    assert target.read_bytes() == b"old"

    restore_move(src, target, overwrite=True)
    assert target.read_bytes() == b"restored"


# -- QuarantineService（隔離・復元の画面・リアルタイム監視から使う入口） ------------------------
@pytest.fixture
def service(tmp_path):
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    yield QuarantineService(db_service, tmp_path / "quarantine")
    db_service.close()


def test_quarantine_through_linked_folder_is_write_error(service, area):
    link = area["tmp"] / "downloads"
    _link_dir(link, area["protected"])

    with pytest.raises(QuarantineWriteError):
        service.quarantine(link / "important.dll", "Fake-Signature")

    assert (area["protected"] / "important.dll").read_bytes() == b"system file"
    assert service.list_all() == []


def test_restore_after_original_folder_replaced_by_link_is_refused(service, area):
    original_dir = area["tmp"] / "original"
    original_dir.mkdir()
    infected = original_dir / "important.dll"
    infected.write_bytes(b"quarantined")
    row = service.quarantine(infected, "Fake-Signature")

    # 隔離の後で元のフォルダがリンク（保護された場所を指す）に変わっていても、リンク先には復元しない
    original_dir.rmdir()
    _link_dir(original_dir, area["protected"])

    with pytest.raises(QuarantineRestoreError):
        service.restore(row.id, overwrite=True)

    assert (area["protected"] / "important.dll").read_bytes() == b"system file"
    assert service.get(row.id).restored == 0

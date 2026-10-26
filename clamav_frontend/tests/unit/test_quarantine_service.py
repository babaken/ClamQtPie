"""
テスト仕様書3.3節: QT-U01〜U05 に対応
"""
import stat

import pytest

from core.exceptions import QuarantineNotFoundError, QuarantineWriteError
from core.quarantine_service import QuarantineService
from db.database import DatabaseService
from tests.conftest import FAKE_INFECTED_CONTENT


@pytest.fixture
def quarantine_service(tmp_path):
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    service = QuarantineService(db_service, tmp_path / "quarantine")
    yield service
    db_service.close()


def _make_eicar(tmp_path, name="eicar.txt"):
    src = tmp_path / name
    # 隔離処理はファイル内容に依存しないため、常駐AVに消されないダミー内容を使う
    src.write_text(FAKE_INFECTED_CONTENT)
    return src


def test_quarantine_moves_file_and_sets_readonly(quarantine_service, tmp_path):
    # QT-U01
    src = _make_eicar(tmp_path)
    row = quarantine_service.quarantine(src, "Eicar-Test-Signature")

    dest = quarantine_service.quarantine_dir / row.uuid
    assert not src.exists()
    assert dest.exists()

    mode = dest.stat().st_mode
    assert not (mode & stat.S_IWUSR)  # 書き込み不可
    assert not (mode & stat.S_IXUSR)  # 実行不可


def test_quarantine_registers_metadata_in_db(quarantine_service, tmp_path):
    # QT-U02
    src = _make_eicar(tmp_path)
    row = quarantine_service.quarantine(src, "Eicar-Test-Signature")

    assert row.virus_name == "Eicar-Test-Signature"
    assert row.size > 0
    assert len(row.sha256) == 64
    assert row.original_path == str(src)
    assert row.restored == 0
    assert row.deleted == 0


def test_quarantine_rolls_back_file_on_db_failure(quarantine_service, tmp_path, monkeypatch):
    # QT-U03: DB登録失敗時にファイルが元に戻ることを確認
    src = _make_eicar(tmp_path)

    def fake_session():
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(quarantine_service.db_service, "session", fake_session)

    with pytest.raises(QuarantineWriteError) as exc_info:
        quarantine_service.quarantine(src, "Eicar-Test-Signature")

    assert exc_info.value.code == "E3004"
    assert src.exists()  # ロールバックにより元の場所に復元されている
    assert list(quarantine_service.quarantine_dir.iterdir()) == []


def test_restore_moves_file_back_and_updates_flag(quarantine_service, tmp_path):
    # QT-U04
    src = _make_eicar(tmp_path)
    row = quarantine_service.quarantine(src, "Eicar-Test-Signature")

    quarantine_service.restore(row.id)

    assert src.exists()
    restored_row = quarantine_service.get(row.id)
    assert restored_row.restored == 1


def test_restore_nonexistent_id_raises_not_found(quarantine_service):
    # QT-U05
    with pytest.raises(QuarantineNotFoundError):
        quarantine_service.restore(9999)


def test_delete_permanently_removes_file_and_sets_flag(quarantine_service, tmp_path):
    src = _make_eicar(tmp_path)
    row = quarantine_service.quarantine(src, "Eicar-Test-Signature")
    dest = quarantine_service.quarantine_dir / row.uuid

    quarantine_service.delete_permanently(row.id)

    assert not dest.exists()
    deleted_row = quarantine_service.get(row.id)
    assert deleted_row.deleted == 1


def test_list_all_returns_all_quarantine_rows(quarantine_service, tmp_path):
    src1 = _make_eicar(tmp_path, "a.txt")
    src2 = _make_eicar(tmp_path, "b.txt")
    quarantine_service.quarantine(src1, "Eicar-Test-Signature")
    quarantine_service.quarantine(src2, "Eicar-Test-Signature")

    rows = quarantine_service.list_all()
    assert len(rows) == 2


def test_restore_does_not_overwrite_existing_file_unless_requested(quarantine_service, tmp_path):
    from core.exceptions import QuarantineRestoreError

    src = _make_eicar(tmp_path)
    row = quarantine_service.quarantine(src, "Eicar-Test-Signature")
    src.write_text("user's new file")  # 同じ場所に新しいファイルができた

    with pytest.raises(QuarantineRestoreError):
        quarantine_service.restore(row.id)
    assert src.read_text() == "user's new file"

    quarantine_service.restore(row.id, overwrite=True)
    assert src.read_text() == FAKE_INFECTED_CONTENT


def test_file_inside_quarantine_folder_is_not_quarantined_again(quarantine_service, tmp_path):
    # 隔離済みのファイルを隔離し直すと、記録とファイルの対応が崩れ、リアルタイム監視では
    # 隔離が無限に繰り返された（Linux実機で発見）
    record = quarantine_service.quarantine(_make_eicar(tmp_path), "Eicar-Test-Signature")
    stored = quarantine_service.quarantine_dir / record.uuid

    with pytest.raises(QuarantineWriteError):
        quarantine_service.quarantine(stored, "Eicar-Test-Signature")

    assert stored.exists()
    assert len(quarantine_service.list_all()) == 1

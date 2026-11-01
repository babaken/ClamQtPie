"""隔離ファイルの変換・所有者の復帰・UTF-8 にできない名前の隔離のテスト（段階 12-2d）"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from core import quarantine_codec
from core.exceptions import QuarantineCorruptedError
from core.quarantine_service import QuarantineService
from db.database import DatabaseService
from db.models import Quarantine

CONTENT = b"MZ" + bytes(range(256)) * 300 + b"tail"


@pytest.fixture
def env(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    work = tmp_path / "work"
    work.mkdir()
    yield db, QuarantineService(db, tmp_path / "quarantine"), work
    db.close()


# -- 変換 -----------------------------------------------------------------------------------
def test_codec_round_trip_and_tamper_detection(tmp_path):
    import hashlib

    src = tmp_path / "plain"
    src.write_bytes(CONTENT)
    encoded = tmp_path / "encoded"
    quarantine_codec.encode_file(src, encoded, hashlib.sha256(CONTENT).hexdigest(), len(CONTENT))
    data = encoded.read_bytes()
    assert data.startswith(b"CQPQ") and CONTENT[:64] not in data
    quarantine_codec.decode_file(encoded, tmp_path / "back")
    assert (tmp_path / "back").read_bytes() == CONTENT

    tampered = tmp_path / "tampered"
    tampered.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(quarantine_codec.QuarantineFormatError):
        quarantine_codec.decode_file(tampered, tmp_path / "out")
    assert not (tmp_path / "out").exists()  # 一致しない中身は書き出さない
    (tmp_path / "other").write_bytes(b"not a quarantine file")
    with pytest.raises(quarantine_codec.QuarantineFormatError):
        quarantine_codec.decode_file(tmp_path / "other", tmp_path / "out2")


def test_quarantined_file_is_not_kept_as_is(env):
    # 他のウイルス対策ソフトに検知・実行されないよう、元の内容のままでは置かない
    _db, service, work = env
    target = work / "x.exe"
    target.write_bytes(CONTENT)
    row = service.quarantine(target, "Eicar")
    stored = (service.quarantine_dir / row.uuid).read_bytes()
    assert stored.startswith(b"CQPQ") and CONTENT[:64] not in stored
    assert row.encoding == "xor-v1"
    assert sorted(p.name for p in service.quarantine_dir.iterdir()) == [row.uuid]  # 途中のファイルを残さない
    service.restore(row.id)
    assert target.read_bytes() == CONTENT
    assert list(service.quarantine_dir.iterdir()) == []


def test_corrupted_quarantine_file_is_not_restored(env):
    _db, service, work = env
    target = work / "x.exe"
    target.write_bytes(CONTENT)
    row = service.quarantine(target, "Eicar")
    stored = service.quarantine_dir / row.uuid
    os.chmod(stored, stat.S_IRUSR | stat.S_IWUSR)
    data = stored.read_bytes()
    stored.write_bytes(data[:100] + bytes([data[100] ^ 0xFF]) + data[101:])
    with pytest.raises(QuarantineCorruptedError):
        service.restore(row.id)
    assert not target.exists()
    assert service.get(row.id).restored == 0
    assert sorted(p.name for p in service.quarantine_dir.iterdir()) == [row.uuid]


def test_files_quarantined_by_older_versions_are_restored(env):
    # 以前の版で隔離した（変換していない）ファイルは、そのまま戻す
    db, service, work = env
    service.quarantine_dir.mkdir(exist_ok=True)
    (service.quarantine_dir / "legacy").write_bytes(CONTENT)
    with db.session() as session:
        row = Quarantine(uuid="legacy", original_path=str(work / "old.exe"), virus_name="Eicar", sha256="",
                         size=len(CONTENT), detected_at="2026-01-01T00:00:00+00:00", restored=0, deleted=0)
        session.add(row)
        session.commit()
        row_id = row.id
    assert service.get(row_id).encoding == "none"
    service.restore(row_id)
    assert (work / "old.exe").read_bytes() == CONTENT


def test_failed_record_puts_the_file_back(env, monkeypatch):
    db, service, work = env
    target = work / "x.exe"
    target.write_bytes(CONTENT)

    def broken_session():
        raise RuntimeError("DB に書けない")

    monkeypatch.setattr(db, "session", broken_session)
    with pytest.raises(Exception):
        service.quarantine(target, "Eicar")
    assert target.read_bytes() == CONTENT
    assert list(service.quarantine_dir.iterdir()) == []


# -- 所有者・権限 ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="POSIX の権限")
def test_permissions_are_restored_without_setuid(env):
    _db, service, work = env
    target = work / "tool"
    target.write_bytes(CONTENT)
    os.chmod(target, 0o4750)
    row = service.quarantine(target, "Eicar")
    service.restore(row.id)
    # 元の権限に戻すが、setuid・setgid は戻さない（復元したファイルが別の利用者の権限で動かないように）
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o750


@pytest.mark.skipif(os.name != "nt", reason="Windows のセキュリティ記述子")
def test_windows_security_descriptor_is_recorded_and_applied(env):
    import json

    _db, service, work = env
    target = work / "x.exe"
    target.write_bytes(CONTENT)
    row = service.quarantine(target, "Eicar")
    assert json.loads(row.owner_info)["sddl"].startswith("O:")
    service.restore(row.id)
    assert target.read_bytes() == CONTENT


# -- UTF-8 にできない名前 -------------------------------------------------------------------
def _unusual_file(work: Path) -> Path | None:
    if os.name == "posix":
        path = Path(os.fsdecode(os.fsencode(str(work)) + b"/eicar\xfe.com"))
    else:
        path = work / "eicar\ud800.com"
    try:
        path.write_bytes(CONTENT)
    except (OSError, UnicodeEncodeError):
        return None
    return path


def test_file_with_unusual_name_can_be_quarantined_and_restored(env):
    # 名前を DB の文字列に書けないファイルも、元のバイト列を持って隔離・復元できる
    _db, service, work = env
    path = _unusual_file(work)
    if path is None:
        pytest.skip("この環境ではその名前のファイルを作れない")
    (work / "eicar.com").write_bytes(b"other file")  # 同じに見える別のファイル
    row = service.quarantine(path, "Eicar")
    assert not os.path.exists(path)
    assert (work / "eicar.com").read_bytes() == b"other file"
    assert r"\u" in service.get(row.id).original_path  # 表示用は見える形
    service.restore(row.id)
    assert Path(path).read_bytes() == CONTENT
    assert (work / "eicar.com").read_bytes() == b"other file"

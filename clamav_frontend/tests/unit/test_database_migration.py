"""
テスト仕様書3.7節: DB-U01, DB-U02 に対応
"""
import pytest
from sqlalchemy import inspect

from db.database import DatabaseService


def test_migration_creates_all_initial_tables(tmp_path):
    # DB-U01
    db_path = tmp_path / "app.db"
    service = DatabaseService(db_path)

    service.connect()

    inspector = inspect(service._engine)
    tables = set(inspector.get_table_names())
    assert {"quarantine", "schedule", "exclude", "alembic_version"} <= tables
    service.close()


def test_migration_is_idempotent(tmp_path):
    # DB-U02: 既にマイグレーション済みのDBに再接続しても壊れない
    db_path = tmp_path / "app.db"
    service = DatabaseService(db_path)
    service.connect()
    service.close()

    service2 = DatabaseService(db_path)
    service2.connect()  # 再度migrate()が呼ばれるが、不足分がないため何も起きない
    inspector = inspect(service2._engine)
    assert "quarantine" in inspector.get_table_names()
    service2.close()


def test_session_can_insert_and_query(tmp_path):
    from db.models import Quarantine

    db_path = tmp_path / "app.db"
    service = DatabaseService(db_path)
    service.connect()

    with service.session() as session:
        session.add(Quarantine(uuid="abc", original_path="/tmp/eicar.txt", virus_name="Eicar-Test"))
        session.commit()

        row = session.query(Quarantine).filter_by(uuid="abc").one()
        assert row.virus_name == "Eicar-Test"

    service.close()


@pytest.mark.skipif(
    __import__("platform").system() == "Windows", reason="POSIXのパーミッションのみ対象"
)
def test_db_file_is_owner_only_on_posix(tmp_path):
    # セキュリティレビュー: app.dbには隔離元パス等が入るため、他ユーザーに読まれないよう0600にする
    import os
    import stat

    db_path = tmp_path / "app.db"
    service = DatabaseService(db_path)
    service.connect()
    try:
        mode = stat.S_IMODE(os.stat(db_path).st_mode)
        assert mode == 0o600, oct(mode)
        # グループ・その他に読み取り権限が無いこと
        assert not (mode & (stat.S_IRGRP | stat.S_IROTH))
    finally:
        service.close()


@pytest.mark.skipif(
    __import__("platform").system() == "Windows", reason="POSIXのパーミッションのみ対象"
)
def test_existing_world_readable_db_is_tightened(tmp_path):
    # 既存の誰でも読めるapp.db（過去バージョンで作られた0644等）も、接続時に0600へ直す
    import os
    import stat

    db_path = tmp_path / "app.db"
    DatabaseService(db_path).connect()  # 1回作る
    os.chmod(db_path, 0o644)  # 過去の緩い権限を再現

    service = DatabaseService(db_path)
    service.connect()
    try:
        assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600
    finally:
        service.close()

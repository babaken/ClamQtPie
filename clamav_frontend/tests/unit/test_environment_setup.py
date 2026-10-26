"""
Phase 0の完了判定用テスト。
pytestが正常に動作し、主要依存パッケージがimportできることを確認する。
Phase 1以降で本ファイルは実際のユニットテストに置き換えていく。
"""


def test_pyside6_importable():
    import PySide6  # noqa: F401
    from PySide6 import QtWidgets  # noqa: F401


def test_sqlalchemy_and_alembic_importable():
    import sqlalchemy  # noqa: F401
    import alembic  # noqa: F401


def test_apscheduler_importable():
    import apscheduler  # noqa: F401


def test_keyring_importable():
    import keyring  # noqa: F401


def test_watchdog_importable():
    import watchdog  # noqa: F401


def test_eicar_fixture_content(eicar_file):
    content = eicar_file.read_text()
    assert content.startswith("X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR")

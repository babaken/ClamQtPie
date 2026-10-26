"""
gui/screens/quarantine.py のサービス層結線に関するテスト
"""
from dataclasses import dataclass
from unittest.mock import MagicMock

from gui.screens.quarantine import QuarantineScreen


@dataclass
class _FakeRecord:
    id: int
    original_path: str
    virus_name: str
    detected_at: str
    size: int
    sha256: str
    restored: bool
    deleted: bool


def _make_screen(qtbot):
    screen = QuarantineScreen()
    qtbot.addWidget(screen)
    return screen


def test_bind_services_loads_list(qtbot):
    screen = _make_screen(qtbot)
    service = MagicMock()
    service.list_all.return_value = [
        _FakeRecord(1, "/tmp/eicar.txt", "Eicar-Test-Signature", "2026-07-14T00:00:00", 68, "abc123", False, False)
    ]

    screen.bind_services(service)

    assert screen.quarantine_table.rowCount() == 1
    assert screen.quarantine_table.item(0, 0).text() == "/tmp/eicar.txt"
    assert screen.quarantine_table.item(0, 5).text() == "隔離中"


def test_search_filters_by_keyword(qtbot):
    screen = _make_screen(qtbot)
    service = MagicMock()
    service.list_all.return_value = [
        _FakeRecord(1, "/tmp/a.txt", "Eicar-Test-Signature", "2026-07-14T00:00:00", 10, "aaa", False, False),
        _FakeRecord(2, "/tmp/b.txt", "Other-Virus", "2026-07-14T00:00:01", 20, "bbb", False, False),
    ]
    screen.bind_services(service)

    screen.search_edit.setText("Other")

    assert screen.quarantine_table.rowCount() == 1
    assert screen.quarantine_table.item(0, 1).text() == "Other-Virus"


def test_restore_click_calls_service_and_reloads(qtbot, monkeypatch):
    screen = _make_screen(qtbot)
    service = MagicMock()
    service.list_all.return_value = [
        _FakeRecord(1, "/tmp/a.txt", "Eicar-Test-Signature", "2026-07-14T00:00:00", 10, "aaa", False, False)
    ]
    screen.bind_services(service)
    screen.quarantine_table.selectRow(0)

    import gui.screens.quarantine as quarantine_module

    monkeypatch.setattr(quarantine_module, "show_confirm_dialog", lambda *a, **k: True)

    screen.restore_button.click()

    service.restore.assert_called_once_with(1, dest_path=None, overwrite=False)
    assert service.list_all.call_count == 2  # bind_services時 + reload時


def test_delete_click_requires_confirmation(qtbot, monkeypatch):
    screen = _make_screen(qtbot)
    service = MagicMock()
    service.list_all.return_value = [
        _FakeRecord(1, "/tmp/a.txt", "Eicar-Test-Signature", "2026-07-14T00:00:00", 10, "aaa", False, False)
    ]
    screen.bind_services(service)
    screen.quarantine_table.selectRow(0)

    import gui.screens.quarantine as quarantine_module

    monkeypatch.setattr(quarantine_module, "show_confirm_dialog", lambda *a, **k: False)

    screen.delete_button.click()

    service.delete_permanently.assert_not_called()


# -- Phase 11: 一括削除・詳細表示・期間フィルタ・省略表示・復元時の同名ファイル ------------------
import gui.screens.quarantine as quarantine_module  # noqa: E402


@dataclass
class _Record(_FakeRecord):
    uuid: str = "uuid-1"


def _screen_with(qtbot, records):
    screen = _make_screen(qtbot)
    service = MagicMock()
    service.list_all.return_value = records
    screen.bind_services(service)
    return screen, service


def _rec(id_, path="/tmp/a.txt", detected_at="2026-10-19T00:00:00+00:00", restored=False, deleted=False):
    return _Record(id_, path, "Eicar", detected_at, 2048, "a" * 64, restored, deleted)


def test_sha256_is_elided_with_full_tooltip_and_size_is_human_readable(qtbot):
    screen, _ = _screen_with(qtbot, [_rec(1)])
    sha_item = screen.quarantine_table.item(0, 4)
    assert sha_item.text() == "a" * 12 + "…"
    assert sha_item.toolTip() == "a" * 64
    assert screen.quarantine_table.item(0, 3).text() == "2.0 KB"


def test_bulk_delete_only_quarantined_rows(qtbot, monkeypatch):
    screen, service = _screen_with(qtbot, [_rec(1), _rec(2), _rec(3, restored=True)])
    monkeypatch.setattr(quarantine_module, "show_confirm_dialog", lambda *a, **k: True)
    screen.quarantine_table.selectAll()

    screen.delete_button.click()

    assert [c.args[0] for c in service.delete_permanently.call_args_list] == [1, 2]


def test_detail_shows_all_fields(qtbot, monkeypatch):
    screen, _ = _screen_with(qtbot, [_rec(1)])
    shown = []
    monkeypatch.setattr(quarantine_module.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    screen.quarantine_table.selectRow(0)

    screen.detail_button.click()

    assert "a" * 64 in shown[0]
    assert "uuid-1" in shown[0]


def test_period_filter(qtbot):
    from PySide6.QtCore import QDate

    screen, _ = _screen_with(
        qtbot, [_rec(1, detected_at="2026-01-05T12:00:00+00:00"), _rec(2, detected_at="2026-10-19T12:00:00+00:00")]
    )
    screen.date_from_edit.setDate(QDate(2026, 10, 1))
    screen.date_to_edit.setDate(QDate(2026, 10, 31))
    screen.period_check.setChecked(True)

    assert screen.quarantine_table.rowCount() == 1


def test_restore_conflict_overwrite_rename_cancel(qtbot, monkeypatch, tmp_path):
    existing = tmp_path / "a.txt"
    existing.write_text("new file")
    screen, service = _screen_with(qtbot, [_rec(1, path=str(existing))])
    monkeypatch.setattr(quarantine_module, "show_confirm_dialog", lambda *a, **k: True)

    for choice, expected in (
        ("overwrite", {"dest_path": None, "overwrite": True}),
        ("rename", {"dest_path": str(tmp_path / "a (restored).txt"), "overwrite": True}),
    ):
        service.restore.reset_mock()
        monkeypatch.setattr(screen, "_ask_restore_conflict", lambda path, c=choice: c)
        monkeypatch.setattr(
            quarantine_module.QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "a (restored).txt"), "")
        )
        screen.quarantine_table.selectRow(0)
        screen.restore_button.click()
        service.restore.assert_called_once_with(1, **expected)

    service.restore.reset_mock()
    monkeypatch.setattr(screen, "_ask_restore_conflict", lambda path: "cancel")
    screen.quarantine_table.selectRow(0)
    screen.restore_button.click()
    service.restore.assert_not_called()

"""トレイGUI の通知（gui/tray_notifications.py）・スキャン履歴の表示・診断画面のサービスの状態のテスト（段階 12-4b）"""
from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject, Signal

from core.diagnostics_service import LEVEL_OK, LEVEL_WARN
from core.exceptions import ServiceUnavailableError
from gui.tray_notifications import DesktopToaster, TrayNotifier


class _Events(QObject):
    event = Signal(str, object)


class _Api:
    def __init__(self, pending=()):
        self.pending = list(pending)
        self.calls = []

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if path == "/notifications/pending":
            return {"notifications": list(self.pending), "total": len(self.pending)}
        if path == "/notifications/ack":
            ids = [n["id"] for n in self.pending] if body.get("all") else body["ids"]
            self.pending = [n for n in self.pending if n["id"] not in ids]
            return {"acked": ids}
        raise AssertionError(path)


class _Toaster:
    def __init__(self):
        self.shown, self.closed = [], []

    def show(self, title, body, open_screen, tag):
        future = concurrent.futures.Future()
        future.set_result(tag)
        self.shown.append((title, body, open_screen, tag))
        return future

    def close(self, handle):
        self.closed.append(handle.result())

    def shutdown(self):
        pass


def _note(i, screen="quarantine", title=None):
    return {"id": i, "created_at": "2026-11-03T01:00:00+00:00", "title": title or f"通知{i}", "body": f"本文{i}",
            "open_screen": screen}


def _notifier(pending=()):
    api, events, toaster = _Api(pending), _Events(), _Toaster()
    return TrayNotifier(api, events, toaster), api, events, toaster


def test_single_pending_notification_is_shown_as_is(qapp):
    notifier, _, _, toaster = _notifier([_note(1)])
    counts = []
    notifier.pending_changed.connect(counts.append)
    notifier.sync_pending()
    assert toaster.shown == [("通知1", "本文1", "quarantine", "n1")]
    assert counts == [1]
    notifier.sync_pending()  # つなぎ直しても、出したものは二度出さない
    assert len(toaster.shown) == 1


def test_many_pending_notifications_become_one_summary(qapp):
    notifier, _, _, toaster = _notifier([_note(1), _note(2, screen=None), _note(3)])
    notifier.sync_pending()
    [(title, body, open_screen, tag)] = toaster.shown
    # 同じ種類の通知でも見分けられるよう、本文の最初の行を添える
    assert title == "未読の通知が 3 件あります" and body == "通知3: 本文3\n通知2: 本文2\n他1件"
    assert open_screen is None and tag == "s3"  # 開く画面がそろわなければ、クリックで画面を開かない


def test_events_show_and_close_notifications(qapp):
    notifier, _, events, toaster = _notifier([_note(1), _note(2)])
    notifier.sync_pending()
    events.event.emit("notification", _note(3))
    events.event.emit("notification", _note(3))  # 同じ通知は二度出さない
    assert [s[3] for s in toaster.shown] == ["s2", "n3"]
    assert [n["id"] for n in notifier.pending] == [3, 2, 1]
    # ほかのトレイGUI・WebUI で既読にしたもの: まとめた通知は、全部が既読になってから消す
    events.event.emit("notification.acked", {"ids": [1, 3]})
    assert toaster.closed == ["n3"]
    events.event.emit("notification.acked", {"ids": [2]})
    assert toaster.closed == ["n3", "s2"] and notifier.pending == []


def test_opening_a_screen_from_a_toast_acks_its_notifications(qapp):
    notifier, api, _, toaster = _notifier([_note(1), _note(2, screen=None)])
    notifier.sync_pending()
    notifier.ack_screen("quarantine")
    assert ("POST", "/notifications/ack", {"ids": [1]}) in api.calls
    assert [n["id"] for n in notifier.pending] == [2]
    notifier.ack(None)
    assert ("POST", "/notifications/ack", {"all": True}) in api.calls
    assert notifier.pending == [] and toaster.closed == ["s2"]


def test_unreachable_service_does_not_break_the_tray(qapp, caplog):
    notifier, api, _, toaster = _notifier()
    api.call = MagicMock(side_effect=ServiceUnavailableError())
    notifier.sync_pending()
    notifier.ack_screen("quarantine")
    assert toaster.shown == [] and "未読の通知を読めませんでした" in caplog.text


# -- デスクトップへの表示・削除 ----------------------------------------------------------------
def test_windows_toast_is_tagged_and_removed(qapp):
    toaster = DesktopToaster(system="win32")
    with patch("core.notification_service.subprocess.run") as run:
        handle = toaster.show("ウイルスを検知しました", "C:\\a.txt: Eicar", "quarantine", "n7")
        toaster.close(handle)
        toaster._executor.shutdown(wait=True)
    show_script, remove_script = (c.args[0][-1] for c in run.call_args_list)
    assert "$toast.Tag = $env:CLAMQTPIE_TOAST_TAG" in show_script and "n7" not in show_script
    assert "History.Remove($env:CLAMQTPIE_TOAST_TAG" in remove_script
    assert [c.kwargs["env"]["CLAMQTPIE_TOAST_TAG"] for c in run.call_args_list] == ["n7", "n7"]


def test_linux_notification_is_closed_by_its_number(qapp):
    toaster = DesktopToaster(system="linux")
    with patch("core.notification_service.subprocess.run", return_value=MagicMock(stdout=b"42\n")) as run, \
            patch("core.notification_service.find_system_tool", return_value="/usr/bin/gdbus"):
        toaster.close(toaster.show("検知", "/tmp/a: Eicar", None, "n1"))
        toaster._executor.shutdown(wait=True)
    notify, close = (c.args[0] for c in run.call_args_list)
    assert "--print-id" in notify
    assert close[0] == "/usr/bin/gdbus" and close[-1] == "42" and close[-2].endswith(".CloseNotification")


def test_failure_to_show_is_only_logged(qapp, caplog):
    toaster = DesktopToaster(system="linux")
    with patch("core.notification_service.subprocess.run", side_effect=OSError("no notify-send")):
        handle = toaster.show("検知", "本文", None, "n1")
        toaster.close(handle)  # 出せなかったものは消さない
        toaster._executor.shutdown(wait=True)
    assert handle.result() is None and "デスクトップ通知の表示に失敗しました" in caplog.text


# -- スキャン履歴 ------------------------------------------------------------------------------
_RECORD = dict(id=5, started_at="2026-11-03T01:00:00+00:00", finished_at="2026-11-03T01:00:09+00:00",
               source="schedule", scan_type="schedule", label="毎晩", targets=["/home/u"], scanned=10,
               detected=1, errors=2, errors_truncated=1, result="completed", detail="")


def test_scan_history_is_listed_and_described(qtbot):
    from gui.screens.scan import ScanScreen, history_detail_text

    screen = ScanScreen()
    qtbot.addWidget(screen)
    assert screen.history_card.isHidden()  # 従来のアプリでは出さない
    service = SimpleNamespace(list_recent=lambda: [SimpleNamespace(**_RECORD)])
    screen.bind_history(service)
    assert not screen.history_card.isHidden()
    row = [screen.history_table.item(0, c).text() for c in range(7)]
    assert row[1:] == ["毎晩", "スケジュール", "完了", "10", "1", "2"]

    text = history_detail_text({**_RECORD, "detections": [{"path": "/home/u/x", "virus_name": "Eicar",
                                                            "action": "quarantined"}],
                                "error_files": [{"path": "/home/u/big", "reason": "size_limit", "detail": "上限"}]})
    assert "種類: 毎晩（スケジュール）" in text
    assert "  /home/u/x: Eicar（隔離しました）" in text
    assert "  /home/u/big: サイズの上限（上限）" in text and "ほかに 1 件" in text


# -- 診断画面のサービスの状態 ------------------------------------------------------------------
def test_diagnostics_shows_service_state(qtbot):
    from gui.screens.diagnostics import DiagnosticsScreen

    screen = DiagnosticsScreen()
    qtbot.addWidget(screen)
    assert screen.service_card.isHidden()
    screen.bind_service_info(lambda: [("clamd の接続先", "確かめました", LEVEL_OK),
                                      ("スリープから起こす設定", "スリープ解除タイマー: 電源接続時 有効", "")])
    screen.refresh_service_info()
    qtbot.waitUntil(lambda: "確かめました" in screen.service_label.text(), timeout=5000)
    assert screen.service_label.text() == ("[OK] clamd の接続先: 確かめました\n"
                                           "スリープから起こす設定: スリープ解除タイマー: 電源接続時 有効")


@pytest.mark.parametrize("clamd, expected", [
    ({"reachable": True, "verified": True}, LEVEL_OK),
    ({"reachable": True, "verified": False}, LEVEL_WARN),
])
def test_service_info_rows(qapp, clamd, expected, monkeypatch):
    import tray_main

    monkeypatch.setattr("core.wake_timer.wake_timer_policy", lambda: "スリープから起こす予定: なし")
    proxies = SimpleNamespace(status=SimpleNamespace(get=lambda: {"clamd": clamd}))
    rows = tray_main.service_info(proxies)
    assert rows[0][0] == "clamd の接続先" and rows[0][2] == expected
    assert rows[1] == ("スリープから起こす設定", "スリープから起こす予定: なし", "")

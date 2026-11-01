"""スキャン履歴のテスト（サービス化・Web API設計書 6.2節。段階 12-2c）"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PySide6.QtCore import QObject, Signal

from api.app import API_PREFIX, create_app
from api.auth import ROLE_USER, ApiTokens
from api.context import ApiContext
from core.clamav_service import SIZE_LIMIT_DETAIL_PREFIX
from core.exceptions import ClamdConnectionError, ClamdTimeoutError
from core.scan_history_service import ScanHistoryCollector, ScanHistoryRecorder, ScanHistoryService
from db.database import DatabaseService
from db.models import ScanHistory


@pytest.fixture
def history(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    yield ScanHistoryService(db)
    db.close()


def _collector(**kwargs):
    values = dict(source="gui", scan_type="custom", label="カスタムスキャン", targets=["/data"])
    values.update(kwargs)
    return ScanHistoryCollector(**values)


def test_record_and_read_back(history):
    collector = _collector()
    collector.add("/data/ok.txt", "OK", "")
    collector.add("/data/bad.exe", "FOUND", "Eicar")
    collector.add("/data/big.iso", "ERROR", f"{SIZE_LIMIT_DETAIL_PREFIX} (100 MB)")
    collector.add("/data/locked", "ERROR", "Permission denied")
    history_id = history.record(collector, scanned=4, result="completed")

    record = history.list_all()[0]
    assert record.id == history_id
    assert (record.scanned, record.detected, record.errors, record.result) == (4, 1, 2, "completed")
    detail = history.get(history_id)
    assert detail["detections"] == [{"path": "/data/bad.exe", "virus_name": "Eicar", "action": "notified"}]
    # 検査できなかったファイルは理由とともに残す（サイズの上限超えを他のエラーと区別する）
    assert [(e["path"], e["reason"]) for e in detail["errors"]] == [("/data/big.iso", "size_limit"),
                                                                    ("/data/locked", "permission")]
    assert history.get(999) is None


def test_errors_are_capped(history, monkeypatch):
    monkeypatch.setattr("core.scan_history_service.MAX_ERRORS", 2)
    collector = _collector()
    for i in range(5):
        collector.add(f"/data/{i}", "ERROR", "Permission denied")
    history_id = history.record(collector, scanned=5, result="completed")
    record = history.list_all()[0]
    assert record.errors == 5 and record.errors_truncated == 3
    assert len(history.get(history_id)["errors"]) == 2


def test_list_filters_and_order(history):
    history.record(_collector(source="schedule"), 0, "completed")
    history.record(_collector(source="gui"), 0, "cancelled")
    assert [r.result for r in history.list_all()] == ["cancelled", "completed"]  # 新しい順
    assert [r.source for r in history.list_all(source="schedule")] == ["schedule"]
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert history.list_all(since=future) == []


def test_purge_removes_old_history_with_its_files(history):
    old_id = history.record(_collector(), 0, "completed")
    collector = _collector()
    collector.add("/data/bad.exe", "FOUND", "Eicar")
    collector.add("/data/x", "ERROR", "Permission denied")
    with history.db_service.session() as session:
        session.get(ScanHistory, old_id).started_at = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        session.commit()
    keep_id = history.record(collector, 2, "completed")
    assert history.purge(90) == 1
    assert [r.id for r in history.list_all()] == [keep_id]
    assert history.get(keep_id)["detections"]  # 新しいものの検知は残る


# -- 手動スキャンの記録（ScanDispatcher のシグナル） ---------------------------------------------
class _Dispatcher(QObject):
    started = Signal(str)
    progress = Signal(int, int, str)
    file_scanned = Signal(str, str, str, str)
    error = Signal(object)
    finished = Signal(dict)

    def __init__(self):
        super().__init__()
        self.current = {"source": "api", "scan_type": "quick", "targets": ["/home/a/Downloads"]}


def test_recorder_records_manual_scans(history, qapp):
    dispatcher = _Dispatcher()
    recorder = ScanHistoryRecorder(history, dispatcher)
    dispatcher.started.emit("クイックスキャン")
    dispatcher.progress.emit(0, 0, "スキャン対象のファイルを確認しています...")
    dispatcher.progress.emit(0, 3, "/home/a/Downloads/slow.bin")
    dispatcher.error.emit(ClamdTimeoutError())  # 1ファイルのエラー（スキャンは続く）
    dispatcher.file_scanned.emit("/home/a/Downloads/bad.exe", "FOUND", "Eicar", "")
    dispatcher.finished.emit({"scanned": 3, "detected": 1, "errors": 1, "cancelled": False})

    record = history.list_all()[0]
    assert recorder.last_history_id == record.id
    assert (record.source, record.scan_type, record.label, record.result) == ("api", "quick", "クイックスキャン",
                                                                            "completed")
    detail = history.get(record.id)
    assert detail["errors"][0]["path"] == "/home/a/Downloads/slow.bin"


@pytest.mark.parametrize("error,summary,expected", [
    (ClamdConnectionError(), {"scanned": 1, "cancelled": False}, "failed"),
    (None, {"scanned": 1, "cancelled": True}, "cancelled"),
])
def test_recorder_results(history, qapp, error, summary, expected):
    dispatcher = _Dispatcher()
    recorder = ScanHistoryRecorder(history, dispatcher)  # noqa: F841 - 参照を持っておく（app.py と同じ）
    dispatcher.started.emit("手動スキャン")
    dispatcher.progress.emit(0, 2, "/x")
    if error is not None:
        dispatcher.error.emit(error)
    dispatcher.finished.emit(summary)
    record = history.list_all()[0]
    assert record.result == expected
    assert bool(record.detail) == (expected == "failed")


# -- API ------------------------------------------------------------------------------------
@pytest.fixture
def client(history, tmp_path):
    collector = _collector(targets=["/data/a\nb"])
    collector.add("/data/x\ny.exe", "FOUND", "Eicar")
    collector.add("/data/z", "ERROR", "Permission denied\nforged")
    history.record(collector, 2, "completed")
    history.record(_collector(source="schedule", scan_type="schedule", label="毎晩"), 0, "failed", "clamd")
    context = ApiContext(
        config_service=None, clamav_service=None, realtime_manager=None, scan_dispatcher=None,
        update_service=None, update_dispatcher=None, scheduler_service=None, quarantine_service=None,
        exclude_service=None, watch_target_service=None, log_reader_service=None, scan_history_service=history,
    )
    tokens = ApiTokens(tmp_path / "api_tokens")
    return SimpleNamespace(
        client=TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)),
        headers={"Authorization": f"Bearer {tokens.token_for(ROLE_USER)}"},
    )


def test_history_api(client):
    get = lambda path, **params: client.client.get(f"{API_PREFIX}{path}", headers=client.headers, params=params)  # noqa: E731
    assert client.client.get(f"{API_PREFIX}/scans/history").status_code == 401
    body = get("/scans/history").json()
    assert body["total"] == 2 and body["items"][0]["label"] == "毎晩"
    assert get("/scans/history", source="schedule").json()["total"] == 1
    assert get("/scans/history", source="other").status_code == 400

    history_id = body["items"][1]["id"]
    detail = get(f"/scans/history/{history_id}").json()
    assert detail["detected"] == 1 and detail["errors"] == 1 and len(detail["error_files"]) == 1
    assert detail["error_files"][0]["reason"] == "permission"
    # ファイル名の改行などで、1件の記録が複数行に見えないこと
    assert "\n" not in str(detail)
    response = get("/scans/history/999")
    assert response.status_code == 404 and response.json()["error"]["code"] == "E8109"

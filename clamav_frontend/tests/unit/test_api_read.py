"""読み取りの API のテスト（サービス化・Web API設計書 6章。段階 12-1）"""
from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.app import API_PREFIX, create_app
from api.auth import ROLE_USER, ApiTokens
from api.context import ApiContext
from core.config_service import ConfigService
from core.exclude_service import ExcludeService
from core.log_reader_service import LogReaderService
from core.watch_target_service import WatchTargetService
from core.update_service import UpdateHistoryRecord
from core.scheduler_service import ScheduleRecord
from db.database import DatabaseService
from db.models import Quarantine


class _FakeClamav:
    def __init__(self, reachable=True, db_date="Fri Oct 30 08:00:00 2026"):
        self.reachable = reachable
        self.db_date = db_date

    def ping(self, retry=True):
        return self.reachable

    def get_version(self):
        return "ClamAV 1.5.4", "28170", self.db_date


@pytest.fixture
def env(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    log_path = tmp_path / "logs" / "app.log"
    log_path.parent.mkdir()
    log_path.write_text(
        "2026-10-29 09:00:00,000 [INFO] <スキャン> 古い行\n"
        "2026-10-31 10:00:00,000 [WARNING] <スキャン> =HYPERLINK(\"http://x\")\n"
        "2026-10-31 11:00:00,000 [ERROR] [E8004] <更新> 定義DBの更新に失敗しました\n",
        encoding="utf-8",
    )
    with db.session() as session:
        session.add_all([
            Quarantine(original_path=r"C:\a\x.exe", virus_name="Eicar", sha256="00", size=68,
                       detected_at="2026-10-31T01:00:00+00:00"),
            Quarantine(original_path=r"C:\a\y.exe", virus_name="Eicar", sha256="11", size=68,
                       detected_at="2026-10-30T01:00:00+00:00", restored=1),
        ])
        session.commit()
    exclude_service = ExcludeService(db)
    exclude_service.add("folder", r"C:\excluded")
    watch_target_service = WatchTargetService(db)
    watch_target_service.add(r"C:\Users\a\Downloads")
    schedules = [
        ScheduleRecord(1, "毎晩フルスキャン", True, "0 3 * * *", "", "full", "", "2026-11-01T03:00:00+09:00"),
        ScheduleRecord(2, "止めたジョブ", False, "0 1 * * *", "", "quick", "", "2026-11-01T01:00:00+09:00"),
    ]
    history = [UpdateHistoryRecord(1, "2026-10-31T00:00:00+00:00", "no_change", "28170", "28170")]
    context = ApiContext(
        config_service=config_service,
        clamav_service=_FakeClamav(),
        realtime_manager=SimpleNamespace(is_running=True, mode_name="watchdog監視"),
        scan_dispatcher=SimpleNamespace(is_running=False),
        update_service=SimpleNamespace(last_executed_at=lambda: None, list_history=lambda: history),
        update_dispatcher=SimpleNamespace(is_running=False),
        scheduler_service=SimpleNamespace(list_all=lambda: schedules),
        quarantine_service=SimpleNamespace(list_all=lambda: QuarantineList(db)),
        exclude_service=exclude_service,
        watch_target_service=watch_target_service,
        log_reader_service=LogReaderService(log_path),
    )
    tokens = ApiTokens(tmp_path / "api_tokens")
    client = TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    yield SimpleNamespace(client=client, headers={"Authorization": f"Bearer {tokens.token_for(ROLE_USER)}"},
                          context=context)
    db.close()


def QuarantineList(db):
    with db.session() as session:
        return list(session.query(Quarantine).order_by(Quarantine.detected_at.desc()).all())


def _get(env, path, **params):
    return env.client.get(f"{API_PREFIX}{path}", headers=env.headers, params=params)


@pytest.mark.parametrize(
    "path",
    ["/status", "/quarantine", "/excludes", "/realtime", "/realtime/targets", "/schedules", "/updates/history",
     "/logs", "/logs/export.csv"],
)
def test_read_apis_need_a_token(env, path):
    response = env.client.get(f"{API_PREFIX}{path}")
    assert response.status_code == 401


def test_status(env):
    body = _get(env, "/status").json()
    assert body["clamd"] == {"reachable": True, "verified": None, "engine_version": "ClamAV 1.5.4", "database_version": "28170",
                             "database_date": "Fri Oct 30 08:00:00 2026"}
    assert body["realtime"] == {"running": True, "mode": "watchdog監視"}
    assert body["scan"] == {"running": False}
    assert body["update"]["running"] is False and body["update"]["auto_update"] is True
    assert body["schedule"] == {"next_run": "2026-11-01T03:00:00+09:00", "next_job_name": "毎晩フルスキャン"}
    assert body["quarantine"] == {"count": 1}  # 復元済みは数えない
    assert body["web"] == {"enabled": False, "off_at": None}
    assert isinstance(body["definitions"]["age_days"], int)


def test_status_when_clamd_is_down(env):
    env.context.clamav_service.reachable = False
    body = _get(env, "/status").json()
    assert body["clamd"]["reachable"] is False
    assert body["definitions"] == {"age_days": None, "stale": False}


def test_status_reports_stale_definitions(env):
    env.context.clamav_service.db_date = "Mon Jan  5 08:00:00 2026"
    assert _get(env, "/status").json()["definitions"]["stale"] is True


def test_lists(env):
    quarantine = _get(env, "/quarantine").json()
    assert quarantine["total"] == 2
    assert [item["status"] for item in quarantine["items"]] == ["quarantined", "restored"]
    assert quarantine["items"][0]["original_path"] == r"C:\a\x.exe"
    assert _get(env, "/excludes").json()["items"] == [
        {"id": 1, "type": "folder", "value": r"C:\excluded", "enabled": True}]
    assert _get(env, "/realtime").json() == {"running": True, "mode": "watchdog監視", "default_action": "quarantine"}
    assert _get(env, "/realtime/targets").json()["items"][0]["path"] == r"C:\Users\a\Downloads"
    assert [s["name"] for s in _get(env, "/schedules").json()["items"]] == ["毎晩フルスキャン", "止めたジョブ"]
    history = _get(env, "/updates/history").json()["items"]
    assert history == [{"id": 1, "executed_at": "2026-10-31T00:00:00+00:00", "result": "no_change",
                        "before_version": "28170", "after_version": "28170"}]


def test_paging(env):
    body = _get(env, "/quarantine", limit=1, offset=1).json()
    assert body["total"] == 2 and len(body["items"]) == 1 and body["items"][0]["status"] == "restored"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1001}, {"offset": -1}, {"limit": "x"}])
def test_paging_limits_are_enforced(env, params):
    # 一度に返す件数に上限を設ける
    response = _get(env, "/quarantine", **params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E8100"


def test_logs_newest_first_with_filters(env):
    body = _get(env, "/logs").json()
    assert body["total"] == 3
    assert [item["level"] for item in body["items"]] == ["ERROR", "WARNING", "INFO"]
    assert body["items"][0]["error_code"] == "E8004"
    assert body["items"][0]["datetime"].startswith("2026-10-31T11:00:00")
    assert _get(env, "/logs", category="更新").json()["total"] == 1
    assert _get(env, "/logs", level="warning").json()["total"] == 1
    assert _get(env, "/logs", q="E8004").json()["total"] == 1
    assert _get(env, "/logs", since="2026-10-30").json()["total"] == 2


def test_log_message_keeps_one_record_per_line(env):
    # ファイル名に含まれる改行などの制御文字で、1件の記録が複数行に見えないこと
    env.context.log_reader_service.log_path.write_text(
        "2026-10-31 10:00:00,000 [WARNING] <スキャン> 検知: a\x1b[31mb\n", encoding="utf-8")
    message = _get(env, "/logs").json()["items"][0]["message"]
    assert "\x1b" not in message


def test_log_query_length_is_limited(env):
    assert _get(env, "/logs", q="x" * 201).status_code == 400


def test_csv_export_neutralizes_formulas(env):
    response = _get(env, "/logs/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["日時", "レベル", "カテゴリ", "コード", "メッセージ"]
    assert [row[3] for row in rows[1:]] == ["E8004", "", ""]
    messages = [row[4] for row in rows[1:]]
    assert all(not m.startswith(("=", "+", "-", "@")) for m in messages)
    assert "'=HYPERLINK(\"http://x\")" in messages  # 先頭に ' を付けて文字として扱わせる

"""サービスの通知の保存・未読と既読の API のテスト（サービス化・Web API設計書 6.10節・7章。段階 12-3c）"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from api.app import API_PREFIX, create_app
from api.auth import ROLE_ADMIN, ROLE_USER, ApiTokens
from api.bridge import EventHub
from api.context import ApiContext
from api.events import connect_notifications
from core.config_service import ConfigService
from core.notification_service import NotificationService
from core.notification_store import MAX_BODY, NotificationStore
from db.database import DatabaseService
from db.models import PendingNotification


@pytest.fixture
def store(tmp_path):
    db = DatabaseService(tmp_path / "app.db")
    db.connect()
    yield NotificationStore(db)
    db.close()


def _set_acked_at(store, notification_id, value):
    with store.db_service.session() as session:
        session.get(PendingNotification, notification_id).acked_at = value
        session.commit()


def test_migration_creates_the_table(store):
    assert "pending_notification" in inspect(store.db_service._engine).get_table_names()


def test_added_notifications_are_pending_until_acked(store):
    added, acked = [], []
    store.on_added, store.on_acked = added.append, acked.append
    first = store.add("ウイルスを検知しました", "/tmp/a: Eicar", open_screen="quarantine")
    second = store.add("定義データベースの更新に失敗しました", "detail")
    assert added == [first, second]
    assert first["open_screen"] == "quarantine" and second["open_screen"] is None

    items, total = store.pending()
    assert [i["id"] for i in items] == [second["id"], first["id"]] and total == 2  # 新しい順
    assert store.ack([first["id"], 9999]) == [first["id"]]  # 無い ID は含めない
    assert store.ack([first["id"]]) == []  # 既読のものはもう一度既読にしない
    assert acked == [[first["id"]]]  # 既読にしたものが無ければ知らせない
    assert store.ack(None) == [second["id"]]
    assert store.pending() == ([], 0)


def test_notification_text_is_kept_on_one_line_and_bounded(store):
    item = store.add("検知\x1b[2J", "a\nb" + "x" * (MAX_BODY * 2), open_screen="../settings")
    assert "\x1b" not in item["title"] and "\n" not in item["body"]
    assert len(item["body"]) == MAX_BODY
    assert item["open_screen"] is None  # 決まった画面のほかは開かせない


def test_pending_is_limited_but_reports_the_total(store):
    for n in range(5):
        store.add(f"通知{n}", "")
    items, total = store.pending(limit=2)
    assert [i["title"] for i in items] == ["通知4", "通知3"] and total == 5


def test_old_acked_notifications_are_purged(store):
    old = store.add("古い既読", "")
    recent = store.add("新しい既読", "")
    unread = store.add("未読", "")
    store.ack([old["id"], recent["id"]])
    _set_acked_at(store, old["id"], (datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
    with store.db_service.session() as session:  # 未読は古くても消さない
        session.get(PendingNotification, unread["id"]).created_at = "2000-01-01T00:00:00+00:00"
        session.commit()
    assert store.purge(30) == 1
    with store.db_service.session() as session:
        assert sorted(row.id for row in session.query(PendingNotification)) == [recent["id"], unread["id"]]


# -- NotificationService ------------------------------------------------------------------------
def test_service_stores_desktop_notifications_instead_of_showing_them(tmp_path):
    stored = []
    service = NotificationService(ConfigService(tmp_path / "config.json"),
                                  desktop_sink=lambda *args: stored.append(args))
    service._config_service.load()
    with patch("core.notification_service.subprocess.run") as run:
        service.notify_job_failure("毎日", "失敗")
        service.notify_detection("/tmp/a", "Eicar", quarantined=True)
        service._aggregator.flush()
    run.assert_not_called()  # デスクトップには出さない
    assert stored[0] == ("スケジュールジョブが失敗しました", "毎日: 失敗", None)
    assert stored[1][0] == "ウイルスを検知しました" and stored[1][2] == "quarantine"


def test_pending_detection_is_stored_when_the_service_stops(tmp_path, store):
    # 最初の検知から60秒まとめている途中でサービスが止まっても、検知の通知を保存する
    from core.runtime import Runtime

    service = NotificationService(ConfigService(tmp_path / "config.json"), desktop_sink=store.add)
    service._config_service.load()
    service.notify_detection("/tmp/a", "Eicar", quarantined=True)
    assert store.pending()[1] == 0
    fields = {name: None for name in Runtime.__dataclass_fields__ if name not in ("run_update", "keep")}
    fields.update(realtime_manager=SimpleNamespace(is_running=False), scheduler_service=SimpleNamespace(shutdown=lambda: None),
                  notification_service=service, db_service=SimpleNamespace(close=lambda: None))
    Runtime(**fields).shutdown()
    items, total = store.pending()
    assert total == 1 and items[0]["title"] == "ウイルスを検知しました"


def test_failure_to_store_does_not_stop_the_notification(tmp_path, caplog):
    def failing(*args):
        raise RuntimeError("db locked")

    service = NotificationService(ConfigService(tmp_path / "config.json"), desktop_sink=failing)
    service._config_service.load()
    with caplog.at_level(logging.WARNING, logger="clamav_frontend"):
        service.notify_update_failure("x")
    assert "通知を保存できませんでした" in caplog.text


# -- API ----------------------------------------------------------------------------------------
class _DirectInvoker:
    def call(self, fn, *args):
        future = concurrent.futures.Future()
        future.set_result(fn(*args))
        return future

    async def run(self, fn, *args):
        return await asyncio.wrap_future(self.call(fn, *args))


@pytest.fixture
def env(tmp_path, store):
    hub = EventHub()
    connect_notifications(hub, store)
    context = ApiContext(
        config_service=None, clamav_service=None, realtime_manager=None, scan_dispatcher=None,
        update_service=None, update_dispatcher=None, scheduler_service=None, quarantine_service=None,
        exclude_service=None, watch_target_service=None, log_reader_service=None,
        invoker=_DirectInvoker(), events=hub, notification_store=store,
    )
    tokens = ApiTokens(tmp_path / "api_tokens")
    client = TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    return SimpleNamespace(client=client, hub=hub, store=store,
                           user={"Authorization": f"Bearer {tokens.token_for(ROLE_USER)}"},
                           admin={"Authorization": f"Bearer {tokens.token_for(ROLE_ADMIN)}"})


def _events(hub, name):
    return [e.data for e in hub.events_since(0) if e.name == name]


def test_notification_apis_need_a_token(env):
    assert env.client.get(f"{API_PREFIX}/notifications/pending").status_code == 401
    assert env.client.post(f"{API_PREFIX}/notifications/ack", json={"all": True}).status_code == 401


def test_pending_and_ack_with_the_user_role(env, caplog):
    first = env.store.add("ウイルスを検知しました", "/tmp/a: Eicar", open_screen="quarantine")
    second = env.store.add("ジョブが失敗しました", "毎日: 失敗")
    assert _events(env.hub, "notification") == [first, second]

    response = env.client.get(f"{API_PREFIX}/notifications/pending", headers=env.user)
    assert response.status_code == 200
    assert response.json() == {"notifications": [second, first], "total": 2}

    with caplog.at_level(logging.INFO, logger="clamav_frontend"):
        response = env.client.post(f"{API_PREFIX}/notifications/ack", json={"ids": [first["id"]]}, headers=env.user)
    assert response.status_code == 200 and response.json() == {"acked": [first["id"]]}
    assert _events(env.hub, "notification.acked") == [{"ids": [first["id"]]}]
    # 誰が消したかを後から追えるよう、件数・ロール・接続元を残す
    assert "通知を既読にしました: 1件" in caplog.text and "user" in caplog.text and "127.0.0.1" in caplog.text

    response = env.client.post(f"{API_PREFIX}/notifications/ack", json={"all": True}, headers=env.admin)
    assert response.json() == {"acked": [second["id"]]}
    assert env.client.get(f"{API_PREFIX}/notifications/pending", headers=env.user).json()["total"] == 0


@pytest.mark.parametrize("body", [{}, {"ids": [1], "all": True}, {"ids": ["1"]}, {"all": "true"},
                                  {"ids": [1], "extra": 1}, {"ids": list(range(1001))}])
def test_ack_rejects_unclear_requests(env, body):
    env.store.add("通知", "")
    response = env.client.post(f"{API_PREFIX}/notifications/ack", json=body, headers=env.user)
    assert response.status_code in (400, 422)
    assert env.store.pending()[1] == 1


def test_notification_apis_are_absent_without_the_store(tmp_path):
    # 従来のアプリ（画面がデスクトップに通知を出す）では付けない
    tokens = ApiTokens(tmp_path / "api_tokens")
    context = ApiContext(
        config_service=None, clamav_service=None, realtime_manager=None, scan_dispatcher=None,
        update_service=None, update_dispatcher=None, scheduler_service=None, quarantine_service=None,
        exclude_service=None, watch_target_service=None, log_reader_service=None, invoker=_DirectInvoker(),
    )
    client = TestClient(create_app(tokens, context), base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    response = client.get(f"{API_PREFIX}/notifications/pending",
                          headers={"Authorization": f"Bearer {tokens.token_for(ROLE_USER)}"})
    assert response.status_code == 404

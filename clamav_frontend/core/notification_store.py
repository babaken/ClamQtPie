"""
デスクトップ通知の保存（サービス化・Web API設計書 7.1節。段階 12-3c）

サービスはデスクトップに通知を出せない（Windows はセッション 0、Linux はログインした利用者のセッションがない）ため、
NotificationService の表示の代わりに pending_notification に未読として保存する。トレイGUI・WebUI が
GET /notifications/pending で読んで表示し、POST /notifications/ack で既読にする。未読・既読の状態はここ1か所で持つ。

- 保存したとき on_added、既読にしたとき on_acked を呼ぶ（api/events.py がイベントにつなぐ）。どのスレッドからも呼ばれる
- 既読にしたものは logging.retention_days を過ぎたら消す（起動時）。既読にしても、検知の記録（隔離一覧・
  スキャン履歴・ログ）は消えない
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from core.activation import ALLOWED_SCREENS
from core.text_safety import visible_controls
from db.database import DatabaseService
from db.models import PendingNotification

logger = logging.getLogger("clamav_frontend")

#: タイトル・本文の長さの上限（通知に出す文字として十分な長さ。長い本文で DB と応答が大きくならないように）
MAX_TITLE = 200
MAX_BODY = 2000
#: GET /notifications/pending で返す未読の数の上限（新しいものから）。全体の件数は別に返す
MAX_PENDING = 500


def _clip(text: str, limit: int) -> str:
    text = visible_controls(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _to_dict(row: PendingNotification) -> dict:
    return {"id": row.id, "created_at": row.created_at, "title": row.title, "body": row.body,
            "open_screen": row.open_screen}


class NotificationStore:
    def __init__(self, db_service: DatabaseService) -> None:
        self.db_service = db_service
        #: 保存した通知（dict）を受け取る
        self.on_added: Callable[[dict], None] | None = None
        #: 既読にした通知の ID の一覧を受け取る
        self.on_acked: Callable[[list[int]], None] | None = None

    def add(self, title: str, body: str, open_screen: str | None = None) -> dict:
        """未読として保存し、保存した内容を返す。open_screen は ALLOWED_SCREENS のものだけを残す"""
        with self.db_service.session() as session:
            row = PendingNotification(
                created_at=datetime.now(timezone.utc).isoformat(),
                title=_clip(title, MAX_TITLE),
                body=_clip(body, MAX_BODY),
                open_screen=open_screen if open_screen in ALLOWED_SCREENS else None,
            )
            session.add(row)
            session.commit()
            item = _to_dict(row)
        if self.on_added is not None:
            self.on_added(item)
        return item

    def pending(self, limit: int = MAX_PENDING) -> tuple[list[dict], int]:
        """未読の通知（新しい順、最大 limit 件）と、未読の全体の件数"""
        with self.db_service.session() as session:
            query = session.query(PendingNotification).filter(PendingNotification.acked_at.is_(None))
            total = query.count()
            rows = query.order_by(PendingNotification.id.desc()).limit(limit).all()
            return [_to_dict(row) for row in rows], total

    def ack(self, ids: list[int] | None) -> list[int]:
        """ids（None ならすべて）の未読を既読にし、既読にした ID を返す（既に既読・無い ID は含めない）"""
        now = datetime.now(timezone.utc).isoformat()
        with self.db_service.session() as session:
            query = session.query(PendingNotification).filter(PendingNotification.acked_at.is_(None))
            if ids is not None:
                if not ids:
                    return []
                query = query.filter(PendingNotification.id.in_(set(ids)))
            rows = query.order_by(PendingNotification.id).all()
            for row in rows:
                row.acked_at = now
            session.commit()
            acked = [row.id for row in rows]
        if acked and self.on_acked is not None:
            self.on_acked(acked)
        return acked

    def purge(self, retention_days: int) -> int:
        """既読にしてから保持期間を過ぎたものを消す。消した件数を返す（未読は消さない）"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self.db_service.session() as session:
            count = session.query(PendingNotification).filter(
                PendingNotification.acked_at.is_not(None), PendingNotification.acked_at < cutoff,
            ).delete(synchronize_session=False)
            session.commit()
            return count

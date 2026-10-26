"""
WatchTargetService — リアルタイム監視対象パス（watch_target テーブル）のCRUD

参照: Windowsリアルタイム監視詳細設計書3.2節
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from db.database import DatabaseService
from db.models import WatchTarget


@dataclass
class WatchTargetRecord:
    id: int
    path: str
    enabled: bool


def _to_record(row: WatchTarget) -> WatchTargetRecord:
    return WatchTargetRecord(id=row.id, path=row.path, enabled=bool(row.enabled))


class WatchTargetService:
    def __init__(self, db: DatabaseService) -> None:
        self.db = db

    def add(self, path: str) -> WatchTargetRecord:
        with self.db.session() as session:
            row = WatchTarget(
                path=path, enabled=1, added_at=datetime.now(timezone.utc).isoformat()
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _to_record(row)

    def remove(self, target_id: int) -> None:
        with self.db.session() as session:
            row = session.get(WatchTarget, target_id)
            if row is not None:
                session.delete(row)
                session.commit()

    def set_enabled(self, target_id: int, enabled: bool) -> None:
        with self.db.session() as session:
            row = session.get(WatchTarget, target_id)
            if row is not None:
                row.enabled = 1 if enabled else 0
                session.commit()

    def disable_by_path(self, path: str) -> None:
        """監視対象パス消失時の無効化（Windowsリアルタイム監視詳細設計書9章、E2001）"""
        with self.db.session() as session:
            row = session.query(WatchTarget).filter(WatchTarget.path == path).first()
            if row is not None:
                row.enabled = 0
                session.commit()

    def list_all(self) -> list[WatchTargetRecord]:
        with self.db.session() as session:
            rows = session.query(WatchTarget).order_by(WatchTarget.id).all()
            return [_to_record(r) for r in rows]

    def list_enabled_paths(self) -> list[str]:
        return [r.path for r in self.list_all() if r.enabled]

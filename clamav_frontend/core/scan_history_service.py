"""
スキャン履歴（サービス化・Web API設計書 6.2節。段階 12-2c）

- 手動（画面・トレイ・Web API）とスケジュールのすべてのスキャンを、終了時に1件記録する
- 検知したファイルと、検査できなかったファイル（理由つき。12章 46）も残す。検査できなかったファイルは
  1回のスキャンで MAX_ERRORS 件までとし、超えた分は件数（errors_truncated）だけを残す
- ファイルに対する操作・記録には、本体が列挙したパス（ScanWorker の file_scanned のパス）だけを使う（12章 38）
- 保持期間は logging.retention_days（起動時に古いものを消す）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.clamav_service import error_reason
from core.exceptions import ClamdConnectionError
from db.database import DatabaseService
from db.models import ScanHistory, ScanHistoryDetection, ScanHistoryError

SOURCE_GUI = "gui"
SOURCE_API = "api"
SOURCE_SCHEDULE = "schedule"

RESULT_COMPLETED = "completed"
RESULT_CANCELLED = "cancelled"
RESULT_FAILED = "failed"

ACTION_QUARANTINED = "quarantined"
ACTION_NOTIFIED = "notified"

#: 1回のスキャンで記録する、検査できなかったファイルの上限
MAX_ERRORS = 1000


@dataclass
class ScanHistoryCollector:
    """スキャン中の結果を集める（file_scanned を受けて add を呼ぶ）"""

    source: str
    scan_type: str
    label: str
    targets: list[str]
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    detections: list[tuple[str, str]] = field(default_factory=list)  # (path, virus_name)
    errors: list[tuple[str, str, str]] = field(default_factory=list)  # (path, reason, detail)
    errors_truncated: int = 0

    def add(self, path: str, status: str, detail: str) -> None:
        if status == "FOUND":
            self.detections.append((path, detail))
        elif status == "ERROR":
            if len(self.errors) < MAX_ERRORS:
                self.errors.append((path, error_reason(detail), detail))
            else:
                self.errors_truncated += 1


@dataclass
class ScanHistoryRecord:
    id: int
    started_at: str
    finished_at: str
    source: str
    scan_type: str
    label: str
    targets: list[str]
    scanned: int
    detected: int
    errors: int
    errors_truncated: int
    result: str
    detail: str


def _to_record(row: ScanHistory) -> ScanHistoryRecord:
    try:
        targets = json.loads(row.targets or "[]")
    except ValueError:
        targets = []
    return ScanHistoryRecord(
        id=row.id,
        started_at=row.started_at or "",
        finished_at=row.finished_at or "",
        source=row.source or "",
        scan_type=row.scan_type or "",
        label=row.label or "",
        targets=targets,
        scanned=row.scanned or 0,
        detected=row.detected or 0,
        errors=row.errors or 0,
        errors_truncated=row.errors_truncated or 0,
        result=row.result or "",
        detail=row.detail or "",
    )


class ScanHistoryService:
    def __init__(self, db_service: DatabaseService) -> None:
        self.db_service = db_service

    def record(
        self,
        collector: ScanHistoryCollector,
        scanned: int,
        result: str,
        detail: str = "",
        detection_actions: dict[str, str] | None = None,
    ) -> int:
        """1回のスキャンを記録して、その ID を返す。
        detection_actions: 検知したパス → 扱い（ACTION_*）。無いものは ACTION_NOTIFIED（手動のスキャンは隔離しない）"""
        actions = detection_actions or {}
        with self.db_service.session() as session:
            row = ScanHistory(
                started_at=collector.started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                source=collector.source,
                scan_type=collector.scan_type,
                label=collector.label,
                targets=json.dumps(collector.targets, ensure_ascii=False),
                scanned=scanned,
                detected=len(collector.detections),
                errors=len(collector.errors) + collector.errors_truncated,
                errors_truncated=collector.errors_truncated,
                result=result,
                detail=detail,
            )
            session.add(row)
            session.flush()
            session.add_all(
                ScanHistoryDetection(history_id=row.id, path=path, virus_name=virus_name,
                                     action=actions.get(path, ACTION_NOTIFIED))
                for path, virus_name in collector.detections
            )
            session.add_all(
                ScanHistoryError(history_id=row.id, path=path, reason=reason, detail=error_detail)
                for path, reason, error_detail in collector.errors
            )
            session.commit()
            return row.id

    def list_all(self, source: str | None = None, since: datetime | None = None) -> list[ScanHistoryRecord]:
        """新しい順"""
        with self.db_service.session() as session:
            query = session.query(ScanHistory)
            if source:
                query = query.filter(ScanHistory.source == source)
            if since is not None:
                query = query.filter(ScanHistory.started_at >= since.astimezone(timezone.utc).isoformat())
            rows = query.order_by(ScanHistory.started_at.desc(), ScanHistory.id.desc()).all()
            return [_to_record(row) for row in rows]

    def get(self, history_id: int) -> dict | None:
        """1件の詳細（検知したファイルと検査できなかったファイルを含む）。無ければ None"""
        with self.db_service.session() as session:
            row = session.get(ScanHistory, history_id)
            if row is None:
                return None
            detections = session.query(ScanHistoryDetection).filter(
                ScanHistoryDetection.history_id == history_id).order_by(ScanHistoryDetection.id).all()
            errors = session.query(ScanHistoryError).filter(
                ScanHistoryError.history_id == history_id).order_by(ScanHistoryError.id).all()
            return {
                "record": _to_record(row),
                "detections": [{"path": d.path, "virus_name": d.virus_name, "action": d.action} for d in detections],
                "errors": [{"path": e.path, "reason": e.reason, "detail": e.detail} for e in errors],
            }

    def purge(self, retention_days: int) -> int:
        """保持期間を過ぎた履歴を消す。消した件数を返す"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self.db_service.session() as session:
            ids = [row.id for row in session.query(ScanHistory.id).filter(ScanHistory.started_at < cutoff)]
            if not ids:
                return 0
            # SQLite の外部キーの連鎖削除（PRAGMA foreign_keys）は有効にしていないため、子の行も明示して消す
            session.query(ScanHistoryDetection).filter(ScanHistoryDetection.history_id.in_(ids)).delete(
                synchronize_session=False)
            session.query(ScanHistoryError).filter(ScanHistoryError.history_id.in_(ids)).delete(
                synchronize_session=False)
            session.query(ScanHistory).filter(ScanHistory.id.in_(ids)).delete(synchronize_session=False)
            session.commit()
            return len(ids)


def is_fatal_scan_error(error, files_known: bool) -> bool:
    """ScanWorker.error のうち、スキャンが途中で終わるもの（clamd に接続できない、対象の列挙に失敗した）か。
    それ以外（1ファイルの応答待ちのタイムアウトなど）は、そのファイルのエラーとしてスキャンを続ける"""
    return isinstance(error, ClamdConnectionError) or not files_known


class ScanHistoryRecorder:
    """手動のスキャン（ScanDispatcher）を記録する。シグナルはメインスレッドで受ける"""

    def __init__(self, service: ScanHistoryService, scan_dispatcher) -> None:
        self._service = service
        self._dispatcher = scan_dispatcher
        self._collector: ScanHistoryCollector | None = None
        self._failure = ""
        self._current_path: str | None = None
        #: 直前に記録した履歴の ID（Web API の scan.finished に付ける）
        self.last_history_id: int | None = None
        scan_dispatcher.started.connect(self._on_started)
        scan_dispatcher.progress.connect(self._on_progress)
        scan_dispatcher.file_scanned.connect(self._on_file_scanned)
        scan_dispatcher.error.connect(self._on_error)
        scan_dispatcher.finished.connect(self._on_finished)

    def _on_started(self, label: str) -> None:
        info = self._dispatcher.current
        self._collector = ScanHistoryCollector(source=info["source"], scan_type=info["scan_type"], label=label,
                                               targets=list(info["targets"]))
        self._failure = ""
        self._current_path: str | None = None
        self.last_history_id = None

    def _on_progress(self, _scanned: int, total: int, current_path: str) -> None:
        # 対象の列挙が済むまでの progress（total が 0）は、ファイルのパスではなく案内の文
        if total > 0:
            self._current_path = current_path

    def _on_file_scanned(self, path: str, status: str, detail: str, _detected_at: str) -> None:
        if self._collector is not None:
            self._collector.add(path, status, detail)

    def _on_error(self, error) -> None:
        message = getattr(error, "message", "") or str(error)
        if is_fatal_scan_error(error, self._current_path is not None):
            self._failure = message
        elif self._collector is not None:
            # 1ファイルのエラー（file_scanned は出ない）。直前の progress のパスのファイル
            self._collector.add(self._current_path, "ERROR", getattr(error, "detail", None) or message)

    def _on_finished(self, summary: dict) -> None:
        collector, self._collector = self._collector, None
        if collector is None:
            return
        if self._failure:
            result = RESULT_FAILED
        elif summary.get("cancelled"):
            result = RESULT_CANCELLED
        else:
            result = RESULT_COMPLETED
        self.last_history_id = self._service.record(collector, summary.get("scanned", 0), result, self._failure)

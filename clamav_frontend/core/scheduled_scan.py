"""
スケジュールスキャンの実行と検知時処理

参照: 画面設計書4章（最終実行結果: 成功／失敗／検知あり）、通知機能詳細仕様書2章
      （ウイルス検知通知の発生元にスケジュールスキャンを含む）
Phase 11: 従来はScanWorkerを実行するだけで検知結果を捨てていた（隔離・通知・最終実行結果への
          反映が無かった）。検知時はリアルタイム監視の「検知時アクション」設定に従って処理する
          （利用者確認済みの方針。無人実行のため判断基準を一箇所にまとめる）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt

from core.clamav_service import ClamAVService
from core.config_model import RealtimeConfig
from core.exceptions import AppError, ClamdConnectionError
from core.exclude_service import ExcludeService
from core.logger import CATEGORY_SCAN, log_event
from core.notification_service import NotificationService
from core.quarantine_service import QuarantineService
from core.scan_dispatcher import ScanWorker


@dataclass
class ScheduledScanResult:
    scanned: int = 0
    detected: int = 0
    errors: int = 0
    detections: list[tuple[str, str]] = field(default_factory=list)  # (path, virus_name)


def run_scheduled_scan(
    target: str,
    job_name: str,
    clamav_service: ClamAVService,
    exclude_service: ExcludeService | None,
    quarantine_service: QuarantineService,
    notification_service: NotificationService | None,
    realtime_config: RealtimeConfig,
) -> ScheduledScanResult:
    """スケジュールスキャンを同期実行する（APSchedulerのワーカースレッドから呼ばれる）

    対象パスが存在しない、またはclamdに接続できない場合は例外を送出し、
    SchedulerService側でジョブ失敗（E4003、on_job_fail通知）として扱わせる。
    """
    if not target or not Path(target).exists():
        raise FileNotFoundError(f"スキャン対象が見つかりません: {target or '（未指定）'}")

    worker = ScanWorker(
        clamav_service,
        [target],
        include_subfolders=True,
        exclude_service=exclude_service,
        label=f"スケジュールスキャン（{job_name}）",
    )
    result = ScheduledScanResult()
    fatal_errors: list[AppError] = []

    def on_file_scanned(path: str, status: str, detail: str, _detected_at: str) -> None:
        if status == "FOUND":
            result.detections.append((path, detail))

    def on_error(error: AppError) -> None:
        # clamdに接続できない場合はジョブ失敗とする（1ファイル単位のタイムアウト等では失敗にしない）
        if isinstance(error, ClamdConnectionError):
            fatal_errors.append(error)

    def on_finished(summary: dict) -> None:
        result.scanned = summary["scanned"]
        result.detected = summary["detected"]
        result.errors = summary["errors"]

    # ワーカーは呼び出し元スレッドで同期実行するため、シグナルも同じスレッドで直接受け取る
    direct = Qt.ConnectionType.DirectConnection
    worker.file_scanned.connect(on_file_scanned, direct)
    worker.error.connect(on_error, direct)
    worker.finished.connect(on_finished, direct)
    worker.run()

    if fatal_errors:
        raise fatal_errors[0]

    for path, virus_name in result.detections:
        _handle_detection(path, virus_name, quarantine_service, notification_service, realtime_config)
    return result


def _handle_detection(
    path: str,
    virus_name: str,
    quarantine_service: QuarantineService,
    notification_service: NotificationService | None,
    realtime_config: RealtimeConfig,
) -> None:
    quarantined = False
    if realtime_config.default_action == "quarantine":
        try:
            quarantine_service.quarantine(path, virus_name)
            quarantined = True
        except Exception as exc:  # noqa: BLE001 - 1件の隔離失敗で他の検知処理を止めない
            log_event(
                CATEGORY_SCAN,
                f"スケジュールスキャンの検知ファイルを隔離できませんでした: {path} (detail: {exc})",
                level=logging.ERROR,
                error_code="E3001",
            )
    if notification_service is not None:
        notification_service.notify_detection(path, virus_name, quarantined=quarantined)

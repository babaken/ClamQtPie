"""
画面に関わらないサービス一式（サービス化・Web API設計書 9章。段階 12-3a）

service_main.py（Windows サービス・systemd・開発中の --console）が使う。従来の app.py の起動の順番から、
画面に関わるもの（ウィンドウ・トレイ・画面のバインド）を除き、内部 API の待ち受けを加えたもの。
QObject のシグナル・QThread を使うため、呼び出し側で QCoreApplication を作ってから build する。

従来の app.py は、段階 12-4 で画面がこのサービスの API のクライアントになるまで、自分でサービスを組み立てる。
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from core.clamav_service import ClamAVService
from core.clamd_process import ClamdServiceController, ClamdStarter
from core.config_service import ConfigService
from core.diagnostics_service import DiagnosticsService
from core.exclude_service import ExcludeService
from core.log_reader_service import LogReaderService
from core.logger import setup_logger
from core.notification_service import NotificationService
from core.quarantine_service import QuarantineService
from core.realtime_factory import create_realtime_manager
from core.scan_dispatcher import ScanDispatcher
from core.scan_history_service import ScanHistoryRecorder, ScanHistoryService
from core.scheduled_scan import run_scheduled_scan
from core.scheduler_service import SchedulerService
from core.secret_store import FileSecretStore
from core.service_paths import ServicePaths
from core.update_service import UpdateDispatcher, UpdateService
from core.version import APP_NAME, APP_VERSION
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService

logger = logging.getLogger("clamav_frontend")


@dataclass
class Runtime:
    paths: ServicePaths
    config_service: Any
    db_service: Any
    clamd_controller: Any
    clamav_service: Any
    exclude_service: Any
    quarantine_service: Any
    scan_dispatcher: Any
    scan_history_service: Any
    watch_target_service: Any
    realtime_manager: Any
    update_service: Any
    update_dispatcher: Any
    notification_service: Any
    log_reader_service: Any
    diagnostics_service: Any
    scheduler_service: Any
    clamd_starter: Any
    #: 定義の更新を無人で行う関数（自動更新の再登録に使う）
    run_update: Any = None
    #: 参照を持っておくもの（シグナルの接続先。消えると接続も切れる）
    keep: list = field(default_factory=list)

    def shutdown(self) -> None:
        if self.realtime_manager.is_running:
            self.realtime_manager.stop()
        self.scheduler_service.shutdown()
        self.db_service.close()


def build(paths: ServicePaths) -> Runtime:
    """サービス一式を組み立てて、スケジュールと（設定が有効なら）リアルタイム監視を始める"""
    # SMTP パスワードは keyring ではなく、データの置き場所のファイルに保存する（SYSTEM・root では keyring を使えない）
    config_service = ConfigService(paths.config_file, secret_store=FileSecretStore(paths.secrets_dir))
    config = config_service.load()
    setup_logger(paths.log_dir, level=config.logging.level, retention_days=config.logging.retention_days)
    logger.info(f"サービスの起動シーケンス開始（{APP_NAME} {APP_VERSION}）")

    db_service = DatabaseService(paths.db_file)
    db_service.connect()

    clamd_controller = ClamdServiceController(config.clamd)
    # 接続の設定はサービスの再起動後に反映する（通信中に設定が変わらないよう写しを渡す。app.py と同じ）
    clamav_service = ClamAVService(copy.deepcopy(config.clamd), restart_clamd=clamd_controller.start)
    exclude_service = ExcludeService(db_service)
    quarantine_service = QuarantineService(db_service, paths.quarantine_dir)
    scan_dispatcher = ScanDispatcher(clamav_service, exclude_service)
    scan_history_service = ScanHistoryService(db_service)
    scan_history_recorder = ScanHistoryRecorder(scan_history_service, scan_dispatcher)
    try:
        scan_history_service.purge(config.logging.retention_days)
    except Exception as exc:  # noqa: BLE001 - 古い履歴を消せなくても起動は続ける
        logger.warning(f"古いスキャン履歴を削除できませんでした (detail: {exc})")
    watch_target_service = WatchTargetService(db_service)
    realtime_manager = create_realtime_manager(
        clamav_service, watch_target_service, exclude_service, quarantine_service, config.realtime,
        app_dir=paths.data_dir,
    )
    logger.info(f"リアルタイム監視方式: {realtime_manager.mode_name}")
    update_service = UpdateService(db_service, clamav_service, freshclam_path=config.update.freshclam_path)
    update_dispatcher = UpdateDispatcher(update_service)
    # サービスはデスクトップに通知を出せない（Windows はセッション 0）。メールの通知は従来どおり送る。
    # 画面に知らせる仕組み（通知の保存）は段階 12-3c
    notification_service = NotificationService(config_service)
    log_reader_service = LogReaderService(paths.log_dir / "app.log")
    diagnostics_service = DiagnosticsService(config_service, clamav_service, realtime_manager,
                                             app_dir=paths.data_dir)

    realtime_manager.detection_handled.connect(
        lambda path, virus_name, quarantined: notification_service.notify_detection(
            path, virus_name, quarantined=quarantined))

    def run_scan(target: str, job_name: str):
        return run_scheduled_scan(target, job_name, clamav_service, exclude_service, quarantine_service,
                                  notification_service, config.realtime, history_service=scan_history_service)

    def run_update():
        record = update_service.update_now(interactive=False)
        if record.result == "failure":
            notification_service.notify_update_failure(record.detail or "freshclamの実行に失敗しました")
        return record

    scheduler_service = SchedulerService(db_service, run_scan, run_update,
                                         on_job_fail=notification_service.notify_job_failure)
    scheduler_service.start()
    scheduler_service.set_auto_update(run_update, config.update.auto_update, config.update.interval_hours,
                                      last_run=update_service.last_executed_at())
    clamd_starter = ClamdStarter(clamav_service)

    if config.realtime.default_enabled and realtime_manager.start():
        logger.info("リアルタイム監視を自動で開始しました")

    return Runtime(
        paths=paths, config_service=config_service, db_service=db_service, clamd_controller=clamd_controller,
        clamav_service=clamav_service, exclude_service=exclude_service, quarantine_service=quarantine_service,
        scan_dispatcher=scan_dispatcher, scan_history_service=scan_history_service,
        watch_target_service=watch_target_service, realtime_manager=realtime_manager,
        update_service=update_service, update_dispatcher=update_dispatcher,
        notification_service=notification_service, log_reader_service=log_reader_service,
        diagnostics_service=diagnostics_service, scheduler_service=scheduler_service, clamd_starter=clamd_starter,
        run_update=run_update, keep=[scan_history_recorder, run_scan],
    )

"""
アプリケーションエントリポイント

参照: タスクトレイ常駐アプリプロセス設計書8章の起動シーケンス
Phase8: 多重起動防止・タスクトレイ統合・自動起動・ログ画面/ホーム画面の結線を追加し、
        起動シーケンスを設計書8章の10ステップに沿って統合する。
"""
from __future__ import annotations

import concurrent.futures
import copy
import sys
from pathlib import Path

# Python 3.15以降、concurrent.futuresはThreadPoolExecutor等を遅延import（PEP 810）する。
# PySide6を先にimportすると遅延importが解決されないまま残り、APSchedulerの起動時に
# "TypeError: 'lazy_import' object is not callable" で落ちるため、PySide6より前に実体化しておく。
# （Windows実機テストで発見。tests/conftest.pyにも同じ処理がある）
concurrent.futures.ThreadPoolExecutor  # noqa: B018
concurrent.futures.ProcessPoolExecutor  # noqa: B018

from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from core.activation import ActivationWatcher, helper_command, register_protocol, user_state_dir
from core.autostart_service import AutostartService
from core.clamav_service import ClamAVService
from core.clamd_client import check_connection
from core.clamd_process import ClamdServiceController, ClamdStarter
from core.config_service import ConfigService
from core.diagnostics_service import DiagnosticsService
from core.exclude_service import ExcludeService
from core.legacy_migration import migrate_legacy_names
from core.log_reader_service import LogReaderService
from core.logger import setup_logger
from core.notification_service import NotificationService
from core.quarantine_service import QuarantineService
from core.realtime_factory import create_realtime_manager
from core.scan_dispatcher import ScanDispatcher
from core.scheduled_scan import run_scheduled_scan
from core.scheduler_service import SchedulerService
from core.single_instance import SingleInstanceLock
from core.update_service import UpdateDispatcher, UpdateService
from core.version import APP_NAME
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService
from gui.error_dialog import set_default_detail_handler, show_confirm_dialog
from gui.main_window import MainWindow
from gui.theme import apply_theme
from gui.tray import TrayIcon, TrayIconState

#: ポータブル構成（config.jsonスキーマ定義書2.1節）: 実行ファイルと同一ディレクトリ
#
# 注意: PyInstallerでフリーズされた実行ファイルでは、__file__は
# _internal/（sys._MEIPASS）配下のバンドル内パスを指してしまうため、
# 実行ファイル本体のディレクトリとは一致しない（--onedir方式でも同様）。
# フリーズ環境かどうかを sys.frozen で判定し、フリーズ時は sys.executable の
# 親ディレクトリ（exeの実際の配置場所）を使用する。
# （インストーラ/パッケージング設計書のビルド検証で実際に発生した不具合の修正）
#
# 一方、アイコン等の同梱リソース（ClamQtPie.specのdatas）は_internal/配下に展開されるため、
# 読み取り専用リソースは_RESOURCE_DIR（フリーズ時はsys._MEIPASS）から参照する。
# （_APP_DIR/iconsを参照していたためexe版でアイコンが見つからなかった。Windows実機テストで発見）
if getattr(sys, "frozen", False):
    _APP_DIR = Path(sys.executable).resolve().parent
    _RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", _APP_DIR))
else:
    _APP_DIR = Path(__file__).resolve().parent
    _RESOURCE_DIR = _APP_DIR

#: 稼働状態の定期更新間隔（ステータスバー・ホーム画面・トレイアイコン）
_CLAMD_STATUS_INTERVAL_MS = 60_000
_REALTIME_STATUS_INTERVAL_MS = 2_000


class _GuiBridge(QObject):
    """バックグラウンドスレッド（APScheduler等）からGUIスレッドへ通知するためのシグナル置き場"""

    scheduled_detection = Signal(int)  # スケジュールスキャンの検知件数


def _append_log_line(level: str, message: str) -> None:
    """ロガー初期化前（多重起動検知時）に、core/logger.pyと同じ書式で1行だけ追記する"""
    from datetime import datetime

    try:
        (_APP_DIR / "logs").mkdir(parents=True, exist_ok=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        with open(_APP_DIR / "logs" / "app.log", "a", encoding="utf-8") as f:
            f.write(f"{now} [{level}] {message}\n")
    except OSError:
        pass  # 記録できなくても後発プロセスの終了は妨げない


def main() -> int:
    # ステップ1: 多重起動チェック（タスクトレイ常駐アプリプロセス設計書3章）
    instance_lock = SingleInstanceLock()  # Linuxは~/.local/share/clamqtpie/app.lock（ユーザー単位）
    if not instance_lock.acquire():
        # 3.2節: 後発プロセスはサイレント終了し、ログにINFOで記録する
        _append_log_line("INFO", "多重起動を検知したため終了しました")
        return 0

    # ステップ2: QApplication生成
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # トレイ格納時にアプリが終了しないようにする
    app.setApplicationName(APP_NAME)
    apply_theme(app)  # 暗色テーマ（gui/theme.py）
    app_icon = _RESOURCE_DIR / "icons" / "clamQtPie.ico"  # scripts/generate_icons.pyで生成
    if app_icon.exists():
        app.setWindowIcon(QIcon(str(app_icon)))

    # ステップ3: config.json読込
    config_service = ConfigService(_APP_DIR / "config.json")
    config = config_service.load()

    # ステップ4: ロガー初期化
    logger = setup_logger(
        _APP_DIR / "logs", level=config.logging.level, retention_days=config.logging.retention_days
    )
    logger.info("アプリ起動シーケンス開始")

    # ステップ5: DB接続・マイグレーション確認
    db_service = DatabaseService(_APP_DIR / "app.db")
    db_service.connect()

    # サービス層構築
    # clamd停止時はOSのサービスとして起動を試みる（core/clamd_process.py、E1006/E8003）
    clamd_controller = ClamdServiceController(config.clamd)
    # 接続の設定（接続方式・ホスト・ポート・ソケット・同時接続数等）はアプリの再起動後に反映する。
    # 設定画面はconfig_service.currentをその場で書き換えるため、写しを渡して通信中の設定が途中で変わらないようにする
    clamav_service = ClamAVService(copy.deepcopy(config.clamd), restart_clamd=clamd_controller.start)
    exclude_service = ExcludeService(db_service)
    quarantine_service = QuarantineService(db_service, _APP_DIR / "quarantine")
    scan_dispatcher = ScanDispatcher(clamav_service, exclude_service)
    watch_target_service = WatchTargetService(db_service)
    # Windows=watchdog / Linux=clamonacc（利用不可ならwatchdogへフォールバック）
    realtime_manager = create_realtime_manager(
        clamav_service,
        watch_target_service,
        exclude_service,
        quarantine_service,
        config.realtime,
        app_dir=_APP_DIR,
    )
    logger.info(f"リアルタイム監視方式: {realtime_manager.mode_name}")
    update_service = UpdateService(db_service, clamav_service, freshclam_path=config.update.freshclam_path)
    update_dispatcher = UpdateDispatcher(update_service)
    notification_service = NotificationService(config_service)
    log_reader_service = LogReaderService(_APP_DIR / "logs" / "app.log")
    # 非フリーズ時（python app.py）はsys.executableがpython.exeになるため、
    # app.pyのパスも含めたコマンドを登録する（そのままではapp.pyなしでpythonが起動してしまう）
    if getattr(sys, "frozen", False):
        autostart_command = sys.executable
    else:
        autostart_command = f'"{sys.executable}" "{Path(__file__).resolve()}"'
    autostart_service = AutostartService(autostart_command)
    # 製品名の変更前（ClamAVフロントエンド）の登録（SMTPパスワード・自動起動・URLスキーム等）を引き継ぐ
    migrate_legacy_names(config_service, autostart_service)
    # 設定画面で保存した内容も反映するよう、診断のたびにconfig_service.currentを読む
    diagnostics_service = DiagnosticsService(config_service, clamav_service, realtime_manager, app_dir=_APP_DIR)

    # 通知フック接続（通知機能詳細仕様書6章シーケンス）
    # 隔離したかどうかは設定値ではなく、隔離を試した結果で通知する（detection_handled）
    realtime_manager.detection_handled.connect(
        lambda path, virus_name, quarantined: notification_service.notify_detection(
            path, virus_name, quarantined=quarantined
        )
    )

    def _on_scan_file_scanned(path: str, status: str, detail: str, detected_at: str) -> None:
        if status == "FOUND":
            notification_service.notify_detection(path, detail, quarantined=False)

    scan_dispatcher.file_scanned.connect(_on_scan_file_scanned)

    gui_bridge = _GuiBridge()

    def _run_scheduled_scan(target: str, job_name: str):
        # 検知時はリアルタイム監視の「検知時アクション」設定に従い隔離・通知する（core/scheduled_scan.py）
        result = run_scheduled_scan(
            target,
            job_name,
            clamav_service,
            exclude_service,
            quarantine_service,
            notification_service,
            config.realtime,
        )
        if result.detected:
            # APSchedulerのスレッドから呼ばれるため、トレイ表示等はシグナル経由でGUIスレッドへ渡す
            gui_bridge.scheduled_detection.emit(result.detected)
        return result

    def _run_scheduled_update():
        # 無人実行（Linuxの一般ユーザーでは認証を求めず、OSのサービスに任せる。update_service参照）
        record = update_service.update_now(interactive=False)
        if record.result == "failure":
            notification_service.notify_update_failure(record.detail or "freshclamの実行に失敗しました")
        return record

    def _on_scheduler_job_failed(job_name: str, detail: str) -> None:
        notification_service.notify_job_failure(job_name, detail)

    scheduler_service = SchedulerService(
        db_service, _run_scheduled_scan, _run_scheduled_update, on_job_fail=_on_scheduler_job_failed
    )
    scheduler_service.start()
    # 初回は前回の定義更新から間隔が過ぎた時点（過ぎていれば起動の少し後）に実行する
    scheduler_service.set_auto_update(
        _run_scheduled_update,
        config.update.auto_update,
        config.update.interval_hours,
        last_run=update_service.last_executed_at(),
    )

    # ステップ6: MainWindow生成（トレイ連携込み、タスクトレイ設計書4章）
    icons_dir = _RESOURCE_DIR / "icons"
    tray_icon = TrayIcon(icons_dir)
    window = MainWindow(tray_icon=tray_icon, config_service=config_service)
    # 全画面のエラーダイアログに「詳細を見る」（ログ画面へ遷移しエラーコードで絞り込み）を付ける
    set_default_detail_handler(window.show_log_for_error_code)

    window.get_screen("scan").bind_services(scan_dispatcher, quarantine_service)
    window.get_screen("quarantine").bind_services(quarantine_service)
    window.get_screen("realtime").bind_services(
        realtime_manager, watch_target_service, exclude_service
    )
    def _show_log_for(keyword: str) -> None:
        window.navigate_to("log")
        window.get_screen("log").filter_by_keyword(keyword)

    window.get_screen("schedule").bind_services(scheduler_service, on_show_log=_show_log_for)
    update_screen = window.get_screen("update")
    update_screen.bind_services(
        update_service,
        update_dispatcher,
        clamav_service,
        config_service=config_service,
        on_open_settings=lambda: window.navigate_to("settings"),
    )
    def _on_settings_saved(saved_config) -> None:
        # 画面設計書8章「即時反映が必要な項目は保存直後に該当サービスへ再読込を通知する」
        setup_logger(
            _APP_DIR / "logs",
            level=saved_config.logging.level,
            retention_days=saved_config.logging.retention_days,
        )
        scheduler_service.set_auto_update(
            _run_scheduled_update,
            saved_config.update.auto_update,
            saved_config.update.interval_hours,
            last_run=update_service.last_executed_at(),
        )
        realtime_screen.sync_action_from_config()
        update_screen.reload_auto_update_label()
        # freshclamの場所は次の更新から使う（実行中の更新はそのまま）
        update_service.freshclam_path = saved_config.update.freshclam_path
        # clamdのサービス名をエンジン サービス画面の停止・起動・表示に反映する（診断は毎回config_service.currentを読む）
        clamd_controller.config = saved_config.clamd
        engine_screen.reload_service_name()
        logger.info("設定を保存し、ログ設定・自動更新設定・freshclamの場所を反映しました")

    realtime_screen = window.get_screen("realtime")
    window.get_screen("settings").bind_services(
        config_service,
        notification_service,
        autostart_service,
        on_saved=_on_settings_saved,
        connection_tester=check_connection,
    )
    window.get_screen("log").bind_services(log_reader_service)
    window.get_screen("diagnostics").bind_services(diagnostics_service)

    scan_screen = window.get_screen("scan")
    quarantine_screen = window.get_screen("quarantine")
    home_screen = window.get_screen("home")
    engine_screen = window.get_screen("engine")
    tray_scan = {"pending": False}  # トレイから開始したスキャンか（完了時に吹き出し通知する）

    def _on_quick_scan() -> None:
        # ホーム画面「今すぐスキャン」: スキャン画面へ遷移してクイックスキャンを開始する
        window.navigate_to("scan")
        window.show_from_tray()
        scan_screen.start_quick_scan()

    def _on_tray_quick_scan() -> None:
        # トレイ「今すぐスキャン」: 画面は開かずにクイックスキャンを実行し、完了時に吹き出し通知する
        # （タスクトレイ常駐アプリプロセス設計書5章）
        if scan_screen.start_quick_scan():
            tray_scan["pending"] = True
            tray_icon.showMessage(APP_NAME, "クイックスキャンを開始しました")
        else:
            tray_icon.showMessage(APP_NAME, "スキャンは既に実行中です")

    def _on_scan_finished(summary: dict) -> None:
        home_screen.refresh_status()
        if tray_scan["pending"]:
            tray_scan["pending"] = False
            state = "中断しました" if summary.get("cancelled") else "完了しました"
            tray_icon.showMessage(
                APP_NAME,
                f"クイックスキャンが{state}: スキャン {summary['scanned']}件 / 検知 {summary['detected']}件",
                QSystemTrayIcon.MessageIcon.Warning if summary["detected"] else QSystemTrayIcon.MessageIcon.Information,
            )

    scan_dispatcher.finished.connect(_on_scan_finished)

    def _on_update_now() -> None:
        window.navigate_to("update")
        window.show_from_tray()
        update_screen.start_update()

    def _on_toggle_realtime() -> None:
        if realtime_manager.is_running:
            # 画面設計書1章: ON→OFF時は確認ダイアログ（ホーム画面・トレイメニュー共通）
            if not show_confirm_dialog(window, "監視停止の確認", "リアルタイム監視を停止しますか？"):
                tray_icon.set_state(TrayIconState.MONITORING, True)  # トレイのチェック表示を戻す
                return
            realtime_manager.stop()
        else:
            realtime_manager.start()
        tray_icon.set_state(TrayIconState.MONITORING, realtime_manager.is_running)

    # clamdの起動（エンジン サービス画面「サービスを開始」、および停止検知時の自動起動1回）
    clamd_starter = ClamdStarter(clamav_service)
    # user_stopped: エンジン サービス画面で利用者が停止を要求した（手動で起動するまで自動起動しない）
    # user_stopped_before_stop: 停止に失敗したときに戻す、停止要求前のuser_stopped
    clamd_start = {"manual": False, "auto_attempted": False, "user_stopped": False, "user_stopped_before_stop": False}

    def _start_clamd(manual: bool) -> bool:
        """起動試行を始めたらTrue。既に試行中ならFalse（先の試行のmanualは上書きしない）"""
        if not clamd_starter.start():
            return False
        clamd_start["manual"] = manual
        if manual:
            clamd_start["user_stopped"] = False
        return True

    def _on_clamd_start_finished(ok: bool, error) -> None:
        engine_screen.on_start_clamd_finished(ok, error)
        if ok:
            return
        if clamd_start["manual"]:
            window.show_error(error)
        else:
            # 自動起動の失敗は（clamd未導入の環境で起動のたびに出ないよう）ダイアログではなく吹き出しで知らせる
            tray_icon.showMessage(
                APP_NAME,
                f"ClamAVサービスを起動できませんでした（{error.code}）。「ClamAV エンジン サービス」画面から再試行できます",
                QSystemTrayIcon.MessageIcon.Warning,
            )

    clamd_starter.finished.connect(_on_clamd_start_finished)

    home_screen.bind_services(
        clamav_service,
        realtime_manager,
        _on_quick_scan,
        _on_update_now,
        _on_toggle_realtime,
        log_reader_service=log_reader_service,
        on_navigate=lambda key: (window.navigate_to(key), window.show_from_tray()),
        stale_days=lambda: config_service.current.update.stale_days,
    )

    def _on_clamd_stop_requested() -> None:
        # 利用者が停止したclamdを、停止検知時の自動起動（config.clamd.auto_start）で起動し直さない。
        # 抑止は手動で起動したときだけ解除する（auto_attemptedは稼働を確認すると解除されるため使わない。
        # Windowsのsc stopは停止処理中の時点で成功を返し、直後のPINGが通ることがある。レビューで指摘）
        # 停止の完了後ではなく要求の時点で立てる。停止処理中（systemctl stopの待ち・sc stopのSTOP_PENDING）に
        # 定期確認のPINGが失敗すると、自動起動が走って停止直後にclamdが起動し直していた（レビューで指摘）
        clamd_start["user_stopped_before_stop"] = clamd_start["user_stopped"]
        clamd_start["user_stopped"] = True

    def _on_clamd_stop_failed() -> None:
        # 停止できなかった場合は、停止を要求する前の状態に戻す
        clamd_start["user_stopped"] = clamd_start["user_stopped_before_stop"]

    engine_screen.bind_services(
        clamav_service,
        clamd_controller,
        on_start_clamd=lambda: _start_clamd(manual=True),
        on_stop_requested=_on_clamd_stop_requested,
        on_stop_failed=_on_clamd_stop_failed,
        on_refresh=home_screen.refresh_status,
        realtime_manager=realtime_manager,
    )

    # 稼働状態の定期更新: ホーム画面のカード・ステータスバー・トレイアイコンを同じ状態で更新する
    # （画面設計書「共通仕様」のステータスバー、タスクトレイ常駐アプリプロセス設計書5.2節の状態表現）。
    # 従来はステータスバーが一度も更新されず「clamd: 確認中...」のままだった（Windows実機テストで発見）
    definitions_state = {"stale": False}

    def _on_status_refreshed(status) -> None:
        window.update_status_bar(status)
        engine_screen.apply_status(status)
        tray_icon.set_state(TrayIconState.ERROR, status.clamd_running is False)
        tray_icon.set_state(TrayIconState.MONITORING, status.realtime_running)
        # 定義が古い間はトレイのツールチップで知らせ、古くなった時点で1回だけ通知する（core/definitions.py）
        if status.definitions_stale:
            tray_icon.set_warning(f"ウイルス定義が {status.definitions_age_days} 日更新されていません")
            if not definitions_state["stale"]:
                notification_service.notify_definitions_stale(status.definitions_age_days)
        elif status.clamd_running is not None:
            tray_icon.set_warning(None)
        if status.clamd_running:
            definitions_state["stale"] = status.definitions_stale
        realtime_screen.sync_toggle_state(status.realtime_running)
        # clamd停止を検知したら1回だけ自動起動を試みる（復旧後に再び停止した場合はもう1回）
        if status.clamd_running:
            clamd_start["auto_attempted"] = False
        elif (
            status.clamd_running is False
            and config.clamd.auto_start
            and not clamd_start["auto_attempted"]
            and not clamd_start["user_stopped"]
        ):
            clamd_start["auto_attempted"] = True
            _start_clamd(manual=False)

    home_screen.status_refreshed.connect(_on_status_refreshed)
    home_screen.refresh_status()
    update_dispatcher.finished.connect(lambda _record: home_screen.refresh_status())
    realtime_manager.detected.connect(lambda _path, _virus: home_screen.refresh_status())

    # タスクトレイ常駐アプリプロセス設計書5.2節「検知あり（未対応）」: 隔離画面を開いた時点で確認済みとする
    def _on_screen_changed(key: str) -> None:
        if key == "quarantine":
            quarantine_screen.reload()
            if window.isVisible():
                tray_icon.set_state(TrayIconState.DETECTION_PENDING, False)
        elif key == "settings":
            # 他の画面で変わった設定（リアルタイム画面の検知時アクション、トレイ案内の表示済み等）を読み直す。
            # 以前は起動時の入力値のまま保存し、それらを上書きしていた（コードレビューで修正）
            window.get_screen("settings").reload_from_config()
        elif key == "log":
            # 常駐中でも最新のログを表示する（期間の終了日も今日へ進める）
            window.get_screen("log").on_shown()

    window.screen_changed.connect(_on_screen_changed)

    # 通知機能詳細仕様書4.1節: 検知のToastをクリックしたら隔離画面を開く（core/activation.py）
    activation_watcher = ActivationWatcher(user_state_dir())
    register_protocol(
        helper_command(_APP_DIR, frozen=getattr(sys, "frozen", False), python_executable=sys.executable)
    )

    def _on_show_requested(key: str) -> None:
        logger.info(f"通知のクリックにより画面を表示します: {key}")
        window.navigate_to(key)
        window.show_from_tray()

    activation_watcher.show_requested.connect(_on_show_requested)
    gui_bridge.scheduled_detection.connect(
        lambda _count: (tray_icon.set_state(TrayIconState.DETECTION_PENDING, True), home_screen.refresh_status())
    )

    clamd_status_timer = QTimer()  # clamdの稼働状態・定義バージョン（60秒ごと）
    clamd_status_timer.timeout.connect(home_screen.refresh_status)
    clamd_status_timer.start(_CLAMD_STATUS_INTERVAL_MS)
    realtime_status_timer = QTimer()  # リアルタイム監視のON/OFF（他画面からの開始・停止も反映）
    realtime_status_timer.timeout.connect(lambda: home_screen.refresh_status(check_clamd=False))
    realtime_status_timer.start(_REALTIME_STATUS_INTERVAL_MS)

    # ステップ7: タスクトレイメニュー結線（タスクトレイ常駐アプリプロセス設計書5章）
    tray_icon.open_action.triggered.connect(window.show_from_tray)
    tray_icon.scan_now_action.triggered.connect(_on_tray_quick_scan)
    tray_icon.update_now_action.triggered.connect(_on_update_now)
    tray_icon.toggle_realtime_action.triggered.connect(_on_toggle_realtime)

    def _on_quit_requested() -> None:
        if not show_confirm_dialog(
            window, "終了確認", "アプリを終了しますか？リアルタイム監視も停止されます"
        ):
            return
        app.quit()

    tray_icon.quit_action.triggered.connect(_on_quit_requested)
    tray_icon.show()

    def _on_detected_update_tray(path: str, virus_name: str) -> None:
        tray_icon.set_state(TrayIconState.DETECTION_PENDING, True)

    realtime_manager.detected.connect(_on_detected_update_tray)

    # ステップ8: リアルタイム監視自動開始（config.realtime.default_enabled、
    # 監視対象0件の場合はRealtimeManager.start()内部で開始しない）
    if config.realtime.default_enabled:
        if realtime_manager.start():
            tray_icon.set_state(TrayIconState.MONITORING, True)

    # ステップ9: 自動起動設定の反映（タスクトレイ常駐アプリプロセス設計書6章）
    try:
        if config.general.autostart and not autostart_service.is_enabled():
            autostart_service.enable()
        elif not config.general.autostart and autostart_service.is_enabled():
            autostart_service.disable()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"自動起動設定の反映に失敗しました (detail: {exc})")

    # ステップ10: 起動時の表示制御（config.general.start_minimizedに従う）
    if not config.general.start_minimized:
        window.show()

    logger.info("アプリ起動シーケンス完了")

    exit_code = app.exec()

    if realtime_manager.is_running:
        realtime_manager.stop()
    scheduler_service.shutdown()
    db_service.close()
    instance_lock.release()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

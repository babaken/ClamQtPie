"""
RealtimeManager — Windowsリアルタイム監視（watchdog連携）

参照: Windowsリアルタイム監視詳細設計書
実装メモ: watchdogライブラリはOS差異（Windows: ReadDirectoryChangesW、
Linux: inotify）を内部で吸収するため、本クラスはOS非依存のコードとして
実装できる。設計書はWindows固有の詳細（3.1〜9章）を規定しているが、
watchdogの抽象化により同一コードでLinux上でも動作する。ただし、
要件定義書ではLinuxは別途clamonacc連携が想定されており、本Phase5では
「Windows優先」の方針どおりwatchdogベースの実装を主軸とし、Linux側の
最終的な採用方針（clamonaccへの置き換え要否）は未確定事項として扱う。

主要な処理:
- 監視対象パスの動的追加/削除（Observer.schedule/unschedule、3.3節）
- 作成・変更イベントのみ対象、ディレクトリイベントは無視（4.1節）
- 除外リストとの早期照合（4.2節）
- パスごとのデバウンスタイマー（5章、既定2.0秒）
- デバウンスタイマー上限到達時は最古のタイマーを即時発火（7章）
- スキャンワーカースレッドプールでのディスパッチ（6.1節）
- ファイルロック時の1回リトライ（6.2節）
- 検知時アクション（自動隔離／通知のみ、6.3節）
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from core.clamav_service import ERROR_REASON_SIZE_LIMIT, ClamAVService, error_reason
from core.config_model import RealtimeConfig
from core.exclude_service import ExcludeService, is_under_any_folder
from core.logger import CATEGORY_REALTIME, log_event
from core.quarantine_service import QuarantineService
from core.watch_target_service import WatchTargetService

logger = logging.getLogger("clamav_frontend")


class _DebounceManager:
    """パスごとのデバウンスタイマー管理（5章・7章）"""

    def __init__(self, interval_sec: float, max_pending: int, on_fire) -> None:
        self._interval_sec = interval_sec
        self._max_pending = max_pending
        self._on_fire = on_fire
        self._timers: dict[str, threading.Timer] = {}
        self._order: list[str] = []  # 挿入順（最古のタイマー特定用、7章）
        self._lock = threading.RLock()

    def notify(self, path: str) -> None:
        with self._lock:
            existing = self._timers.get(path)
            if existing is not None:
                existing.cancel()
            else:
                self._order.append(path)
                if len(self._order) > self._max_pending:
                    oldest = self._order.pop(0)
                    oldest_timer = self._timers.pop(oldest, None)
                    if oldest_timer is not None:
                        oldest_timer.cancel()
                        # エラーコード一覧E2003: GUI非表示・ログのみ
                        log_event(
                            CATEGORY_REALTIME,
                            f"デバウンスタイマーが上限（{self._max_pending}件）に達したため即時スキャンします: {oldest}",
                            level=logging.DEBUG,
                            error_code="E2003",
                        )
                        self._fire(oldest)

            timer = threading.Timer(self._interval_sec, self._fire, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
            if path in self._order:
                self._order.remove(path)
        self._on_fire(path)

    def cancel_all(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._order.clear()


class _EventHandler(FileSystemEventHandler):
    """作成・変更・移動（移動先）のイベントを処理し、除外判定後にデバウンスへ積む（4.1〜4.2節）"""

    def __init__(
        self,
        exclude_service: ExcludeService | None,
        debounce: _DebounceManager,
        app_dirs: list[Path] | None = None,
    ) -> None:
        self._exclude_service = exclude_service
        self._debounce = debounce
        self._app_dirs = app_dirs or []

    def on_created(self, event) -> None:
        self._handle(event)

    def on_modified(self, event) -> None:
        self._handle(event)

    def on_moved(self, event) -> None:
        # 一時ファイルに書いてから名前を変える保存方式（ブラウザのダウンロードの.part等）では、
        # 完成したファイルは移動イベントでしか通知されないため、移動先を検査する
        # （Phase 11改訂。以前は対象外としており、ダウンロードしたファイルを検査していなかった）。
        # フォルダの移動は、watchdogが中のファイルごとの移動イベントも発行するため個別に扱わない
        if event.is_directory:
            return
        self._notify(event.dest_path)

    def _handle(self, event) -> None:
        if event.is_directory:
            return
        self._notify(event.src_path)

    def _notify(self, path: str) -> None:
        if self._app_dirs and is_under_any_folder(path, self._app_dirs):
            # アプリ自身のフォルダ（ログ・隔離フォルダ等）は検査しない。隔離フォルダへの移動を検査すると、
            # 隔離したファイルが再び検知されて隔離が無限に繰り返された（Linux実機で発見）
            return
        if self._exclude_service is not None and self._exclude_service.is_excluded(path):
            return
        self._debounce.notify(path)


class RealtimeManager(QObject):
    detected = Signal(str, str)  # path, virus_name（検知した時点。隔離の前）
    detection_handled = Signal(str, str, bool)  # path, virus_name, 隔離できたか（検知時アクションの後）
    watch_target_lost = Signal(str)  # path
    scan_error = Signal(object)  # AppError

    #: 画面設計書3章「現在の監視方式ラベル」に表示する名称
    mode_name = "watchdog監視"

    def __init__(
        self,
        clamav_service: ClamAVService,
        watch_target_service: WatchTargetService,
        exclude_service: ExcludeService,
        quarantine_service: QuarantineService,
        config: RealtimeConfig,
        extra_exclude_dirs: list[Path] | None = None,
    ) -> None:
        """extra_exclude_dirs: 常に除外するフォルダ（アプリ自身のログ・隔離フォルダ等。自己検査の防止）"""
        super().__init__()
        self._clamav_service = clamav_service
        self._watch_target_service = watch_target_service
        self._exclude_service = exclude_service
        self._quarantine_service = quarantine_service
        self.config = config
        self._extra_exclude_dirs = extra_exclude_dirs or []

        self._observer: Observer | None = None
        self._scheduled_watches: dict[str, object] = {}  # path -> watchdog watch handle
        self._debounce: _DebounceManager | None = None
        self._scan_queue: queue.Queue[str] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._stop_event = threading.Event()
        # clamdに接続できない状態か（接続エラーは状態が変わったときに1回だけ通知する。_scan_one）
        self._clamd_unavailable = False
        self._clamd_state_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    # -- 開始・停止 -----------------------------------------------------
    def start(self) -> bool:
        """監視対象が0件の場合は開始しない（3.1節、6.1節ホーム画面連携）"""
        if self.is_running:
            return True

        targets = self._watch_target_service.list_enabled_paths()
        if not targets:
            return False

        self._stop_event.clear()
        with self._clamd_state_lock:
            self._clamd_unavailable = False
        self._debounce = _DebounceManager(
            interval_sec=self.config.windows_debounce_sec,
            max_pending=self.config.max_pending_timers,
            on_fire=self._enqueue_scan,
        )

        handler = _EventHandler(self._exclude_service, self._debounce, self._extra_exclude_dirs)
        self._observer = Observer()
        for target in targets:
            self._schedule_path(target, handler)
        self._observer.start()

        self._workers = [
            threading.Thread(target=self._worker_loop, daemon=True)
            for _ in range(max(1, self.config.scan_worker_threads))
        ]
        for w in self._workers:
            w.start()

        log_event(CATEGORY_REALTIME, f"リアルタイム監視を開始しました（{self.mode_name}）: {', '.join(targets)}")
        return True

    def stop(self) -> None:
        if self.is_running:
            log_event(CATEGORY_REALTIME, f"リアルタイム監視を停止しました（{self.mode_name}）")
        if self._debounce is not None:
            self._debounce.cancel_all()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        self._scheduled_watches.clear()

        self._stop_event.set()
        for _ in self._workers:
            self._scan_queue.put(_SENTINEL)
        for w in self._workers:
            w.join(timeout=2)
        self._workers = []

    # -- 監視対象の動的変更（3.3節） -------------------------------------
    def add_target(self, path: str) -> None:
        if not self.is_running:
            return
        handler = _EventHandler(self._exclude_service, self._debounce, self._extra_exclude_dirs)
        self._schedule_path(path, handler)

    def on_excludes_changed(self, exclude_type: str) -> None:
        """除外設定の変更通知。イベントごとにExcludeServiceへ照合するため再設定は不要"""

    def remove_target(self, path: str) -> None:
        watch = self._scheduled_watches.pop(path, None)
        if watch is not None and self._observer is not None:
            self._observer.unschedule(watch)

    def _schedule_path(self, path: str, handler: _EventHandler) -> None:
        target_path = Path(path)
        if not target_path.exists():
            log_event(CATEGORY_REALTIME, f"監視対象フォルダが見つからないため監視を停止しました: {path}", level=logging.WARNING, error_code="E2001")
            self.watch_target_lost.emit(path)
            self._watch_target_service.disable_by_path(path)
            return
        try:
            watch = self._observer.schedule(handler, path, recursive=True)
            self._scheduled_watches[path] = watch
        except OSError:
            log_event(CATEGORY_REALTIME, f"監視対象フォルダが見つからないため監視を停止しました: {path}", level=logging.WARNING, error_code="E2001")
            self.watch_target_lost.emit(path)
            self._watch_target_service.disable_by_path(path)

    # -- スキャンディスパッチ（6章） ---------------------------------------
    def _enqueue_scan(self, path: str) -> None:
        self._scan_queue.put(path)

    def _worker_loop(self) -> None:
        while True:
            item = self._scan_queue.get()
            if item is _SENTINEL:
                return
            self._scan_one(item)

    def _scan_one(self, path: str, retried: bool = False) -> None:
        from core.exceptions import AppError, ClamdConnectionError

        try:
            results = self._clamav_service.scan_file(path, priority=True)
        except ClamdConnectionError as exc:
            # clamdに接続できない間は、ファイルごとには通知しない（コードレビューで修正。以前はファイルの
            # イベントごとにエラーダイアログが入れ子で積み重なり、操作できなくなっていた）。
            # 接続できなくなった最初の1回だけ通知し、次に検査できた時点で解除する
            with self._clamd_state_lock:
                first = not self._clamd_unavailable
                self._clamd_unavailable = True
            if first:
                log_event(
                    CATEGORY_REALTIME,
                    "clamdに接続できないため、リアルタイム監視の検査ができません。clamdが復旧するまで、検査できなかったファイルは個別に通知しません",
                    level=logging.WARNING,
                    error_code=exc.code,
                )
                self.scan_error.emit(exc)
            else:
                logger.debug(f"clamdに接続できないため検査できませんでした: {path}")
            return
        except AppError as exc:
            self.scan_error.emit(exc)
            return

        with self._clamd_state_lock:
            recovered = self._clamd_unavailable
            self._clamd_unavailable = False
        if recovered:
            log_event(CATEGORY_REALTIME, "clamdに接続できたため、リアルタイム監視の検査を再開しました")

        for result in results:
            if result.status == "ERROR" and not os.path.exists(path):
                # 一時ファイル等がスキャンの前に削除・移動された。移動先は移動イベントで検査するため、
                # 警告にしない（以前はPermission deniedのE2004が大量に記録されていた）
                logger.debug(f"スキャンの前にファイルが削除・移動されました: {path}")
                return
            if result.status == "ERROR" and not retried and error_reason(result.detail) != ERROR_REASON_SIZE_LIMIT:
                # 6.2節: ファイルロック等のエラーは1回だけ1秒後リトライする
                time.sleep(self.config.lock_retry_wait_sec)
                self._scan_one(path, retried=True)
                return
            if result.status == "ERROR":
                # 6.2節: リトライ後も失敗した場合はWARNINGログのみ（GUI通知しない）
                log_event(
                    CATEGORY_REALTIME,
                    f"ファイルをスキャンできませんでした: {path} ({result.detail})",
                    level=logging.WARNING,
                    error_code="E2004",
                )
            if result.status == "FOUND":
                self._handle_detection(path, result.detail)

    def _handle_detection(self, path: str, virus_name: str) -> None:
        log_event(
            CATEGORY_REALTIME,
            f"ウイルスを検知しました: {path} ({virus_name}) [リアルタイム監視]",
            level=logging.WARNING,
        )
        self.detected.emit(path, virus_name)
        quarantined = False
        if self.config.default_action == "quarantine":
            try:
                self._quarantine_service.quarantine(path, virus_name)
                quarantined = True
            except Exception as exc:  # noqa: BLE001 - GUI通知のため広く捕捉
                logger.error(f"リアルタイム検知の自動隔離に失敗しました (detail: {exc})")
        # 通知（トースト・メール）は隔離を試した後の実際の結果で送る（コードレビューで修正。
        # 以前は設定値から「自動的に隔離されました」と通知し、隔離に失敗しても同じ文面だった）
        self.detection_handled.emit(path, virus_name, quarantined)


_SENTINEL = object()

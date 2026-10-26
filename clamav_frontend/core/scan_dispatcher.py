"""
ScanDispatcher — バックグラウンドスレッドでのスキャン実行と進捗通知

参照: 画面設計書2章（進捗バー、現在スキャン中のファイルパス、検知/スキャン件数カウンタ）、
      Windowsリアルタイム監視詳細設計書6章（ScanDispatcherは共通化して先に作る、との方針どおり
      Phase4でオンデマンドスキャン向けに先行実装し、Phase5でリアルタイム監視からも再利用する）

設計上の補足（実装時の判断）:
  clamdソケット通信プロトコル設計書2.1節では「ディレクトリはMULTISCANを使用する」と
  されているが、MULTISCANは全件スキャン完了まで応答が返らないため、画面設計書2章が求める
  「現在スキャン中のファイルパス」のリアルタイム表示や検知の逐次反映ができない。
  そのため本Dispatcherはディレクトリ内ファイルを列挙し、1ファイルずつ scan_file() を
  呼び出す方式で実装する（進捗可視化を優先）。MULTISCANはClamAVService/テスト上は
  引き続き提供し、将来的な性能最適化（並列MULTISCAN化）の余地として残す。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from core.clamav_service import ClamAVService
from core.exceptions import AppError, ClamdConnectionError
from core.exclude_service import ExcludeService
from core.logger import CATEGORY_SCAN, log_event


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScanWorker(QObject):
    """QThread上で実行される実処理本体"""

    progress = Signal(int, int, str)  # scanned_count, total_count, current_path
    file_scanned = Signal(str, str, str, str)  # path, status, detail, detected_at(iso)
    finished = Signal(dict)  # {"scanned": int, "detected": int, "errors": int, "cancelled": bool}
    error = Signal(object)  # AppError
    # 待ち状態の通知（state, path, 経過秒）。state: "queued"=clamdの接続枠の空き待ち（待機中）、
    # "stalled"=clamdの応答待ちがSTALL_NOTICE_SEC以上続いている（応答待ち）、"scanning"=通常に戻った
    activity = Signal(str, str, int)

    #: 1ファイルの応答待ちがこの秒数を超えたら「応答待ち」を表示する（プロトコル設計書4章: 10秒）
    STALL_NOTICE_SEC = 10.0
    _MONITOR_INTERVAL_SEC = 1.0

    def __init__(
        self,
        clamav_service: ClamAVService,
        targets: list[str],
        include_subfolders: bool = True,
        apply_exclude: bool = True,
        exclude_service: ExcludeService | None = None,
        label: str = "手動スキャン",
        skip_dirs: tuple[str, ...] = (),
    ) -> None:
        """label: ログに記録するスキャンの種類（例: 「手動スキャン」「スケジュールスキャン（日次）」）
        skip_dirs: 列挙時に立ち入らないフォルダ（Linuxフルスキャンの/proc等。core/scan_targets.py）"""
        super().__init__()
        self._label = label
        self._clamav_service = clamav_service
        self._targets = [Path(t) for t in targets]
        self._include_subfolders = include_subfolders
        self._apply_exclude = apply_exclude
        self._exclude_service = exclude_service
        self._skip_dirs = {os.path.normcase(os.path.normpath(d)) for d in skip_dirs}
        self._stop_requested = False
        self._thread_ident: int | None = None
        self._current: tuple[str, float] | None = None  # (スキャン中のパス, 開始時刻)
        self._stalled = False
        self._done = threading.Event()

    def stop(self) -> None:
        """停止要求。clamdの応答待ち中なら通信を打ち切ってすぐに止める
        （プロトコル設計書4章: スキャンはタイムアウトせず、ユーザーによる明示キャンセルのみで中断）"""
        self._stop_requested = True
        if self._thread_ident is not None and self._current is not None:
            self._clamav_service.abort_thread(self._thread_ident)

    def _monitor_stall(self) -> None:
        """応答待ちの監視（別スレッド）。同じファイルの応答がSTALL_NOTICE_SEC以上ない間、経過秒を通知する"""
        while not self._done.wait(self._MONITOR_INTERVAL_SEC):
            current = self._current
            if current is None:
                continue
            elapsed = time.monotonic() - current[1]
            if elapsed >= self.STALL_NOTICE_SEC:
                self._stalled = True
                self.activity.emit("stalled", current[0], int(elapsed))

    def run(self) -> None:
        self._thread_ident = threading.get_ident()
        self._done.clear()
        monitor = threading.Thread(target=self._monitor_stall, daemon=True)
        monitor.start()
        try:
            self._run()
        finally:
            self._done.set()
            self._current = None

    def _run(self) -> None:
        targets_text = ", ".join(str(t) for t in self._targets)
        log_event(CATEGORY_SCAN, f"{self._label}を開始しました: {targets_text}")
        self.progress.emit(0, 0, "スキャン対象のファイルを確認しています...")
        try:
            files = self._enumerate_files()
        except AppError as exc:
            self.error.emit(exc)
            self._finish({"scanned": 0, "detected": 0, "errors": 0, "cancelled": False})
            return

        total = len(files)
        scanned = 0
        detected = 0
        errors = 0
        aborted = False

        for path in files:
            if self._stop_requested:
                break

            self.progress.emit(scanned, total, str(path))

            try:
                results = self._scan_one(path)
            except ClamdConnectionError as exc:
                if self._stop_requested:
                    break  # 停止操作で通信を打ち切った結果のエラーは表示しない
                # clamdに接続できない場合は以降のファイルも全て失敗するため中断する
                # （続行するとファイル数分のエラーダイアログが表示されてしまう）
                errors += 1
                aborted = True
                self.error.emit(exc)
                break
            except AppError as exc:
                if self._stop_requested:
                    break
                errors += 1
                self.error.emit(exc)
                scanned += 1
                continue
            if self._stop_requested:
                break  # 接続枠の空き待ち・応答待ちの途中で停止された

            for result in results:
                if result.status == "FOUND":
                    detected += 1
                    log_event(
                        CATEGORY_SCAN,
                        f"ウイルスを検知しました: {result.path} ({result.detail}) [{self._label}]",
                        level=logging.WARNING,
                    )
                elif result.status == "ERROR":
                    errors += 1
                    # システムファイル等で大量に出得るため、個別のエラーはDEBUGに留め件数を完了ログに出す
                    log_event(
                        CATEGORY_SCAN,
                        f"スキャンできませんでした: {result.path} ({result.detail})",
                        level=logging.DEBUG,
                    )
                self.file_scanned.emit(result.path, result.status, result.detail, _now_iso())

            scanned += 1

        self._finish(
            {
                "scanned": scanned,
                "detected": detected,
                "errors": errors,
                "cancelled": self._stop_requested or aborted,
            }
        )

    def _scan_one(self, path: Path):
        """1ファイルをスキャンする。待機中・応答待ちの表示と停止操作による打ち切りに対応する"""
        waited = []

        def on_wait() -> None:
            waited.append(True)
            self.activity.emit("queued", str(path), 0)

        def on_start() -> None:
            # 応答待ちの計測は接続枠を確保してclamdとの通信を始めた時点から（空き待ちの時間は含めない）
            self._current = (str(path), time.monotonic())

        try:
            return self._clamav_service.scan_file(
                path, on_wait=on_wait, cancelled=lambda: self._stop_requested, on_start=on_start
            )
        finally:
            self._current = None
            if self._stalled or waited:
                self._stalled = False
                self.activity.emit("scanning", "", 0)

    def _finish(self, summary: dict) -> None:
        state = "中断しました" if summary["cancelled"] else "完了しました"
        log_event(
            CATEGORY_SCAN,
            f"{self._label}が{state}: スキャン {summary['scanned']}件 / 検知 {summary['detected']}件 / "
            f"エラー {summary['errors']}件",
            level=logging.WARNING if summary["detected"] else logging.INFO,
        )
        self.finished.emit(summary)

    def _enumerate_files(self) -> list[Path]:
        # 除外ルールはスキャン開始時に1回だけ読み込む（ファイルごとにDBを読むとフルスキャンで極端に遅くなる）
        if self._apply_exclude and self._exclude_service is not None:
            is_excluded = self._exclude_service.make_matcher()
        else:
            is_excluded = lambda _path: False  # noqa: E731

        files: list[Path] = []
        for target in self._targets:
            if self._stop_requested:
                break
            if target.is_file():
                if not is_excluded(str(target)):
                    files.append(target)
                continue

            if not target.is_dir():
                continue

            if self._include_subfolders:
                for root, dirs, filenames in os.walk(target):
                    if self._stop_requested:
                        break
                    # 立ち入らないフォルダ（Linuxの/proc等）はos.walkの探索対象から外す
                    dirs[:] = [
                        d for d in dirs
                        if os.path.normcase(os.path.normpath(os.path.join(root, d))) not in self._skip_dirs
                    ]
                    for name in filenames:
                        candidate = Path(root) / name
                        if not is_excluded(str(candidate)):
                            files.append(candidate)
            else:
                for entry in target.iterdir():
                    if entry.is_file() and not is_excluded(str(entry)):
                        files.append(entry)
        return files


class ScanDispatcher(QObject):
    """スキャン画面（Phase4以降）から利用する公開インターフェース

    QThread上でScanWorkerを実行し、GUIスレッドをブロックしない。
    """

    progress = Signal(int, int, str)
    file_scanned = Signal(str, str, str, str)
    finished = Signal(dict)
    error = Signal(object)
    activity = Signal(str, str, int)  # ScanWorker.activityの中継（待機中・応答待ちの表示用）

    def __init__(
        self,
        clamav_service: ClamAVService,
        exclude_service: ExcludeService | None = None,
    ) -> None:
        super().__init__()
        self._clamav_service = clamav_service
        self._exclude_service = exclude_service
        self._thread: QThread | None = None
        self._worker: ScanWorker | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(
        self,
        targets: list[str],
        include_subfolders: bool = True,
        apply_exclude: bool = True,
        label: str = "手動スキャン",
        skip_dirs: tuple[str, ...] = (),
    ) -> None:
        if self.is_running:
            return  # 画面設計書2章: 開始/停止は排他制御

        self._thread = QThread()
        self._worker = ScanWorker(
            self._clamav_service,
            targets,
            include_subfolders=include_subfolders,
            apply_exclude=apply_exclude,
            exclude_service=self._exclude_service,
            label=label,
            skip_dirs=skip_dirs,
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.file_scanned.connect(self.file_scanned)
        self._worker.error.connect(self.error)
        self._worker.activity.connect(self.activity)
        self._worker.finished.connect(self._on_finished)

        self._thread.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()

    def _on_finished(self, summary: dict) -> None:
        self.finished.emit(summary)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None

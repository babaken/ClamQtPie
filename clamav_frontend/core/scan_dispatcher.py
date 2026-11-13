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
import re
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from core.clamav_service import ERROR_REASON_SIZE_LIMIT, ClamAVService, error_reason
from core.exceptions import AppError, ClamdConnectionError
from core.exclude_service import ExcludeService
from core.logger import CATEGORY_SCAN, log_event
from core.text_safety import visible_controls

logger = logging.getLogger("clamav_frontend")


#: フォルダをたどる途中で入ってよいマウントのファイルシステム（ローカルのディスクとメモリ）。
#: ネットワーク（nfs・cifs・sshfs などの fuse）・疑似ファイルシステム・イメージ（squashfs・overlay など）には入らない。
#: マウントした場所そのものを対象に指定したときは、種類にかかわらず検査する（README_DEV §96）
LOCAL_DISK_FILESYSTEMS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "jfs", "reiserfs", "zfs",
    "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "tmpfs",
})

_MOUNTINFO_PATH = "/proc/self/mountinfo"
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


def parse_mountinfo(text: str) -> dict[str, str]:
    """/proc/self/mountinfo の中身から「マウントした場所 → ファイルシステムの種類」を作る。
    同じ場所に重ねてマウントしたときは、後の行（上に見えているもの）を使う"""
    mounts: dict[str, str] = {}
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split(" ")
        if not sep or len(fields) < 5 or not right:
            continue
        point = _MOUNTINFO_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4])
        mounts[point] = right.split(" ")[0]
    return mounts


def _read_mount_types() -> dict[str, str] | None:
    """今のマウントの一覧。Windows や、一覧が読めないときは None（マウントでは分けずにたどる）"""
    if os.name == "nt":
        return None
    try:
        with open(_MOUNTINFO_PATH, encoding="utf-8", errors="surrogateescape") as f:
            return parse_mountinfo(f.read())
    except OSError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScanWorker(QObject):
    """QThread上で実行される実処理本体"""

    # パスは object で渡す（str で渡すと、Qt の文字列に変わるときに UTF-8 にできない名前の文字
    # （対になっていないサロゲート）が黙って消え、受け取った側（隔離・履歴・画面）で別のファイルの名前になる）
    progress = Signal(int, int, object)  # scanned_count, total_count, current_path
    file_scanned = Signal(object, str, str, str)  # path, status, detail, detected_at(iso)
    finished = Signal(dict)  # {"scanned": int, "detected": int, "errors": int, "cancelled": bool}
    error = Signal(object)  # AppError
    # 待ち状態の通知（state, path, 経過秒）。state: "queued"=clamdの接続枠の空き待ち（待機中）、
    # "stalled"=clamdの応答待ちがSTALL_NOTICE_SEC以上続いている（応答待ち）、"scanning"=通常に戻った
    activity = Signal(str, object, int)

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
        self._reported_mounts: set[str] = set()  # 入らなかったマウントのログは 1 回だけ残す（2 回たどるため）
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
        # 対象のファイルの一覧はメモリに持たない（フルスキャンでは数百万件になり、サービスのメモリが
        # 数 GB に増えていた）。先に数だけ数え（進み具合の分母）、検査ではもう一度たどりながら 1 件ずつ渡す
        try:
            is_excluded = self._make_exclude_matcher()
            total = sum(1 for _ in self._iter_files(is_excluded))
        except AppError as exc:
            self.error.emit(exc)
            self._finish({"scanned": 0, "detected": 0, "errors": 0, "cancelled": False})
            return

        scanned = 0
        detected = 0
        errors = 0
        aborted = False

        for path in self._iter_files(is_excluded):
            if self._stop_requested:
                break

            # 数えた後に増えたファイルがあっても、分母が検査した数を下回らないようにする
            total = max(total, scanned + 1)
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
                # 理由をログに残す（サービスではジョブ・履歴に要約しか残らず、管理者に理由が分からない）
                logger.error(f"{self._label}を中断します: {exc.message} (detail: {visible_controls(str(exc.detail))})",
                             extra={"error_code": exc.code})
                self.error.emit(exc)
                break
            except AppError as exc:
                if self._stop_requested:
                    break
                errors += 1
                self.error.emit(exc)
                scanned += 1
                continue
            except Exception as exc:  # noqa: BLE001 - 1ファイルの想定外の失敗でスキャン全体を止めない
                # 止まると完了の合図も出ず、画面が「スキャン中」のまま戻らなくなる
                logger.exception(f"スキャン中に想定外のエラーが発生しました: {visible_controls(str(path))}",
                                 extra={"error_code": "E9001"})
                errors += 1
                scanned += 1
                self.file_scanned.emit(str(path), "ERROR", f"想定外のエラー: {type(exc).__name__}", _now_iso())
                continue
            if self._stop_requested:
                break  # 接続枠の空き待ち・応答待ちの途中で停止された

            # 検知・エラーの記録と通知（隔離に使われる）には、列挙したパスを使う。clamdの応答に含まれる
            # パスは、検査を依頼したファイルと一致する保証がないため使わない（ClamAVService._bind_to_requested_path）
            for result in results:
                if result.status == "FOUND":
                    detected += 1
                    log_event(
                        CATEGORY_SCAN,
                        f"ウイルスを検知しました: {path} ({result.detail}) [{self._label}]",
                        level=logging.WARNING,
                    )
                elif result.status == "ERROR":
                    errors += 1
                    # システムファイル等で大量に出得るため、個別のエラーはDEBUGに留め件数を完了ログに出す。
                    # サイズ等の上限で検査していないファイルは、見落とされないようINFOで残す
                    size_limit = error_reason(result.detail) == ERROR_REASON_SIZE_LIMIT
                    log_event(
                        CATEGORY_SCAN,
                        f"スキャンできませんでした: {path} ({result.detail})",
                        level=logging.INFO if size_limit else logging.DEBUG,
                    )
                self.file_scanned.emit(str(path), result.status, result.detail, _now_iso())

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

    def _make_exclude_matcher(self):
        # 除外ルールはスキャン開始時に1回だけ読み込む（ファイルごとにDBを読むとフルスキャンで極端に遅くなる）
        if self._apply_exclude and self._exclude_service is not None:
            return self._exclude_service.make_matcher()
        return lambda _path: False

    def _iter_files(self, is_excluded) -> Iterator[Path]:
        """検査するファイルを 1 件ずつ返す（一覧は作らない。数えるときと検査するときの 2 回呼ぶ）"""
        # マウントの一覧はたどるたびに読み直す。Linux では同じフォルダを 2 回たどらないように、
        # たどったフォルダ（デバイス番号・inode）を覚える（バインドマウント・btrfs のサブボリューム）
        mounts = _read_mount_types()
        seen_dirs: set[tuple[int, int]] | None = set() if mounts is not None else None
        for target in self._targets:
            if self._stop_requested:
                break
            if target.is_file():
                if not is_excluded(str(target)):
                    yield target
                continue

            if not target.is_dir():
                continue

            if self._include_subfolders:
                target_str = str(target)
                # マウントの一覧と比べるための本当のパス（対象をリンクで指定したとき）。たどる途中のフォルダは
                # リンクをたどらないため、対象の本当のパスに続きをつなげれば本当のパスになる
                real_target = os.path.realpath(target_str) if mounts is not None else target_str
                if seen_dirs is not None:
                    _remember_dir(target_str, seen_dirs)
                for root, dirs, filenames in os.walk(target):
                    if self._stop_requested:
                        break
                    # 立ち入らないフォルダ（Linuxの/proc等）・リンク・ジャンクション・ローカルのディスク以外の
                    # マウントは os.walk の探索対象から外す
                    dirs[:] = [
                        d for d in dirs
                        if self._should_descend(
                            os.path.join(root, d), target_str, real_target, mounts, seen_dirs,
                        )
                    ]
                    for name in filenames:
                        candidate = Path(root) / name
                        if not _is_link(str(candidate)) and not is_excluded(str(candidate)):
                            yield candidate
            else:
                for entry in target.iterdir():
                    if self._stop_requested:
                        break
                    if not _is_link(str(entry)) and entry.is_file() and not is_excluded(str(entry)):
                        yield entry

    def _should_descend(
        self,
        path: str,
        target: str,
        real_target: str,
        mounts: dict[str, str] | None,
        seen_dirs: set[tuple[int, int]] | None,
    ) -> bool:
        """たどる途中のフォルダに入るか（対象に指定したフォルダそのものには使わない）"""
        if os.path.normcase(os.path.normpath(path)) in self._skip_dirs or _is_link(path):
            return False
        if mounts is None:
            return True
        real = os.path.normpath(real_target + path[len(target):])
        fstype = mounts.get(real)
        if fstype is not None and fstype not in LOCAL_DISK_FILESYSTEMS:
            if real not in self._reported_mounts:
                self._reported_mounts.add(real)
                logger.info(
                    "ローカルのディスクではないマウント（%s）には入りません: %s（検査するには、この場所を直接指定してください）",
                    fstype, path,
                )
            return False
        return seen_dirs is None or _remember_dir(path, seen_dirs)


def _remember_dir(path: str, seen_dirs: set[tuple[int, int]]) -> bool:
    """初めてたどるフォルダなら覚えて True。同じフォルダを別の場所から見ている（すでにたどった）なら False"""
    try:
        st = os.stat(path)
    except OSError:
        return True  # 開けないフォルダは os.walk に任せる（中身を読めなければ何も返さない）
    key = (st.st_dev, st.st_ino)
    if key in seen_dirs:
        return False
    seen_dirs.add(key)
    return True


def _is_link(path: str) -> bool:
    """リンク・ジャンクションか。スキャンの対象のフォルダの中から外の場所をたどらないために使う
    （os.walk は Windows のジャンクションをたどる。一般の利用者でも、ほかの利用者のフォルダを指すジャンクションを
    自分のフォルダに作れるため、サービスでは頼んだ利用者のフォルダの外を列挙してしまう。12章 R-9）。
    リンクの先が対象の中にあれば、そこを列挙したときに検査する"""
    return os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path))


class ScanDispatcher(QObject):
    """スキャン画面（Phase4以降）から利用する公開インターフェース

    QThread上でScanWorkerを実行し、GUIスレッドをブロックしない。
    """

    # パスは object で渡す（ScanWorker と同じ理由）
    progress = Signal(int, int, object)
    file_scanned = Signal(object, str, str, str)
    finished = Signal(dict)
    error = Signal(object)
    activity = Signal(str, object, int)  # ScanWorker.activityの中継（待機中・応答待ちの表示用）
    #: スキャンを始めたとき（label）。画面・トレイ・Web API のどこから始めても出す（Web API のジョブの記録に使う）
    started = Signal(str)

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
        #: 実行中（または直前）のスキャンの出どころ・種類・対象（スキャン履歴の記録に使う）
        self.current: dict = {"source": "gui", "scan_type": "custom", "targets": []}

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
        source: str = "gui",
        scan_type: str = "custom",
    ) -> None:
        """source: "gui"（画面・トレイ）| "api"、scan_type: "quick" | "full" | "custom"（スキャン履歴に残す）"""
        if self.is_running:
            return  # 画面設計書2章: 開始/停止は排他制御
        self.current = {"source": source, "scan_type": scan_type, "targets": list(targets)}

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
        self.started.emit(label)

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()

    #: サービスを止めるときに、スキャンのスレッドが終わるのを待つ上限（ミリ秒）
    SHUTDOWN_WAIT_MS = 10_000

    def shutdown(self) -> bool:
        """サービスの終了時に、実行中のスキャンを止めてスレッドの終わりを待つ（終わったら True）

        終了の処理ではイベントループがもう回らないため、finished から _on_finished が呼ばれず、
        QThread が動いたまま破棄されてプロセスが異常終了していた（Linux 実機で発見）。
        ここでは停止を求めた後、スレッドのイベントループに終わりを伝えて待つ。
        """
        thread = self._thread
        if thread is None:
            return True
        self.stop()
        thread.quit()  # run() から戻った後に、スレッドのイベントループを終える
        if not thread.wait(self.SHUTDOWN_WAIT_MS):
            logger.warning(f"スキャンのスレッドが {self.SHUTDOWN_WAIT_MS // 1000} 秒以内に終わりませんでした")
            return False
        self._thread = None
        self._worker = None
        return True

    def _on_finished(self, summary: dict) -> None:
        self.finished.emit(summary)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None

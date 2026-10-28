"""
ClamAVService — ClamdClientをラップした上位サービス層

参照: clamdソケット通信プロトコル設計書4〜7章
- PING失敗時のリトライ（3回、1秒間隔）
- clamd自動再起動試行（1回のみ）
- 同時実行数制御（max_concurrent_connectionsをセマフォで制御）
- 単一ファイルのスキャン方式の選択（UNIXソケットならFILDES、それ以外はSCAN→権限エラー時INSTREAM）
"""
from __future__ import annotations

import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import BinaryIO, Callable

from core.clamd_client import ClamdClient, FdPassUnsupportedError, ScanResult, fdpass_available
from core.config_model import ClamdConfig
from core.exceptions import ClamdConnectionError, ClamdProcessError, ClamdStreamSizeError

logger = logging.getLogger("clamav_frontend")

#: スキャンの前にファイルが削除・移動されていた場合のERRORの詳細
FILE_VANISHED_DETAIL = "ファイルが見つかりません（スキャンの前に削除または移動されました）"
#: 1ファイルのSCANに対して、clamdの応答が1件の結果として解釈できなかった場合のERRORの詳細
UNEXPECTED_RESPONSE_DETAIL = "clamdの応答を1件の結果として解釈できませんでした"
#: サイズ等の上限を超えて検査していないファイルのERRORの詳細の先頭
SIZE_LIMIT_DETAIL_PREFIX = "サイズ等の上限を超えたため検査していません"
#: clamdがスキャンの上限（MaxFileSize等）を超えたファイルを報告するときの名前（clamd.confのAlertExceedsMax yes）。
#: マルウェアと判定したわけではないため、検知ではなく「上限を超えて未検査」として扱う（大きいだけの正常な
#: ファイルを自動で隔離しないため）
_LIMITS_EXCEEDED_PREFIX = "Heuristics.Limits.Exceeded"

#: スキャンのERRORの理由（スキャン結果の画面・ログで区別して表示する）
ERROR_REASON_SIZE_LIMIT = "size_limit"
ERROR_REASON_PERMISSION = "permission"
ERROR_REASON_VANISHED = "vanished"
ERROR_REASON_OTHER = "other"


def error_reason(detail: str) -> str:
    """ERRORの結果の詳細から、検査できなかった理由を返す（ERROR_REASON_*）"""
    if detail == FILE_VANISHED_DETAIL:
        return ERROR_REASON_VANISHED
    if detail.startswith(SIZE_LIMIT_DETAIL_PREFIX):
        return ERROR_REASON_SIZE_LIMIT
    if "denied" in detail.lower():
        return ERROR_REASON_PERMISSION
    return ERROR_REASON_OTHER


def _limits_exceeded_as_unscanned(results: list[ScanResult]) -> list[ScanResult]:
    """clamdの「上限を超えた」報告（Heuristics.Limits.Exceeded.*のFOUND）を、上限を超えて未検査のERRORにする"""
    return [
        ScanResult(path=r.path, status="ERROR", detail=f"{SIZE_LIMIT_DETAIL_PREFIX}（{r.detail}）")
        if r.status == "FOUND" and r.detail.startswith(_LIMITS_EXCEEDED_PREFIX)
        else r
        for r in results
    ]


def _bind_to_requested_path(results: list[ScanResult], path: str | Path) -> list[ScanResult]:
    """1ファイルのSCANの結果を、検査を依頼したパスの結果に置き換える

    応答に含まれるパスは、検査を依頼したファイルと一致する保証がないため使わない。隔離・通知・ログは
    呼び出し側が知っているパスで行う（FILDESのscan_fdが表示用のパスに置き換えているのと同じ）。
    1ファイルの検査なのに結果が1件でない場合は、どの結果がそのファイルのものか分からないためERRORとする。
    """
    if len(results) != 1:
        return [ScanResult(path=str(path), status="ERROR", detail=UNEXPECTED_RESPONSE_DETAIL)]
    return [ScanResult(path=str(path), status=results[0].status, detail=results[0].detail)]


def _is_permission_error(result: ScanResult) -> bool:
    # clamdの応答例: "<path>: lstat() failed: Permission denied. ERROR" / "<path>: Access denied. ERROR"
    return result.status == "ERROR" and "denied" in result.detail.lower()


#: 接続枠の空き待ちの間に停止操作を確認する間隔（秒）
_WAIT_POLL_SEC = 0.5

_PING_RETRY_COUNT = 3
_PING_RETRY_INTERVAL_SEC = 1.0


class ClamAVService:
    """スキャン画面・リアルタイム監視の双方から利用される共通サービス

    参照: Windowsリアルタイム監視詳細設計書6.1節、clamdソケット通信プロトコル設計書7章
    """

    def __init__(
        self,
        config: ClamdConfig,
        restart_clamd: Callable[[], bool] | None = None,
    ) -> None:
        """
        restart_clamd: clamdサービスの起動を試みるコールバック（core/clamd_process.py、E8003関連）
        """
        self.config = config
        self._client = ClamdClient(config)
        self._restart_clamd = restart_clamd
        self._semaphore = threading.Semaphore(config.max_concurrent_connections)
        # プロトコル設計書5章「キュー超過時はリアルタイム検知を優先」: オンデマンドスキャンは
        # 同時接続数の上限より1つ少ない数までしか使えないようにし、リアルタイム用の枠を常に1つ残す
        self._ondemand_semaphore = threading.Semaphore(max(config.max_concurrent_connections - 1, 1))
        # FILDES（ファイルディスクリプタ渡し）を使うか。UNIXソケット接続のときだけ使い、
        # clamdが非対応と分かったら以降はSCANに戻す
        self._use_fdpass = fdpass_available() and self._client.uses_unix_socket()

    # -- 疎通確認 -----------------------------------------------------
    def ping(self, retry: bool = True) -> bool:
        """PING確認。retry=Trueの場合は3回・1秒間隔でリトライする（4章）"""
        attempts = _PING_RETRY_COUNT if retry else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._client.ping()
            except ClamdConnectionError as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(_PING_RETRY_INTERVAL_SEC)

        if retry:
            # retry=Falseは定期的な状態確認（ホーム画面・ステータスバー、60秒ごと）で使うため、
            # clamd停止中に毎回WARNINGを記録してログが埋まらないよう、リトライ付きの確認時のみ記録する
            logger.warning(
                f"clamdへのPINGが{attempts}回失敗しました (detail: {last_error})",
                extra={"error_code": "E1001"},
            )
        return False

    def ensure_available(self) -> bool:
        """PING失敗時、自動再起動を1回だけ試みる（4章）"""
        if self.ping():
            return True

        if self._restart_clamd is None:
            return False

        logger.info("clamdが応答しないため自動再起動を試みます")
        try:
            self._restart_clamd()
        except Exception as exc:
            logger.error(
                f"ClamAVサービスの起動に失敗しました (detail: {exc})",
                extra={"error_code": "E8003"},
            )
            raise ClamdProcessError(detail=str(exc)) from exc

        time.sleep(5)  # 4章: 再起動試行後5秒待って再度PING
        try:
            if self._client.ping():
                return True
        except ClamdConnectionError:
            pass

        logger.error("ClamAVサービスを起動できませんでした", extra={"error_code": "E1006"})
        return False

    @property
    def endpoint(self) -> str:
        return self._client.endpoint

    def get_version(self) -> tuple[str, str, str]:
        return self._client.get_version()

    def get_database_version(self) -> str:
        _, db_version, _ = self.get_version()
        return db_version

    # -- スキャン ---------------------------------------------------------
    def scan_file(
        self,
        path: str | Path,
        priority: bool = False,
        on_wait: Callable[[], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> list[ScanResult]:
        """priority=True: リアルタイム監視からの呼び出し（オンデマンドスキャンより優先して接続枠を使える）

        on_wait: オンデマンドスキャンが接続枠の空きを待つことになった時に1回呼ばれる
                 （プロトコル設計書5章「オンデマンドスキャンはキュー待機（進捗バーに待機中を表示）」）
        cancelled: 接続枠の空き待ちの間に定期的に確認し、Trueなら待つのをやめて空の結果を返す（停止操作）
        on_start: 接続枠を確保してclamdとの通信を始める直前に呼ばれる（応答待ち時間の計測開始用）
        """
        if priority:
            return _limits_exceeded_as_unscanned(self._scan_file(path))
        if not self._ondemand_semaphore.acquire(blocking=False):
            if on_wait is not None:
                on_wait()
            while not self._ondemand_semaphore.acquire(timeout=_WAIT_POLL_SEC):
                if cancelled is not None and cancelled():
                    return []
        try:
            if on_start is not None:
                on_start()
            return _limits_exceeded_as_unscanned(self._scan_file(path))
        finally:
            self._ondemand_semaphore.release()

    def abort_thread(self, thread_ident: int) -> bool:
        """指定スレッドで応答待ちのclamd通信を打ち切る（スキャンの停止操作用）"""
        return self._client.abort_thread(thread_ident)

    def _scan_file(self, path: str | Path) -> list[ScanResult]:
        """単一ファイルスキャン（2.1節）。同時接続数はセマフォで制御（5章）

        UNIXソケット接続（Linux）では、アプリが開いたファイルをFILDESでclamdへ渡して検査する。
        SCANはclamdプロセス自身がファイルを開くため、clamdの実行ユーザー（Linuxでは通常clamav）が
        読めないファイル（0700/0750のホームディレクトリ配下等）はPermission deniedのERRORになり、
        INSTREAMでの送り直しで通信が2回になる上、clamd.logに毎回警告が残っていた（Linux実機で発見）。
        FILDESを使えない場合（Windows・TCP接続・clamdが非対応・アプリ自身が開けない・通常のファイル
        以外）はSCANを送り、権限エラーならアプリ自身がファイルを開いてINSTREAMで内容を送り直す。
        """
        with self._semaphore:
            if self._use_fdpass:
                result = self._scan_by_fdpass(path)
                if result is not None:
                    return [result]
            results = _bind_to_requested_path(self._client.scan_path(path, multi=False), path)
            if _is_permission_error(results[0]):
                return [self._scan_by_stream(path, results[0])]
            return results

    def _scan_by_fdpass(self, path: str | Path) -> ScanResult | None:
        """FILDESで検査する。FILDESを使えない場合はNone（呼び出し側でSCANを送る）"""
        try:
            # 名前付きパイプ等で開くのを待たされないようO_NONBLOCKで開き、通常のファイルだけを渡す
            # （シンボリックリンク・特殊ファイルは従来どおりSCANでclamdの設定に従って扱う）
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return ScanResult(path=str(path), status="ERROR", detail=FILE_VANISHED_DETAIL)
        except OSError:
            return None  # アプリ自身が開けない（clamdなら読める場合がある）
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            return self._client.scan_fd(fd, display_path=str(path))
        except FdPassUnsupportedError as exc:
            self._use_fdpass = False
            logger.warning(
                f"clamdがファイルディスクリプタ渡し（FILDES）に対応していないため、以降はSCANでスキャンします (detail: {exc})"
            )
            return None
        finally:
            os.close(fd)

    def _scan_by_stream(self, path: str | Path, original: ScanResult) -> ScanResult:
        try:
            with open(path, "rb") as f:
                result = self._client.scan_stream(f)
        except FileNotFoundError:
            # 一時ファイル等がスキャン前に削除・移動された（clamdは読めないため元のERRORは
            # Permission deniedになり、原因が分かりにくかった。Linux実機で発見）
            return ScanResult(path=str(path), status="ERROR", detail=FILE_VANISHED_DETAIL)
        except OSError:
            return original  # アプリ自身も読めない場合は元のERRORのまま返す
        except ClamdStreamSizeError as exc:
            # INSTREAMの送信上限（clamd.max_stream_size_mb）超過。一括スキャン中に毎回例外で
            # 中断させないよう、ファイル単位のERROR結果として返す
            return ScanResult(
                path=str(path), status="ERROR", detail=f"{SIZE_LIMIT_DETAIL_PREFIX}（{exc.message}。{original.detail}）"
            )
        return ScanResult(path=str(path), status=result.status, detail=result.detail)

    def scan_directory(self, path: str | Path) -> list[ScanResult]:
        """ディレクトリスキャン（MULTISCAN、2.1節）

        注意: 結果のパスはclamdの応答をそのまま返す。隔離などの操作に使う場合は、パスが依頼した
        ディレクトリの配下にあり、実在する通常のファイルであることを呼び出し側で確かめること
        （現在はScanDispatcherが1ファイルずつscan_fileを呼ぶため、どこからも使っていない）。
        """
        with self._semaphore:
            return self._client.scan_path(path, multi=True)

    def scan_stream(self, file_obj: BinaryIO) -> ScanResult:
        """INSTREAM経由スキャン（3.3節、将来のリモート隔離連携等を想定）"""
        with self._semaphore:
            return self._client.scan_stream(file_obj)

    def update_database(self) -> bool:
        """定義DB再読込（RELOAD）。freshclam実行自体は別プロセス起動が必要なため
        本フェーズでは「clamdへ再読込を指示する」部分のみ実装する。
        """
        with self._semaphore:
            return self._client.reload()

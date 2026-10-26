"""
ClamdClient — clamdソケット通信の低レベルプロトコル層

参照: clamdソケット通信プロトコル設計書 1〜3章
- 接続種別（Unix/TCP）の自動判定
- z終端（NULL終端）コマンドの送受信
- PING / VERSION / SCAN / MULTISCAN / INSTREAM / FILDES / RELOAD
- 1コマンド1接続（都度接続方式、1.2節）
"""
from __future__ import annotations

import platform
import re
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from core.config_model import ClamdConfig
from core.exceptions import (
    ClamdConnectionError,
    ClamdProtocolError,
    ClamdStreamSizeError,
    ClamdTimeoutError,
)

ScanStatus = Literal["OK", "FOUND", "ERROR"]

_SCAN_LINE_RE = re.compile(r"^(?P<path>.*): (?:(?P<ok>OK)|(?P<detail>.+) (?P<status>FOUND|ERROR))$")


class FdPassUnsupportedError(Exception):
    """FILDES（ファイルディスクリプタ渡し）を使えない（clamdが非対応・UNIXソケット以外等）"""


def fdpass_available() -> bool:
    """このOSでファイルディスクリプタ渡しを使えるか（UNIXソケットのSCM_RIGHTS。Windowsは不可）"""
    return hasattr(socket, "send_fds") and hasattr(socket, "AF_UNIX")


@dataclass
class ScanResult:
    path: str
    status: ScanStatus
    detail: str = ""


class ClamdClient:
    """clamdへの1コマンド分の通信を担う低レベルクライアント

    参照: clamdソケット通信プロトコル設計書1.2節
    「コマンドごとに接続→送信→受信→切断の都度接続方式を基本とする」
    """

    def __init__(self, config: ClamdConfig) -> None:
        self.config = config
        # スレッドごとの通信中ソケット。スキャンの停止操作で、応答待ちの通信を別スレッドから打ち切るために使う
        # （clamdソケット通信プロトコル設計書4章「ユーザーによる明示キャンセルのみで中断」）
        self._active_socks: dict[int, socket.socket] = {}
        self._active_lock = threading.Lock()

    def abort_thread(self, thread_ident: int) -> bool:
        """指定スレッドが通信中のソケットを閉じ、応答待ちを打ち切る。打ち切った場合True"""
        with self._active_lock:
            sock = self._active_socks.pop(thread_ident, None)
        if sock is None:
            return False
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        return True

    def _close(self, sock: socket.socket) -> None:
        with self._active_lock:
            if self._active_socks.get(threading.get_ident()) is sock:
                del self._active_socks[threading.get_ident()]
        sock.close()

    # -- 接続 -----------------------------------------------------------
    def uses_unix_socket(self) -> bool:
        return self._resolve_connection_type() == "unix"

    @property
    def endpoint(self) -> str:
        """接続先の表示用文字列（エンジン サービス画面・診断レポート）"""
        if self.uses_unix_socket():
            return f"UNIXソケット {self.config.unix_socket_path}"
        return f"TCP {self.config.tcp_host}:{self.config.tcp_port}"

    def _resolve_connection_type(self) -> str:
        """接続方式を決定する（config.jsonスキーマ定義書1.1節）"""
        if self.config.connection_type != "auto":
            return self.config.connection_type
        return "unix" if platform.system() != "Windows" else "tcp"

    def _connect(self) -> socket.socket:
        conn_type = self._resolve_connection_type()
        try:
            if conn_type == "unix":
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self.config.connect_timeout_sec)
                sock.connect(self.config.unix_socket_path)
            else:
                sock = socket.create_connection(
                    (self.config.tcp_host, self.config.tcp_port),
                    timeout=self.config.connect_timeout_sec,
                )
            sock.settimeout(self.config.read_timeout_sec)
            with self._active_lock:
                self._active_socks[threading.get_ident()] = sock
            return sock
        except PermissionError as exc:
            raise ClamdConnectionError(
                "ClamAVサービスへのアクセス権限がありません", code="E1005", detail=str(exc)
            ) from exc
        except OSError as exc:
            raise ClamdConnectionError(detail=str(exc)) from exc

    # -- 送受信 ---------------------------------------------------------
    def _send_command(self, sock: socket.socket, command: str) -> None:
        sock.sendall(f"z{command}\0".encode("utf-8"))

    def _recv_until_close(self, sock: socket.socket) -> bytes:
        chunks: list[bytes] = []
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except socket.timeout as exc:
            raise ClamdTimeoutError(detail=str(exc)) from exc
        except OSError as exc:
            raise ClamdConnectionError(detail=str(exc)) from exc
        return b"".join(chunks)

    def _run_command(self, command: str, no_read_timeout: bool = False) -> str:
        """no_read_timeout: スキャン系コマンド用。プロトコル設計書4章「スキャン系読み取りタイムアウト:
        なし」に従い、大容量ファイルの検査中にread_timeout_sec（既定30秒）で打ち切らない"""
        sock = self._connect()
        try:
            if no_read_timeout:
                sock.settimeout(None)
            self._send_command(sock, command)
            raw = self._recv_until_close(sock)
        finally:
            self._close(sock)
        # z終端応答はNULLで区切られる。末尾の空要素は除去する。
        text = raw.decode("utf-8", errors="replace")
        return text.rstrip("\x00")

    # -- コマンド ---------------------------------------------------------
    def ping(self) -> bool:
        response = self._run_command("PING")
        return response.strip() == "PONG"

    def get_version(self) -> tuple[str, str, str]:
        """VERSION応答を (engine_version, db_version, db_date) に分解する（3.4節）"""
        response = self._run_command("VERSION").strip()
        # 例: "ClamAV 1.3.1/27200/Mon Jan  6 08:00:00 2026"
        m = re.match(r"^(?P<engine>ClamAV [\d.]+)/(?P<dbver>\d+)/(?P<dbdate>.+)$", response)
        if not m:
            raise ClamdProtocolError(detail=f"VERSION応答を解釈できません: {response!r}")
        return m.group("engine"), m.group("dbver"), m.group("dbdate")

    def reload(self) -> bool:
        response = self._run_command("RELOAD").strip()
        return response == "RELOADING"

    def scan_path(self, path: str | Path, multi: bool = False) -> list[ScanResult]:
        """SCAN / MULTISCAN（2.1節: ディレクトリはMULTISCAN、単一ファイルはSCAN）"""
        command = f"{'MULTISCAN' if multi else 'SCAN'} {path}"
        response = self._run_command(command, no_read_timeout=True)
        return self._parse_scan_response(response)

    def scan_stream(self, file_obj: BinaryIO) -> ScanResult:
        """INSTREAM（3.3節: 長さプレフィックス方式のチャンク送信）"""
        sock = self._connect()
        try:
            self._send_command(sock, "INSTREAM")
            total_sent = 0
            max_bytes = self.config.max_stream_size_mb * 1024 * 1024
            chunk_size = self.config.instream_chunk_size

            while True:
                chunk = file_obj.read(chunk_size)
                if not chunk:
                    break
                total_sent += len(chunk)
                if total_sent > max_bytes:
                    self._close(sock)
                    raise ClamdStreamSizeError(
                        detail=f"送信サイズ {total_sent} バイトが上限 {max_bytes} バイトを超過"
                    )
                sock.sendall(len(chunk).to_bytes(4, byteorder="big") + chunk)

            # ストリーム終端: 長さ0のチャンク
            sock.sendall((0).to_bytes(4, byteorder="big"))
            sock.settimeout(None)  # 検査結果の待ちはスキャン系のため無制限（プロトコル設計書4章）
            raw = self._recv_until_close(sock)
        finally:
            self._close(sock)

        text = raw.decode("utf-8", errors="replace").rstrip("\x00").strip()
        results = self._parse_scan_response(text)
        return results[0] if results else ScanResult(path="stream", status="ERROR", detail=text)

    def scan_fd(self, fd: int, display_path: str) -> ScanResult:
        """FILDES（プロトコル設計書2章: ファイルディスクリプタ渡し）

        アプリが開いたファイルのディスクリプタをUNIXソケットのSCM_RIGHTSでclamdへ渡し、clamdは
        それを直接読んで検査する。clamdの実行ユーザー（clamav）が読めないファイルもSCANの権限エラー
        →INSTREAMでの送り直し無しに1回の通信で検査でき、内容のコピーもINSTREAMの送信上限も無い。
        応答は「fd[N]: <結果>」のため、パスはdisplay_pathに読み替える。

        FdPassUnsupportedError（呼び出し側は以降FILDESを使わない）は、clamdが応答を返し、それが「fd[」で
        始まらない場合（UNKNOWN COMMAND等）だけ送出する。応答が無い場合（スキャンの停止操作による打ち切り、
        clamdの再起動・終了）はClamdConnectionErrorとする。以前は空の応答も「非対応」と判断し、
        アプリを再起動するまでFILDESを使わなくなっていた（コードレビューで修正）。
        """
        sock = self._connect()
        send_error: OSError | None = None
        try:
            self._send_command(sock, "FILDES")
            try:
                socket.send_fds(sock, [b"\0"], [fd])
            except OSError as exc:
                # 非対応のclamdは応答を返して接続を閉じるため、送信に失敗しても応答を読んでから判断する
                send_error = exc
            sock.settimeout(None)  # 検査結果の待ちはスキャン系のため無制限（プロトコル設計書4章）
            raw = self._recv_until_close(sock)
        finally:
            self._close(sock)

        text = raw.decode("utf-8", errors="replace").rstrip("\x00").strip()
        if not text:
            detail = "FILDESの応答がありません" + (f" ({send_error})" if send_error is not None else "")
            raise ClamdConnectionError(detail=detail)
        results = self._parse_scan_response(text)
        if len(results) != 1 or not results[0].path.startswith("fd["):
            raise FdPassUnsupportedError(text)  # 例: "UNKNOWN COMMAND"
        return ScanResult(path=display_path, status=results[0].status, detail=results[0].detail)

    # -- 応答パース ---------------------------------------------------------
    @staticmethod
    def _parse_scan_response(response: str) -> list[ScanResult]:
        """SCAN/MULTISCAN応答をScanResultのリストへ変換する（3.1節）

        フォーマット不正な行はE1003としてはログ側の責務に委ね、
        ここでは「不明な結果」として status=ERROR で返す。
        """
        results: list[ScanResult] = []
        for line in response.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _SCAN_LINE_RE.match(line)
            if not m:
                results.append(ScanResult(path=line, status="ERROR", detail="応答フォーマット不正"))
                continue
            if m.group("ok"):
                results.append(ScanResult(path=m.group("path"), status="OK"))
            else:
                status = m.group("status")
                results.append(
                    ScanResult(path=m.group("path"), status=status, detail=m.group("detail"))
                )
        return results


def check_connection(config: ClamdConfig) -> str:
    """設定画面の「接続テスト」: 指定した接続先へPINGし、「エンジンの版 / 定義 版」を返す

    保存前の入力値で試すため、アプリが使っているClamdClientではなく一時的なクライアントで接続する。
    接続できない・PINGに応答しない場合はClamdConnectionError等の例外を送出する。
    """
    client = ClamdClient(config)
    if not client.ping():
        raise ClamdConnectionError(detail=f"{client.endpoint} がPINGに応答しません")
    engine, db_version, _ = client.get_version()
    return f"{engine} / 定義 {db_version}"

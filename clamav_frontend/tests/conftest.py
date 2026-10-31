"""
pytest共通fixture

テスト仕様書2.2節/4章に対応。
Phase 1以降で以下のfixtureを実装していく:
  - tmp_config_dir: config.json用の一時ディレクトリ
  - tmp_db: 一時SQLiteデータベース（Alembicマイグレーション適用済み）
  - fake_clamd_server: モックclamdサーバ（ソケット応答を制御）
  - real_clamd_available: 実clamdへの疎通確認（テスト仕様書5章、CLM-I01等のスキップ判定に使用）
  - qapp: PySide6 QApplication（Phase3以降のGUIテスト用、offscreenモード）
"""
import concurrent.futures
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

# Python 3.15の遅延import対策（app.pyと同じ。PySide6より前に実体化しておく必要がある）
concurrent.futures.ThreadPoolExecutor  # noqa: B018
concurrent.futures.ProcessPoolExecutor  # noqa: B018

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: EICAR標準テスト文字列。常駐AVに削除されるためリポジトリにはファイルとして置かず、
#: eicar_file fixtureで実行時に生成する
EICAR_CONTENT = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

#: fake_clamd_serverを使うテスト用のダミー検体。応答はresponse_mapで決まるため中身は任意でよく、
#: EICARを使うとWindows Defender等に削除・ブロックされてテストが失敗する
FAKE_INFECTED_CONTENT = "dummy infected file for fake clamd\n"


@pytest.fixture(autouse=True)
def _restore_app_logger():
    """setup_logger()はアプリ共通ロガーの出力先と伝播設定を変更するため、テストごとに元へ戻す
    （caplog等、ルートロガーへの伝播を前提とするテストへ影響させない）"""
    import logging

    app_logger = logging.getLogger("clamav_frontend")
    saved = (list(app_logger.handlers), app_logger.propagate, app_logger.level)
    yield
    for handler in list(app_logger.handlers):
        if handler not in saved[0]:
            app_logger.removeHandler(handler)
            handler.close()
    app_logger.propagate, app_logger.level = saved[1], saved[2]


@pytest.fixture(autouse=True)
def _fail_on_unexpected_modal_dialog(monkeypatch):
    """想定外のモーダルダイアログでテストが無限に停止するのを防ぐ

    offscreenではダイアログを閉じる手段がないため、exec()/静的関数を即時returnに差し替え、
    表示されていたらテスト失敗にする。ダイアログを検証するテストは各自でmonkeypatchし直せばよい。
    """
    from PySide6.QtWidgets import QMessageBox

    shown: list[str] = []

    def _fake_exec(self):
        shown.append(f"{self.windowTitle()}: {self.text()} {self.informativeText()}")
        return QMessageBox.StandardButton.Ok

    def _fake_static(default):
        def _impl(parent, title, text, *args, **kwargs):
            shown.append(f"{title}: {text}")
            return default

        return _impl

    monkeypatch.setattr(QMessageBox, "exec", _fake_exec)
    monkeypatch.setattr(QMessageBox, "question", _fake_static(QMessageBox.StandardButton.No))
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, _fake_static(QMessageBox.StandardButton.Ok))

    yield

    if shown:
        pytest.fail(f"想定外のモーダルダイアログが表示されました: {shown}")


@pytest.fixture(scope="session")
def qapp():
    """GUIテスト用のQApplication（テスト仕様書6.1節手動確認の前段として自動テスト化）"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def eicar_file(tmp_path) -> Path:
    """EICARテストファイルを一時ディレクトリに生成して返す

    Windows Defender等の常駐AVに削除・ブロックされた場合はスキップする
    （tmp_pathをAVの除外対象にすれば実行される）。
    """
    dst = tmp_path / "eicar.txt"
    try:
        dst.write_text(EICAR_CONTENT)
        time.sleep(0.5)  # 常駐AVのリアルタイム検査は非同期のため少し待ってから確認する
        if dst.read_text() != EICAR_CONTENT:
            raise OSError("EICARファイルの内容が変化しました")
    except OSError as exc:
        pytest.skip(f"常駐AVによりEICARファイルがブロックされたためスキップ ({exc})")
    return dst


def _clamd_reachable(host: str = "127.0.0.1", port: int = 3310, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        pass
    # Linuxの既定はUNIXソケット接続（ClamdConfig.unix_socket_path）。TCPだけ見るとLinux実機でスキップされる
    from core.config_model import ClamdConfig

    unix_socket_path = ClamdConfig().unix_socket_path
    if hasattr(socket, "AF_UNIX") and os.path.exists(unix_socket_path):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(unix_socket_path)
                return True
        except OSError:
            return False
    return False


@pytest.fixture(scope="session")
def real_clamd_available() -> bool:
    """実clamdが利用可能かどうか（テスト仕様書5章: 利用不可ならCLM-I01等をスキップ）"""
    return _clamd_reachable()


requires_real_clamd = pytest.mark.skipif(
    not _clamd_reachable(),
    reason="実clamdに接続できないため結合テストをスキップ（テスト仕様書5章）",
)


class FakeClamdServer:
    """clamdソケット通信プロトコル設計書に準拠した最小限のモックclamdサーバ

    テスト側で response_map（コマンド文字列 -> 応答文字列）を差し替えることで、
    PING/VERSION/SCAN/MULTISCAN/INSTREAM/RELOAD の各応答パターンを模擬する。
    unix_socket_pathを指定するとUNIXソケットで待ち受け、FILDES（ファイルディスクリプタ渡し）も受け付ける。
    FILDESは受け取ったファイルの内容にFAKE_INFECTED_CONTENTが含まれればFOUND、それ以外はOKを返す
    （response_map["FILDES"]があればそれを返す）。
    """

    def __init__(self, unix_socket_path: str | None = None) -> None:
        self.unix_socket_path = unix_socket_path
        if unix_socket_path:
            self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server_sock.bind(unix_socket_path)
            self.host, self.port = "", 0
        else:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.bind(("127.0.0.1", 0))
            self.host, self.port = self._server_sock.getsockname()
        self._server_sock.listen(5)
        self.response_map: dict[str, str] = {
            "PING": "PONG",
        }
        #: 受け付けたコマンド名（FILDES/SCAN等の使い分けの確認用）
        self.commands: list[str] = []
        self.delay_sec: float = 0.0
        self._stop = False
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

    def _serve_loop(self) -> None:
        try:
            self._server_sock.settimeout(0.2)
        except OSError:
            return  # stop()と競合し、既にsocketがcloseされている場合
        while not self._stop:
            try:
                conn, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            fds: list[int] = []
            try:
                if self.unix_socket_path:
                    # 実際のclamdと同じく、コマンドと一緒に届くファイルディスクリプタも受け取る
                    data, fds, _flags, _addr = socket.recv_fds(conn, 65536, 1)
                else:
                    data = conn.recv(65536)
            except OSError:
                return
            if not data:
                return

            if self.delay_sec:
                import time

                time.sleep(self.delay_sec)

            text = data.decode("utf-8", errors="replace")
            command_line = text.split("\0")[0]
            command_name = command_line.lstrip("z").split(" ")[0]
            self.commands.append(command_name)

            if command_name == "INSTREAM":
                # INSTREAMはストリーム終端（長さ0チャンク）まで読み切ってから応答する
                self._drain_instream(conn, data)

            if command_name == "FILDES" and "FILDES" not in self.response_map:
                response = self._scan_passed_fd(conn, fds)
            else:
                response = self.response_map.get(command_name, f"UNKNOWN COMMAND: {command_name}")
            try:
                conn.sendall(response.encode("utf-8") + b"\x00")
            except OSError:
                pass

    def _scan_passed_fd(self, conn: socket.socket, fds: list[int]) -> str:
        if not fds:
            try:
                _data, fds, _flags, _addr = socket.recv_fds(conn, 1, 1)
            except OSError:
                fds = []
        if not fds:
            return "FILDES: didn't receive file descriptor. ERROR"
        fd = fds[0]
        try:
            content = b""
            while chunk := os.read(fd, 65536):
                content += chunk
        finally:
            os.close(fd)
        if FAKE_INFECTED_CONTENT.encode() in content:
            return f"fd[{fd}]: Eicar-Test-Signature FOUND"
        return f"fd[{fd}]: OK"

    def _drain_instream(self, conn: socket.socket, first_chunk: bytes) -> None:
        """"zINSTREAM\\0"以降に続くチャンクを、長さ0チャンクが来るまで読み捨てる"""
        buf = first_chunk.split(b"\0", 1)[1] if b"\0" in first_chunk else b""
        while True:
            while len(buf) < 4:
                more = conn.recv(65536)
                if not more:
                    return
                buf += more
            length = int.from_bytes(buf[:4], byteorder="big")
            buf = buf[4:]
            if length == 0:
                return
            while len(buf) < length:
                more = conn.recv(65536)
                if not more:
                    return
                buf += more
            buf = buf[length:]

    def stop(self) -> None:
        self._stop = True
        self._server_sock.close()
        self._thread.join(timeout=2)


@pytest.fixture
def fake_clamd_server():
    server = FakeClamdServer()
    yield server
    server.stop()


requires_fdpass = pytest.mark.skipif(
    not (hasattr(socket, "send_fds") and hasattr(socket, "AF_UNIX")) or sys.platform == "win32",
    reason="ファイルディスクリプタ渡し（UNIXソケット）が使えない環境",
)


@pytest.fixture
def fake_clamd_unix_server():
    """UNIXソケットで待ち受けるFakeClamdServer（FILDES対応）。ソケットのパス長の上限（108バイト）に
    収めるため、tmp_pathではなく短い一時ディレクトリに置く"""
    import shutil
    import tempfile

    sock_dir = tempfile.mkdtemp(prefix="fclamd")
    server = FakeClamdServer(unix_socket_path=os.path.join(sock_dir, "clamd.sock"))
    yield server
    server.stop()
    shutil.rmtree(sock_dir, ignore_errors=True)


def tooltip_text(tooltip: str) -> str:
    """ツールチップが画面に表示される文字（gui.plain_text.plain_tooltipでHTMLにしたものを文字に戻す）"""
    from PySide6.QtGui import QTextDocument

    document = QTextDocument()
    document.setHtml(tooltip)
    return document.toPlainText()

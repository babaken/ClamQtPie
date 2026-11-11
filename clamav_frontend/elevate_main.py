"""
昇格ヘルパー（サービス化・Web API設計書 3.2節。段階 12-4c）

トレイGUI から頼まれた管理者（admin）の操作を、管理者の権限で実行する。admin のトークンは頼んだ側に返さない。
1. 操作の一覧（JSON）を受け取る（Linux: 標準入力。Windows: --pipe で指定した名前付きパイプ。UAC で昇格した
   プロセスには標準入出力をつなげないため）
2. 一覧の操作が、すべて管理者の操作の一覧（api_client/admin_ops.py）にあることを確かめる
3. admin.token を自分で読み、接続先がサービスであることを確かめる（core/service_endpoint.py）
4. 自分の確認画面に、操作の内容を日本語で表示する（説明は受け取った操作から自分で作る。頼んだ側の説明は使わない）。
   「実行」のときだけ、順に実行する。途中で失敗したら残りは実行しない
5. 結果（応答の JSON と、どこまで実行したか）を返す

起動: Windows は ClamQtPieElevate.exe（uac_admin=True）、Linux は pkexec（polkit の auth_admin）。
確認画面を自動で通す仕組み（環境変数・引数）は作らない（頼んだ側が確認を飛ばせないように）。
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import struct
import sys
from pathlib import Path

from core.lazy_import_fix import reify_concurrent_futures

reify_concurrent_futures()

from api_client.admin_ops import Lookup, Operation, describe, parse_operations  # noqa: E402
from core.exceptions import AppError  # noqa: E402

logger = logging.getLogger("clamav_frontend")

API_PREFIX = "/api/v1"
#: 受け取る一覧の大きさの上限
MAX_REQUEST_BYTES = 1024 * 1024
#: 1 つの操作の応答を待つ時間（ヘルスチェック・clamd の起動は数十秒かかることがある）
_TIMEOUT_SEC = 300
_PIPE_NAME = re.compile(r"^clamqtpie-elevate-[0-9a-f]{32}$")
#: CreateFile の dwFlagsAndAttributes（winbase.h。pywin32 に定数が無い）
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000

EXIT_DONE, EXIT_FAILED, EXIT_CANCELLED, EXIT_INVALID = 0, 1, 2, 3


class AdminApi:
    """admin のトークンで内部 API を呼ぶ（このヘルパーの中だけ）"""

    def __init__(self, api_dir: Path, verify=None) -> None:
        from core.service_endpoint import read_endpoint, verify_service_listener

        self.port = read_endpoint(api_dir)
        if self.port is None:
            raise AppError("ClamQtPie サービスの接続先が分かりません", code="E8105")
        (verify or verify_service_listener)(self.port)  # トークンを送る前に、接続先がサービスか確かめる
        self._token = (api_dir / "admin.token").read_text(encoding="ascii").strip()

    def call(self, method: str, path: str, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=_TIMEOUT_SEC)
        try:
            headers = {"Authorization": f"Bearer {self._token}"}
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request(method, API_PREFIX + path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read(16 * 1024 * 1024)
            status = response.status
        finally:
            connection.close()
        if status >= 400:
            from api_client.client import error_from_body

            raise error_from_body(status, raw)
        return json.loads(raw) if raw else None

    def items(self, path: str) -> list[dict]:
        page = self.call("GET", f"{path}?limit=1000")
        return page.get("items", []) if isinstance(page, dict) else []

    def lookup(self, operations: list[Operation]) -> Lookup:
        """説明に使う名前を読む（読めなくても、ID で説明する）"""
        lookup = Lookup()
        paths = {op.path for op in operations}
        wanted = {
            "quarantine": any(p.startswith("/quarantine/") for p in paths),
            "excludes": any(p.startswith("/excludes/") for p in paths),
            "targets": any(p.startswith("/realtime/targets/") for p in paths),
            "schedules": any(p.startswith("/schedules/") for p in paths),
            "config": "/config" in paths,
        }
        sources = {"quarantine": "/quarantine", "excludes": "/excludes", "targets": "/realtime/targets",
                   "schedules": "/schedules"}
        for name, path in sources.items():
            if wanted[name]:
                try:
                    setattr(lookup, name, {item["id"]: item for item in self.items(path) if "id" in item})
                except (AppError, OSError, ValueError):
                    pass
        if wanted["config"]:
            try:
                lookup.config = self.call("GET", "/config") or {}
            except (AppError, OSError, ValueError):
                pass
        return lookup


def run_request(raw: bytes, api_dir: Path, confirm, verify=None) -> tuple[int, dict]:
    """受け取った一覧を確かめ、確認を求め、実行する。(終了コード, 結果) を返す

    confirm: 説明の行の一覧を受け取り、「実行」なら True を返す関数（確認画面）
    """
    try:
        operations = parse_operations(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        return EXIT_INVALID, {"status": "invalid", "error": {"code": "E8112", "message": str(exc)[:300]}}
    try:
        api = AdminApi(api_dir, verify)
    except (AppError, OSError) as exc:
        return EXIT_FAILED, {"status": "failed", "completed": 0, "results": [], "error": _error_json(exc)}
    lookup = api.lookup(operations)
    lines = [line for op in operations for line in describe(op, lookup)]
    if not confirm(lines):
        return EXIT_CANCELLED, {"status": "cancelled", "completed": 0, "results": []}
    results = []
    for op in operations:
        try:
            results.append(api.call(op.method, op.path, op.body))
        except (AppError, OSError, ValueError) as exc:
            logger.warning(f"管理者の操作を実行できませんでした: {op.method} {op.path} ({exc})")
            return EXIT_FAILED, {"status": "failed", "completed": len(results), "results": results,
                                 "error": _error_json(exc)}
    return EXIT_DONE, {"status": "done", "completed": len(results), "results": results}


def _error_json(exc: Exception) -> dict:
    if isinstance(exc, AppError):
        return {"code": exc.code, "message": exc.message, "detail": exc.detail or ""}
    return {"code": "E8112", "message": "管理者の権限で操作を実行できませんでした", "detail": str(exc)[:300]}


# -- 確認画面 ------------------------------------------------------------------------------------
def confirm_dialog(lines: list[str]) -> bool:
    from PySide6.QtWidgets import QApplication, QDialog

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        # root（Linux）・別の利用者（Windows の UAC）で動くため、利用者の画面のテーマにならない。トレイGUI と同じ
        # 暗色のテーマにそろえる（Linux 実機の確認での指摘）
        from gui.theme import apply_theme

        apply_theme(app)
    dialog = build_confirm_dialog(lines)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    del app  # 終わるまで QApplication を残す
    return accepted


def build_confirm_dialog(lines: list[str]):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout

    from core.version import APP_NAME

    dialog = QDialog()
    dialog.setWindowTitle(f"{APP_NAME}: 管理者の権限での操作の確認")
    dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    dialog.resize(640, 360)
    layout = QVBoxLayout(dialog)
    label = QLabel("次の操作を、管理者の権限で実行します。内容を確認して「実行」を押してください。\n"
                   "心当たりのない操作であれば「キャンセル」を押してください。")
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    layout.addWidget(label)
    text = QPlainTextEdit()
    text.setReadOnly(True)
    text.setPlainText("\n".join(lines))  # ファイル名などは文字のまま表示する
    layout.addWidget(text)
    buttons = QDialogButtonBox()
    run_button = buttons.addButton("実行", QDialogButtonBox.ButtonRole.AcceptRole)
    cancel_button = buttons.addButton("キャンセル", QDialogButtonBox.ButtonRole.RejectRole)
    run_button.setAutoDefault(False)
    cancel_button.setDefault(True)  # Enter だけでは実行しない
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    # 開いたときに本文の全体が選択された状態にしない（本文に入力のフォーカスが移ると全体が選択された。Linux 実機の
    # 確認での指摘）。フォーカスは「キャンセル」に置く
    text.moveCursor(QTextCursor.MoveOperation.Start)
    cancel_button.setFocus()
    dialog.cancel_button = cancel_button
    dialog.text = text
    return dialog


# -- 受け渡し ------------------------------------------------------------------------------------
def read_frame(read) -> bytes:
    """長さ（4 バイト）と中身。read(n) は n バイトまでを返す関数"""
    header = _read_exact(read, 4)
    (size,) = struct.unpack(">I", header)
    if size > MAX_REQUEST_BYTES:
        raise ValueError("受け取った内容が大きすぎます")
    return _read_exact(read, size)


def frame(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _read_exact(read, size: int) -> bytes:
    chunks, remaining = [], size
    while remaining:
        chunk = read(remaining)
        if not chunk:
            raise ValueError("受け取った内容が途中で終わっています")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def open_pipe_client(name: str):
    """頼んだトレイGUI が作った名前付きパイプにつなぐ（Windows）。

    つなぐ側（このヘルパー）は管理者の権限で動くため、パイプを作った側に自分の権限を使わせない
    （SECURITY_IDENTIFICATION。なりすまし（ImpersonateNamedPipeClient）で管理者の権限を得られないように）
    """
    import win32file

    if not _PIPE_NAME.match(name):
        raise ValueError("パイプの名前が正しくありません")
    return win32file.CreateFile(
        rf"\\.\pipe\{name}", win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None, win32file.OPEN_EXISTING,
        _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION, None)


def serve_pipe(name: str, api_dir: Path, confirm, verify=None) -> int:
    import win32file

    handle = open_pipe_client(name)
    try:
        raw = read_frame(lambda n: win32file.ReadFile(handle, n)[1])
        code, result = run_request(raw, api_dir, confirm, verify)
        win32file.WriteFile(handle, frame(json.dumps(result, ensure_ascii=False).encode("utf-8")))
        win32file.FlushFileBuffers(handle)
        return code
    finally:
        handle.Close()


def main(argv: list[str] | None = None) -> int:
    from core.service_paths import ServicePaths

    argv = sys.argv[1:] if argv is None else argv
    api_dir = ServicePaths.default().api_dir  # 頼んだ側からは場所を受け取らない（トークンを別の場所へ送らせない）
    if sys.platform == "win32":
        if len(argv) != 2 or argv[0] != "--pipe":
            return EXIT_INVALID
        try:
            return serve_pipe(argv[1], api_dir, confirm_dialog)
        except (OSError, ValueError) as exc:
            logger.warning(f"昇格ヘルパーの受け渡しに失敗しました ({exc})")
            return EXIT_INVALID
    try:
        raw = read_frame(sys.stdin.buffer.read)
    except ValueError:
        return EXIT_INVALID
    code, result = run_request(raw, api_dir, confirm_dialog)
    sys.stdout.buffer.write(frame(json.dumps(result, ensure_ascii=False).encode("utf-8")))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())

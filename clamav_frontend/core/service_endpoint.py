"""
内部 API の接続先（サービス化・Web API設計書 3.1節・9.4節。段階 12-3d）

- サービスは待ち受けたポートを api/endpoint.json（トークンと同じ権限）に書く。止まっている間も前回の値が残る
- クライアント（clamqtpie-ctl、段階 12-4 のトレイGUI）は、それを読んで接続する。ループバックのポートは
  一般の利用者でも先に待ち受けられるため、トークンを送る前に毎回、待ち受けているのがサービスかを確かめる
  （12章 22）。違えば接続をやめて E8105（ServiceUnavailableError）
  - Windows: 待ち受けているプロセスが ClamQtPie のサービスの PID であること
  - Linux: 待ち受けているソケットの所有者が root であること
"""
from __future__ import annotations

import json
import os
import platform
from pathlib import Path

from core.exceptions import ServiceUnavailableError
from core.peer_verify import tcp_listener_pids, tcp_listener_uids, windows_service_pid
from core.private_files import write_private_file
from core.service_status import WINDOWS_SERVICE_NAME

ENDPOINT_FILE = "endpoint.json"
#: endpoint.json の大きさの上限（ポート番号と PID だけのため、十分な大きさ）
_MAX_SIZE = 4096


def write_endpoint(api_dir: Path, port: int) -> None:
    write_private_file(Path(api_dir) / ENDPOINT_FILE,
                       json.dumps({"port": port, "pid": os.getpid()}).encode("ascii"))


def read_endpoint(api_dir: Path) -> int | None:
    """endpoint.json のポート番号。無い・読めない・正しくなければ None（呼び出し側は設定の api.port を使う）"""
    try:
        with open(Path(api_dir) / ENDPOINT_FILE, "rb") as f:
            data = json.loads(f.read(_MAX_SIZE + 1)[:_MAX_SIZE].decode("ascii"))
    except (OSError, ValueError):
        return None
    port = data.get("port") if isinstance(data, dict) else None
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        return port
    return None


def verify_service_listener(port: int, system: str | None = None) -> None:
    """ループバックの port で待ち受けているのが ClamQtPie のサービスかを確かめる。違えば ServiceUnavailableError"""
    system = system or platform.system()
    try:
        if system == "Windows":
            pids = tcp_listener_pids(port)
            service_pid = windows_service_pid(WINDOWS_SERVICE_NAME)
            if not pids:
                raise ServiceUnavailableError(detail=f"127.0.0.1:{port} で待ち受けているプログラムがありません")
            if service_pid is None or any(pid != service_pid for pid in pids):
                raise ServiceUnavailableError(
                    "接続先が ClamQtPie のサービスではありません",
                    detail=f"127.0.0.1:{port} で待ち受けているプロセスがサービスと一致しません")
        else:
            uids = tcp_listener_uids(port)
            if not uids:
                raise ServiceUnavailableError(detail=f"127.0.0.1:{port} で待ち受けているプログラムがありません")
            if any(uid != 0 for uid in uids):
                raise ServiceUnavailableError(
                    "接続先が ClamQtPie のサービスではありません",
                    detail=f"127.0.0.1:{port} で root 以外が待ち受けています")
    except ServiceUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - 確かめられなければ、接続しない側に倒す
        raise ServiceUnavailableError(detail=f"接続先を確かめられません ({exc})") from exc

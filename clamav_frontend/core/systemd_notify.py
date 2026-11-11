"""
systemd への状態の通知（sd_notify。サービス化・Web API設計書 9章。段階 12-3a）

unit は Type=notify。内部 API が待ち受けを始めてから READY=1 を送り、止めるときに STOPPING=1 を送る。
systemd から起動されていない（NOTIFY_SOCKET が無い）ときは何もしない。libsystemd は使わず、
NOTIFY_SOCKET の UNIX ソケット（先頭が @ なら抽象名前空間）へ 1 行を送るだけにする。
"""
from __future__ import annotations

import os
import socket


def notify(state: str) -> bool:
    """state を送る（例: "READY=1"）。送れたら True"""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
        return True
    except OSError:
        return False

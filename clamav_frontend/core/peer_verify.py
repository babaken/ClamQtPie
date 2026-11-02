"""
接続先が本物かを確かめる（サービス化・Web API設計書 3.1節・9章。段階 12-3d）

ループバックの TCP ポートは一般の利用者でも先に待ち受けられ、UNIX ソケットも置き場所によっては作り直せる。
clamd が止まっている間（起動前・定義の更新後の再起動中・異常終了後）に偽のサーバーを立てられると、
検査するファイルの中身（INSTREAM）を渡したり、偽の「検知なし」を信じたりしてしまう。そのため接続の前に、
待ち受けているのが決まったプロセス・利用者かを確かめる（12章 43）。

- Windows（TCP）: 待ち受けているプロセスの PID が、clamd のサービスの PID と一致すること
- Linux（TCP）: /proc/net/tcp・tcp6 で待ち受けているソケットの所有者が、root か clamd.conf の User であること
- Linux（UNIX ソケット）: ソケットと親のディレクトリの所有者が root か clamd.conf の User で、
  ほかの利用者が親のディレクトリに書き込めないこと

内部 API（12-3d-2）でも同じ関数を使う。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import platform
import socket
import stat
import struct
import threading
import time
from pathlib import Path

logger = logging.getLogger("clamav_frontend")

#: 確かめた結果を使い回す秒数（1ファイルごとに接続するスキャンで、毎回確かめないように）
CACHE_SEC = 5.0
_TCP_LISTEN = "0A"
#: clamd.conf に User が無いときの clamd の利用者（Debian・Ubuntu の既定）
DEFAULT_CLAMD_USER = "clamav"
DEFAULT_WINDOWS_SERVICE = "clamd"


class VerifyError(Exception):
    """確かめられなかった（理由はメッセージ）"""


def is_local_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# -- Linux -------------------------------------------------------------------------------------
def _decode_address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """/proc/net/tcp のアドレス（4 バイトごとにリトルエンディアンの 16 進）"""
    raw = bytes.fromhex(text)
    raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return ipaddress.ip_address(raw)


def _reaches_loopback(address) -> bool:
    """ループバックへの接続を受け付ける待ち受けか（ループバック・すべてのアドレス・IPv4 を写した IPv6）"""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback or address.is_unspecified


def tcp_listener_uids(port: int, proc_net: Path = Path("/proc/net")) -> list[int]:
    """ループバックの port で待ち受けているソケットの所有者の uid の一覧"""
    uids = []
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc_net / name).read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 8 or parts[3] != _TCP_LISTEN:
                continue
            address_text, _, port_text = parts[1].partition(":")
            try:
                if int(port_text, 16) != port or not _reaches_loopback(_decode_address(address_text)):
                    continue
                uids.append(int(parts[7]))
            except ValueError:
                continue
    return uids


def trusted_ids(user: str | None) -> tuple[set[int], set[int]]:
    """root と user（clamd の利用者）の uid・gid"""
    uids, gids = {0}, {0}
    try:
        import pwd

        entry = pwd.getpwnam(user or DEFAULT_CLAMD_USER)
        uids.add(entry.pw_uid)
        gids.add(entry.pw_gid)
    except (ImportError, KeyError):
        pass
    return uids, gids


def check_unix_socket(path: str, uids: set[int], gids: set[int]) -> None:
    """UNIX ソケットとその親のディレクトリが、決まった利用者のものかを確かめる。違えば VerifyError"""
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise VerifyError(f"ソケットを確かめられません ({exc})") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise VerifyError("ソケットではありません")
    if info.st_uid not in uids:
        raise VerifyError(f"ソケットの所有者が clamd の利用者ではありません (uid={info.st_uid})")
    parent = os.path.dirname(os.path.realpath(path))
    try:
        parent_info = os.stat(parent)
    except OSError as exc:
        raise VerifyError(f"ソケットのフォルダを確かめられません ({exc})") from exc
    if parent_info.st_uid not in uids:
        raise VerifyError(f"ソケットのフォルダの所有者が clamd の利用者ではありません (uid={parent_info.st_uid})")
    if parent_info.st_mode & stat.S_IWOTH or (parent_info.st_mode & stat.S_IWGRP and parent_info.st_gid not in gids):
        raise VerifyError("ソケットのフォルダに、ほかの利用者が書き込めます")


# -- Windows -----------------------------------------------------------------------------------
#: GetExtendedTcpTable の TCP_TABLE_OWNER_PID_LISTENER（待ち受けだけを、PID つきで返す）
_TCP_TABLE_OWNER_PID_LISTENER = 3
_ERROR_INSUFFICIENT_BUFFER = 122
#: MIB_TCPROW_OWNER_PID・MIB_TCP6ROW_OWNER_PID の大きさ
_ROW_SIZE = {socket.AF_INET: 24, socket.AF_INET6: 56}


def _tcp_table(family: int, table_class: int = _TCP_TABLE_OWNER_PID_LISTENER) -> bytes:
    import ctypes

    get_table = ctypes.windll.iphlpapi.GetExtendedTcpTable
    size = ctypes.c_ulong(0)
    for _ in range(5):  # 表の大きさを問い合わせている間に増えることがあるため、何回か試す
        buffer = ctypes.create_string_buffer(size.value or 1)
        result = get_table(buffer, ctypes.byref(size), False, family, table_class, 0)
        if result == 0:
            return buffer.raw
        if result != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(f"GetExtendedTcpTable が失敗しました ({result})")
    raise OSError("GetExtendedTcpTable の表を読めません")


def parse_tcp_table(raw: bytes, family: int) -> list[tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int, int]]:
    """GetExtendedTcpTable の表から（アドレス, ポート, PID）の一覧"""
    (count,) = struct.unpack_from("<I", raw, 0)
    size = _ROW_SIZE[family]
    rows = []
    for index in range(count):
        offset = 4 + index * size
        if family == socket.AF_INET:
            _state, address, port, _ra, _rp, pid = struct.unpack_from("<6I", raw, offset)
            ip = ipaddress.IPv4Address(struct.pack("<I", address))
        else:
            ip = ipaddress.IPv6Address(raw[offset:offset + 16])
            port, = struct.unpack_from("<I", raw, offset + 20)
            pid, = struct.unpack_from("<I", raw, offset + 52)
        rows.append((ip, socket.ntohs(port & 0xFFFF), pid))
    return rows


def tcp_listener_pids(port: int) -> list[int]:
    """ループバックの port で待ち受けているプロセスの PID の一覧（GetExtendedTcpTable。一般の権限で読める）"""
    pids = []
    for family in (socket.AF_INET, socket.AF_INET6):
        for address, local_port, pid in parse_tcp_table(_tcp_table(family), family):
            if local_port == port and _reaches_loopback(address):
                pids.append(pid)
    return pids


def windows_service_pid(name: str) -> int | None:
    """動いているサービスのプロセスの PID（止まっている・無ければ None）"""
    import psutil

    try:
        pid = psutil.win_service_get(name).pid()
    except Exception:  # noqa: BLE001 - サービスが無い・問い合わせられない
        return None
    return pid or None


# -- clamd -------------------------------------------------------------------------------------
class ClamdEndpointVerifier:
    """clamd に接続する前に、相手が clamd かを確かめる（結果は CACHE_SEC 秒使い回す）"""

    def __init__(self, config, clamd_user: str | None = None, system: str | None = None,
                 clock=time.monotonic) -> None:
        self.config = config
        self._clamd_user = clamd_user
        self._system = system or platform.system()
        self._clock = clock
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._error: str | None = None
        self._warned_remote = False
        #: 最後に確かめた結果（True: 確かめた、False: 確かめられない・接続先が別の PC、None: まだ）
        self.verified: bool | None = None

    def invalidate(self) -> None:
        """接続に失敗したときなど、次の接続で確かめ直す"""
        with self._lock:
            self._checked_at = None

    def check(self) -> None:
        """確かめる。違えば VerifyError（接続しない）。別の PC の clamd は確かめられないため通す"""
        with self._lock:
            now = self._clock()
            if self._checked_at is None or now - self._checked_at >= CACHE_SEC:
                self._error = self._verify()
                self._checked_at = now
            error = self._error
        if error is not None:
            raise VerifyError(error)

    def _uses_unix(self) -> bool:
        kind = self.config.connection_type
        return kind == "unix" or (kind == "auto" and self._system != "Windows")

    def _verify(self) -> str | None:
        try:
            if self._uses_unix():
                uids, gids = trusted_ids(self._clamd_user)
                check_unix_socket(self.config.unix_socket_path, uids, gids)
            elif not is_local_host(self.config.tcp_host):
                if not self._warned_remote:
                    logger.warning(f"clamd の接続先が別の PC（{self.config.tcp_host}）のため、相手を確かめずに接続します")
                    self._warned_remote = True
                self.verified = False
                return None
            elif self._system == "Windows":
                self._verify_windows_tcp()
            else:
                self._verify_linux_tcp()
        except VerifyError as exc:
            self.verified = False
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - 確かめる処理そのものの失敗も、確かめられないとして扱う
            self.verified = False
            return f"接続先を確かめられません ({exc})"
        self.verified = True
        return None

    def _verify_windows_tcp(self) -> None:
        port = self.config.tcp_port
        name = self.config.service_name or DEFAULT_WINDOWS_SERVICE
        pids = tcp_listener_pids(port)
        if not pids:
            raise VerifyError(f"127.0.0.1:{port} で待ち受けているプログラムがありません")
        service_pid = windows_service_pid(name)
        if service_pid is None:
            raise VerifyError(f"clamd のサービス（{name}）が動いていません")
        if any(pid != service_pid for pid in pids):
            raise VerifyError(f"127.0.0.1:{port} で待ち受けているのが clamd のサービス（{name}）ではありません")

    def _verify_linux_tcp(self) -> None:
        port = self.config.tcp_port
        uids = tcp_listener_uids(port)
        if not uids:
            raise VerifyError(f"127.0.0.1:{port} で待ち受けているプログラムがありません")
        trusted, _ = trusted_ids(self._clamd_user)
        if any(uid not in trusted for uid in uids):
            raise VerifyError(f"127.0.0.1:{port} で待ち受けているのが clamd の利用者ではありません")

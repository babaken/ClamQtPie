"""
内部 API に接続してきた利用者を割り出す（サービス化・Web API設計書 3.2節・12章 R-9。段階 12-3d）

サービス（SYSTEM・root）は全員のファイルを読めるため、user のトークンで任意の場所をスキャンできると、
ほかの利用者のフォルダのファイル名と検知の有無が分かってしまう。そこで user のトークンでのスキャンは、
頼んだ利用者のプロファイル・ホームの中だけに限る。接続はループバックの TCP なので、接続元のポートから
相手のソケットを OS の表で探し、その持ち主を求める。

- Windows: GetExtendedTcpTable（接続中・PID つき）で接続元のプロセスを求め、そのプロセスの利用者（SID）の
  プロファイルのフォルダ（ProfileList の ProfileImagePath）
- Linux: /proc/net/tcp・tcp6 で接続元のソケットの所有者の uid を求め、そのホーム
"""
from __future__ import annotations

import ipaddress
import os
import platform
import socket
import struct
from pathlib import Path

from core.peer_verify import _decode_address, _tcp_table

#: GetExtendedTcpTable の TCP_TABLE_OWNER_PID_CONNECTIONS（接続中のものを、PID つきで返す）
_TCP_TABLE_OWNER_PID_CONNECTIONS = 4
_TCP_ESTABLISHED_LINUX = "01"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROFILE_LIST = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"


def _same(a, b) -> bool:
    """アドレスの比較（IPv4 を写した IPv6 も同じとみなす）"""
    a, b = ipaddress.ip_address(a), ipaddress.ip_address(b)
    a = getattr(a, "ipv4_mapped", None) or a
    b = getattr(b, "ipv4_mapped", None) or b
    return a == b


# -- Linux -------------------------------------------------------------------------------------
def linux_connection_uid(client: tuple[str, int], server: tuple[str, int],
                         proc_net: Path = Path("/proc/net")) -> int | None:
    """接続元（client）のソケットの所有者の uid。見つからなければ None"""
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc_net / name).read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 8 or parts[3] != _TCP_ESTABLISHED_LINUX:
                continue
            try:
                local_addr, _, local_port = parts[1].partition(":")
                remote_addr, _, remote_port = parts[2].partition(":")
                if (int(local_port, 16) == client[1] and int(remote_port, 16) == server[1]
                        and _same(_decode_address(local_addr), client[0])
                        and _same(_decode_address(remote_addr), server[0])):
                    return int(parts[7])
            except ValueError:
                continue
    return None


def linux_home(uid: int) -> str | None:
    import pwd

    try:
        return pwd.getpwuid(uid).pw_dir or None
    except KeyError:
        return None


# -- Windows -----------------------------------------------------------------------------------
def parse_connection_table(raw: bytes, family: int) -> list[tuple]:
    """GetExtendedTcpTable（接続中）の表から（ローカルのアドレス, ポート, 相手のアドレス, ポート, PID）の一覧"""
    (count,) = struct.unpack_from("<I", raw, 0)
    rows = []
    for index in range(count):
        if family == socket.AF_INET:
            offset = 4 + index * 24
            _state, laddr, lport, raddr, rport, pid = struct.unpack_from("<6I", raw, offset)
            local = ipaddress.IPv4Address(struct.pack("<I", laddr))
            remote = ipaddress.IPv4Address(struct.pack("<I", raddr))
        else:
            offset = 4 + index * 56
            local = ipaddress.IPv6Address(raw[offset:offset + 16])
            (lport,) = struct.unpack_from("<I", raw, offset + 20)
            remote = ipaddress.IPv6Address(raw[offset + 24:offset + 40])
            (rport,) = struct.unpack_from("<I", raw, offset + 44)
            (pid,) = struct.unpack_from("<I", raw, offset + 52)
        rows.append((local, socket.ntohs(lport & 0xFFFF), remote, socket.ntohs(rport & 0xFFFF), pid))
    return rows


def windows_connection_pid(client: tuple[str, int], server: tuple[str, int]) -> int | None:
    family = socket.AF_INET6 if ":" in client[0] else socket.AF_INET
    for local, lport, remote, rport, pid in parse_connection_table(
            _tcp_table(family, _TCP_TABLE_OWNER_PID_CONNECTIONS), family):
        if lport == client[1] and rport == server[1] and _same(local, client[0]) and _same(remote, server[0]):
            return pid
    return None


def windows_profile_of_pid(pid: int) -> str | None:
    """プロセスの利用者のプロファイルのフォルダ"""
    import winreg

    import win32api
    import win32security

    process = win32api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        token = win32security.OpenProcessToken(process, win32security.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
    finally:
        process.Close()
    sid_text = win32security.ConvertSidToStringSid(sid)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{_PROFILE_LIST}\\{sid_text}") as key:
            value, _ = winreg.QueryValueEx(key, "ProfileImagePath")
    except OSError:
        return None
    return os.path.expandvars(value) or None


# -- 共通 --------------------------------------------------------------------------------------
def requester_home(client: tuple[str, int], server: tuple[str, int], system: str | None = None) -> str | None:
    """接続元の利用者のプロファイル・ホーム。割り出せなければ None（呼び出し側は、場所を限る操作を断る）"""
    system = system or platform.system()
    try:
        if system == "Windows":
            pid = windows_connection_pid(client, server)
            return windows_profile_of_pid(pid) if pid else None
        uid = linux_connection_uid(client, server)
        return linux_home(uid) if uid is not None else None
    except Exception:  # noqa: BLE001 - 割り出せなければ断る側に倒す
        return None


def is_inside(path: str, folder: str) -> bool:
    """path（リンクをたどった実際の場所）が folder の中か。リンクで外を指していれば中ではない"""
    real = os.path.normcase(os.path.realpath(path))
    base = os.path.normcase(os.path.realpath(folder))
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)

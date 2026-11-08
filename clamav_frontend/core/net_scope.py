"""
このPC が直接つながっている LAN の範囲（サービス化・Web API設計書 3.1節・3.5節。段階 12-5e）

ローカル CA の Name Constraints・サーバー証明書の SAN（12-5e1）と、LAN からの接続の接続元の検査・待ち受けの
アドレス（12-5e2）で使う。
- 各ネットワークインターフェースの IPv4 のアドレスとネットマスクから、直接つながっているサブネットを求める
- プライベートアドレス（10.0.0.0/8・172.16.0.0/12・192.168.0.0/16）の範囲のものだけを使う。リンクローカル
  （169.254.0.0/16）・ループバックは除く
- 仮想ブリッジ（Docker・libvirt・WSL・Hyper-V の既定のスイッチなど）と VPN のインターフェースは除く
  （このPC の中や遠くのネットワークで、目の前の LAN ではないため）
- IPv6 は扱わない（v0.10.0 の LAN からの接続は IPv4 だけ。12-5e の dig Q7）
"""
from __future__ import annotations

import ipaddress
import re
import socket
import sys
from dataclasses import dataclass

#: LAN として扱うプライベートアドレスの範囲
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
#: ループバック（証明書の SAN・Name Constraints に入れる）
LOOPBACK_NETWORK = ipaddress.IPv4Network("127.0.0.0/8")

#: 除くインターフェースの名前（小文字で比べる。前方一致）
_VIRTUAL_PREFIXES = (
    "docker", "br-", "virbr", "veth", "vnet", "cni", "flannel", "lxcbr", "lxdbr", "podman",  # コンテナ・仮想マシンのブリッジ
    "tun", "tap", "wg", "tailscale", "zt", "utun", "ppp",  # VPN
    "vethernet (wsl", "vethernet (default switch)",  # WSL・Hyper-V の既定のスイッチ（NAT の内側）
    "virtualbox host-only", "vmware network adapter",  # 仮想マシンのホストだけのネットワーク
)
#: Windows: 除くアダプターの説明（DriverDesc。小文字で比べる。部分一致）。Windows のインターフェースの名前は
#: 「イーサネット 3」のような表示名で、仮想のものか見分けられないため
_VIRTUAL_DESCRIPTIONS = (
    "virtualbox host-only", "vmware virtual ethernet", "tap-windows", "wireguard", "wintun", "tailscale",
    "zerotier", "openvpn", "kernel debug",
)
#: ホスト名として証明書に入れられる文字（英数字とハイフン。ラベルは 1〜63 文字）
_DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


@dataclass(frozen=True)
class LocalAddress:
    interface: str
    address: ipaddress.IPv4Address
    network: ipaddress.IPv4Network


def is_virtual_interface(name: str, description: str = "") -> bool:
    lowered = name.strip().lower()
    described = description.strip().lower()
    return lowered.startswith(_VIRTUAL_PREFIXES) or any(word in described for word in _VIRTUAL_DESCRIPTIONS)


_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}"
_NETWORK_KEY = r"SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}"


def windows_descriptions() -> dict[str, str]:
    """Windows: {インターフェースの表示名: アダプターの説明}（レジストリから。読めなければ空）"""
    if sys.platform != "win32":
        return {}
    import winreg

    result = {}
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CLASS_KEY)
    except OSError:
        return result
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub) as key:
                    guid = winreg.QueryValueEx(key, "NetCfgInstanceId")[0]
                    description = winreg.QueryValueEx(key, "DriverDesc")[0]
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{_NETWORK_KEY}\{guid}\Connection") as key:
                    result[winreg.QueryValueEx(key, "Name")[0]] = description
            except OSError:
                continue
    return result


def is_lan_address(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in PRIVATE_NETWORKS) and not address.is_link_local


def _interfaces():
    """{名前: [(アドレス, ネットマスク), ...]}（起動しているインターフェースの IPv4 だけ）"""
    import psutil

    stats = psutil.net_if_stats()
    result = {}
    for name, addresses in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        result[name] = [(a.address, a.netmask) for a in addresses if a.family == socket.AF_INET and a.netmask]
    return result


def lan_addresses(interfaces=None, descriptions=None) -> list[LocalAddress]:
    """LAN として扱うこのPC のアドレスと、そのサブネット（名前・アドレスの順）。descriptions は Windows の
    アダプターの説明（省くと、interfaces も省いたときだけレジストリから読む）"""
    found = []
    if descriptions is None:
        descriptions = windows_descriptions() if interfaces is None else {}
    for name, addresses in (interfaces if interfaces is not None else _interfaces()).items():
        if is_virtual_interface(name, descriptions.get(name, "")):
            continue
        for address, netmask in addresses:
            try:
                ip = ipaddress.IPv4Address(address)
                network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
            except ValueError:
                continue
            if not is_lan_address(ip) or network.prefixlen >= 32:
                continue
            # サブネットがプライベートアドレスの範囲からはみ出すもの（ネットマスクの誤りなど）は使わない
            if not any(network.subnet_of(private) for private in PRIVATE_NETWORKS):
                continue
            found.append(LocalAddress(name, ip, network))
    return sorted(found, key=lambda a: (a.interface, int(a.address)))


def lan_networks(addresses: list[LocalAddress] | None = None) -> list[ipaddress.IPv4Network]:
    """LAN として扱うサブネット（重なりを除く）"""
    networks = sorted({a.network for a in (addresses if addresses is not None else lan_addresses())},
                      key=lambda n: (int(n.network_address), n.prefixlen))
    return [n for n in networks if not any(n != other and n.subnet_of(other) for other in networks)]


def host_names(hostname: str | None = None, fqdn: str | None = None) -> list[str]:
    """証明書に入れるこのPC の名前（小文字。localhost、短い名前、FQDN、<名前>.local・.lan・.home.arpa）。
    DNS の名前として使えない文字を含むものは入れない"""
    short = (hostname if hostname is not None else socket.gethostname()).strip().lower().rstrip(".")
    short = short.split(".", 1)[0]
    full = (fqdn if fqdn is not None else socket.getfqdn()).strip().lower().rstrip(".")
    names = ["localhost"]
    if _DNS_LABEL.match(short):
        names += [short, f"{short}.local", f"{short}.lan", f"{short}.home.arpa"]
    if full and "." in full and all(_DNS_LABEL.match(label) for label in full.split(".")) \
            and not full.endswith((".in-addr.arpa", ".ip6.arpa")):
        names.append(full)
    return list(dict.fromkeys(names))

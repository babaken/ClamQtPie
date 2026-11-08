"""
WebUI の LAN からの接続（サービス化・Web API設計書 3.1節・3.3節。段階 12-5e2。v0.10.0 では試験的）

- 許可の条件（3 つ。満たさなければ許可できない）: HTTPS（ローカル CA）・二段階認証を設定済み・パスワードが 12 文字以上。
  条件が崩れたら（HTTPS を無効にした・二段階認証を解除した・短いパスワードにした・CA の期限が切れた）許可を OFF にする
- 許可する範囲（LanScope）: このPC が直接つながっている LAN（core/net_scope.py。IPv4 のプライベートアドレスだけ、
  仮想ブリッジ・VPN を除く）。待ち受けるのはループバックと LAN の各アドレス。接続元はループバックか LAN のサブネット、
  Host はループバック・このPC の名前・LAN のアドレスのどれかとポート。自動の判定を上書きする設定は v0.10.0 では作らない
- 許可している間のセッションの有効期限は 15 分（設定の値がもっと短ければそちら）
- Windows: ファイアウォールの受信の規則（ClamQtPie WebUI。TCP の WebUI のポート、相手はローカルサブネット、
  プライベートのネットワークだけ、サービスの実行ファイルだけ）を、LAN から待ち受けるときに作り、やめたら消す。
  LAN のネットワークがパブリックなら、つながらないことを知らせる（規則は広げない）
- Linux: ファイアウォールは変えない。有効なら開け方を、無効なら注意を知らせる
"""
from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field

from core import net_scope
from core.exec_path import child_env, system_tool
from core.web_password import MIN_LENGTH_LAN

logger = logging.getLogger("clamav_frontend")

#: LAN からの接続を許可するのに要るパスワードの長さ
MIN_PASSWORD_LENGTH = MIN_LENGTH_LAN
#: 許可している間のセッションの有効期限（分）の上限
LAN_SESSION_TIMEOUT_MIN = 15
#: Windows のファイアウォールの規則の名前
FIREWALL_RULE = "ClamQtPie WebUI"

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_LOOPBACK_CLIENTS = ("127.0.0.1", "::1")


def lan_problems(api) -> list[str]:
    """LAN からの接続を許可できない理由（空なら許可できる）"""
    problems = []
    if api.web_tls == "off":
        problems.append("HTTPS が無効です（clamqtpie-ctl web tls on で有効にしてください）")
    if not api.web_totp_enabled:
        problems.append("二段階認証を設定していません（clamqtpie-ctl web totp setup で設定してください）")
    if api.web_password_hash and not api.web_password_length:
        # 長さを記録する前の版で設定したパスワード（ハッシュからは長さが分からない）
        problems.append(f"パスワードの長さを確かめられません（{MIN_PASSWORD_LENGTH} 文字以上のパスワードを "
                        "clamqtpie-ctl web set-login で設定し直してください）")
    elif api.web_password_length < MIN_PASSWORD_LENGTH:
        problems.append(f"パスワードが {MIN_PASSWORD_LENGTH} 文字より短いです"
                        "（clamqtpie-ctl web set-login で変えてください）")
    return problems


def session_timeout_min(api) -> int:
    """WebUI のセッションの有効期限（LAN からの接続を許可している間は 15 分まで）"""
    if api.web_lan:
        return min(api.session_timeout_min, LAN_SESSION_TIMEOUT_MIN)
    return api.session_timeout_min


@dataclass(frozen=True)
class LanScope:
    """LAN から待ち受ける範囲（待ち受けを始めたときに決め、変わったら待ち受けを始め直す）"""

    addresses: tuple[ipaddress.IPv4Address, ...] = ()
    networks: tuple[ipaddress.IPv4Network, ...] = ()
    names: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def current(cls) -> "LanScope":
        found = net_scope.lan_addresses()
        return cls(addresses=tuple(sorted({a.address for a in found})),
                   networks=tuple(net_scope.lan_networks(found)),
                   names=tuple(n for n in net_scope.host_names() if n != "localhost"))

    @property
    def listen_hosts(self) -> list[str]:
        """待ち受けるアドレス（ループバックと LAN の各アドレス）"""
        return ["127.0.0.1", *(str(a) for a in self.addresses)]

    def allows_client(self, client: str) -> bool:
        """接続元がループバックか LAN のサブネットか"""
        if client in _LOOPBACK_CLIENTS:
            return True
        try:
            ip = ipaddress.ip_address(client)
        except ValueError:
            return False
        return ip.version == 4 and any(ip in network for network in self.networks)

    def allowed_hosts(self, port) -> set[str]:
        """Host ヘッダに使える値（小文字。名前かアドレスとポート）"""
        names = [*_LOOPBACK_HOSTS, *self.names, *(str(a) for a in self.addresses)]
        return {f"{name}:{port}" for name in names}

    def urls(self, port) -> list[str]:
        """LAN の端末から開く URL（アドレスと名前）"""
        return [*(f"https://{a}:{port}/web/login" for a in self.addresses),
                *(f"https://{n}:{port}/web/login" for n in self.names if n.endswith(".local"))]


def is_loopback(client: str) -> bool:
    return client in _LOOPBACK_CLIENTS


# -- ファイアウォール ------------------------------------------------------------------------------
def _run(cmd: list[str], runner=subprocess.run) -> subprocess.CompletedProcess:
    kwargs = dict(capture_output=True, timeout=30, env=child_env(), check=False)
    if sys.platform == "win32":
        kwargs.update(encoding="oem", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        kwargs.update(text=True, errors="replace")
    return runner(cmd, **kwargs)


def open_firewall(port: int, program: str, runner=subprocess.run) -> str | None:
    """Windows: 受信の規則を作り直す（前の規則は消す）。作れなければ理由。Windows 以外は何もしない"""
    if sys.platform != "win32":
        return None
    close_firewall(runner)
    netsh = system_tool("netsh", "Windows")
    try:
        result = _run([netsh, "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}", "dir=in",
                       "action=allow", "protocol=TCP", f"localport={int(port)}", "remoteip=localsubnet",
                       "profile=private", f"program={program}", "enable=yes"], runner)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode != 0:
        return (result.stdout or result.stderr or "").strip()[:300] or f"終了コード {result.returncode}"
    return None


def close_firewall(runner=subprocess.run) -> None:
    """Windows: 受信の規則を消す（無くてもよい）"""
    if sys.platform != "win32":
        return
    try:
        _run([system_tool("netsh", "Windows"), "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"],
             runner)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"ファイアウォールの規則（{FIREWALL_RULE}）を消せませんでした (detail: {exc})",
                       extra={"error_code": "E8100"})


def network_notes(port: int, scope: LanScope, runner=subprocess.run) -> list[str]:
    """LAN から待ち受けるときに知らせること（Windows: パブリックのネットワーク。Linux: ファイアウォール）"""
    if not scope.addresses:
        return ["LAN のアドレスが見つかりません（LAN から待ち受けていません）"]
    if sys.platform == "win32":
        return _windows_public_notes(runner)
    return _linux_firewall_notes(port, scope, runner)


def _windows_public_notes(runner) -> list[str]:
    powershell = system_tool("powershell", "Windows")
    try:
        result = _run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                       "Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory | ConvertTo-Json"],
                      runner)
        profiles = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(profiles, dict):
        profiles = [profiles]
    public = [str(p.get("InterfaceAlias", "")) for p in profiles
              if isinstance(p, dict) and str(p.get("NetworkCategory")) in ("0", "Public")]
    if not public:
        return []
    return [f"パブリックのネットワーク（{', '.join(public)}）からはつながりません（ファイアウォールの規則はプライベートの"
            "ネットワークだけです）。つながらないときは、このネットワークをプライベートに切り替えてください（設定 → "
            "ネットワーク → プロパティ）。信頼できないネットワークでは切り替えないでください"]


def _linux_firewall_notes(port: int, scope: LanScope, runner) -> list[str]:
    for tool, args, active in (("ufw", ["status"], "Status: active"), ("firewall-cmd", ["--state"], "running")):
        path = system_tool(tool)
        try:
            result = _run([path, *args], runner)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and active in (result.stdout or ""):
            subnets = ", ".join(str(n) for n in scope.networks)
            if tool == "ufw":
                how = " / ".join(f"sudo ufw allow from {n} to any port {port} proto tcp" for n in scope.networks)
            else:
                how = (f"sudo firewall-cmd --zone=home --add-port={port}/tcp --permanent など（{subnets} を home の"
                       "ゾーンに入れてください）")
            return [f"ファイアウォール（{tool}）が有効です。LAN の端末からつなぐには、ポート {port} を開けてください: {how}"]
    return ["ファイアウォールが有効になっていないようです。WebUI はこのPC の LAN のサブネットからの接続だけを受け付けますが、"
            "ファイアウォールも有効にしておくことをおすすめします"]

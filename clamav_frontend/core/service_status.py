"""
ClamQtPie サービスが動いているか（段階 12-3a）

段階 12-3 の間は、従来のアプリ（画面＋処理）と新しいサービス（処理だけ）が別々にある。両方を同時に動かすと、
監視やスケジュールが二重に動くため、従来のアプリはサービスが動いていれば起動しない（段階 12-4 で画面が
サービスにつながるようになるまでの扱い）。
"""
from __future__ import annotations

import platform
import subprocess

from core.exec_path import child_env, find_system_tool
from core.version import APP_ID, APP_SLUG

#: Windows のサービス名・Linux の unit の名前
WINDOWS_SERVICE_NAME = APP_ID
SYSTEMD_UNIT = f"{APP_SLUG}.service"


def service_running(system: str | None = None, run=subprocess.run) -> bool:
    """サービスが動いていれば True。確かめられないとき（コマンドが無い等）は False"""
    system = system or platform.system()
    try:
        if system == "Windows":
            sc = find_system_tool("sc", system)
            if sc is None:
                return False
            # 出力は OS の言語の文字コード（日本語なら CP932）。状態の RUNNING は英字で出るため、バイト列のまま探す
            result = run([sc, "query", WINDOWS_SERVICE_NAME], capture_output=True, timeout=5,
                         env=child_env(None, system))
            return result.returncode == 0 and b"RUNNING" in (result.stdout or b"")
        systemctl = find_system_tool("systemctl", system)
        if systemctl is None:
            return False
        result = run([systemctl, "is-active", "--quiet", SYSTEMD_UNIT], capture_output=True, timeout=5,
                     env=child_env(None, system))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

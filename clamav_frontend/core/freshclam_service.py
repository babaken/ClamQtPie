"""
OS の定義更新サービス（Linux の clamav-freshclam）の状態と開始・停止

定義更新画面（トレイGUI・WebUI）から、clamav-freshclam を止める・動かす。
- 停止: systemctl disable --now（今止めて、PC の起動時にも起動しないようにする）。この後は ClamQtPie の自動更新が
  定義を更新する（core/update_service.py は、OS のサービスが動いている間は無人の更新を行わない）
- 開始: systemctl enable --now（今動かして、PC の起動時にも起動する）
Windows には無い（ClamAV の Windows 版は freshclam をサービスとして登録しない）。
"""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable

from core.exceptions import DefinitionUpdateError
from core.exec_path import child_env, find_system_tool

#: systemd の unit の名前（Debian・Ubuntu・Fedora・Arch Linux の ClamAV のパッケージで同じ）
FRESHCLAM_UNIT = "clamav-freshclam.service"


@dataclass(frozen=True)
class FreshclamServiceState:
    #: この OS に clamav-freshclam があるか（Windows・未導入・systemd が無いときは False。画面に出さない）
    available: bool = False
    #: 動いているか
    active: bool = False
    #: PC の起動時に自動で起動するか
    enabled: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class FreshclamServiceController:
    def __init__(self, system: str | None = None, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 find_tool: Callable[[str, str], str | None] = find_system_tool) -> None:
        """system/runner/find_tool: テスト用の差し替え口（OS 判定・systemctl の実行・コマンドの場所）"""
        self._system = system or platform.system()
        self._run = runner
        self._find_tool = find_tool

    def _systemctl(self) -> str | None:
        return self._find_tool("systemctl", self._system) if self._system == "Linux" else None

    def _query(self, systemctl: str, *args: str) -> subprocess.CompletedProcess | None:
        try:
            return self._run([systemctl, *args, FRESHCLAM_UNIT], capture_output=True, text=True, errors="replace",
                             timeout=10, env=child_env(system=self._system))
        except (OSError, subprocess.SubprocessError):
            return None

    def status(self) -> FreshclamServiceState:
        """状態を調べる。調べられないときは available=False"""
        systemctl = self._systemctl()
        if systemctl is None:
            return FreshclamServiceState()
        shown = self._query(systemctl, "show", "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState")
        if shown is None or shown.returncode != 0:
            return FreshclamServiceState()
        values = dict(line.split("=", 1) for line in (shown.stdout or "").splitlines() if "=" in line)
        if values.get("LoadState") != "loaded":
            return FreshclamServiceState()  # 未導入（not-found）・壊れた unit
        return FreshclamServiceState(
            available=True,
            active=values.get("ActiveState") in ("active", "activating", "reloading"),
            enabled=values.get("UnitFileState") in ("enabled", "enabled-runtime"),
        )

    def set_running(self, running: bool) -> FreshclamServiceState:
        """running=True: enable --now（開始し、PC の起動時にも起動する）。False: disable --now（停止し、自動で起動しない）。
        失敗は DefinitionUpdateError（E8004）"""
        systemctl = self._systemctl()
        if systemctl is None or not self.status().available:
            raise DefinitionUpdateError("OS の定義更新サービスがありません", detail=FRESHCLAM_UNIT)
        command = [systemctl, "enable" if running else "disable", "--now", FRESHCLAM_UNIT]
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            # 従来のアプリ（利用者の権限）のときだけ。pkexec も決まった場所の絶対パスで起動する
            pkexec = self._find_tool("pkexec", self._system)
            if pkexec is None:
                raise DefinitionUpdateError("OS の定義更新サービスを操作する権限がありません", detail=FRESHCLAM_UNIT)
            command = [pkexec, *command]
        try:
            completed = self._run(command, capture_output=True, text=True, errors="replace", timeout=60,
                                  env=child_env(system=self._system))
        except (OSError, subprocess.SubprocessError) as exc:
            raise DefinitionUpdateError(_action_text(running) + "できませんでした", detail=str(exc)) from exc
        if completed.returncode != 0:
            output = f"{completed.stdout or ''}{completed.stderr or ''}".strip()
            raise DefinitionUpdateError(_action_text(running) + "できませんでした",
                                        detail=f"exit {completed.returncode}: {output}"[:500])
        return self.status()


def _action_text(running: bool) -> str:
    return "OS の定義更新サービスを開始" if running else "OS の定義更新サービスを停止"


def summary_text(state: FreshclamServiceState, auto_update: bool) -> str:
    """誰が定義を更新しているかの説明（定義更新画面に出す）"""
    if state.available and state.active:
        return "定義の更新は OS のサービス（clamav-freshclam）が行っています（ClamQtPie の自動更新は行いません）"
    if auto_update:
        return "ClamQtPie が自動更新します"
    return "自動更新は OFF です（OS のサービスも止まっているため、定義は「今すぐ更新」でだけ更新されます）"

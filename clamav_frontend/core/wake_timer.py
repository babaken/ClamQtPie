"""
スケジュールのためにスリープから起こすタイマー（サービス化・Web API設計書 9.1節。段階 12-3c）

wake が有効なジョブのうち、次の実行時刻が最も早いもの1つに合わせて、OS の復帰タイマーを1つだけ設定する
（SchedulerService が設定し直す）。復帰したあとの再スリープは OS のアイドル設定に任せる。
- Windows: CreateWaitableTimer と SetWaitableTimer(fResume=True)。タイマーはこのプロセスが持つハンドルに
  結び付き、サービスが終われば消える。電源オプションの「スリープ解除タイマーの許可」が有効なときだけ働く
  （wake_timer_policy で確かめる。多くの PC でバッテリー駆動時は無効）
- Linux: /etc/systemd/system/clamqtpie-wake.timer（WakeSystem=true）と、何もしない clamqtpie-wake.service を書き、
  systemd に起こさせる。root で動くサービスだけが使う

どちらも、できなければログに残すだけで、スケジュール自体は止めない（起こせなくても、復帰後に取り返す）。
"""
from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from core.exec_path import child_env, find_system_tool
from core.version import APP_NAME, APP_SLUG

logger = logging.getLogger("clamav_frontend")

WAKE_UNIT = f"{APP_SLUG}-wake"
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
#: FILETIME（1601-01-01 からの 100 ナノ秒単位）の起点
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class WakeTimer:
    """何もしないタイマー（従来のアプリ・対応しない環境）"""

    def set(self, when: datetime) -> None:
        pass

    def clear(self) -> None:
        pass


class WindowsWakeTimer(WakeTimer):
    def __init__(self) -> None:
        import win32event

        self._win32event = win32event
        # 手動でリセットするタイマー（名前を付けない。他のプロセスから開かせない）
        self._handle = win32event.CreateWaitableTimer(None, True, None)

    def set(self, when: datetime) -> None:
        due = int((when.astimezone(timezone.utc) - _FILETIME_EPOCH).total_seconds() * 10_000_000)
        # 正の値は絶対時刻（UTC の FILETIME）。最後の True でスリープから起こす
        self._win32event.SetWaitableTimer(self._handle, due, 0, None, None, True)

    def clear(self) -> None:
        self._win32event.CancelWaitableTimer(self._handle)


class SystemdWakeTimer(WakeTimer):
    def __init__(self, unit_dir: Path = SYSTEMD_UNIT_DIR, run=subprocess.run) -> None:
        self._dir = unit_dir
        self._run = run
        self._systemctl = find_system_tool("systemctl", "Linux")
        self._true = find_system_tool("true", "Linux") or "/bin/true"

    @property
    def timer_path(self) -> Path:
        return self._dir / f"{WAKE_UNIT}.timer"

    @property
    def service_path(self) -> Path:
        return self._dir / f"{WAKE_UNIT}.service"

    def set(self, when: datetime) -> None:
        stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        _write_unit(self.service_path,
                    f"[Unit]\nDescription={APP_NAME}: スケジュールのためにスリープから起こす\n\n"
                    f"[Service]\nType=oneshot\nExecStart={self._true}\n")
        _write_unit(self.timer_path,
                    f"[Unit]\nDescription={APP_NAME}: スケジュールのためにスリープから起こす\n\n"
                    f"[Timer]\nOnCalendar={stamp}\nWakeSystem=true\nAccuracySec=1s\nPersistent=false\n")
        self._systemctl_run("daemon-reload")
        # 時刻を変えたタイマーを読み直させる
        self._systemctl_run("restart", f"{WAKE_UNIT}.timer")

    def clear(self) -> None:
        if not self.timer_path.exists() and not self.service_path.exists():
            return
        self._systemctl_run("stop", f"{WAKE_UNIT}.timer", check=False)
        for path in (self.timer_path, self.service_path):
            path.unlink(missing_ok=True)
        self._systemctl_run("daemon-reload")

    def _systemctl_run(self, *args: str, check: bool = True) -> None:
        if self._systemctl is None:
            raise OSError("systemctl が見つかりません")
        self._run([self._systemctl, *args], check=check, capture_output=True, timeout=30,
                  env=child_env(None, "Linux"))


def _write_unit(path: Path, text: str) -> None:
    """root だけが書ける場所に、決まった内容を置き換えで書く（途中まで書いたファイルを systemd に読ませない）"""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def create_wake_timer(system: str | None = None) -> WakeTimer:
    """サービスで使うタイマー。作れなければ何もしないタイマー"""
    system = system or platform.system()
    try:
        if system == "Windows":
            return WindowsWakeTimer()
        if system == "Linux" and hasattr(os, "geteuid") and os.geteuid() == 0:
            return SystemdWakeTimer()
    except Exception as exc:  # noqa: BLE001 - 起こせなくても、スケジュールは復帰後に取り返す
        logger.warning(f"スリープから起こすタイマーを使えません (detail: {exc})")
    return WakeTimer()


# -- 電源の設定（clamqtpie-ctl status） -------------------------------------------------------
#: powercfg の「スリープ解除タイマーの許可」（RTCWAKE）の値
_RTCWAKE_TEXT = {0: "無効", 1: "有効", 2: "重要なタイマーのみ"}
_HEX_LINE = re.compile(r":\s*0x([0-9a-fA-F]{8})\s*$")


def parse_rtcwake(output: str) -> tuple[int, int] | None:
    """powercfg /query の出力から（AC, DC）の値。表示の言語によらないよう、末尾の 2 つの 16 進の値を読む"""
    values = [int(m.group(1), 16) for line in output.splitlines() if (m := _HEX_LINE.search(line))]
    return (values[-2], values[-1]) if len(values) >= 2 else None


def wake_timer_policy(system: str | None = None, run=subprocess.run) -> str:
    """スリープから起こせるかの説明（1 行）"""
    from core.clamd_process import console_text_kwargs  # 日本語の Windows の powercfg は OEM コードページで出力する

    system = system or platform.system()
    if system == "Windows":
        powercfg = find_system_tool("powercfg", system)
        if powercfg is None:
            return "確認できません（powercfg が見つかりません）"
        try:
            result = run([powercfg, "/query", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"], capture_output=True,
                         timeout=10, env=child_env(None, system), **console_text_kwargs(system))
        except (OSError, subprocess.SubprocessError) as exc:
            return f"確認できません ({exc})"
        values = parse_rtcwake(result.stdout or "")
        if values is None:
            return "確認できません（電源プランの値を読めません）"
        ac, dc = (_RTCWAKE_TEXT.get(v, str(v)) for v in values)
        note = "" if values[1] == 1 else "（バッテリー駆動時は起こせません）"
        return f"スリープ解除タイマー: 電源接続時 {ac}、バッテリー駆動時 {dc}{note}"
    if system == "Linux":
        timer = SYSTEMD_UNIT_DIR / f"{WAKE_UNIT}.timer"
        if not timer.exists():
            return "スリープから起こす予定: なし"
        match = re.search(r"^OnCalendar=(.+)$", timer.read_text(encoding="utf-8", errors="replace"), re.M)
        return f"スリープから起こす予定: {match.group(1) if match else '不明'}（{WAKE_UNIT}.timer）"
    return "対応していません"

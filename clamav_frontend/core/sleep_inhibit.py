"""
ジョブの実行中にスリープを防ぐ（サービス化・Web API設計書 9.1節。段階 12-3c）

スケジュールのジョブ（スキャン・定義の更新）の途中で PC がスリープすると、スキャンが途中で止まり、復帰後に
clamd との接続が切れて失敗になる。そのため、実行中だけスリープを防ぎ、終わったら解除する（復帰した後の
再スリープは OS のアイドル設定に任せる）。
- Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)。呼んだスレッドにだけ効くため、
  ジョブを実行するスレッドで呼び、同じスレッドで解除する
- Linux: systemd-inhibit --what=sleep で、何もしないプロセス（標準入力を読み続ける cat）を動かしておき、
  終わったら標準入力を閉じて終わらせる（プロセスが終わると抑止も解ける）

どちらも、できなければ何もしない（ジョブ自体は止めない）。
"""
from __future__ import annotations

import logging
import platform
import subprocess
from contextlib import contextmanager

from core.exec_path import child_env, find_system_tool
from core.version import APP_NAME

logger = logging.getLogger("clamav_frontend")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def prevent_sleep(reason: str, system: str | None = None):
    system = system or platform.system()
    release = _windows_prevent() if system == "Windows" else _linux_prevent(reason, system) if system == "Linux" else None
    try:
        yield
    finally:
        if release is not None:
            try:
                release()
            except Exception as exc:  # noqa: BLE001 - 解除の失敗でジョブの結果を変えない
                logger.debug(f"スリープの抑止を解除できませんでした (detail: {exc})")


def _windows_prevent():
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if not kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED):
        logger.debug("スリープを抑止できませんでした（SetThreadExecutionState）")
        return None
    return lambda: kernel32.SetThreadExecutionState(_ES_CONTINUOUS)


def _linux_prevent(reason: str, system: str):
    inhibit = find_system_tool("systemd-inhibit", system)
    cat = find_system_tool("cat", system)
    if inhibit is None or cat is None:
        return None
    try:
        process = subprocess.Popen(
            [inhibit, "--what=sleep", f"--who={APP_NAME}", f"--why={reason}", "--mode=block", cat],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=child_env(None, system),
        )
    except OSError as exc:
        logger.debug(f"スリープを抑止できませんでした (detail: {exc})")
        return None

    def release() -> None:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    return release

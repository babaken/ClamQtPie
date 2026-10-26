"""
リアルタイム監視方式の選択

参照: 要件定義書2章、基本設計書（Realtime: Linux=clamonacc、Windows=watchdog制御）
- Windows: 常にwatchdog（RealtimeManager）
- Linux: config.realtime.linux_backend == "clamonacc"（既定）ならclamonacc。
         clamonacc/pkexecが無い環境ではwatchdogへフォールバックする（WARNINGログ）
"""
from __future__ import annotations

import logging
import platform
import shutil
from pathlib import Path
from typing import Callable

from core.clamav_service import ClamAVService
from core.clamonacc_manager import ClamonaccRealtimeManager, is_clamonacc_available
from core.config_model import RealtimeConfig
from core.exclude_service import ExcludeService
from core.quarantine_service import QuarantineService
from core.realtime_manager import RealtimeManager
from core.watch_target_service import WatchTargetService

logger = logging.getLogger("clamav_frontend")


def create_realtime_manager(
    clamav_service: ClamAVService,
    watch_target_service: WatchTargetService,
    exclude_service: ExcludeService,
    quarantine_service: QuarantineService,
    config: RealtimeConfig,
    app_dir: Path,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> RealtimeManager | ClamonaccRealtimeManager:
    system = system or platform.system()
    # アプリ自身が書き込むフォルダは検査対象外にする（自己検査・隔離ファイルの再検知防止。
    # watchdog方式で除外していなかったため、隔離フォルダへの移動が再検知されて隔離が無限に繰り返された）
    app_dirs = [app_dir / "logs", app_dir / "quarantine", app_dir / "clamonacc"]

    if system == "Linux" and config.linux_backend == "clamonacc":
        available, reason = is_clamonacc_available(config, which)
        if available:
            return ClamonaccRealtimeManager(
                watch_target_service,
                exclude_service,
                quarantine_service,
                config,
                work_dir=app_dir / "clamonacc",
                extra_exclude_dirs=app_dirs,
                which=which,
            )
        logger.warning(f"clamonaccを利用できないためwatchdog監視で動作します ({reason})")

    return RealtimeManager(
        clamav_service,
        watch_target_service,
        exclude_service,
        quarantine_service,
        config,
        extra_exclude_dirs=app_dirs,
    )

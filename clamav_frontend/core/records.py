"""
画面とサービスの両方で使う、記録の型（DB・APScheduler に依存しない）

トレイGUI はサービスの内部 API の応答をこの型にして画面に渡す。サービスの部品（scheduler_service など）から
取り出すと、トレイGUI が使わない SQLAlchemy・Alembic・APScheduler まで読み込み、起動が遅くメモリも増えていた
（Rust の検証の計測で見つけた。README_DEV §93）。元のモジュールからも同じ名前で使える。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 1回のスキャンで記録する、検査できなかったファイルの上限
SCAN_HISTORY_MAX_ERRORS = 1000


@dataclass
class ScheduleRecord:
    id: int
    name: str
    enabled: bool
    cron: str
    target: str
    scan_type: str
    last_run: str
    next_run: str
    #: スリープ中でも起こして実行する（段階 12-3c）
    wake: bool = False


@dataclass
class UpdateHistoryRecord:
    id: int
    executed_at: str
    result: str  # "success" | "failure" | "no_change" | "skipped"（skippedは履歴に記録しない）
    before_version: str
    after_version: str
    detail: str = ""  # 失敗・スキップの理由（DBには保存しない。画面・通知・ログ用）


@dataclass
class WatchTargetRecord:
    id: int
    path: str
    enabled: bool

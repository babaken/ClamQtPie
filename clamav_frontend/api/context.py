"""
API から呼ぶサービスの置き場（app.py が作ったものをそのまま渡す。API 側では作らない）

画面（gui/）と同じサービスを共有する。段階 12-1 の読み取りの API は、DB・ログファイルの読み取りと、
状態の参照（実行中かどうか）だけを行い、FastAPI のスレッドプールから直接呼ぶ（サービス化・Web API設計書 8章）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ApiContext:
    config_service: Any
    clamav_service: Any
    realtime_manager: Any
    scan_dispatcher: Any
    update_service: Any
    update_dispatcher: Any
    scheduler_service: Any
    quarantine_service: Any
    exclude_service: Any
    watch_target_service: Any
    log_reader_service: Any

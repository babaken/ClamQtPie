"""トレイGUI は、サービスだけが使う重い部品（DB・スケジューラ・Web API の土台）を読み込まない

トレイGUI はサービスの内部 API を呼ぶだけなので、SQLAlchemy・Alembic・APScheduler・FastAPI などは要らない。
表示用の型をサービスの部品から取り出していたため、これらまで読み込み、起動が遅くメモリも増えていた（README_DEV §93）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2]
HEAVY = ("sqlalchemy", "alembic", "apscheduler", "fastapi", "uvicorn", "starlette", "pydantic", "watchdog", "db")


def _loaded_after_import(module: str) -> list[str]:
    code = (f"import json, sys; import {module}; "
            f"print(json.dumps([m for m in {HEAVY!r} if m in sys.modules]))")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run([sys.executable, "-c", code], cwd=APP_DIR, env=env, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", ["tray_main", "elevate_main"])
def test_gui_entry_points_do_not_load_service_only_packages(module):
    assert _loaded_after_import(module) == []


def test_record_types_are_shared_with_the_service():
    # サービスの部品からも同じ型として使える（API の応答とサービスの記録が同じ型のまま）
    from core import records, scheduler_service, update_service, watch_target_service
    from core.scan_history_service import MAX_ERRORS

    assert scheduler_service.ScheduleRecord is records.ScheduleRecord
    assert update_service.UpdateHistoryRecord is records.UpdateHistoryRecord
    assert watch_target_service.WatchTargetRecord is records.WatchTargetRecord
    assert MAX_ERRORS == records.SCAN_HISTORY_MAX_ERRORS


def test_cron_trigger_still_reads_standard_weekdays():
    from datetime import datetime, timezone

    from core.cron import cron_trigger
    from core.scheduler_service import cron_trigger as from_scheduler

    assert from_scheduler is cron_trigger
    fire = cron_trigger("0 3 * * 0").get_next_fire_time(None, datetime(2026, 11, 11, tzinfo=timezone.utc))
    assert fire.weekday() == 6  # 日曜

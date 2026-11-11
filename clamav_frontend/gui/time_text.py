"""日時の表示（保存・API の日時は UTC の ISO 形式。画面ではこの PC の時刻で表示する）"""
from __future__ import annotations

from datetime import datetime


def local_time_text(value: str, seconds: bool = True) -> str:
    """ISO 形式の日時を、この PC の時刻の「YYYY-MM-DD HH:MM(:SS)」にする。読めなければそのまま"""
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or ""
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")

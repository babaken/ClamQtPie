"""
定義データベースの古さの判定

clamdのVERSION応答の定義の日付（例: "Mon Jan  6 08:00:00 2026"、Cのasctime形式）から経過日数を求める。
定義の更新が止まっていても日付が表示されるだけで気づきにくいため、設定（update.stale_days）の日数より古ければ
ホーム画面・トレイ・通知で警告する。Linuxで定義の更新をOSのサービスに任せている場合も、clamdが読み込んで
いる定義の日付で判定するため働く。
"""
from __future__ import annotations

from datetime import datetime

_MONTHS = {name: index for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
)}


def parse_clamd_db_date(text: str) -> datetime | None:
    """clamdの定義の日付を読む。読めなければNone（曜日・月名はロケールに依存しない英語の略称）"""
    parts = text.split()
    if len(parts) != 5 or parts[1] not in _MONTHS:
        return None
    try:
        hour, minute, second = (int(v) for v in parts[3].split(":"))
        return datetime(int(parts[4]), _MONTHS[parts[1]], int(parts[2]), hour, minute, second)
    except ValueError:
        return None


def definitions_age_days(db_date: str, now: datetime | None = None) -> int | None:
    """定義の日付からの経過日数（読めなければNone）"""
    parsed = parse_clamd_db_date(db_date)
    if parsed is None:
        return None
    return max(0, ((now or datetime.now()) - parsed).days)

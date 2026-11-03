"""
WebUI を自動で OFF にするまでの長さ・日時の読み取り（サービス化・Web API設計書 3.3節「時間を決めて ON にする」。段階 12-5a）

- 長さは数字＋単位（m 分、h 時間、d 日、w 週、y 年＝365 日）。組み合わせてよい（30m、23h、2d、1d12h、1y）。
  大文字・小文字は区別しない
- 範囲は 1 分〜366 日（527040 分）。範囲外・書き方の誤りは ValueError
- 日時（--until）はローカル時刻として読み、過去と 366 日より先は ValueError
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MIN_MINUTES = 1
MAX_MINUTES = 366 * 24 * 60  # 527040
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 24 * 60, "w": 7 * 24 * 60, "y": 365 * 24 * 60}
_PART = re.compile(r"(\d+)([mhdwy])")


def parse_duration_minutes(text: str) -> int:
    value = (text or "").strip().lower()
    if not value or not re.fullmatch(r"(?:\d+[mhdwy])+", value):
        raise ValueError(f"長さの書き方が正しくありません（例: 30m、8h、2d、1d12h）: {text}")
    total = sum(int(number) * _UNIT_MINUTES[unit] for number, unit in _PART.findall(value))
    if not (MIN_MINUTES <= total <= MAX_MINUTES):
        raise ValueError(f"長さは 1 分〜366 日で指定してください: {text}")
    return total


def check_off_minutes(minutes) -> int:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not (MIN_MINUTES <= minutes <= MAX_MINUTES):
        raise ValueError("長さは 1〜527040 分（366 日）で指定してください")
    return minutes


def off_at_after(minutes: int, now: datetime | None = None) -> str:
    """今から minutes 分後（UTC の ISO 8601）"""
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(minutes=check_off_minutes(minutes))).isoformat(timespec="seconds")


def parse_until(text: str, now: datetime | None = None) -> str:
    """日時（タイムゾーンが無ければローカル時刻）を UTC の ISO 8601 にする"""
    try:
        moment = datetime.fromisoformat((text or "").strip())
    except ValueError:
        raise ValueError(f"日時の書き方が正しくありません（例: 2026-11-01T18:00）: {text}") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()  # ローカル時刻として読む
    return check_off_at(moment.astimezone(timezone.utc).isoformat(timespec="seconds"), now)


def check_off_at(value: str, now: datetime | None = None) -> str:
    """OFF にする日時（ISO 8601、タイムゾーン付き）が、今より後で 366 日以内か確かめる"""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("日時の書き方が正しくありません") from None
    if moment.tzinfo is None:
        raise ValueError("日時にはタイムゾーンを付けてください")
    now = now or datetime.now(timezone.utc)
    if moment <= now:
        raise ValueError("過去の日時は指定できません")
    if moment - now > timedelta(minutes=MAX_MINUTES):
        raise ValueError("366 日より先の日時は指定できません")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_off_at(value: str) -> datetime | None:
    """保存した OFF にする日時を読む（空・壊れていれば None）"""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def local_time_text(value: str) -> str:
    """表示用（ローカル時刻）。今日なら時刻だけ"""
    moment = parse_off_at(value)
    if moment is None:
        return ""
    local = moment.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%Y-%m-%d %H:%M")

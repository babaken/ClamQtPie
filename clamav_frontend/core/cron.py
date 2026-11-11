"""
標準の cron 式から APScheduler のトリガーを作る（画面の入力の確かめとサービスのスケジュールで共有する）

APScheduler は呼んだときにだけ読み込む（トレイGUI の起動で読み込まないため。README_DEV §93）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apscheduler.triggers.cron import CronTrigger

#: 標準のcron式の曜日（0と7が日曜）→ APSchedulerの曜日名
_CRON_WEEKDAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def cron_trigger(cron: str) -> "CronTrigger":
    """標準のcron式（曜日は0と7が日曜、1が月曜…6が土曜）からCronTriggerを作る。不正な式はValueError

    APSchedulerのfrom_crontabは曜日欄の数字を0=月曜として解釈するため、そのまま渡すと1日ずれる
    （プリセット「毎週日曜 3:00」（0 3 * * 0）が毎週月曜に実行され、7は不正な値になっていた。コードレビューで発見）。
    画面の表示（schedule.pyのdescribe_cron）とDBに保存済みのcron式は標準の数え方なので、曜日欄だけ名前に変換して渡す。
    """
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(f"Cron式は「分 時 日 月 曜日」の5つの欄で指定してください: {cron!r}")
    fields[4] = _weekday_field_to_names(fields[4])
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger.from_crontab(" ".join(fields))


def _weekday_field_to_names(field: str) -> str:
    """曜日欄（例: "0"、"1-5"、"*/2"、"sat,sun"）を曜日名の並び（例: "sun"、"mon,tue,wed,thu,fri"）にする"""
    if field == "*":
        return field
    days: set[int] = set()
    for part in field.split(","):
        base, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError(f"曜日の間隔が正しくありません: {part!r}")
        if base == "*":
            start, end = 0, 6
        elif "-" in base:
            first, last = base.split("-", 1)
            start, end = _weekday_number(first), _weekday_number(last)
            if start > end:
                raise ValueError(f"曜日の範囲が正しくありません: {part!r}")
        else:
            start = _weekday_number(base)
            end = 6 if step_text else start
        days.update(day % 7 for day in range(start, end + 1, step))
    if len(days) == 7:
        return "*"
    return ",".join(_CRON_WEEKDAY_NAMES[day] for day in sorted(days))


def _weekday_number(text: str) -> int:
    value = text.strip().lower()
    if value in _CRON_WEEKDAY_NAMES:
        return _CRON_WEEKDAY_NAMES.index(value)
    if not value.isdigit() or not 0 <= int(value) <= 7:
        raise ValueError(f"曜日の指定が正しくありません: {text!r}（0〜7、または sun〜sat）")
    return int(value)

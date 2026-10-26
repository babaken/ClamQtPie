"""
LogReaderService — ログファイルの読込・フィルタ・CSV出力

参照: 画面設計書7章、エラーコード一覧・例外設計書3章（カテゴリ推定に使用）
ログフォーマット: core/logger.py の _LOG_FORMAT
  "%(asctime)s [%(levelname)s]%(error_code)s %(message)s"
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

_LOG_LINE_RE = re.compile(
    r"^(?P<datetime>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\]"
    r"(?: \[(?P<code>E\d{4})\])?(?: <(?P<category>[^>]+)>)? (?P<message>.*)$"
)

#: エラーコード帯からカテゴリを推定する（エラーコード一覧・例外設計書3章）
_CATEGORY_BY_PREFIX = {
    "E1": "スキャン",
    "E2": "リアルタイム",
    "E3": "隔離",
    "E4": "スケジュール",
    "E5": "システム",
    "E6": "システム",
    "E7": "通知",
    "E8": "システム",
    "E9": "システム",
}


@dataclass
class LogEntry:
    datetime: datetime
    level: str
    category: str
    message: str
    error_code: str = ""


#: ログ画面のカテゴリフィルタに並べる順（画面設計書7章の5区分＋エラーコード帯由来のスケジュール・通知）
LOG_CATEGORIES = ("スキャン", "リアルタイム", "更新", "隔離", "スケジュール", "通知", "システム")


def _category_for(code: str) -> str:
    if not code:
        return "システム"
    return _CATEGORY_BY_PREFIX.get(code[:2], "システム")


class LogReaderService:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path

    def clear(self) -> int:
        """ログをすべて消去する（ログ画面の「ログをクリア」）。削除した過去のファイルの数を返す"""
        from core.logger import clear_logs

        return clear_logs(self.log_path.parent)

    def read_entries(self, since: date | None = None) -> list[LogEntry]:
        """ログを古い順に読む。日付付きの過去のファイル（app.log.YYYY-MM-DD）も読み、最後に現在のapp.logを読む

        以前は現在のapp.logだけを読んでいたため、日次のローテーションの後は前日以前のログが
        ログ画面・ホームの直近アクティビティに出なかった（コードレビューで修正）。
        since: ファイル名の日付がこの日より前の過去のファイルは読まない（ホームの直近アクティビティ等）
        """
        entries: list[LogEntry] = []
        for path in self._log_files(since):
            entries.extend(self._read_file(path))
        return entries

    def _log_files(self, since: date | None) -> list[Path]:
        rotated: list[tuple[date, Path]] = []
        prefix = self.log_path.name + "."
        for path in self.log_path.parent.glob(prefix + "*"):
            try:
                day = datetime.strptime(path.name[len(prefix):], "%Y-%m-%d").date()
            except ValueError:
                continue
            # 過去のファイルには、ファイル名の日付の翌日未明の行が含まれることがあるため1日余裕を見る
            if since is None or day >= since - timedelta(days=1):
                rotated.append((day, path))
        files = [path for _, path in sorted(rotated)]
        if self.log_path.exists():
            files.append(self.log_path)
        return files

    def _read_file(self, path: Path) -> list[LogEntry]:
        entries: list[LogEntry] = []
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return entries  # 読み込みの間にログのクリア・ローテーションで消えた
        with f:
            for line in f:
                m = _LOG_LINE_RE.match(line.rstrip("\n"))
                if not m:
                    continue
                dt = datetime.strptime(m.group("datetime"), "%Y-%m-%d %H:%M:%S,%f")
                code = m.group("code") or ""
                entries.append(
                    LogEntry(
                        datetime=dt,
                        level=m.group("level"),
                        # 明示カテゴリ（logger.log_event）を優先し、無ければエラーコード帯から推定する
                        category=m.group("category") or _category_for(code),
                        message=m.group("message"),
                        error_code=code,
                    )
                )
        return entries

    def filter_entries(
        self,
        entries: list[LogEntry],
        levels: set[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        keyword: str = "",
        categories: set[str] | None = None,
    ) -> list[LogEntry]:
        result = entries
        if levels:
            result = [e for e in result if e.level in levels]
        if categories is not None:
            result = [e for e in result if e.category in categories]
        if date_from is not None:
            result = [e for e in result if e.datetime >= date_from]
        if date_to is not None:
            result = [e for e in result if e.datetime <= date_to]
        if keyword:
            result = [
                e
                for e in result
                if keyword in e.message or keyword in e.error_code or keyword in e.category
            ]
        return result

    def export_csv(self, entries: list[LogEntry], output_path: Path) -> None:
        """CSV出力（画面設計書7章、UTF-8 BOM付き）"""
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["日時", "レベル", "カテゴリ", "メッセージ"])
            for e in entries:
                writer.writerow(
                    [e.datetime.strftime("%Y-%m-%d %H:%M:%S"), e.level, e.category, e.message]
                )

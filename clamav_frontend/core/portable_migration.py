"""
旧版（ポータブル）のデータをサービスの置き場所へ移す（サービス化・Web API設計書 9.2節。段階 12-3b）

clamqtpie-ctl migrate --from DIR から呼ぶ。旧版のフォルダは一般の利用者が書き換えられる場所にあるため、
中身を信頼せずに、次のようにして SYSTEM・root のサービスへ取り込む（12章 31）。
- ファイルはリンクをたどらずに読み（core/safe_fs.open_regular_file）、リンク・通常でないファイルは移さない
- 設定は、Web API で変えられる項目（config_model.WRITABLE_FIELDS）だけを、範囲を確かめて移す。保護された項目は
  移さず、旧版の値が既定値と違えば一覧を表示する（必要なら clamqtpie-ctl config set で設定し直す）
- DB はファイルごと置き換えず、旧版の DB（の写し）から決まった表・列だけを読み、値を確かめてから移行先の DB に入れる
  （旧版の DB に余計な表・トリガーなどがあっても取り込まない）。隔離ファイルの名前（uuid）は形を確かめ、
  隔離したファイルの元の所有者・アクセス権（owner_info）は移さない（復元のときに旧版の値を当てはめない）
- 除外・監視対象・スケジュール・隔離ファイルの一覧を表示し、確認を求めてから移す（--yes で省略）
- 移したファイルは移行先のフォルダで新しく作るため、所有者・アクセス権は移行先のもの（旧版のものを引き継がない）

隔離ファイルは1件ずつ写し、途中で失敗したら写したものを消して中止する。すべて終わってから旧版の隔離ファイルを消し、
旧版のフォルダに移行済みの印（MIGRATED.txt）を置く。旧版のフォルダそのものは消さない。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core import safe_fs
from core.config_model import GUI_SECTIONS, WRITABLE_FIELDS, WRITABLE_RANGES, ConfigModel, validate_and_fix
from core.exclude_service import VALID_TYPES as VALID_EXCLUDE_TYPES
from core.quarantine_codec import ENCODING_NONE, ENCODING_XOR_V1
from core.scheduler_service import VALID_SCAN_TYPES, cron_trigger

logger = logging.getLogger("clamav_frontend")

MARKER_NAME = "MIGRATED.txt"
#: 旧版の config.json の大きさの上限（これより大きいものは設定ファイルとみなさない）
_CONFIG_SIZE_LIMIT = 1024 * 1024
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_LOG_NAME_RE = re.compile(r"app\.log(\.\d{4}-\d{2}-\d{2})?")
_CHUNK = 1024 * 1024
#: 移行先に既にあれば移行しない表（サービスを使い始めた後のデータを上書きしないため）
_USER_TABLES = ("quarantine", "schedule", "exclude", "watch_target", "scan_history")


class MigrationError(Exception):
    pass


@dataclass
class LegacyData:
    config: dict | None = None
    quarantine: list[dict] = field(default_factory=list)
    schedules: list[dict] = field(default_factory=list)
    excludes: list[dict] = field(default_factory=list)
    watch_targets: list[dict] = field(default_factory=list)
    update_history: list[dict] = field(default_factory=list)
    scan_history: list[dict] = field(default_factory=list)
    #: 移さなかったもの（理由つき）
    skipped: list[str] = field(default_factory=list)


# -- 旧版のフォルダの確認 ----------------------------------------------------------------------
def check_source(source: Path, target_data_dir: Path) -> Path:
    if not source.is_absolute():
        raise MigrationError(f"旧版のフォルダは絶対パスで指定してください: {source}")
    source = Path(os.path.normpath(source))
    if os.path.islink(source) or os.path.isjunction(source) or not source.is_dir():
        raise MigrationError(f"旧版のフォルダが見つかりません（リンクは使えません）: {source}")
    target = Path(os.path.normpath(target_data_dir))
    if _overlaps(source, target):
        raise MigrationError(f"旧版のフォルダと移行先が重なっています: {source}")
    if os.path.lexists(source / MARKER_NAME):
        raise MigrationError(f"既に移行したフォルダです（{MARKER_NAME} があります）: {source}")
    if not (os.path.lexists(source / "config.json") or os.path.lexists(source / "app.db")):
        raise MigrationError(f"旧版の ClamQtPie のフォルダではありません（config.json・app.db がありません）: {source}")
    wal = source / "app.db-wal"
    if os.path.lexists(wal) and os.lstat(wal).st_size > 0:
        raise MigrationError("旧版の ClamQtPie が動いているか、正しく終了していません。旧版を終了してから移行してください")
    return source


def _overlaps(a: Path, b: Path) -> bool:
    a_text, b_text = os.path.normcase(str(a)), os.path.normcase(str(b))
    return a_text == b_text or a_text.startswith(b_text + os.sep) or b_text.startswith(a_text + os.sep)


# -- 旧版のデータの読み込み --------------------------------------------------------------------
def read_legacy(source: Path, work_dir: Path) -> LegacyData:
    data = LegacyData()
    data.config = _read_config(source / "config.json", data.skipped)
    if os.path.lexists(source / "app.db"):
        copy = work_dir / f".migrate-{os.getpid()}.db"
        try:
            _copy_regular(source / "app.db", copy)
            _read_db(copy, data)
        finally:
            copy.unlink(missing_ok=True)
    return data


def _read_config(path: Path, skipped: list[str]) -> dict | None:
    if not os.path.lexists(path):
        return None
    try:
        with safe_fs.open_regular_file(path) as f:
            raw = f.read(_CONFIG_SIZE_LIMIT + 1)
    except OSError as exc:
        raise MigrationError(f"旧版の設定を読めませんでした: {exc}") from None
    if len(raw) > _CONFIG_SIZE_LIMIT:
        raise MigrationError("旧版の設定ファイルが大きすぎます")
    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        skipped.append("設定（config.json を読めないため、既定値のまま）")
        return None
    return config if isinstance(config, dict) else None


def _copy_regular(src: Path, dest: Path) -> None:
    """src をリンクをたどらずに読み、dest（移行先の新しいファイル）へ写す。dest は本人だけが読める"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with safe_fs.open_regular_file(src) as f_in:
        fd = os.open(dest, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as f_out:
                while chunk := f_in.read(_CHUNK):
                    f_out.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise


def _read_db(path: Path, data: LegacyData) -> None:
    try:
        # immutable: 写しなので、ロック・WAL のファイルを作らずに読む
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        raise MigrationError(f"旧版の DB を開けませんでした: {exc}") from None
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MigrationError("旧版の DB が壊れています")
        data.quarantine = _unique_uuid([r for r in _rows(conn, "quarantine") if _valid_quarantine(r, data.skipped)],
                                       data.skipped)
        data.schedules = [r for r in _rows(conn, "schedule") if _valid_schedule(r, data.skipped)]
        data.excludes = [r for r in _rows(conn, "exclude") if _valid_exclude(r, data.skipped)]
        data.watch_targets = [r for r in _rows(conn, "watch_target") if _valid_text(r.get("path"))]
        data.update_history = [r for r in _rows(conn, "update_history")
                               if r.get("result") in ("success", "failure", "no_change")]
        history = {r["id"]: {**r, "detections": [], "errors": []} for r in _rows(conn, "scan_history")
                   if isinstance(r.get("id"), int)}
        for kind, table in (("detections", "scan_history_detection"), ("errors", "scan_history_error")):
            for row in _rows(conn, table):
                parent = history.get(row.get("history_id"))
                if parent is not None:
                    parent[kind].append(row)
        data.scan_history = list(history.values())
    except sqlite3.Error as exc:
        raise MigrationError(f"旧版の DB を読めませんでした: {exc}") from None
    finally:
        conn.close()


#: 旧版の DB から読む列（無い列は None。古い版の DB には後から足した列が無い）
_COLUMNS = {
    "quarantine": ("uuid", "original_path", "virus_name", "sha256", "size", "detected_at", "restored", "deleted",
                   "encoding", "original_path_raw"),
    "schedule": ("name", "enabled", "cron", "target", "scan_type"),
    "exclude": ("type", "value", "enabled"),
    "watch_target": ("path", "enabled", "added_at"),
    "update_history": ("executed_at", "result", "before_version", "after_version"),
    "scan_history": ("id", "started_at", "finished_at", "source", "scan_type", "label", "targets", "scanned",
                     "detected", "errors", "errors_truncated", "result", "detail"),
    "scan_history_detection": ("history_id", "path", "virus_name", "action"),
    "scan_history_error": ("history_id", "path", "reason", "detail"),
}


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return []
    present = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    wanted = _COLUMNS[table]
    columns = [c for c in wanted if c in present]
    if not columns:
        return []
    select = ", ".join(f'"{c}"' for c in columns)
    rows = []
    for values in conn.execute(f'SELECT {select} FROM "{table}" ORDER BY rowid'):
        row = dict.fromkeys(wanted)
        row.update(zip(columns, values))
        if "enabled" in row:
            row["enabled"] = 1 if row["enabled"] in (None, 1, True) else 0  # 列の無い古い DB では有効
        rows.append(row)
    return rows


def _valid_text(value) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value


def _flag(value) -> int:
    return 1 if value in (1, True) else 0


def _valid_quarantine(row: dict, skipped: list[str]) -> bool:
    uuid = row.get("uuid")
    if not isinstance(uuid, str) or not _UUID_RE.fullmatch(uuid):
        skipped.append(f"隔離の記録（ファイルの名前が正しくありません）: {row.get('original_path')!r}")
        return False
    if not _valid_text(row.get("original_path")):
        skipped.append(f"隔離の記録（元の場所が正しくありません）: {uuid}")
        return False
    row["encoding"] = row.get("encoding") or ENCODING_NONE
    if row["encoding"] not in (ENCODING_NONE, ENCODING_XOR_V1):
        skipped.append(f"隔離の記録（保存の形式が分かりません）: {row['original_path']}")
        return False
    if row.get("original_path_raw") is not None and not isinstance(row["original_path_raw"], bytes):
        row["original_path_raw"] = None
    if not (isinstance(row.get("sha256"), str) and _SHA256_RE.fullmatch(row["sha256"])):
        row["sha256"] = None
    if not isinstance(row.get("size"), int) or isinstance(row.get("size"), bool) or row["size"] < 0:
        row["size"] = None
    for name in ("virus_name", "detected_at"):
        if not isinstance(row.get(name), str):
            row[name] = ""
    row["restored"], row["deleted"] = _flag(row.get("restored")), _flag(row.get("deleted"))
    return True


def _unique_uuid(rows: list[dict], skipped: list[str]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for row in rows:
        if row["uuid"] in seen:
            skipped.append(f"隔離の記録（ファイルの名前が重複しています）: {row['original_path']}")
            continue
        seen.add(row["uuid"])
        unique.append(row)
    return unique


def _valid_schedule(row: dict, skipped: list[str]) -> bool:
    if row.get("scan_type") not in VALID_SCAN_TYPES or not _valid_text(row.get("name")):
        skipped.append(f"スケジュール（種別・名前が正しくありません）: {row.get('name')!r}")
        return False
    try:
        cron_trigger(row.get("cron") if isinstance(row.get("cron"), str) else "")
    except ValueError:
        skipped.append(f"スケジュール（実行の時刻の指定が正しくありません）: {row['name']}")
        return False
    if not isinstance(row.get("target"), str) or "\x00" in row["target"]:
        row["target"] = ""
    return True


def _valid_exclude(row: dict, skipped: list[str]) -> bool:
    if row.get("type") not in VALID_EXCLUDE_TYPES or not _valid_text(row.get("value")):
        skipped.append(f"除外（種類・値が正しくありません）: {row.get('value')!r}")
        return False
    return True


# -- 設定 ------------------------------------------------------------------------------------
def build_config(current: ConfigModel, legacy: dict | None) -> tuple[ConfigModel, list[tuple[str, object]], list[str]]:
    """移行先の設定を作る。Web API で変えられる項目だけを旧版から移す。
    (新しい設定, 移さなかった保護された項目で既定値と違うもの, 移した項目) を返す"""
    config = ConfigModel.from_dict(current.to_dict())
    if not legacy:
        return config, [], []
    defaults = ConfigModel()
    changed: list[str] = []
    for key in WRITABLE_FIELDS:
        section, name = key.split(".")
        raw = legacy.get(section)
        if not isinstance(raw, dict) or name not in raw:
            continue
        value = raw[name]
        before = getattr(getattr(config, section), name)
        if not _same_type(before, value) or not _in_range(key, value):
            continue
        trial = ConfigModel.from_dict(config.to_dict())
        setattr(getattr(trial, section), name, float(value) if isinstance(before, float) else value)
        validate_and_fix(trial, lambda _message: None)
        accepted = getattr(getattr(trial, section), name)
        if accepted == value and accepted != before:
            setattr(getattr(config, section), name, accepted)
            changed.append(key)
    diffs: list[tuple[str, object]] = []
    for f in fields(defaults):
        section = f.name
        if section == "schema_version" or section in GUI_SECTIONS:
            continue
        raw = legacy.get(section)
        if not isinstance(raw, dict):
            continue
        for sub in fields(getattr(defaults, section)):
            key = f"{section}.{sub.name}"
            if key in WRITABLE_FIELDS or sub.name not in raw:
                continue
            if raw[sub.name] != getattr(getattr(defaults, section), sub.name):
                diffs.append((key, raw[sub.name]))
    return config, diffs, changed


def _same_type(current, value) -> bool:
    if isinstance(current, bool):
        return isinstance(value, bool)
    if isinstance(current, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(current, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str) and "\x00" not in value


def _in_range(key: str, value) -> bool:
    low_high = WRITABLE_RANGES.get(key)
    return low_high is None or low_high[0] <= value <= low_high[1]


# -- 移行先の確認・書き込み ----------------------------------------------------------------------
def ensure_target_empty(db) -> None:
    from sqlalchemy import text

    with db.session() as session:
        for table in _USER_TABLES:
            if session.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar():
                raise MigrationError("移行先に既にデータ（隔離・スケジュール・除外・監視対象・スキャンの履歴）があるため、"
                                     "移行しません")


def copy_quarantine_files(source: Path, quarantine_dir: Path, rows: list[dict], skipped: list[str]) -> list[str]:
    """隔離中の記録のファイルを1件ずつ写す。写した uuid を返す。途中で失敗したら写したものを消して MigrationError。
    ファイルが無い・リンクなどの記録は移さない（rows から除く）"""
    copied: list[str] = []
    keep: list[dict] = []
    try:
        for row in rows:
            if row["restored"] or row["deleted"]:
                keep.append(row)
                continue
            src = source / "quarantine" / row["uuid"]
            if not os.path.lexists(src):
                skipped.append(f"隔離ファイル（旧版のフォルダにありません）: {row['original_path']}")
                continue
            try:
                _copy_regular(src, quarantine_dir / row["uuid"])
            except safe_fs.UnsafePathError:
                skipped.append(f"隔離ファイル（通常のファイルではありません）: {row['original_path']}")
                continue
            copied.append(row["uuid"])
            keep.append(row)
    except OSError as exc:
        remove_copied(quarantine_dir, copied)
        raise MigrationError(f"隔離ファイルを写せませんでした。移したものを元に戻して中止しました: {exc}") from None
    rows[:] = keep
    return copied


def remove_copied(quarantine_dir: Path, copied: list[str]) -> None:
    for uuid in copied:
        try:
            (quarantine_dir / uuid).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"移行を中止したときに、写した隔離ファイルを消せませんでした: {uuid} ({exc})")


def insert_rows(db, data: LegacyData) -> None:
    from db.models import (Exclude, Quarantine, Schedule, ScanHistory, ScanHistoryDetection, ScanHistoryError,
                           UpdateHistory, WatchTarget)

    now = datetime.now(timezone.utc).isoformat()
    with db.session() as session:
        for row in data.quarantine:
            session.add(Quarantine(
                uuid=row["uuid"], original_path=row["original_path"], original_path_raw=row["original_path_raw"],
                virus_name=row["virus_name"], sha256=row["sha256"], size=row["size"], detected_at=row["detected_at"],
                restored=row["restored"], deleted=row["deleted"], encoding=row["encoding"], owner_info=None,
            ))
        for row in data.schedules:
            session.add(Schedule(name=row["name"], enabled=_flag(row.get("enabled")), cron=row["cron"],
                                 target=row["target"], scan_type=row["scan_type"]))
        for row in data.excludes:
            session.add(Exclude(type=row["type"], value=row["value"], enabled=_flag(row.get("enabled"))))
        for row in data.watch_targets:
            added = row.get("added_at") if isinstance(row.get("added_at"), str) else now
            session.add(WatchTarget(path=row["path"], enabled=_flag(row.get("enabled")), added_at=added))
        for row in data.update_history:
            session.add(UpdateHistory(**{k: _text_or_none(row.get(k))
                                         for k in ("executed_at", "result", "before_version", "after_version")}))
        for row in data.scan_history:
            history = ScanHistory(**{k: _int_or_zero(row.get(k)) if k in ("scanned", "detected", "errors",
                                                                             "errors_truncated") else
                                     _text_or_none(row.get(k))
                                     for k in _COLUMNS["scan_history"] if k != "id"})
            session.add(history)
            session.flush()
            for d in row["detections"]:
                session.add(ScanHistoryDetection(history_id=history.id, **{k: _text_or_none(d.get(k))
                                                                           for k in ("path", "virus_name", "action")}))
            for e in row["errors"]:
                session.add(ScanHistoryError(history_id=history.id, **{k: _text_or_none(e.get(k))
                                                                       for k in ("path", "reason", "detail")}))
        session.commit()


def _text_or_none(value):
    return value if isinstance(value, str) else None


def _int_or_zero(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def copy_logs(source: Path, log_dir: Path) -> list[str]:
    """旧版のログ（app.log・app.log.<日付>）を、移行先の過去のファイル（app.log.<日付>）として写す。
    写せなかったものの説明を返す

    今日より後の日付の名前は使わない（昨日の日付にまとめる）。サービスが今日の分を夜中に app.log.<今日> へ切り替えるとき、
    同じ名前が既にあると切り替えられなくなるため（旧版の現在のログは、旧版を終了した今日の日付になりやすい）。
    同じ名前になるものは、古い順につなげて1つのファイルにする。移行先に同じ名前が既にあれば写さない。
    """
    problems: list[str] = []
    legacy_logs = source / "logs"
    if os.path.islink(legacy_logs) or not legacy_logs.is_dir():
        return problems
    latest = date.today() - timedelta(days=1)
    groups: dict[str, list[os.DirEntry]] = {}
    entries = [e for e in os.scandir(legacy_logs) if _LOG_NAME_RE.fullmatch(e.name)]
    # 過去のファイル（日付の順）、現在のログの順につなげる
    for entry in sorted(entries, key=lambda e: (e.name == "app.log", e.name)):
        try:
            if entry.name == "app.log":
                day = date.fromtimestamp(entry.stat(follow_symlinks=False).st_mtime)  # 最後に書いた日
            else:
                day = date.fromisoformat(entry.name[len("app.log."):])
        except (OSError, ValueError, OverflowError):
            problems.append(f"ログ（日付が分かりません）: {entry.name}")
            continue
        groups.setdefault(f"app.log.{min(day, latest).isoformat()}", []).append(entry)
    for name, members in groups.items():
        names = "、".join(e.name for e in members)
        dest = log_dir / name
        if os.path.lexists(dest):
            problems.append(f"ログ（移行先に同じ名前があります）: {names}")
            continue
        try:
            _concat_regular([Path(e.path) for e in members], dest)
        except OSError as exc:
            problems.append(f"ログ: {names}（{exc}）")
    return problems


def _concat_regular(sources: list[Path], dest: Path) -> None:
    """sources をリンクをたどらずに読み、つなげて dest（移行先の新しいファイル）へ書く"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(dest, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f_out:
            for src in sources:
                with safe_fs.open_regular_file(src) as f_in:
                    while chunk := f_in.read(_CHUNK):
                        f_out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def remove_legacy_quarantine(source: Path, copied: list[str]) -> list[str]:
    problems = []
    for uuid in copied:
        path = source / "quarantine" / uuid
        try:
            if sys.platform == "win32":
                os.chmod(path, 0o600, follow_symlinks=False)  # 読み取り専用だと消せない
            os.unlink(path)  # unlink はリンクをたどらない
        except OSError as exc:
            problems.append(f"旧版の隔離ファイルを消せませんでした: {uuid}（{exc}）")
    return problems


def write_marker(source: Path, target_data_dir: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source / MARKER_NAME, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"このフォルダのデータは {datetime.now().isoformat(timespec='seconds')} に "
                f"ClamQtPie のサービス（{target_data_dir}）へ移しました。\n"
                "旧版のアプリは使わないでください。不要になったら、このフォルダは削除してかまいません。\n")


# -- 利用者ごとの GUI の設定 --------------------------------------------------------------------
def write_gui_settings(settings: dict) -> str:
    """実行した利用者の gui.json を書き出し、書き出した場所を返す。書けなければ MigrationError

    Linux で sudo から実行したときは、その利用者の権限に下げた子プロセスで書く（root で利用者のホームに書かない）。
    """
    from core.gui_config import gui_config_path, write_gui_config

    if sys.platform == "win32":
        path = gui_config_path()
        try:
            write_gui_config(path, settings)
        except FileExistsError:
            raise MigrationError(f"既にあるため書き出しませんでした: {path}") from None
        except OSError as exc:
            raise MigrationError(f"書き出せませんでした: {path}（{exc}）") from None
        return str(path)
    if os.geteuid() != 0:
        path = gui_config_path()
        try:
            write_gui_config(path, settings)
        except FileExistsError:
            raise MigrationError(f"既にあるため書き出しませんでした: {path}") from None
        except OSError as exc:
            raise MigrationError(f"書き出せませんでした: {path}（{exc}）") from None
        return str(path)
    return _write_gui_settings_as_sudo_user(settings)


def _write_gui_settings_as_sudo_user(settings: dict) -> str:
    import pwd

    from core.gui_config import gui_config_path, write_gui_config

    user = os.environ.get("SUDO_USER")
    if not user or user == "root":
        raise MigrationError("sudo から実行していないため、利用者が分かりません（GUI の設定は既定値のままになります）")
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        raise MigrationError(f"利用者が見つかりません: {user}") from None
    path = gui_config_path(home=entry.pw_dir)
    pid = os.fork()
    if pid == 0:  # 子プロセス: 利用者の権限に下げて書く
        code = 1
        try:
            os.initgroups(user, entry.pw_gid)
            os.setgid(entry.pw_gid)
            os.setuid(entry.pw_uid)
            write_gui_config(path, settings)
            code = 0
        except FileExistsError:
            code = 2
        except BaseException:  # noqa: BLE001 - 子プロセスは何があっても終了コードで知らせる
            code = 1
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    if code == 2:
        raise MigrationError(f"既にあるため書き出しませんでした: {path}")
    if code != 0:
        raise MigrationError(f"書き出せませんでした: {path}")
    return str(path)


# -- SMTP パスワード -----------------------------------------------------------------------------
def legacy_smtp_password(legacy_config: dict | None) -> str | None:
    """旧版が利用者の資格情報ストア（keyring）に保存した SMTP パスワード。Windows だけ（UAC で昇格しても、
    同じ利用者の資格情報ストアを読める）。Linux は sudo では利用者の Secret Service を読めないため None"""
    if sys.platform != "win32":
        return None
    account = "smtp_default"
    smtp = (legacy_config or {}).get("smtp")
    if isinstance(smtp, dict) and _valid_text(smtp.get("keyring_account")):
        account = smtp["keyring_account"]
    try:
        import keyring

        from core.version import APP_ID, LEGACY_APP_ID

        # 製品名を変える前の旧版（ClamAVフロントエンド）は、旧名のサービス名に保存していた
        for service_name in (APP_ID, LEGACY_APP_ID):
            password = keyring.get_password(service_name, account)
            if password is not None:
                return password
        return None
    except Exception as exc:  # noqa: BLE001 - keyring が使えない環境では移さない
        logger.warning(f"旧版の SMTP パスワードを読めませんでした (detail: {exc})")
        return None

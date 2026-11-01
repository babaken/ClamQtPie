"""
状態を変える API の処理（サービス化・Web API設計書 6章。段階 12-2）

ここの関数はメインスレッドで呼ぶ（ルートから QtInvoker で渡す。8章）。実行中かどうかの判定もメインスレッドで行い、
画面・API・スケジュールからの同時の操作が取り合わないようにする。
処理の中身は画面（gui/screens/*）の操作と同じにし、変えた後は ApiContext.notify_changed で画面を読み直させる。
"""
from __future__ import annotations

import copy
import os
from dataclasses import fields

from api.context import ApiContext
from core.config_model import ConfigModel, validate_and_fix
from core import scan_targets
from core.exceptions import (
    ApiConflictError,
    ApiNotFoundError,
    ConfigValidationError,
    JobAlreadyRunningError,
    RealtimeError,
)
from core.logger import (
    CATEGORY_QUARANTINE,
    CATEGORY_REALTIME,
    CATEGORY_SCAN,
    CATEGORY_SYSTEM,
    CATEGORY_UPDATE,
    log_event,
)

#: 変更した対象の名前（ApiContext.notify_changed に渡す）
CHANGED_EXCLUDES = "excludes"
CHANGED_TARGETS = "targets"
CHANGED_QUARANTINE = "quarantine"
CHANGED_SCHEDULES = "schedules"
CHANGED_CONFIG = "config"
CHANGED_REALTIME = "realtime"
CHANGED_LOGS = "logs"
CHANGED_CLAMD = "clamd"
#: 画面以外（Web API）からスキャン・定義の更新を始めた（画面を「実行中」の表示にする）
CHANGED_SCAN_STARTED = "scan_started"
CHANGED_UPDATE_STARTED = "update_started"


def _notify(ctx: ApiContext, what: str) -> None:
    if ctx.notify_changed is not None:
        ctx.notify_changed(what)


def audit(category: str, message: str) -> None:
    """Web API で変えた内容をログに残す（誰がいつ何を変えたかを後から追えるように。値の制御文字はロガーが置き換える）"""
    log_event(category, f"Web API: {message}")


def _state(enabled: bool) -> str:
    return "有効" if enabled else "無効"


# -- 6.3 隔離 -------------------------------------------------------------------------
def restore_quarantine(ctx: ApiContext, quarantine_id: int, overwrite: bool) -> None:
    """元の場所にだけ復元する（12章 25。復元先を指定させない）"""
    row = ctx.quarantine_service.get(quarantine_id)
    if row.restored or row.deleted:
        raise ValueError("既に復元または削除されています")
    overwrite_text = "する" if overwrite else "しない"
    audit(CATEGORY_QUARANTINE, f"隔離ファイルの復元を要求しました: id={quarantine_id}（上書き: {overwrite_text}）")
    ctx.quarantine_service.restore(quarantine_id, dest_path=None, overwrite=overwrite)
    _notify(ctx, CHANGED_QUARANTINE)


def delete_quarantine(ctx: ApiContext, quarantine_id: int) -> None:
    row = ctx.quarantine_service.get(quarantine_id)
    if row.restored or row.deleted:
        raise ValueError("既に復元または削除されています")
    audit(CATEGORY_QUARANTINE, f"隔離ファイルの完全削除を要求しました: id={quarantine_id}")
    ctx.quarantine_service.delete_permanently(quarantine_id)
    _notify(ctx, CHANGED_QUARANTINE)


# -- 6.4 除外 -------------------------------------------------------------------------
def _normalize_exclude_value(type_: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("値が空です")
    if type_ in ("file", "folder") and not os.path.isabs(value):
        raise ValueError("ファイル・フォルダは絶対パスで指定してください")
    if type_ == "extension":
        # 画面の入力と同じく、小文字にして先頭に . を付ける
        value = value.lower()
        value = value if value.startswith(".") else f".{value}"
    return value


def _find_exclude(ctx: ApiContext, exclude_id: int):
    rule = next((r for r in ctx.exclude_service.list_all() if r.id == exclude_id), None)
    if rule is None:
        raise ApiNotFoundError()
    return rule


def _after_exclude_changed(ctx: ApiContext, type_: str) -> None:
    # 監視中なら反映する（watchdog はイベントごとに照合、clamonacc はフォルダの除外の反映に再起動が要る）
    manager = ctx.realtime_manager
    if manager.is_running and hasattr(manager, "on_excludes_changed"):
        manager.on_excludes_changed(type_)
    _notify(ctx, CHANGED_EXCLUDES)


def add_exclude(ctx: ApiContext, type_: str, value: str, enabled: bool):
    value = _normalize_exclude_value(type_, value)
    if any(r.type == type_ and r.value == value for r in ctx.exclude_service.list_all()):
        raise ValueError("同じ除外が既にあります")
    rule = ctx.exclude_service.add(type_, value, enabled)  # 種類が正しくなければ ValueError
    audit(CATEGORY_REALTIME, f"除外を追加しました: {type_} {value}（{_state(enabled)}）")
    _after_exclude_changed(ctx, type_)
    return rule


def set_exclude_enabled(ctx: ApiContext, exclude_id: int, enabled: bool) -> None:
    rule = _find_exclude(ctx, exclude_id)
    ctx.exclude_service.set_enabled(exclude_id, enabled)
    audit(CATEGORY_REALTIME, f"除外を{_state(enabled)}にしました: {rule.type} {rule.value}")
    _after_exclude_changed(ctx, rule.type)


def remove_exclude(ctx: ApiContext, exclude_id: int) -> None:
    rule = _find_exclude(ctx, exclude_id)
    ctx.exclude_service.remove(exclude_id)
    audit(CATEGORY_REALTIME, f"除外を削除しました: {rule.type} {rule.value}")
    _after_exclude_changed(ctx, rule.type)


# -- 6.5 リアルタイム監視 -------------------------------------------------------------
def set_realtime(ctx: ApiContext, enabled: bool) -> bool:
    manager = ctx.realtime_manager
    audit(CATEGORY_REALTIME, f"リアルタイム監視の{'開始' if enabled else '停止'}を要求しました")
    if enabled:
        if not manager.is_running and not manager.start():
            if not ctx.watch_target_service.list_enabled_paths():
                raise ValueError("有効な監視対象がありません")
            raise RealtimeError(detail="リアルタイム監視を開始できませんでした")
    elif manager.is_running:
        manager.stop()
    _notify(ctx, CHANGED_REALTIME)
    return manager.is_running


def _find_target(ctx: ApiContext, target_id: int):
    record = next((r for r in ctx.watch_target_service.list_all() if r.id == target_id), None)
    if record is None:
        raise ApiNotFoundError()
    return record


def add_watch_target(ctx: ApiContext, path: str):
    path = path.strip()
    if not os.path.isabs(path) or not os.path.isdir(path):
        raise ValueError("存在するフォルダを絶対パスで指定してください")
    if any(r.path == path for r in ctx.watch_target_service.list_all()):
        raise ValueError("同じ監視対象が既にあります")
    record = ctx.watch_target_service.add(path)
    audit(CATEGORY_REALTIME, f"監視対象を追加しました: {path}")
    if ctx.realtime_manager.is_running:
        ctx.realtime_manager.add_target(path)
    _notify(ctx, CHANGED_TARGETS)
    return record


def set_watch_target_enabled(ctx: ApiContext, target_id: int, enabled: bool) -> None:
    record = _find_target(ctx, target_id)
    ctx.watch_target_service.set_enabled(target_id, enabled)
    audit(CATEGORY_REALTIME, f"監視対象を{_state(enabled)}にしました: {record.path}")
    # 監視中は差分だけ反映する（watchdog: そのパスだけ / clamonacc: 再起動）
    if ctx.realtime_manager.is_running and enabled != record.enabled:
        if enabled:
            ctx.realtime_manager.add_target(record.path)
        else:
            ctx.realtime_manager.remove_target(record.path)
    _notify(ctx, CHANGED_TARGETS)


def remove_watch_target(ctx: ApiContext, target_id: int) -> None:
    record = _find_target(ctx, target_id)
    ctx.watch_target_service.remove(target_id)
    audit(CATEGORY_REALTIME, f"監視対象を削除しました: {record.path}")
    if ctx.realtime_manager.is_running:
        ctx.realtime_manager.remove_target(record.path)
    _notify(ctx, CHANGED_TARGETS)


# -- 6.6 スケジュール -----------------------------------------------------------------
def _ensure_schedule(ctx: ApiContext, job_id: int) -> None:
    if not any(r.id == job_id for r in ctx.scheduler_service.list_all()):
        raise ApiNotFoundError()


def _check_schedule_target(scan_type: str, target: str) -> None:
    # スキャンの対象は絶対パスで指定させる（相対パスだと、アプリの作業フォルダからの位置として扱われる）
    if scan_type == "scan" and not os.path.isabs(target):
        raise ValueError("スキャンの対象は絶対パスで指定してください")


def add_schedule(ctx: ApiContext, name: str, scan_type: str, cron: str, target: str):
    _check_schedule_target(scan_type, target)
    record = ctx.scheduler_service.add_job(name, scan_type, cron, target)  # 内容が正しくなければ E4001・E4002
    audit(CATEGORY_SCAN, f"スケジュールを追加しました: id={record.id} {name}（{scan_type}、{cron}、{target}）")
    _notify(ctx, CHANGED_SCHEDULES)
    return record


def update_schedule(ctx: ApiContext, job_id: int, name: str, scan_type: str, cron: str, target: str):
    _ensure_schedule(ctx, job_id)
    _check_schedule_target(scan_type, target)
    record = ctx.scheduler_service.update_job(job_id, name, scan_type, cron, target)
    audit(CATEGORY_SCAN, f"スケジュールを変更しました: id={job_id} {name}（{scan_type}、{cron}、{target}）")
    _notify(ctx, CHANGED_SCHEDULES)
    return record


def set_schedule_enabled(ctx: ApiContext, job_id: int, enabled: bool) -> None:
    _ensure_schedule(ctx, job_id)
    ctx.scheduler_service.set_enabled(job_id, enabled)
    audit(CATEGORY_SCAN, f"スケジュールを{_state(enabled)}にしました: id={job_id}")
    _notify(ctx, CHANGED_SCHEDULES)


def remove_schedule(ctx: ApiContext, job_id: int) -> None:
    _ensure_schedule(ctx, job_id)
    ctx.scheduler_service.remove_job(job_id)
    audit(CATEGORY_SCAN, f"スケジュールを削除しました: id={job_id}")
    _notify(ctx, CHANGED_SCHEDULES)


def check_schedule_runnable(ctx: ApiContext, job_id: int) -> None:
    """「今すぐ実行」の前の確認（ジョブがあり、実行中でないこと）。実行そのものは別のスレッドで行う"""
    _ensure_schedule(ctx, job_id)
    if ctx.scheduler_service.is_job_running(job_id):
        raise JobAlreadyRunningError()
    audit(CATEGORY_SCAN, f"スケジュールの今すぐ実行を要求しました: id={job_id}")


# -- 6.8 設定 -------------------------------------------------------------------------
#: Web API で変えられる項目。設定画面で変えられるもの（と、定義の古さの日数）に限る。それ以外は読み取りだけ。
#: 次のものは特に、Web API から変えられると権限を越えた操作に使えるため含めない（12章 25）:
#: - 実行ファイル・設定ファイルの場所（update.freshclam_path、realtime.clamonacc_path・clamonacc_config_file）:
#:   任意のプログラムを動かせる
#: - clamd の接続先（clamd.connection_type・tcp_host・tcp_port・unix_socket_path）: 偽の clamd に検査するファイルの中身を
#:   送らせ、常に「検知なし」と返させられる（ポートだけでも、このPC の一般の利用者が待ち受けたポートに向けられる）
#: - clamd.service_name: 任意のサービスを起動・停止させられる
#: - general（自動起動・最小化など）: 画面ごとの設定。段階 12-3 で利用者ごとの GUI の設定に移す
#: 変えるときは設定ファイルを直接直す（段階 12-3 からは clamqtpie-ctl config set）
WRITABLE_FIELDS = (
    "clamd.auto_start",
    "logging.level",
    "logging.retention_days",
    "notification.on_detect_desktop",
    "notification.on_detect_mail",
    "notification.on_job_fail",
    "notification.on_update_fail",
    "realtime.default_action",
    "realtime.default_enabled",
    "smtp.encryption",
    "smtp.from_address",
    "smtp.host",
    "smtp.port",
    "smtp.to_address",
    "smtp.user",
    "update.auto_update",
    "update.interval_hours",
    "update.stale_days",
)
#: 読み込み時の検証（validate_and_fix）に無い範囲（設定画面の入力欄と同じ）
_RANGES = {"update.interval_hours": (1, 24)}
#: GET /config に含めず、PUT /config でも受け付けない部分（API 自身の設定）
HIDDEN_SECTIONS = ("api",)
#: 変えたら保存している SMTP のパスワードを消す項目（送信先を差し替えてパスワードを送らせないため）
SMTP_CREDENTIAL_FIELDS = ("smtp.host", "smtp.port", "smtp.user", "smtp.encryption")

_SECTIONS = tuple(f.name for f in fields(ConfigModel) if f.name != "schema_version")


def _readonly_fields() -> list[str]:
    model = ConfigModel()
    return [
        f"{section}.{f.name}"
        for section in _SECTIONS if section not in HIDDEN_SECTIONS
        for f in fields(getattr(model, section))
        if f"{section}.{f.name}" not in WRITABLE_FIELDS
    ]


def config_view(ctx: ApiContext) -> dict:
    """GET /config の内容（API の設定を除き、SMTP のパスワードは有無だけを返す）"""
    data = ctx.config_service.current.to_dict()
    for section in HIDDEN_SECTIONS:
        data.pop(section, None)
    data["smtp"]["has_password"] = _has_smtp_password(ctx)
    data["protected"] = _readonly_fields()
    return data


def _has_smtp_password(ctx: ApiContext) -> bool:
    try:
        return bool(ctx.config_service.get_smtp_password())
    except Exception:  # noqa: BLE001 - keyring が使えない環境では「なし」として返す
        return False


def _checked_value(name: str, current, value):
    """型を確かめる（bool は bool だけ、int は bool 以外の int、float は int も可、str は str）"""
    if isinstance(current, bool):
        ok = isinstance(value, bool)
    elif isinstance(current, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(current, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value
    else:
        ok = isinstance(value, str)
    if not ok:
        raise ConfigValidationError(detail=name)
    return value


def _build_config(ctx: ApiContext, body: dict) -> ConfigModel:
    """入力から新しい設定を作る（現在の設定は変えない）。全部の項目がそろっていること"""
    for section in HIDDEN_SECTIONS:
        if section in body:
            raise ConfigValidationError("この項目は Web API では変更できません", detail=section)
    unknown = sorted(set(body) - set(_SECTIONS) - {"schema_version", "protected"})
    if unknown:
        raise ConfigValidationError(detail=", ".join(unknown))

    current = ctx.config_service.current
    new = copy.deepcopy(current)
    for section in _SECTIONS:
        if section in HIDDEN_SECTIONS:
            continue
        raw = body.get(section)
        if not isinstance(raw, dict):
            raise ConfigValidationError(detail=section)
        target = getattr(new, section)
        names = {f.name for f in fields(target)}
        extra = set(raw) - names - ({"has_password"} if section == "smtp" else set())
        missing = names - set(raw)
        if extra or missing:
            raise ConfigValidationError(detail=", ".join(sorted(f"{section}.{n}" for n in extra | missing)))
        for name in names:
            setattr(target, name, _checked_value(f"{section}.{name}", getattr(target, name), raw[name]))

    readonly = [name for name in _readonly_fields() if _get(new, name) != _get(current, name)]
    if readonly:
        raise ConfigValidationError("この項目は Web API では変更できません", detail=", ".join(readonly))

    out_of_range = [name for name, (low, high) in _RANGES.items() if not low <= _get(new, name) <= high]
    if out_of_range:
        raise ConfigValidationError(detail=", ".join(out_of_range))
    # 範囲外などの値は、読み込み時のように直さずに受け付けない（直された項目の名前だけを返す）
    fixed = copy.deepcopy(new)
    validate_and_fix(fixed, lambda _message: None)
    changed = [
        f"{section}.{f.name}"
        for section in _SECTIONS
        for f in fields(getattr(new, section))
        if getattr(getattr(new, section), f.name) != getattr(getattr(fixed, section), f.name)
    ]
    if changed:
        raise ConfigValidationError(detail=", ".join(changed))
    return new


def _get(model: ConfigModel, dotted: str):
    section, name = dotted.split(".")
    return getattr(getattr(model, section), name)


def save_config(ctx: ApiContext, body: dict) -> None:
    """PUT /config。現在の設定のオブジェクトをその場で書き換えてから保存する
    （各サービスは起動時に受け取った設定の部分（config.realtime など）を持ち続けているため。設定画面と同じ）"""
    new = _build_config(ctx, body)
    current = ctx.config_service.current
    credentials_changed = any(_get(new, name) != _get(current, name) for name in SMTP_CREDENTIAL_FIELDS)
    changed = [
        f"{section}.{f.name}"
        for section in _SECTIONS if section not in HIDDEN_SECTIONS
        for f in fields(getattr(current, section))
        if getattr(getattr(new, section), f.name) != getattr(getattr(current, section), f.name)
    ]
    for section in _SECTIONS:
        if section in HIDDEN_SECTIONS:
            continue
        target = getattr(current, section)
        for f in fields(target):
            setattr(target, f.name, getattr(getattr(new, section), f.name))
    ctx.config_service.save(current)
    # 値はログに残さない（送信先のアドレスなどを含むため）。変えた項目の名前だけ
    audit(CATEGORY_SYSTEM, f"設定を保存しました: {', '.join(changed) or '変更なし'}")
    if credentials_changed:
        ctx.config_service.delete_smtp_password()
        audit(CATEGORY_SYSTEM, "SMTP の送信先が変わったため、保存していた SMTP のパスワードを削除しました")
    if ctx.apply_config is not None:
        ctx.apply_config(current)
    _notify(ctx, CHANGED_CONFIG)


def set_smtp_password(ctx: ApiContext, password: str) -> None:
    if not password:
        raise ValueError("パスワードが空です")
    ctx.config_service.set_smtp_password(password)
    audit(CATEGORY_SYSTEM, "SMTP のパスワードを設定しました")
    _notify(ctx, CHANGED_CONFIG)


def delete_smtp_password(ctx: ApiContext) -> None:
    ctx.config_service.delete_smtp_password()
    audit(CATEGORY_SYSTEM, "SMTP のパスワードを削除しました")
    _notify(ctx, CHANGED_CONFIG)


# -- 6.9 ログ -------------------------------------------------------------------------
def clear_logs(ctx: ApiContext) -> int:
    count = ctx.log_reader_service.clear()
    audit(CATEGORY_SYSTEM, "ログを消去しました")  # 消去の後の最初の行として残す
    _notify(ctx, CHANGED_LOGS)
    return count


# -- 6.2 スキャン・6.7 定義の更新（ジョブ。段階 12-2b） -----------------------------------
SCAN_LABELS = {"quick": "クイックスキャン", "full": "フルスキャン", "custom": "手動スキャン"}


def _scan_targets(scan_type: str, targets: list[str] | None) -> tuple[list[str], tuple[str, ...]]:
    if scan_type == "full":
        return scan_targets.full_scan_targets(), scan_targets.full_scan_skip_dirs()
    if scan_type == "quick" and not targets:
        # 段階 12-2 まではアプリが利用者の権限で動くため、画面と同じ場所（ダウンロード等）を使う。
        # サービス化（12-3）の後は、利用者が設定で指定した場所（scan.quick_targets）だけにする
        return scan_targets.quick_scan_targets(), ()
    if not targets:
        raise ValueError("スキャンの対象を指定してください")
    missing = [t for t in targets if not os.path.isabs(t) or not os.path.exists(t)]
    if missing:
        raise ValueError("スキャンの対象は存在するファイル・フォルダを絶対パスで指定してください")
    return list(targets), ()


def start_scan(ctx: ApiContext, scan_type: str, targets: list[str] | None, include_subfolders: bool,
               apply_exclude: bool) -> dict:
    if scan_type not in SCAN_LABELS:
        raise ValueError("スキャンの種類が正しくありません")
    if ctx.scan_dispatcher.is_running:
        raise ApiConflictError(detail="スキャンを実行中です")
    paths, skip_dirs = _scan_targets(scan_type, targets)
    if not paths:
        raise ValueError("スキャンの対象がありません")
    label = SCAN_LABELS[scan_type]
    audit(CATEGORY_SCAN, f"{label}を開始しました: {', '.join(paths)}")
    ctx.jobs.expect_source("scan", "api")
    ctx.scan_dispatcher.start(paths, include_subfolders=include_subfolders, apply_exclude=apply_exclude,
                              label=label, skip_dirs=skip_dirs, source="api", scan_type=scan_type)
    _notify(ctx, CHANGED_SCAN_STARTED)
    return ctx.jobs.current("scan")


def cancel_scan(ctx: ApiContext) -> None:
    if not ctx.scan_dispatcher.is_running:
        raise ApiConflictError("実行中のスキャンはありません")
    audit(CATEGORY_SCAN, "スキャンの中止を要求しました")
    ctx.scan_dispatcher.stop()


def start_update(ctx: ApiContext) -> dict:
    if ctx.update_dispatcher.is_running:
        raise ApiConflictError(detail="定義の更新を実行中です")
    audit(CATEGORY_UPDATE, "定義の更新を開始しました")
    ctx.jobs.expect_source("update", "api")
    ctx.update_dispatcher.start()
    _notify(ctx, CHANGED_UPDATE_STARTED)
    return ctx.jobs.current("update")
